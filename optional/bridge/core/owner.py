"""Private owner controls for exact connections; never exposed as MCP tools.

The trusted local chooser mints decisions on its button callbacks. Ordinary
model-call arguments have no owner port. This is the user's selected bridge-only
boundary; it does not claim to defeat full-access code manipulating native UI or
calling Python internals outside that API.
"""
from dataclasses import dataclass
import secrets
import threading
import time

from kitlib.ident import digest
from .pairing import ChatSelection,PairingError,PairingStore


class OwnerDecisions:
    def __init__(self, *, clock=time.monotonic):
        self._clock,self._pending,self._lock=clock,{},threading.RLock()

    def mint(self,selection):
        if type(selection) is not ChatSelection: raise PairingError('invalid-owner-selection')
        with self._lock:
            now=self._clock()
            self._pending={key:value for key,value in self._pending.items() if value[1]>now}
            if len(self._pending)>=32: raise PairingError('owner-decision-capacity')
            proof=secrets.token_bytes(32)
            self._pending[proof]=(selection,now+10)
            return proof

    def authorize(self,selection,proof):
        if type(proof) is not bytes: return False
        with self._lock:
            expected=self._pending.pop(proof,None)
            return expected is not None and expected[0]==selection and expected[1]>self._clock()


def display(value,fallback):
    if type(value) is not str or not value: return fallback
    return ''.join(character for character in value[:256] if ord(character)>=32 and ord(character)!=127)


@dataclass(frozen=True)
class ChatChoice:
    reference:str
    title:str
    runtime:str
    workspace:str


class OwnerControls:
    def __init__(self,pairings,decisions, *, describe=None):
        if type(pairings) is not PairingStore or type(decisions) is not OwnerDecisions:
            raise PairingError('invalid-owner-components')
        self.pairings,self.decisions,self.describe=pairings,decisions,describe

    def chats(self):
        result=[]
        for binding in self.pairings.registered():
            try: detail=self.describe(binding) if self.describe is not None else {}
            except Exception: detail={}
            if type(detail) is not dict: detail={}
            result.append(ChatChoice(binding.reference,display(detail.get('title'),binding.identity.session_id),
                display(detail.get('runtime'),binding.identity.adapter_id),
                display(detail.get('workspace'),binding.identity.workspace.workspace_id)))
        return tuple(result)

    def connections(self):
        observed={peer.reference for peer in self.pairings.registered()}
        return tuple(route for route in self.pairings.owner_routes() if all(ref in observed for ref in route))

    def change(self,sender_ref,recipient_ref, *, enabled,bidirectional=True):
        for reference in (sender_ref,recipient_ref): digest(reference)
        by_ref={binding.reference:binding for binding in self.pairings.registered()}
        sender,recipient=by_ref.get(sender_ref),by_ref.get(recipient_ref)
        if sender is None or recipient is None: raise PairingError('visible-chat-changed')
        selection=self.pairings.propose(sender.identity.session_id,recipient.identity.session_id,
            enabled=enabled,bidirectional=bidirectional)
        if (selection.sender_ref,selection.recipient_ref)!=(sender_ref,recipient_ref):
            raise PairingError('visible-chat-changed')
        return self.pairings.owner_select(selection,self.decisions.mint(selection))
