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
from .pairing import ChatBinding,ChatSelection,PairingError,PairingStore

LEASE_SECONDS=3600
MAX_LEASES=32


@dataclass(frozen=True)
class WriterGrant:
    """The owner's exact choice: writer may carry delivery to recipient for another host."""
    writer_ref:str
    recipient_ref:str
    enabled:bool


class OwnerDecisions:
    def __init__(self, *, clock=time.monotonic):
        self._clock,self._pending,self._lock=clock,{},threading.RLock()

    def mint(self,selection):
        if type(selection) not in (ChatSelection,WriterGrant): raise PairingError('invalid-owner-selection')
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
            return (expected is not None and type(expected[0]) is type(selection)
                and expected[0]==selection and expected[1]>self._clock())


class WriterLeases:
    """Owner-granted writer leases, held in memory only and never persisted.

    A lease names one writer chat that may carry a delivery to one recipient chat
    for a sender of another host. It grants no route: the sender's own selected
    route and every native check still apply at each write, and the host decides
    which chats can be a writer. One writer per recipient; a new grant replaces
    the old one. A lease lapses after LEASE_SECONDS, on revocation, when either
    chat's exact binding changes, or when the service stops.
    """
    def __init__(self, *, owner_check, clock=time.monotonic):
        if not callable(owner_check): raise PairingError('invalid-owner-components')
        self._check,self._clock,self._lock,self._leases=owner_check,clock,threading.RLock(),{}

    def _prune(self):
        now=self._clock()
        self._leases={key:value for key,value in self._leases.items() if value[2]>now}

    def grant(self,writer,recipient,grant,proof):
        """Owner-control port only; must not be exported as an ordinary agent tool."""
        if (type(writer) is not ChatBinding or type(recipient) is not ChatBinding
                or type(grant) is not WriterGrant or type(grant.enabled) is not bool
                or writer.identity.session_id==recipient.identity.session_id
                or (grant.writer_ref,grant.recipient_ref)!=(writer.reference,recipient.reference)
                or type(proof) is not bytes):
            raise PairingError('invalid-owner-selection')
        try: approved=self._check(grant,proof) is True
        except Exception: approved=False
        if not approved: raise PairingError('owner-control-unverified')
        with self._lock:
            self._prune()
            if not grant.enabled:
                current=self._leases.get(recipient.reference)
                if current is not None and current[0]==writer: del self._leases[recipient.reference]
                return
            if recipient.reference not in self._leases and len(self._leases)>=MAX_LEASES:
                raise PairingError('writer-lease-capacity')
            self._leases[recipient.reference]=(writer,recipient,self._clock()+LEASE_SECONDS)

    def writer(self,recipient):
        """The writer currently leased for this exact recipient binding, or None."""
        if type(recipient) is not ChatBinding: return None
        with self._lock:
            self._prune()
            current=self._leases.get(recipient.reference)
            return current[0] if current is not None and current[1]==recipient else None

    def holds(self,writer,recipient):
        return type(writer) is ChatBinding and self.writer(recipient)==writer

    def current(self):
        with self._lock:
            self._prune()
            return tuple(sorted((writer.reference,recipient.reference)
                for writer,recipient,_expires in self._leases.values()))

    def clear(self):
        with self._lock: self._leases.clear()


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
    def __init__(self,pairings,decisions, *, describe=None,leases=None):
        if (type(pairings) is not PairingStore or type(decisions) is not OwnerDecisions
                or leases is not None and type(leases) is not WriterLeases):
            raise PairingError('invalid-owner-components')
        self.pairings,self.decisions,self.describe,self.leases=pairings,decisions,describe,leases

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

    def writers(self):
        """Current writer leases between visible chats, as (writer, recipient) references."""
        if self.leases is None: return ()
        observed={peer.reference for peer in self.pairings.registered()}
        return tuple(lease for lease in self.leases.current() if all(ref in observed for ref in lease))

    def lease(self,writer_ref,recipient_ref, *, enabled):
        """Owner button: let the writer chat carry delivery to the recipient chat, or stop it."""
        if self.leases is None: raise PairingError('writer-lease-unavailable')
        for reference in (writer_ref,recipient_ref): digest(reference)
        by_ref={binding.reference:binding for binding in self.pairings.registered()}
        writer,recipient=by_ref.get(writer_ref),by_ref.get(recipient_ref)
        if writer is None or recipient is None: raise PairingError('visible-chat-changed')
        if enabled is True:
            # Fresh observed-and-live check of both exact chats; it grants no route.
            # Stopping a lease needs no live chat.
            checked=self.pairings.propose(writer.identity.session_id,recipient.identity.session_id)
            if (checked.sender_ref,checked.recipient_ref)!=(writer_ref,recipient_ref):
                raise PairingError('visible-chat-changed')
        grant=WriterGrant(writer_ref,recipient_ref,enabled)
        self.leases.grant(writer,recipient,grant,self.decisions.mint(grant))
