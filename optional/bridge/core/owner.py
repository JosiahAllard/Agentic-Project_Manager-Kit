"""Private owner controls for exact connections; never exposed as MCP tools.

The trusted local chooser mints decisions on its button callbacks. Ordinary
model-call arguments have no owner port. This is the user's selected bridge-only
boundary; it does not claim to defeat full-access code manipulating native UI or
calling Python internals outside that API.
"""
from dataclasses import dataclass
import json
import os
import secrets
import stat
import threading
import time

from kitlib.ident import digest
from .pairing import ChatBinding,ChatSelection,PairingError,PairingStore

MAX_LEASES=32
LEASE_LOG='writer-leases.log'  # beside the pairings store, in the bridge state folder


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
    the old one. A lease has no expiry (owner ruling 2026-10-26): it lasts until
    it is revoked, either chat's exact binding leaves (drop_chat), or the service
    stops. Each entry keeps its grant time (clock, wall seconds) for display only.
    """
    def __init__(self, *, owner_check, clock=time.time):
        if not callable(owner_check): raise PairingError('invalid-owner-components')
        self._check,self.clock,self._lock,self._leases=owner_check,clock,threading.RLock(),{}

    def grant(self,writer,recipient,grant,proof):
        """Owner-control port only; must not be exported as an ordinary agent tool.

        Returns the grant time, or for a stop whether a lease was removed.
        """
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
            if not grant.enabled:
                current=self._leases.get(recipient.reference)
                if current is None or current[0]!=writer: return False
                del self._leases[recipient.reference]
                return True
            if recipient.reference not in self._leases and len(self._leases)>=MAX_LEASES:
                raise PairingError('writer-lease-capacity')
            granted=self.clock()
            self._leases[recipient.reference]=(writer,recipient,granted)
            return granted

    def lease_for(self,writer_ref,recipient_ref):
        """The exact (writer, recipient) bindings of a current lease, or None."""
        with self._lock:
            current=self._leases.get(recipient_ref)
            if current is None or current[0].reference!=writer_ref: return None
            return current[0],current[1]

    def withdraw(self,writer,recipient):
        """Remove exactly this lease with no owner proof; removing never grants anything."""
        with self._lock:
            current=self._leases.get(recipient.reference)
            if current is not None and current[:2]==(writer,recipient): del self._leases[recipient.reference]

    def drop_chat(self,binding):
        """A chat left: drop every lease naming its exact binding. Returns the dropped pairs."""
        with self._lock:
            gone=[key for key,(writer,recipient,_granted) in self._leases.items() if binding in (writer,recipient)]
            dropped=[]
            for key in gone:
                writer,recipient,_granted=self._leases.pop(key)
                dropped.append((writer.reference,recipient.reference))
            return tuple(dropped)

    def writer(self,recipient):
        """The writer currently leased for this exact recipient binding, or None."""
        if type(recipient) is not ChatBinding: return None
        with self._lock:
            current=self._leases.get(recipient.reference)
            return current[0] if current is not None and current[1]==recipient else None

    def holds(self,writer,recipient):
        return type(writer) is ChatBinding and self.writer(recipient)==writer

    def current(self):
        return tuple((writer,recipient) for writer,recipient,_granted in self.granted())

    def granted(self):
        """Current leases as (writer, recipient, grant time) references."""
        with self._lock:
            return tuple(sorted((writer.reference,recipient.reference,granted)
                for writer,recipient,granted in self._leases.values()))

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
        self._log_lock=threading.Lock()
        if leases is not None: pairings.on_leave(self._chat_left)

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
        return tuple(lease[:2] for lease in self.writer_grants())

    def writer_grants(self):
        """Current writer leases between visible chats, as (writer, recipient, grant time)."""
        if self.leases is None: return ()
        observed={peer.reference for peer in self.pairings.registered()}
        return tuple(lease for lease in self.leases.granted() if all(ref in observed for ref in lease[:2]))

    def lease(self,writer_ref,recipient_ref, *, enabled):
        """Owner button: let the writer chat carry delivery to the recipient chat, or stop it.

        A grant returns its grant time and lasts until revoked or either chat leaves.
        """
        if self.leases is None: raise PairingError('writer-lease-unavailable')
        for reference in (writer_ref,recipient_ref): digest(reference)
        if enabled is False: return self.revoke(writer_ref,recipient_ref,by='owner')  # needs no live chat
        if enabled is not True: raise PairingError('invalid-owner-selection')
        by_ref={binding.reference:binding for binding in self.pairings.registered()}
        writer,recipient=by_ref.get(writer_ref),by_ref.get(recipient_ref)
        if writer is None or recipient is None: raise PairingError('visible-chat-changed')
        # Fresh observed-and-live check of both exact chats; it grants no route.
        checked=self.pairings.propose(writer.identity.session_id,recipient.identity.session_id)
        if (checked.sender_ref,checked.recipient_ref)!=(writer_ref,recipient_ref):
            raise PairingError('visible-chat-changed')
        grant=WriterGrant(writer_ref,recipient_ref,True)
        granted=self.leases.grant(writer,recipient,grant,self.decisions.mint(grant))
        try:
            # A chat that left between the check and the grant drops nothing (drop_chat
            # ran first), so refuse it here; one that leaves later is dropped by drop_chat.
            observed=self.pairings.registered()
            if writer not in observed or recipient not in observed: raise PairingError('visible-chat-changed')
            self._log('grant','owner',writer_ref,recipient_ref,granted)
        except Exception as error:
            self.leases.withdraw(writer,recipient)
            if type(error) is PairingError: raise
            raise PairingError('writer-lease-log-failed') from None
        return granted

    def revoke(self,writer_ref,recipient_ref, *, by):
        """Revoke a writer lease; by is 'owner' or 'pm'. Private port, never an MCP tool.

        Works whether or not either chat is still visible. Returns whether a lease
        was removed. Removing grants nothing, so a failed log write never blocks it.
        """
        if self.leases is None: raise PairingError('writer-lease-unavailable')
        if by not in ('owner','pm'): raise PairingError('invalid-owner-selection')
        for reference in (writer_ref,recipient_ref): digest(reference)
        bound=self.leases.lease_for(writer_ref,recipient_ref)
        if bound is None: return False
        grant=WriterGrant(writer_ref,recipient_ref,False)
        removed=self.leases.grant(bound[0],bound[1],grant,self.decisions.mint(grant))
        if removed:
            try: self._log('revoke',by,writer_ref,recipient_ref,self.leases.clock())
            except Exception: pass
        return removed

    def _chat_left(self,binding):
        for writer_ref,recipient_ref in self.leases.drop_chat(binding):
            try: self._log('closed','bridge',writer_ref,recipient_ref,self.leases.clock())
            except Exception: pass

    def _log(self,event,by,writer_ref,recipient_ref,when):
        """Append one JSON line to the grant/revoke log beside the pairings store."""
        line=json.dumps({'at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(when)),'event':event,'by':by,
            'writer':writer_ref,'recipient':recipient_ref},sort_keys=True,separators=(',',':'))+'\n'
        path=self.pairings.path.parent/LEASE_LOG
        with self._log_lock:
            try:
                with path.open('xb'):
                    if self.pairings.prepare_new_file is not None: self.pairings.prepare_new_file(path)
            except FileExistsError: pass
            info=path.lstat()
            if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                    or getattr(info,'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise PairingError('nonregular-lease-log')
            with path.open('ab') as handle:
                handle.write(line.encode('ascii'))
                handle.flush()
                os.fsync(handle.fileno())
