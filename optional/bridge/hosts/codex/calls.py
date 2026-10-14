"""Codex host-side source context for the common bridge MCP API."""
from kitlib.ident import identifier
from optional.bridge.core.messages import DeliveryError, SenderAuthority
from .account import CodexAccount
from .context import CodexObserver


class CodexCalls:
    def __init__(self, observer, account, host_id):
        if type(observer) is not CodexObserver or type(account) is not CodexAccount:
            raise DeliveryError('invalid-codex-caller-observers')
        self.observer,self.account,self.host_id=observer,account,identifier(host_id)

    def observe(self, host_metadata):
        # This port receives host-supplied metadata over an already verified
        # frontend channel. It must never be exposed as ordinary tool arguments.
        before=self.account.observe()
        facts=self.observer.observe(host_metadata)
        if facts.account_ref!=before or self.account.observe()!=before:
            raise DeliveryError('native-caller-account-changed')
        # Use the native chat as a conservative task root. New operation IDs or
        # native turns cannot reset a recorded refusal. Native incoming-operation
        # ancestry remains a separate controller integration requirement.
        return SenderAuthority(facts.binding(self.host_id),facts.thread_id)
