"""Existing desktop-chat dispatch through its own app-owned native tool pipe.

Only a host frontend may install the lease/receipt ports. They are not MCP
arguments. A lease must retain the selected route and original writer, attest
current account/permission/workspace facts, and authorize its selected native
executor. Constructing DesktopLease is not authentication. The adapter does not
resume threads, create a relay chat, choose models or answer native approvals.

The tools/call wire shape is measured private protocol, not a public stable
Codex contract. Correlated success confirms scheduling, never running or reply.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json

from optional.bridge.core.contracts import AdapterDescription, Capability
from optional.bridge.core.messages import BeforeWriteError, DeliveryError, NativeResult, OutboundMessage
from optional.bridge.core.pairing import ChatBinding
from optional.bridge.core.prompt import delivery_prompt
from .context import CodexContext, UUID
from .delivery_transport import MAX_FRAME, NativeEndpoint, NativeToolsPipe, strict_json


@dataclass(frozen=True)
class DesktopLease:
    recipient: ChatBinding
    executor: CodexContext
    endpoint: NativeEndpoint = field(repr=False)
    recheck: object = field(repr=False)


def native_id(message):
    """Recoverable exact call correlation; an ID supplies no authority."""
    if type(message) is not OutboundMessage:
        raise BeforeWriteError('invalid-codex-message')
    message.__post_init__()
    fields = [message.operation_id, message.task_root, message.sender.reference,
        message.recipient.reference, message.intent.body_sha256, message.expires_at]
    encoded = json.dumps(fields, separators=(',', ':'), allow_nan=False).encode('ascii')
    return 'codex-' + hashlib.sha256(encoded).hexdigest()


class CodexDesktopAdapter:
    def __init__(self, *, lease=None, receipts=None, timeout=5):
        if any(port is not None and not callable(port) for port in (lease, receipts)):
            raise BeforeWriteError('invalid-codex-host-ports')
        self._leases, self._receipts = lease, receipts
        self._pipe = NativeToolsPipe(timeout=timeout)

    def describe(self):
        capabilities = () if self._leases is None else (Capability.SEND,)
        if capabilities and self._receipts is not None:
            capabilities += (Capability.READ_STATUS,)
        return AdapterDescription('codex-desktop', capabilities,
            'SEND is restricted to a distinct selected Codex source with a frontend-issued writer lease; '
            'private tools/call protocol; acceptance means scheduling only; '
            'exact native receipt observation is a separate host port.')

    @contextmanager
    def _lease(self, message):
        if (type(message) is not OutboundMessage or self._leases is None
                or message.recipient.identity.adapter_id != 'codex-desktop'):
            raise BeforeWriteError('codex-desktop-lease-unavailable')
        message.__post_init__()
        try:
            scope = self._leases(message)
        except Exception:
            raise BeforeWriteError('codex-desktop-lease-unverified') from None
        # Host scope retains route/writer ownership throughout connect/write/return.
        with scope as lease:
            if (type(lease) is not DesktopLease or lease.recipient != message.recipient
                    or type(lease.executor) is not CodexContext or lease.executor.kind != 'desktop'
                    or type(lease.endpoint) is not NativeEndpoint or not callable(lease.recheck)
                    or any(type(value) is not str or UUID.fullmatch(value) is None
                        for value in (lease.executor.thread_id, lease.executor.turn_id))):
                raise BeforeWriteError('codex-desktop-lease-unverified')
            executor = lease.executor.binding(message.recipient.identity.workspace.host_id)
            # Native self-target dispatch is unsafe in the observed companion.
            # An unrelated relay thread must never substitute for this source.
            if executor != message.sender or executor == message.recipient:
                raise BeforeWriteError('codex-desktop-executor-not-selected')
            try:
                checked = lease.recheck() is True
            except Exception:
                checked = False
            if not checked:
                raise BeforeWriteError('codex-desktop-context-changed')
            yield lease

    def send(self, message):
        may_have_written = False
        try:
            with self._lease(message) as lease:
                correlation = native_id(message)
                request = {'jsonrpc': '2.0', 'id': correlation, 'method': 'tools/call', 'params': {
                    'arguments': {'threadId': lease.recipient.identity.native_session_id, 'prompt': delivery_prompt(message)},
                    'callerSource': 'codex', 'callId': correlation, 'namespace': 'codex_app',
                    'threadId': lease.executor.thread_id, 'turnId': lease.executor.turn_id,
                    'tool': 'send_message_to_thread'}}
                try:
                    result = self._pipe.call(lease.endpoint, request, before_write=lease.recheck)
                except BeforeWriteError:
                    raise
                except Exception:
                    may_have_written = True
                    raise
                may_have_written = True
                if lease.recheck() is not True:
                    raise ValueError('context changed')
                if type(result) is not dict or result.get('success') is not True or result.get('isError', False) is not False:
                    raise ValueError('no native success')
                contents = [result]
                if result.get('structuredContent') is not None:
                    contents.append(result['structuredContent'])
                # Native tools may include structured data plus either or both
                # text representations. Validate all of them: field precedence
                # must not hide a refusal or a contradictory target/correlation.
                for field_name in ('contentItems', 'content'):
                    if field_name not in result:
                        continue
                    items = result[field_name]
                    if type(items) is not list or len(items) > 128 or any(type(item) is not dict for item in items):
                        raise ValueError('invalid tool content')
                    texts = []
                    for item in items:
                        if item.get('type') not in ('inputText', 'text') or type(item.get('text')) is not str:
                            raise ValueError('unsupported tool content')
                        texts.append(item['text'])
                    if texts:
                        data = '\n'.join(texts).encode('utf-8')
                        if len(data) > MAX_FRAME:
                            raise ValueError('tool content limit')
                        contents.append(strict_json(data))
                for content in contents:
                    if (type(content) is not dict or content.get('success', True) is not True
                            or content.get('isError', False) is not False
                            or content.get('error') is not None):
                        raise ValueError('invalid structured success')
                    for key, expected in (('threadId', lease.recipient.identity.native_session_id),
                            ('callId', correlation), ('hostId', 'local')):
                        if key in content and content[key] != expected:
                            raise ValueError('contradictory native receipt')
                return NativeResult(message.operation_id, message.recipient.reference,
                    'recipient-accepted', correlation)
        except Exception:
            # Includes host scope-exit failures: after I/O they cannot become a
            # certified before-write result merely by raising BeforeWriteError.
            if may_have_written:
                raise DeliveryError('codex-native-result-unconfirmed') from None
            raise BeforeWriteError('codex-native-context-unavailable-before-write') from None

    def reconcile(self, message, observed_id):
        expected = native_id(message)
        if observed_id is not None and observed_id != expected:
            raise DeliveryError('codex-native-receipt-id-mismatch')
        if self._receipts is None:
            return None
        with self._lease(message):
            try:
                result = self._receipts(message, expected)
            except Exception:
                raise DeliveryError('codex-native-receipt-unavailable') from None
            if result is None:
                return None
            if (type(result) is not NativeResult or result.operation_id != message.operation_id
                    or result.recipient_ref != message.recipient.reference or result.native_id != expected):
                raise DeliveryError('codex-native-receipt-mismatch')
            result.__post_init__()
            return result


class CodexTerminalAdapter:
    """Explicitly unavailable until the original terminal offers safe control.

    Installed queue accepts the body in argv. Installed experimental schemas
    also expose thread/queue/add and thread/queue/start, with text in RPC input.
    The existing-daemon proxy path and preservation of the original terminal's
    writer and approval routing still require an authenticated host lease.
    A second app-server resume is never a substitute for that native facility.
    """
    def describe(self):
        return AdapterDescription('codex-terminal', (),
            'Missing verified running-daemon control connection and original-terminal writer/approval-routing lease; '
            'no second app-server writer or replacement chat fallback.')

    def send(self, message):
        raise BeforeWriteError('codex-terminal-native-control-unavailable')

    def reconcile(self, message, native_id):
        return None
