"""Kernel-owned broker assembly and private native/owner/frontend ports.

The installed bootstrap supplies the protected state directory and verified
native frontend/channel observers. This class owns durable policy/outbox state;
it does not export enrollment, adapter installation or owner writes to agents.
Private pipe serving and stdio forwarding live in bridgewin.service/frontend.
Deployment lifecycle and native dispatch providers are assembled separately.
"""
from pathlib import Path
import threading

from kitlib.proc.pipe import protect_file
from optional.bridge.win.evidence import regular
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.core.callers import CallProofs
from optional.bridge.core.dispatch import CoreBroker
from optional.bridge.core.messages import DeliveryError
from optional.bridge.core.mcp import Server
from optional.bridge.core.outbox import Outbox,MAX_MESSAGES,MAX_PENDING
from optional.bridge.core.owner import OwnerControls,OwnerDecisions,WriterLeases
from optional.bridge.core.pairing import ChatBinding,PairingStore


class Controller:
    def __init__(self,state_root,leader):
        if type(leader) is not ServiceLeader or not leader.owns():
            raise DeliveryError('service-ownership-unverified')
        root=Path(state_root)
        if not root.is_absolute(): raise DeliveryError('invalid-service-state-root')
        regular(root,directory=True)
        self.root,self.leader=root.resolve(strict=True),leader
        self._peers,self._sources,self._lock={},[],threading.RLock()
        self._closed=False
        self._pending_cursor=None
        self.pairs=self.outbox=self.leases=None
        try:
            for name in ('pairings.sqlite','outbox.sqlite'):
                path=self.root/name
                if path.exists():
                    regular(path)
                    protect_file(path)  # Controller-owned state only; no native files.
            self.decisions=OwnerDecisions()
            self.pairs=PairingStore(self.root/'pairings.sqlite',live_check=self._live,
                owner_check=self.decisions.authorize,prepare_new_file=protect_file)
            self.outbox=Outbox(self.root/'outbox.sqlite',leadership_check=self.leader.owns,
                prepare_new_file=protect_file)
            self.outbox.claim()
            self.core=CoreBroker(self.pairs,self.outbox,{},authorize=self._authorize,
                dispatch_check=lambda message:self._live(message.sender) and self._live(message.recipient))
            # Writer leases live only in this process: a restart drops every one.
            self.leases=WriterLeases(owner_check=self.decisions.authorize)
            self.owner=OwnerControls(self.pairs,self.decisions,describe=self._describe,leases=self.leases)
        except Exception:
            self.close()
            raise

    def _require(self):
        if self._closed or not self.leader.owns(): raise DeliveryError('service-ownership-unverified')

    def _live(self,binding):
        if self._closed or not self.leader.owns(): return False
        with self._lock: current=self._peers.get(binding.identity.session_id)
        if current is None or current[0]!=binding: return False
        try: return current[1](binding) is True
        except Exception: return False

    def _describe(self,binding):
        with self._lock: current=self._peers.get(binding.identity.session_id)
        return current[2] if current is not None and current[0]==binding else {}

    def enroll(self,binding,verify,description=None):
        """Verified native adapter/controller port, not a client-supplied identity API."""
        self._require()
        if type(binding) is not ChatBinding or not callable(verify):
            raise DeliveryError('invalid-native-enrollment')
        if verify(binding) is not True: raise DeliveryError('native-enrollment-unverified')
        with self._lock:
            if len(self._peers)>=128 and binding.identity.session_id not in self._peers:
                raise DeliveryError('native-enrollment-capacity')
            self._peers[binding.identity.session_id]=(binding,verify,
                dict(description) if type(description) is dict else {})
        return self.pairs.observe(binding)

    def adapter(self,adapter):
        """Install controller-owned adapter code; ordinary MCP tools cannot call this."""
        self._require()
        description=adapter.describe()
        self.core.adapters[description.adapter_id]=adapter

    def frontend(self,observe):
        """Bind a source observer only after the private native channel is verified."""
        self._require()
        with self._lock:
            if len(self._sources)>=128: raise DeliveryError('frontend-capacity')
            calls=CallProofs(observe=observe)
            self._sources.append(calls)
        return Server(self.core,calls)

    def _authorize(self,intent,proof):
        self._require()
        with self._lock: sources=tuple(self._sources)
        for calls in sources:
            authority=calls.authorize(intent,proof)
            if authority is not None: return authority
        return None

    def detach_frontend(self,server):
        """Private lifecycle cleanup; an exited frontend leaves no reusable authority."""
        with self._lock:
            if server.calls in self._sources: self._sources.remove(server.calls)

    def dispatch_pending(self,limit=32, *, continue_check=None):
        self._require()
        if (type(limit) is not int or not 1<=limit<=MAX_MESSAGES
                or continue_check is not None and not callable(continue_check)):
            raise DeliveryError('invalid-pending-limit')
        self.core.promote_replies(limit)
        pending=self.outbox.pending(MAX_PENDING)  # Bounded IDs only, no bodies.
        with self._lock: cursor=self._pending_cursor
        start=pending.index(cursor)+1 if cursor in pending else 0
        batch=(pending[start:]+pending[:start])[:limit]
        receipts=[]
        for operation in batch:
            self._require()
            if continue_check is not None and continue_check() is not True: break
            try: receipt=self.core.dispatch_once(operation)
            except DeliveryError:
                self._require()  # Lost leadership still stops the supervisor.
                receipt=self.outbox.receipt(operation)
            receipts.append(receipt)
            with self._lock: self._pending_cursor=operation
        return tuple(receipts)

    def close(self):
        if self._closed: return
        self._closed=True
        if self.leases is not None: self.leases.clear()
        for storage in (self.outbox,self.pairs):
            if storage is not None: storage.close()
        with self._lock:
            self._sources.clear()
            self._peers.clear()

    def __enter__(self): return self

    def __exit__(self,*error): self.close()
