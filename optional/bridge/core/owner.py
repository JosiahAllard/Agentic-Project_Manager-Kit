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
from .pmroutes import PmRoutes,append_log,stamp

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
    it is revoked, a different binding is observed for either chat's session
    (drop_replaced), or the service stops. A failed live check or a withdraw is
    not a close and keeps the lease; the write-time checks still need the writer
    live and in an active turn. Each entry keeps its grant time (clock, wall
    seconds) for display only.
    """
    def __init__(self, *, owner_check, clock=time.time):
        if not callable(owner_check): raise PairingError('invalid-owner-components')
        self._check,self.clock,self._lock,self._leases=owner_check,clock,threading.RLock(),{}

    def grant(self,writer,recipient,grant,proof):
        """Owner-control port only; must not be exported as an ordinary agent tool.

        A grant returns (grant time, the entry it replaced or None), both read under
        the lease lock; a stop returns whether a lease was removed.
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
            granted,replaced=self.clock(),self._leases.get(recipient.reference)
            self._leases[recipient.reference]=(writer,recipient,granted)
            return granted,replaced

    def lease_for(self,writer_ref,recipient_ref):
        """The exact (writer, recipient) bindings of a current lease, or None."""
        with self._lock:
            current=self._leases.get(recipient_ref)
            if current is None or current[0].reference!=writer_ref: return None
            return current[0],current[1]

    def withdraw(self,writer,recipient, *, restore=None):
        """Undo exactly this lease with no owner proof, putting back the entry it replaced.

        restore must be the owner-granted entry this lease replaced, as grant() returned it.
        """
        with self._lock:
            current=self._leases.get(recipient.reference)
            if current is None or current[:2]!=(writer,recipient): return
            if restore is not None and restore[1]==recipient: self._leases[recipient.reference]=restore
            else: del self._leases[recipient.reference]

    def drop_replaced(self,binding):
        """binding is now current for its session: drop leases naming another binding of it.

        Returns the dropped (writer, recipient) reference pairs.
        """
        if type(binding) is not ChatBinding: return ()
        session=binding.identity.session_id
        with self._lock:
            gone=[key for key,(writer,recipient,_granted) in self._leases.items()
                if any(chat.identity.session_id==session and chat!=binding for chat in (writer,recipient))]
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
class PmStop:
    """OwnerControls.stop_pm's result: removed, whether a PM route was removed; unblocked, None
    when the PM cannot make the direction again, else why the owner's block was not written."""
    removed:bool
    unblocked:str|None


@dataclass(frozen=True)
class ChatChoice:
    reference:str
    title:str
    runtime:str
    workspace:str


class OwnerControls:
    def __init__(self,pairings,decisions, *, describe=None,leases=None,pm=None):
        if (type(pairings) is not PairingStore or type(decisions) is not OwnerDecisions
                or leases is not None and type(leases) is not WriterLeases
                or pm is not None and (type(pm) is not PmRoutes or pm.pairings is not pairings)):
            raise PairingError('invalid-owner-components')
        self.pairings,self.decisions,self.describe,self.leases,self.pm=pairings,decisions,describe,leases,pm
        if leases is not None: pairings.on_observe(self._chat_observed)

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
        revision=self.pairings.owner_select(selection,self.decisions.mint(selection))
        if enabled is False and self.pm is not None:
            # The owner's disconnect also ends any PM route on these directions; the disabled
            # rows just written keep the PM from making it again.
            for left,right in [(sender_ref,recipient_ref)]+([(recipient_ref,sender_ref)] if bidirectional else []):
                self.pm.stop(left,right,by='owner')
        return revision

    def pm_routes(self, *, check=True):
        """Every PM route the owner's own row does not decide, as (sender, recipient, time made,
        deliverable). Deliverable: both chats are listed and, with check, both are still
        kit-launched (live checks run). An undeliverable route is still returned so the owner can
        see and Stop it."""
        if self.pm is None: return ()
        observed=set(self.pairings.registered())
        result=[]
        for sender_ref,recipient_ref,made in self.pm.current():
            route=self.pm.route(sender_ref,recipient_ref)
            if route is None: continue
            try:
                if self.pairings.owner_choice(route.sender,route.recipient) is not None: continue
            except PairingError: continue
            deliverable=route.sender in observed and route.recipient in observed
            if deliverable and check: deliverable=self.pm.allows(route.sender,route.recipient)
            result.append((sender_ref,recipient_ref,made,deliverable))
        return tuple(result)

    def stop_pm(self,sender_ref,recipient_ref):
        """Owner button: stop the PM route in this one direction and keep the PM from making it
        again: a disabled owner row for it, which only the owner's Connect undoes. Works whether
        or not either chat is still listed.

        The block is written first. If it cannot be written the route is still removed, the stop's
        log line carries `unblocked` (the reason) and so does the result: stopped, but the PM may
        make it again. A direction with an owner row needs no block and gets none."""
        if self.pm is None: raise PairingError('pm-routes-unavailable')
        for reference in (sender_ref,recipient_ref): digest(reference)
        with self.pairings.held():  # no PM connect can land between the block and the stop
            route=self.pm.route(sender_ref,recipient_ref)
            if route is None: return PmStop(False,None)
            unblocked=None
            try:
                if self.pairings.owner_choice(route.sender,route.recipient) is None:
                    block=ChatSelection(route.sender.reference,route.recipient.reference,False,False)
                    self.pairings.owner_block(route.sender,route.recipient,self.decisions.mint(block))
            except Exception as error:
                unblocked=getattr(error,'code',None) or 'owner-block-failed'
            removed=self.pm.stop(sender_ref,recipient_ref,by='owner',unblocked=unblocked)
            return PmStop(removed,unblocked)

    def writers(self):
        """Current writer leases between visible chats, as (writer, recipient) references."""
        return tuple(lease[:2] for lease in self.writer_grants() if lease[3])

    def writer_grants(self):
        """Every current writer lease, as (writer, recipient, grant time, both chats listed).

        A lease whose chat is not listed (closed, between turns or missed by discovery)
        is still returned, so the owner can see and Stop it; it cannot deliver.
        """
        if self.leases is None: return ()
        observed={peer.reference for peer in self.pairings.registered()}
        return tuple((writer,recipient,granted,writer in observed and recipient in observed)
            for writer,recipient,granted in self.leases.granted())

    def lease(self,writer_ref,recipient_ref, *, enabled):
        """Owner button: let the writer chat carry delivery to the recipient chat, or stop it.

        A grant returns its grant time and lasts until revoked or either chat's binding is replaced.
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
        granted,replaced=self.leases.grant(writer,recipient,grant,self.decisions.mint(grant))
        try:
            # A binding replaced between the check and the grant was dropped before
            # this lease existed, so refuse it here.
            observed=self.pairings.registered()
            if writer not in observed or recipient not in observed: raise PairingError('visible-chat-changed')
            self._log('grant','owner',writer_ref,recipient_ref,granted)
        except Exception as error:
            # Undo this grant only; the writer it replaced keeps its lease.
            self.leases.withdraw(writer,recipient,restore=replaced)
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

    def _chat_observed(self,binding):
        for writer_ref,recipient_ref in self.leases.drop_replaced(binding):
            try: self._log('replaced','bridge',writer_ref,recipient_ref,self.leases.clock())
            except Exception: pass

    def _log(self,event,by,writer_ref,recipient_ref,when):
        """Append one JSON line to the grant/revoke log beside the pairings store."""
        append_log(self.pairings,LEASE_LOG,[{'at':stamp(when),'event':event,'by':by,
            'writer':writer_ref,'recipient':recipient_ref}])
