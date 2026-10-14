"""Agent-neutral caller-bound admission and dispatch over trusted adapter ports.

Default host ports deny. Runtime adapters are installed by the controller, not
registered by a model call. This implements core mechanics, not actual native
identity/owner verification, service installation or a live MCP endpoint.
"""
import hashlib

from .contracts import AdapterDescription, Capability, CONTRACT_VERSION
from .messages import DeliveryError, MessageIntent, OutboundMessage, SenderAuthority, text_bytes, REPLY_OPERATION_PREFIX
from .outbox import Outbox
from .pairing import PairingError, PairingStore


class CoreBroker:
    def __init__(self, pairings, outbox, adapters, *, authorize=None, dispatch_check=None):
        if type(pairings) is not PairingStore or type(outbox) is not Outbox or type(adapters) is not dict:
            raise DeliveryError('invalid-broker-components')
        if any(port is not None and not callable(port) for port in (authorize, dispatch_check)):
            raise DeliveryError('invalid-broker-ports')
        self.pairings, self.outbox, self.adapters = pairings, outbox, dict(adapters)
        self._authorize, self._dispatch_check = authorize, dispatch_check

    def _authority(self, intent, proof):
        if type(proof) is not bytes or not 0 < len(proof) <= 4096:
            raise DeliveryError('caller-context-unverified')
        try:
            authority = self._authorize(intent, proof) if self._authorize is not None else None
        except Exception:
            authority = None
        if type(authority) is not SenderAuthority:
            raise DeliveryError('caller-context-unverified')
        return authority

    def admit(self, operation, recipient, text, proof, *, expires_at=None):
        if type(operation) is str and operation.startswith(REPLY_OPERATION_PREFIX):
            raise DeliveryError('reserved-reply-operation')
        intent = MessageIntent(operation, recipient, hashlib.sha256(text_bytes(text)).hexdigest(), expires_at)
        authority = self._authority(intent, proof)
        message = OutboundMessage(operation, authority.task_root, authority.sender, recipient, text, expires_at)
        with self.pairings.selected_guard(message.sender, message.recipient):
            return self.outbox.admit(message)

    def _adapter(self, message):
        adapter = self.adapters.get(message.recipient.identity.adapter_id)
        if adapter is None:
            raise DeliveryError('runtime-adapter-unavailable')
        try:
            description = adapter.describe()
        except Exception:
            raise DeliveryError('runtime-adapter-unavailable') from None
        if (type(description) is not AdapterDescription
                or description.adapter_id != message.recipient.identity.adapter_id
                or type(description.contract_version) is not int
                or description.contract_version != CONTRACT_VERSION
                or type(description.capabilities) is not tuple
                or any(type(capability) is not Capability for capability in description.capabilities)
                or Capability.SEND not in description.capabilities
                or not callable(getattr(adapter, 'send', None))):
            raise DeliveryError('runtime-adapter-incompatible')
        return adapter

    def dispatch_once(self, operation):
        """Private supervisor operation. Queued contexts must be re-attested before native I/O."""
        message = self.outbox.message(operation)
        receipt = self.outbox.receipt(operation)
        if receipt.state != 'queued':
            return receipt  # Including unknown and held: no automatic retry.
        try:
            with self.pairings.selected_guard(message.sender, message.recipient):
                # Check the original full bindings first. A remembered logical
                # choice never moves queued work onto a reopened process.
                try:
                    verified = self._dispatch_check is not None and self._dispatch_check(message) is True
                except Exception:
                    verified = False
                if not verified:
                    raise DeliveryError('dispatch-context-unverified')
                adapter = self._adapter(message)
                reverse=self._reverse_selected(message)
                return self.outbox.run_dispatch(operation, adapter.send,reverse_reply=reverse)
        except PairingError:
            return self.outbox.reject_queued(operation, 'chat-route-revoked-or-changed')

    def _read(self, operation, proof):
        message = self.outbox.message(operation)
        authority = self._authority(message.intent, proof)
        if authority.sender != message.sender or authority.task_root != message.task_root:
            raise DeliveryError('operation-caller-mismatch')
        return message

    def receipt(self, operation, proof):
        self._read(operation, proof)
        return self.outbox.receipt(operation)

    def reply(self, operation, proof):
        message = self._read(operation, proof)
        try:
            self.pairings.require_selected(message.recipient, message.sender)
        except PairingError:
            raise DeliveryError('reply-route-not-selected') from None
        return self.outbox.reply(operation)

    def reply_to(self, operation, text, proof):
        """Explicit recipient reply; an operation ID alone never authorizes its author."""
        text_bytes(text)
        message = self.outbox.message(operation)
        if message.reply_of is not None:
            raise DeliveryError('reply-to-reply-delivery')
        authority = self._authority(message.intent, proof)
        if authority.sender != message.recipient:
            raise DeliveryError('operation-recipient-mismatch')
        try:
            with self.pairings.selected_guard(message.recipient, message.sender):
                return self.outbox.recipient_reply(operation, text,reverse_reply=True)
        except PairingError:
            raise DeliveryError('reply-route-not-selected') from None

    def events(self, operation, proof, *, after=0, limit=32):
        self._read(operation, proof)
        return self.outbox.events(operation, after, limit)

    def poll_native(self, operation, proof):
        """Query the original adapter's authenticated receipt; never resend the message."""
        message = self._read(operation, proof)
        receipt = self.outbox.receipt(operation)
        if receipt.state in ('queued', 'reply-received', 'refused', 'failed', 'expired'):
            return receipt
        adapter = self._adapter(message)
        if not callable(getattr(adapter, 'reconcile', None)):
            raise DeliveryError('native-reconciliation-unavailable')
        with self.pairings.selected_guard(message.sender, message.recipient):
            reverse=self._reverse_selected(message)
            with self.outbox.native_observation(operation):
                result = adapter.reconcile(message, receipt.native_id)
                if result is None:return self.outbox.receipt(operation)
                if self.outbox.receipt(operation).state in ('reply-received','refused','expired','failed'):
                    return self.outbox.receipt(operation)
                return self.outbox.append_native(operation, result,reverse_reply=reverse)

    def _reverse_selected(self, message):
        # Core already holds the forward guard; do not nest another transaction.
        if message.reply_of is not None:return False
        try:self.pairings.require_selected(message.recipient,message.sender)
        except PairingError:return False
        return True

    def promote_replies(self, limit=32):
        """Private worker recovery of already-authenticated pending reverse jobs."""
        for operation in self.outbox.pending_replies(limit):
            message=self.outbox.message(operation)
            try:
                with self.pairings.selected_guard(message.recipient,message.sender):
                    self.outbox.promote_reply(operation)
            except PairingError:
                self.outbox.suppress_reply(operation,'chat-route-revoked-or-changed')
