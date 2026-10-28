"""Routes the PM makes between kit-launched chats (README "PM routes").

The PM's own chat, attested with the PM launch mark, may connect and disconnect two chats
that each carry a launch mark now. Any other chat still needs the owner's click. A PM route
is not owner policy: it never writes the owner's route table and holds no owner decision.
It lives in this process only, keyed by the exact chat bindings, and opens a direction
only while that direction has no owner row and both chats are still kit-launched; that is
checked again at every use. The PM removes only routes it made. The owner sees every PM
route in the window and can stop it; every change and every refused attempt is logged.
The launch mark is only as strong as the same-user boundary README "Launch mark" states.
"""
from dataclasses import dataclass
import json
import os
import stat
import threading
import time

from kitlib.ident import digest
from .hostpairs import HostPairs, REFUSED as HOST_PAIR_REFUSED
from .messages import SenderAuthority
from .pairing import ChatBinding, PairingError, PairingStore

PM_MARK = 'pm'  # the PM session's launch mark (kitlib/launcher/window.py PM)
MAX_PM_ROUTES = 64
PM_LOG = 'pm-routes.log'  # beside the pairings store, in the bridge state folder
_LOG_LOCK = threading.Lock()


def append_log(pairings, name, records):
    """Append JSON lines, in one write, to the state file `name` beside the pairings store."""
    data = ''.join(json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n' for record in records)
    path = pairings.path.parent / name
    with _LOG_LOCK:
        try:
            with path.open('xb'):
                if pairings.prepare_new_file is not None: pairings.prepare_new_file(path)
        except FileExistsError: pass
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise PairingError('nonregular-state-log')
        with path.open('ab') as handle:
            handle.write(data.encode('ascii'))
            handle.flush()
            os.fsync(handle.fileno())


def stamp(when):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when))


@dataclass(frozen=True)
class PmRoute:
    sender: ChatBinding
    recipient: ChatBinding
    caller: str  # the PM chat's reference
    made: float


def _reference(value):
    try: return digest(value)
    except (ValueError, TypeError): raise PairingError('invalid-chat-reference') from None


class PmRoutes:
    def __init__(self, pairings, *, launched, host_pairs=None, clock=time.time):
        """`launched(binding)`: that live chat's launch mark or None (Controller.launched)."""
        if (type(pairings) is not PairingStore or not callable(launched)
                or host_pairs is not None and type(host_pairs) is not HostPairs):
            raise PairingError('invalid-pm-components')
        self.pairings, self._launched, self.host_pairs, self.clock = pairings, launched, host_pairs, clock
        # Lock order: the pairings store's lock, then this one; never the reverse.
        self._lock, self._routes = threading.RLock(), {}
        pairings.on_observe(self._observed)
        pairings.extend_routes(self.allows)

    def _mark(self, binding):
        try: mark = self._launched(binding)
        except Exception: return None
        return mark if type(mark) is str and mark else None

    def allows(self, sender, recipient):
        """The route port: this exact PM route exists and both chats are kit-launched now."""
        with self._lock: route = self._routes.get((sender.reference, recipient.reference))
        if route is None or (route.sender, route.recipient) != (sender, recipient): return False
        if self.host_pairs is not None and not self.host_pairs.allowed(sender, recipient): return False
        return self._mark(sender) is not None and self._mark(recipient) is not None

    def _caller(self, authority):
        if type(authority) is not SenderAuthority: raise PairingError('caller-context-unverified')
        if authority.launched != PM_MARK: raise PairingError('pm-launch-mark-required')
        return authority.sender

    def _refused(self, action, authority, refs, error):
        record = {'at': stamp(self.clock()), 'event': 'refused', 'by': 'pm', 'action': action,
            'code': getattr(error, 'code', 'pm-route-refused'),
            'caller': authority.sender.reference if type(authority) is SenderAuthority else None,
            'caller_mark': authority.launched if type(authority) is SenderAuthority else None}
        for key, value in zip(('sender', 'recipient'), refs):
            try: record[key] = digest(value)
            except (ValueError, TypeError): record[key] = None
        try: append_log(self.pairings, PM_LOG, [record])
        except Exception: pass

    def _record(self, event, by, route, when, **extra):
        return {'at': stamp(when), 'event': event, 'by': by, 'caller': route.caller,
            'sender': route.sender.reference, 'recipient': route.recipient.reference, **extra}

    def chats(self, authority):
        """The PM's view: live kit-launched chats and the routes between them."""
        self._caller(authority)
        chats, marks = [], {}
        for binding in self.pairings.registered():
            mark = self._mark(binding)
            if mark is None: continue
            marks[binding.reference] = mark
            chats.append({'reference': binding.reference, 'chat_id': binding.identity.session_id,
                'runtime': binding.identity.adapter_id, 'mark': mark})
        routes = [{'sender': left, 'recipient': right, 'by': 'owner'} for left, right in self.pairings.owner_routes()
            if left in marks and right in marks]
        routes += [{'sender': left, 'recipient': right, 'by': 'pm'} for left, right, _made in self.current()]
        return {'chats': chats, 'routes': routes}

    def connect(self, authority, sender_ref, recipient_ref, *, bidirectional=True):
        """PM port: connect two kit-launched chats. Returns each direction and who holds it."""
        try:
            caller = self._caller(authority)
            return self._connect(caller, _reference(sender_ref), _reference(recipient_ref), bidirectional)
        except PairingError as error:
            self._refused('connect', authority, (sender_ref, recipient_ref), error)
            raise

    def _connect(self, caller, sender_ref, recipient_ref, bidirectional):
        if type(bidirectional) is not bool: raise PairingError('invalid-selection-options')
        with self.pairings.held():
            by_ref = {binding.reference: binding for binding in self.pairings.registered()}
            sender, recipient = by_ref.get(sender_ref), by_ref.get(recipient_ref)
            if sender is None or recipient is None: raise PairingError('chat-not-observed')
            if sender.identity.session_id == recipient.identity.session_id:
                raise PairingError('same-chat-selection')
            checked = self.pairings.propose(sender.identity.session_id, recipient.identity.session_id)
            if (checked.sender_ref, checked.recipient_ref) != (sender_ref, recipient_ref):
                raise PairingError('visible-chat-changed')
            marks = {'sender_mark': self._mark(sender), 'recipient_mark': self._mark(recipient)}
            if marks['sender_mark'] is None: raise PairingError('sender-not-kit-launched')
            if marks['recipient_mark'] is None: raise PairingError('recipient-not-kit-launched')
            directions = [(sender, recipient)] + ([(recipient, sender)] if bidirectional else [])
            held = []
            for left, right in directions:
                if self.host_pairs is not None and not self.host_pairs.allowed(left, right):
                    raise PairingError(HOST_PAIR_REFUSED)
                choice = self.pairings.owner_choice(left, right)
                if choice is False: raise PairingError('owner-disconnected')
                held.append('owner' if choice else 'pm')
            now = self.clock()
            with self._lock:
                new = [PmRoute(left, right, caller.reference, now) for (left, right), by in zip(directions, held)
                    if by == 'pm' and (left.reference, right.reference) not in self._routes]
                if len(self._routes) + len(new) > MAX_PM_ROUTES: raise PairingError('pm-route-capacity')
                for route in new: self._routes[(route.sender.reference, route.recipient.reference)] = route
            try:
                if new:
                    swapped = {'sender_mark': marks['recipient_mark'], 'recipient_mark': marks['sender_mark']}
                    append_log(self.pairings, PM_LOG, [self._record('connect', 'pm', route, now,
                        **(marks if route.sender == sender else swapped)) for route in new])
            except Exception:
                with self._lock:
                    for route in new: self._routes.pop((route.sender.reference, route.recipient.reference), None)
                raise PairingError('pm-route-log-failed') from None
            return {'routes': [{'sender': left.reference, 'recipient': right.reference, 'by': by}
                for (left, right), by in zip(directions, held)]}

    def disconnect(self, authority, sender_ref, recipient_ref, *, bidirectional=True):
        """PM port: remove the PM's own routes between two chats; an owner route is never touched."""
        try:
            self._caller(authority)
            if type(bidirectional) is not bool: raise PairingError('invalid-selection-options')
            sender_ref, recipient_ref = _reference(sender_ref), _reference(recipient_ref)
            keys = [(sender_ref, recipient_ref)] + ([(recipient_ref, sender_ref)] if bidirectional else [])
            with self.pairings.held():
                with self._lock:
                    if not any(key in self._routes for key in keys): raise PairingError('route-not-made-by-pm')
                removed = self._remove(keys, 'disconnect', 'pm')
            return {'removed': [{'sender': left, 'recipient': right} for left, right in removed],
                'kept': [{'sender': left, 'recipient': right} for left, right in keys if (left, right) not in removed]}
        except PairingError as error:
            self._refused('disconnect', authority, (sender_ref, recipient_ref), error)
            raise

    def stop(self, sender_ref, recipient_ref, *, by, unblocked=None):
        """Owner port: stop the PM route in this one direction. Returns whether one was removed.
        unblocked: why the owner's block for it could not be written; logged with the stop."""
        if by not in ('owner', 'bridge'): raise PairingError('invalid-owner-selection')
        if unblocked is not None and type(unblocked) is not str: raise PairingError('invalid-owner-selection')
        key = (_reference(sender_ref), _reference(recipient_ref))
        extra = {} if unblocked is None else {'unblocked': unblocked}
        with self.pairings.held(): return bool(self._remove([key], 'stop', by, **extra))

    def route(self, sender_ref, recipient_ref):
        with self._lock: return self._routes.get((sender_ref, recipient_ref))

    def _remove(self, keys, event, by, **extra):
        """Remove these directions; removing grants nothing, so a failed log write never blocks it."""
        with self._lock: gone = [self._routes.pop(key) for key in keys if key in self._routes]
        if gone:
            now = self.clock()
            try: append_log(self.pairings, PM_LOG, [self._record(event, by, route, now, **extra) for route in gone])
            except Exception: pass
        return [(route.sender.reference, route.recipient.reference) for route in gone]

    def _observed(self, binding):
        """A session now has this exact binding: its routes for any other binding of it end."""
        session = binding.identity.session_id
        with self._lock:
            keys = [key for key, route in self._routes.items()
                if any(chat.identity.session_id == session and chat != binding for chat in (route.sender, route.recipient))]
        if keys: self._remove(keys, 'replaced', 'bridge')

    def current(self):
        """Every PM route as (sender, recipient, time made) references."""
        with self._lock:
            return tuple(sorted((route.sender.reference, route.recipient.reference, route.made)
                for route in self._routes.values()))

    def clear(self):
        with self._lock: self._routes.clear()
