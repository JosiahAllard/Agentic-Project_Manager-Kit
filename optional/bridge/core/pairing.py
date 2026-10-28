"""Remembered owner chat choices with exact live-binding dispatch checks.

No process discovery, owner UI or native adapter is supplied here. Verifiers
must come from protected host integration, never wire arguments; both default
to denial. Dataclass construction is not attestation. No MCP tool exposes owner
writes. Storage must live in protected authority state before deployment.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import threading

from kitlib.ident import digest, identifier, integer
from .contracts import SessionIdentity, WorkspaceIdentity

APPLICATION_ID = 0x42503131
SCHEMA_VERSION = 2
MAX_CHATS = 128
MAX_ROUTES = 512
ROUTES_SCHEMA = '''
CREATE TABLE routes (
 sender TEXT NOT NULL CHECK(length(sender)=64),
 recipient TEXT NOT NULL CHECK(length(recipient)=64),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 PRIMARY KEY(sender,recipient), CHECK(sender<>recipient)
);
'''
LEGACY_SCHEMA = '''
CREATE TABLE legacy_routes (
 sender TEXT NOT NULL CHECK(length(sender)=64),
 recipient TEXT NOT NULL CHECK(length(recipient)=64),
 PRIMARY KEY(sender,recipient), CHECK(sender<>recipient)
);
'''
SCHEMA = '''
CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
INSERT INTO meta VALUES(1,0);
''' + ROUTES_SCHEMA + LEGACY_SCHEMA



class PairingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('bridge pairing rejected: ' + code)


@dataclass(frozen=True)
class ChatBinding:
    identity: SessionIdentity
    account_ref: str = field(repr=False)
    permission_revision: int

    def __post_init__(self):
        if type(self.identity) is not SessionIdentity or type(self.identity.workspace) is not WorkspaceIdentity:
            raise PairingError('invalid-chat-binding')
        for key, value in asdict(self.identity).items():
            if key == 'workspace':
                for part in value.values():
                    identifier(part)
            else:
                identifier(value)
        identifier(self.account_ref)
        integer(self.permission_revision)

    @property
    def reference(self):
        data = json.dumps(asdict(self), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        return hashlib.sha256(data.encode('ascii')).hexdigest()


    @property
    def logical_reference(self):
        """Stable selection key only; never a live permission or dispatch proof.

        The trusted observer must supply physical workspace identity. Titles,
        path strings or process identity alone cannot authenticate this key.
        """
        identity = self.identity
        data = {'session_id': identity.session_id, 'native_session_id': identity.native_session_id,
            'adapter_id': identity.adapter_id, 'workspace': asdict(identity.workspace),
            'account_ref': self.account_ref}
        encoded = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ChatSelection:
    sender_ref: str
    recipient_ref: str
    enabled: bool
    bidirectional: bool


class PairingStore:
    """Shared-core policy, not a native identity issuer or human-control implementation.

    live_check(binding) and owner_check(selection, opaque_proof) must return
    exactly True after authoritative checks. Names, IDs and truthy values confer
    no permissions. Adapters observe chats; an authenticated owner chooses routes.
    """
    def __init__(self, path, *, live_check=None, owner_check=None, prepare_new_file=None):
        if any(value is not None and not callable(value) for value in (live_check, owner_check, prepare_new_file)):
            raise PairingError('invalid-verifier-port')
        self.path = Path(path)
        self._live_check, self._owner_check = live_check, owner_check
        # Kept so a sibling state file (the writer-lease log) is created the same way.
        self.prepare_new_file = prepare_new_file
        self._lock, self._peers, self._db, self._observers = threading.RLock(), {}, None, []
        self._extension = None
        if not self.path.is_absolute() or not self.path.parent.is_dir():
            raise PairingError('protected-absolute-state-path-required')
        try:
            with self.path.open('xb'):
                if prepare_new_file is not None:
                    prepare_new_file(self.path)
            new = True
        except FileExistsError:
            new = False
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or self.path.is_symlink()
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or info.st_size > 8 * 1024 * 1024):
            raise PairingError('nonregular-or-oversized-state')
        try:
            self._db = sqlite3.connect(str(self.path), timeout=1, isolation_level=None, check_same_thread=False)
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or self.path.is_symlink():
                raise PairingError('state-file-replaced')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA max_page_count=2048')
            if new:
                self._db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA +
                    'PRAGMA application_id=%d; PRAGMA user_version=%d; COMMIT;' % (APPLICATION_ID, SCHEMA_VERSION))
            elif self._db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                raise PairingError('foreign-or-unsupported-state')
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (1, SCHEMA_VERSION):
                raise PairingError('foreign-or-unsupported-state')
            if self._db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise PairingError('state-integrity-failed')
            rows = self._db.execute('SELECT id,revision FROM meta').fetchall()
            if len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int or not 0 <= rows[0][1] < 2**53:
                raise PairingError('invalid-state-revision')
            legacy = self._db.execute('SELECT sender,recipient FROM '+
                ('routes' if version == 1 else 'legacy_routes')).fetchall()
            choices = [] if version == 1 else self._db.execute('SELECT sender,recipient,enabled FROM routes').fetchall()
            if len(legacy)+len(choices) > MAX_ROUTES:
                raise PairingError('route-capacity')
            for row in legacy+choices:
                digest(row[0])
                digest(row[1])
                if row[0] == row[1] or len(row) == 3 and (type(row[2]) is not int or row[2] not in (0,1)):
                    raise PairingError('invalid-stored-route')
            if version == 1:
                # Old rows contain only one-way exact-binding hashes. Preserve
                # them without guessing logical identities for reopened chats.
                self._db.executescript('BEGIN IMMEDIATE;\nALTER TABLE routes RENAME TO legacy_routes;\n'+
                    ROUTES_SCHEMA+'PRAGMA user_version=%d;\nCOMMIT;' % SCHEMA_VERSION)
        except (sqlite3.Error, OSError, ValueError):
            self.close()
            raise PairingError('pairing-state-open-failed') from None

    def _ready(self):
        if self._db is None:
            raise PairingError('pairing-store-closed')

    @contextmanager
    def _write(self):
        self._ready()
        try:
            self._db.execute('BEGIN IMMEDIATE')
            yield self._db
            revision = self._db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
            integer(revision + 1)
            self._db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
            self._db.execute('COMMIT')
        except BaseException:
            if self._db.in_transaction:
                self._db.execute('ROLLBACK')
            raise

    def _route_count(self):
        return (self._db.execute('SELECT COUNT(*) FROM routes').fetchone()[0]
            + self._db.execute('SELECT COUNT(*) FROM legacy_routes').fetchone()[0])

    def _promote_legacy(self):
        """Promote only concurrently observed and freshly verified original hashes."""
        by_ref = {peer.reference: peer for peer in self._peers.values()}
        candidates = [(sender, recipient) for sender, recipient in self._db.execute(
            'SELECT sender,recipient FROM legacy_routes') if sender in by_ref and recipient in by_ref]
        if not candidates:
            return
        with self._write() as db:
            for sender, recipient in candidates:
                left, right = by_ref[sender], by_ref[recipient]
                try:
                    self._observed(left)
                    self._observed(right)
                except PairingError:
                    continue
                # A disabled logical row is retained disconnection evidence;
                # an unresolved old grant must not overwrite an owner revoke.
                db.execute('INSERT OR IGNORE INTO routes VALUES(?,?,1)',
                    (left.logical_reference, right.logical_reference))
                db.execute('DELETE FROM legacy_routes WHERE sender=? AND recipient=?', (sender, recipient))
            if self._route_count() > MAX_ROUTES:
                raise PairingError('route-capacity')

    def on_observe(self, callback):
        """Private service port: callback(binding) runs after each verified observation.

        A listener uses it to learn that a session now has this exact binding, so
        any other binding it holds for that session is replaced. A failed live
        check or a withdraw is not reported: a chat between turns or missed by one
        discovery pass is not closed. Runs under the store lock; a failing
        callback never blocks the observation.
        """
        if not callable(callback):
            raise PairingError('invalid-verifier-port')
        with self._lock:
            self._observers.append(callback)

    def extend_routes(self, check):
        """Private service port, set once: check(sender, recipient) is True opens a direction
        that has no owner row (core/pmroutes.py, README "PM routes"). An owner row decides alone:
        enabled passes, disabled refuses whatever check says. Runs under the store lock, so it
        must not wait on anything that waits on this store."""
        if not callable(check):
            raise PairingError('invalid-verifier-port')
        with self._lock:
            if self._extension is not None:
                raise PairingError('route-extension-set')
            self._extension = check

    @contextmanager
    def held(self):
        """Hold the store lock: a route extension's changes serialize with every route check
        and every guarded send."""
        with self._lock:
            self._ready()
            yield

    def owner_choice(self, sender, recipient):
        """The owner's row for this direction of these chats' logical identities: True
        (connected), False (disconnected) or None (never chosen)."""
        if type(sender) is not ChatBinding or type(recipient) is not ChatBinding:
            raise PairingError('chat-not-observed')
        with self._lock:
            self._ready()
            row = self._db.execute('SELECT enabled FROM routes WHERE sender=? AND recipient=?',
                                   (sender.logical_reference, recipient.logical_reference)).fetchone()
            return None if row is None else row[0] == 1

    def _route_open(self, sender, recipient):
        row = self._db.execute('SELECT enabled FROM routes WHERE sender=? AND recipient=?',
                               (sender.logical_reference, recipient.logical_reference)).fetchone()
        if row is not None:
            return row[0] == 1
        try:
            return self._extension is not None and self._extension(sender, recipient) is True
        except Exception:
            return False

    def owner_block(self, sender, recipient, proof):
        """Owner-control port only: write a disabled row for this one direction, whether or not
        the chats are observed now. It connects nothing, so no live check is needed."""
        if type(sender) is not ChatBinding or type(recipient) is not ChatBinding or type(proof) is not bytes:
            raise PairingError('invalid-owner-selection')
        selection = ChatSelection(sender.reference, recipient.reference, False, False)
        with self._lock:
            self._ready()
            try:
                approved = self._owner_check is not None and self._owner_check(selection, proof) is True
            except Exception:
                approved = False
            if not approved:
                raise PairingError('owner-control-unverified')
            with self._write() as db:
                db.execute('INSERT INTO routes VALUES(?,?,0) ON CONFLICT(sender,recipient) '
                    'DO UPDATE SET enabled=0', (sender.logical_reference, recipient.logical_reference))
                if self._route_count() > MAX_ROUTES:
                    raise PairingError('route-capacity')

    def _live(self, binding):
        try:
            verified = self._live_check is not None and self._live_check(binding) is True
        except Exception:
            verified = False
        if not verified:
            if self._peers.get(binding.identity.session_id) == binding:
                del self._peers[binding.identity.session_id]
            raise PairingError('chat-live-binding-unverified')

    def observe(self, binding):
        """Trusted adapter port; self-claimed identities are denied by default."""
        if type(binding) is not ChatBinding:
            raise PairingError('invalid-chat-binding')
        with self._lock:
            self._ready()
            self._live(binding)
            session = binding.identity.session_id
            previous = self._peers.get(session)
            if previous is None and len(self._peers) >= MAX_CHATS:
                raise PairingError('chat-capacity')
            self._peers[session] = binding
            self._promote_legacy()
            self._observed(binding)
            for callback in tuple(self._observers):
                try:
                    callback(binding)
                except Exception:
                    pass
            return binding.reference

    def registered(self):
        """Observed metadata snapshot only; dispatch must freshly validate both chats."""
        with self._lock:
            self._ready()
            return tuple(self._peers.values())

    def remembered_routes(self):
        """Private enabled logical-choice snapshot, including suspended chats.

        This is not liveness, permissions or authority to dispatch. Legacy rows
        whose stable identity is unknown remain preserved but cannot be projected.
        """
        with self._lock:
            self._ready()
            return tuple(tuple(row) for row in self._db.execute(
                'SELECT sender,recipient FROM routes WHERE enabled=1 ORDER BY sender,recipient'))

    def owner_routes(self):
        """Private current exact-reference projection; not a liveness proof."""
        with self._lock:
            self._ready()
            current = {peer.logical_reference: peer.reference for peer in self._peers.values()}
            return tuple(sorted((current[left], current[right]) for left, right in self.remembered_routes()
                if left in current and right in current))

    def _observed(self, binding):
        if type(binding) is not ChatBinding or self._peers.get(binding.identity.session_id) != binding:
            raise PairingError('chat-not-observed')
        self._live(binding)

    def propose(self, sender_id, recipient_id, *, enabled=True, bidirectional=True):
        """Prepare exact immutable references for owner review; grants nothing."""
        if type(enabled) is not bool or type(bidirectional) is not bool:
            raise PairingError('invalid-selection-options')
        identifier(sender_id)
        identifier(recipient_id)
        with self._lock:
            self._ready()
            sender, recipient = self._peers.get(sender_id), self._peers.get(recipient_id)
            if sender is None or recipient is None:
                raise PairingError('chat-not-observed')
            if sender_id == recipient_id:
                raise PairingError('same-chat-selection')
            for peer in (sender, recipient):
                self._observed(peer)
            return ChatSelection(sender.reference, recipient.reference, enabled, bidirectional)

    def _selection_peers(self, selection):
        by_ref = {peer.reference: peer for peer in self._peers.values()}
        peers = (by_ref.get(selection.sender_ref), by_ref.get(selection.recipient_ref))
        if any(peer is None for peer in peers):
            raise PairingError('selection-chat-changed')
        for peer in peers:
            self._observed(peer)
        return peers

    def owner_select(self, selection, proof):
        """Owner-control port only; must not be exported as an ordinary agent tool."""
        if (type(selection) is not ChatSelection or type(selection.enabled) is not bool
                or type(selection.bidirectional) is not bool or selection.sender_ref == selection.recipient_ref
                or type(proof) is not bytes or not 0 < len(proof) <= 4096):
            raise PairingError('invalid-owner-selection')
        digest(selection.sender_ref)
        digest(selection.recipient_ref)
        with self._lock:
            self._ready()
            self._selection_peers(selection)
            try:
                approved = self._owner_check is not None and self._owner_check(selection, proof) is True
            except Exception:
                approved = False
            if not approved:
                raise PairingError('owner-control-unverified')
            # Approval remains bound to exact fresh native facts. Only the
            # owner's resulting route choice survives later incarnations.
            sender, recipient = self._selection_peers(selection)
            routes = [(sender, recipient)]
            if selection.bidirectional:
                routes.append((recipient, sender))
            with self._write() as db:
                for left, right in routes:
                    db.execute('INSERT INTO routes VALUES(?,?,?) ON CONFLICT(sender,recipient) '
                        'DO UPDATE SET enabled=excluded.enabled',
                        (left.logical_reference, right.logical_reference, int(selection.enabled)))
                    db.execute('DELETE FROM legacy_routes WHERE sender=? AND recipient=?',
                        (left.reference, right.reference))
                if self._route_count() > MAX_ROUTES:
                    raise PairingError('route-capacity')
            return self._db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]

    def require_selected(self, sender, recipient):
        """A necessary route gate; native caller attribution/refusals remain separate gates."""
        with self._lock:
            self._ready()
            if type(sender) is not ChatBinding or type(recipient) is not ChatBinding:
                raise PairingError('chat-not-observed')
            # The route row first: a route that is off is refused as such, whatever the
            # chats' liveness. The live checks still run, since they drop a dead chat from
            # the observed list (the owner window's only prune for hosts with no withdraw).
            # With no owner row, a route extension (PM routes) may open it.
            if not self._route_open(sender, recipient):
                for binding in (sender, recipient):
                    try:
                        self._observed(binding)
                    except PairingError:
                        pass
                raise PairingError('chat-route-not-selected')
            self._observed(sender)
            self._observed(recipient)

    @contextmanager
    def selected_guard(self, sender, recipient):
        """Serialize a bounded broker action with owner route changes, including other DB clients.

        This is a private service boundary, not an agent-controlled callback.
        Native identity verification and native write timeouts remain mandatory.
        """
        with self._lock:
            self.require_selected(sender, recipient)
            self._db.execute('BEGIN IMMEDIATE')
            try:
                if not self._route_open(sender, recipient):
                    raise PairingError('chat-route-not-selected')
                yield
                self._db.execute('COMMIT')
            finally:
                if self._db.in_transaction:
                    self._db.execute('ROLLBACK')

    def withdraw(self, session_id):
        """Suspend a native observation; preserve the remembered owner choice."""
        identifier(session_id)
        with self._lock:
            self._ready()
            peer = self._peers.get(session_id)
            if peer is not None:
                del self._peers[session_id]

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._peers.clear()

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()
