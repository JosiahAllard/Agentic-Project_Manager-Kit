"""Durable message bodies, native receipts, refusal roots and cursor events.

This is private broker storage, not an RPC surface or an ownership verifier.
The protected supervisor supplies a fresh kernel-backed leadership check; no
write or native callback runs with the default denied ownership port. An epoch
does not itself prove OS ownership. State requires protected installation.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import stat
import threading
import time

from kitlib.ident import identifier, integer
from .contracts import SessionIdentity, WorkspaceIdentity
from .messages import (BeforeWriteError, DeliveryError, DeliveryEvent, DeliveryReceipt,
                       NativeResult, OutboundMessage, TERMINAL_STATES, text_bytes,
                       REPLY_OPERATION_PREFIX,reply_operation_id)
from .pairing import ChatBinding

APPLICATION_ID = 0x424F5831
SCHEMA_VERSION = 3
MAX_MESSAGES = 512
MAX_REPLY_DELIVERIES = 512
MAX_PENDING = MAX_MESSAGES + MAX_REPLY_DELIVERIES
REPLY_DELIVERY_SECONDS = 3600
REPLY_TABLE = '''CREATE TABLE reply_deliveries(
 parent TEXT PRIMARY KEY,child TEXT UNIQUE NOT NULL,state TEXT NOT NULL,
 expires REAL NOT NULL,reason TEXT
)'''
MAX_EVENTS = 8192
EVENT_TABLE = 'CREATE TABLE event_reservations(operation TEXT PRIMARY KEY,slots INTEGER NOT NULL)'
SCHEMA = '''
CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,last_clock REAL NOT NULL);
INSERT INTO meta VALUES(1,0,0);
CREATE TABLE refusals(root TEXT PRIMARY KEY,reason TEXT NOT NULL);
CREATE TABLE messages(
 id TEXT PRIMARY KEY,root TEXT NOT NULL,source TEXT NOT NULL,target TEXT NOT NULL,
 source_json TEXT NOT NULL,target_json TEXT NOT NULL,body TEXT NOT NULL,body_sha TEXT NOT NULL,
 expires REAL,state TEXT NOT NULL,last_confirmed TEXT NOT NULL,dispatch_epoch INTEGER,
 native_id TEXT,reply TEXT,reply_sha TEXT,reason TEXT,reply_of TEXT
);
CREATE TABLE events(
 cursor INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT NOT NULL,state TEXT NOT NULL,epoch INTEGER NOT NULL
);
CREATE INDEX events_operation ON events(operation,cursor);
'''+REPLY_TABLE+';'+EVENT_TABLE+';'



def _binding(data):
    if type(data) is not str or len(data) > 8192:
        raise DeliveryError('invalid-stored-binding')
    try:
        value = json.loads(data)
        identity = value.pop('identity')
        identity['workspace'] = WorkspaceIdentity(**identity['workspace'])
        return ChatBinding(SessionIdentity(**identity), **value)
    except (ValueError, TypeError, KeyError):
        raise DeliveryError('invalid-stored-binding') from None


class Outbox:
    def __init__(self, path, *, leadership_check=None, clock=time.time, prepare_new_file=None):
        if (not callable(clock) or leadership_check is not None and not callable(leadership_check)
                or prepare_new_file is not None and not callable(prepare_new_file)):
            raise DeliveryError('invalid-outbox-ports')
        self.path, self._leader, self._clock = Path(path), leadership_check, clock
        self._lock, self._db, self._epoch = threading.RLock(), None, None
        if not self.path.is_absolute() or not self.path.parent.is_dir():
            raise DeliveryError('protected-absolute-state-path-required')
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
                or info.st_size > 128 * 1024 * 1024):
            raise DeliveryError('nonregular-or-oversized-state')
        try:
            self._db = sqlite3.connect(str(self.path), timeout=1, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or self.path.is_symlink():
                raise DeliveryError('state-file-replaced')
            self._schema=self._db.execute('PRAGMA user_version').fetchone()[0]
            if not new and (self._db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                            or self._schema not in (1,2,SCHEMA_VERSION)):
                raise DeliveryError('foreign-or-unsupported-state')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA max_page_count=32768')
            if new:
                self._db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA +
                    'PRAGMA application_id=%d;PRAGMA user_version=%d;COMMIT;' % (APPLICATION_ID, SCHEMA_VERSION))
                self._schema=SCHEMA_VERSION
            if self._db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise DeliveryError('corrupt-outbox')
            row = self._db.execute('SELECT id,epoch,last_clock FROM meta').fetchall()
            if (len(row) != 1 or row[0]['id'] != 1 or type(row[0]['epoch']) is not int
                    or not 0 <= row[0]['epoch'] < 2**53
                    or type(row[0]['last_clock']) not in (int, float)
                    or not 0 <= row[0]['last_clock'] <= 1e12 or not math.isfinite(row[0]['last_clock'])):
                raise DeliveryError('invalid-outbox-meta')
            normal=' WHERE reply_of IS NULL' if self._schema>=2 else ''
            if (self._db.execute('SELECT COUNT(*) FROM messages'+normal).fetchone()[0] > MAX_MESSAGES
                    or self._db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] > MAX_PENDING
                    or self._db.execute('SELECT COUNT(*) FROM events').fetchone()[0] > MAX_EVENTS
                    or self._schema>=2 and (self._db.execute(
                        'SELECT COUNT(*) FROM reply_deliveries').fetchone()[0]>MAX_MESSAGES
                        or self._db.execute('SELECT COUNT(*) FROM messages WHERE reply_of IS NOT NULL').fetchone()[0]>MAX_REPLY_DELIVERIES)):
                raise DeliveryError('outbox-capacity')
            if self._schema>=3 and (self._reserved(self._db)+self._db.execute(
                    'SELECT COUNT(*) FROM events').fetchone()[0]>MAX_EVENTS):
                raise DeliveryError('event-capacity')
        except (sqlite3.Error, OSError, DeliveryError):
            self.close()
            raise DeliveryError('outbox-open-failed') from None

    def _ready(self):
        if self._db is None:
            raise DeliveryError('outbox-closed')

    def _ownership(self):
        try:
            valid = self._leader is not None and self._leader() is True
        except Exception:
            valid = False
        if not valid:
            raise DeliveryError('broker-ownership-unverified')

    @contextmanager
    def _write(self, claimed=True):
        self._ready()
        self._ownership()
        self._db.execute('BEGIN IMMEDIATE')
        try:
            meta = self._db.execute('SELECT epoch,last_clock FROM meta WHERE id=1').fetchone()
            if claimed and (self._epoch is None or self._epoch != meta['epoch']):
                raise DeliveryError('stale-broker-fence')
            now = self._clock()
            if (type(now) not in (int, float) or not 0 <= now <= 1e12 or not math.isfinite(now)
                    or now < meta['last_clock']):
                raise DeliveryError('outbox-clock-unverified')
            self._ownership()
            yield self._db, now
            self._db.execute('UPDATE meta SET last_clock=? WHERE id=1', (now,))
            self._db.execute('COMMIT')
        except BaseException:
            if self._db.in_transaction:
                self._db.execute('ROLLBACK')
            raise

    def _reserved(self, db, exclude=None):
        values=db.execute('SELECT operation,slots FROM event_reservations').fetchall()
        if (len(values)>MAX_PENDING or any(type(row['slots']) is not int
                or not 1<=row['slots']<=MAX_PENDING for row in values)):
            raise DeliveryError('invalid-event-reservation')
        return sum(row['slots'] for row in values if row['operation']!=exclude)

    def _event(self, db, operation, state, epoch=None):
        owned=db.execute('SELECT slots FROM event_reservations WHERE operation=?',(operation,)).fetchone()
        count=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        if count+self._reserved(db,exclude=operation)>=MAX_EVENTS:
            raise DeliveryError('event-capacity')
        if owned is not None:
            if owned['slots']==1:db.execute('DELETE FROM event_reservations WHERE operation=?',(operation,))
            else:db.execute('UPDATE event_reservations SET slots=slots-1 WHERE operation=?',(operation,))
        db.execute('INSERT INTO events(operation,state,epoch) VALUES(?,?,?)',
                   (operation,state,self._epoch if epoch is None else epoch))

    def _start_reservation(self, db, row):
        siblings=db.execute("SELECT COUNT(*) FROM messages WHERE root=? AND state='queued' AND id<>?",
            (row['root'],row['id'])).fetchone()[0]
        slots=1+siblings
        db.execute('INSERT INTO event_reservations VALUES(?,?)',(row['id'],slots))
        if db.execute('SELECT COUNT(*) FROM events').fetchone()[0]+self._reserved(db)>MAX_EVENTS:
            raise DeliveryError('event-capacity')

    def _release_reservation(self, db, operation):
        db.execute('DELETE FROM event_reservations WHERE operation=?',(operation,))

    def _new_queued_sibling(self, db, root):
        # Every active callback reservation needs the possible sibling refusal
        # transition. Native observation also reserves accepted/held/unknown
        # parents, so message state cannot stand in for a live reservation.
        db.execute("UPDATE event_reservations SET slots=slots+1 WHERE operation IN "
            "(SELECT id FROM messages WHERE root=?)",(root,))
        if db.execute('SELECT COUNT(*) FROM events').fetchone()[0]+self._reserved(db)>=MAX_EVENTS:
            raise DeliveryError('event-capacity')

    def _reserve_events(self, db, row, dispatch=False):
        # An authenticated refusal may cancel every queued sibling. Reserve
        # that entire transition before native I/O, not after some writes.
        siblings = db.execute("SELECT COUNT(*) FROM messages WHERE root=? AND state='queued' AND id<>?",
                              (row['root'], row['id'])).fetchone()[0]
        needed = 1 + siblings + int(dispatch)
        count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        if count + self._reserved(db,exclude=row['id']) + needed > MAX_EVENTS:
            raise DeliveryError('event-capacity')

    def claim(self):
        """Supervisor-only: requires current OS ownership, then advances a durable fence."""
        with self._lock:
            with self._write(claimed=False) as (db, now):
                epoch = db.execute('SELECT epoch FROM meta WHERE id=1').fetchone()[0] + 1
                integer(epoch)
                db.execute('UPDATE meta SET epoch=? WHERE id=1', (epoch,))
                if self._schema==1:
                    # DDL is inside the verified owner transaction/fence. It
                    # does not create deliveries for historical stored replies.
                    db.execute('ALTER TABLE messages ADD COLUMN reply_of TEXT')
                    db.execute(REPLY_TABLE)
                    for legacy in db.execute("SELECT id FROM messages WHERE state='reply-received'").fetchall():
                        db.execute('INSERT INTO reply_deliveries VALUES(?,?,?,?,?)',
                            (legacy['id'],reply_operation_id(legacy['id']),'suppressed',
                             now+REPLY_DELIVERY_SECONDS,'legacy-recorded-reply'))
                if self._schema<3:
                    db.execute(EVENT_TABLE)
                    db.execute('PRAGMA user_version=3')
                rows = db.execute("SELECT id,state FROM messages WHERE state IN ('dispatching','recipient-accepted','held')").fetchall()
                for row in rows:
                    db.execute("UPDATE messages SET state='unknown',reason='broker-restarted' WHERE id=?", (row['id'],))
                    self._release_reservation(db,row['id'])
                    self._event(db, row['id'], 'unknown', epoch)
                db.execute('DELETE FROM event_reservations')
            self._epoch = epoch
            self._schema=SCHEMA_VERSION
            return epoch

    def _row(self, operation):
        self._ready()
        identifier(operation)
        return self._db.execute('SELECT * FROM messages WHERE id=?', (operation,)).fetchone()

    def _message(self, row):
        if row is None:
            raise DeliveryError('unknown-operation')
        message = OutboundMessage(row['id'], row['root'], _binding(row['source_json']),
                                  _binding(row['target_json']), row['body'], row['expires'],
                                  row['reply_of'] if 'reply_of' in row.keys() else None)
        if (message.sender.reference != row['source'] or message.recipient.reference != row['target']
                or message.intent.body_sha256 != row['body_sha']):
            raise DeliveryError('stored-message-integrity')
        if message.reply_of is not None:
            parent=self._row(message.reply_of)
            link=self._db.execute('SELECT * FROM reply_deliveries WHERE parent=?',(message.reply_of,)).fetchone()
            if (parent is None or parent['reply_of'] is not None or link is None
                    or link['child']!=row['id'] or link['state']!='queued'
                    or parent['source']!=row['target'] or parent['target']!=row['source']
                    or parent['source_json']!=row['target_json'] or parent['target_json']!=row['source_json']
                    or parent['root']!=row['root'] or parent['reply']!=row['body']
                    or parent['reply_sha']!=row['body_sha'] or link['expires']!=row['expires']):
                raise DeliveryError('stored-reply-delivery-integrity')
        return message

    def message(self, operation):
        """Private controller data; an RPC must authorize before revealing any body."""
        with self._lock:
            return self._message(self._row(operation))

    def _receipt(self, row):
        if row is None:
            return None
        return DeliveryReceipt(row['id'], row['state'], self._epoch or 0, row['native_id'],
                               row['reason'], row['body_sha'], row['reply_sha'])

    def receipt(self, operation):
        with self._lock:
            return self._receipt(self._row(operation))

    def pending(self, limit=32):
        """Private supervisor queue snapshot, never exposed to ordinary callers."""
        if type(limit) is not int or not 1<=limit<=MAX_PENDING:
            raise DeliveryError('invalid-pending-limit')
        with self._lock:
            self._ready()
            return tuple(row[0] for row in self._db.execute(
                "SELECT id FROM messages WHERE state='queued' ORDER BY rowid LIMIT ?",(limit,)))

    def admit(self, message):
        if type(message) is not OutboundMessage:
            raise DeliveryError('invalid-outbound-message')
        message.__post_init__()
        if message.reply_of is not None or message.operation_id.startswith(REPLY_OPERATION_PREFIX):
            raise DeliveryError('reserved-reply-operation')
        with self._lock:
            with self._write() as (db, now):
                prior = self._row(message.operation_id)
                if prior is not None:
                    if self._message(prior) != message:
                        raise DeliveryError('operation-conflict')
                    return self._receipt(prior)
                if db.execute('SELECT 1 FROM refusals WHERE root=?', (message.task_root,)).fetchone():
                    raise DeliveryError('task-root-refused')
                if db.execute('SELECT COUNT(*) FROM messages WHERE reply_of IS NULL').fetchone()[0] >= MAX_MESSAGES:
                    raise DeliveryError('message-capacity')
                if message.expires_at is not None and now >= message.expires_at:
                    raise DeliveryError('message-already-expired')
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,\'queued\',\'queued\',NULL,NULL,NULL,NULL,NULL,NULL)',
                    (message.operation_id, message.task_root, message.sender.reference, message.recipient.reference,
                     json.dumps(asdict(message.sender), sort_keys=True), json.dumps(asdict(message.recipient), sort_keys=True),
                     message.text, message.intent.body_sha256, message.expires_at))
                self._new_queued_sibling(db,message.task_root)
                self._event(db, message.operation_id, 'queued')
            return self.receipt(message.operation_id)

    def _refuse_root(self, db, root):
        db.execute('INSERT OR IGNORE INTO refusals VALUES(?,?)', (root, 'native-refusal'))
        db.execute("UPDATE reply_deliveries SET state='suppressed',reason='task-root-refused' "
            "WHERE state='pending' AND parent IN (SELECT id FROM messages WHERE root=?)",(root,))
        queued = db.execute("SELECT id FROM messages WHERE root=? AND state='queued'", (root,)).fetchall()
        for row in queued:
            db.execute("UPDATE messages SET state='refused',last_confirmed='refused',reason='task-root-refused' WHERE id=?", (row['id'],))
            self._event(db, row['id'], 'refused')

    def _native(self, db, row, result, now, reverse_reply=False):
        if (type(result) is not NativeResult or result.operation_id != row['id']
                or result.recipient_ref != row['target']
                or row['native_id'] is not None and result.native_id != row['native_id']):
            raise DeliveryError('native-receipt-correlation')
        reply_sha = hashlib.sha256(text_bytes(result.reply)).hexdigest() if result.reply is not None else None
        if row['state'] in TERMINAL_STATES:
            if (row['state'], row['native_id'], row['reply_sha']) != (result.state, result.native_id, reply_sha):
                raise DeliveryError('terminal-receipt-conflict')
            return
        if row['state'] not in ('dispatching', 'recipient-accepted', 'held', 'unknown'):
            raise DeliveryError('native-receipt-before-dispatch')
        if row['state'] == 'unknown' and result.state == 'failed':
            raise DeliveryError('unknown-is-not-confirmed-nondelivery')
        if (row['state'], row['native_id'], row['reply_sha']) == (result.state, result.native_id, reply_sha):
            return
        db.execute('UPDATE messages SET state=?,last_confirmed=?,native_id=?,reply=?,reply_sha=?,reason=NULL WHERE id=?',
                   (result.state, result.state, result.native_id, result.reply, reply_sha, row['id']))
        self._release_reservation(db,row['id'])
        self._event(db, row['id'], result.state)
        if result.state == 'reply-received':
            self._record_reverse(db,self._row(row['id']),now,reverse_reply)
        if result.state == 'refused':
            self._refuse_root(db, row['root'])

    def append_native(self, operation, result, *, reverse_reply=False):
        """Private authenticated-adapter event port; caller-supplied receipts are not accepted by RPC."""
        with self._lock:
            with self._write() as (db, now):
                row = self._row(operation)
                if row is None:
                    raise DeliveryError('unknown-operation')
                self._reserve_events(db, row)
                self._native(db, row, result,now,reverse_reply)
            return self.receipt(operation)

    def reject_queued(self, operation, reason):
        identifier(reason)
        with self._lock:
            with self._write() as (db, now):
                row = self._row(operation)
                if row is None:
                    raise DeliveryError('unknown-operation')
                if row['state'] == 'queued':
                    db.execute("UPDATE messages SET state='refused',last_confirmed='refused',reason=? WHERE id=?", (reason, operation))
                    self._event(db, operation, 'refused')
            return self.receipt(operation)

    def run_dispatch(self, operation, send, *, reverse_reply=False):
        """Durable intent/reservation first; native callback runs outside SQL.

        The bounded callback retains this instance's lock and Core route guard.
        Reentrant authenticated replies may commit their own transaction; the
        post-I/O row always wins over a late acknowledgement or transport error.
        """
        if not callable(send):raise DeliveryError('invalid-native-send-port')
        with self._lock:
            with self._write() as (db,now):
                row=self._row(operation);message=self._message(row)
                if row['state']!='queued':return self._receipt(row)
                if db.execute('SELECT 1 FROM refusals WHERE root=?',(message.task_root,)).fetchone():
                    self._refuse_root(db,message.task_root);return self.receipt(operation)
                if message.expires_at is not None and now>=message.expires_at:
                    db.execute("UPDATE messages SET state='expired',last_confirmed='expired',reason='queued-expiry' WHERE id=?",(operation,))
                    self._event(db,operation,'expired');return self.receipt(operation)
                self._reserve_events(db,row,dispatch=True)
                db.execute("UPDATE messages SET state='dispatching',dispatch_epoch=? WHERE id=?",(self._epoch,operation))
                self._event(db,operation,'dispatching')
                self._start_reservation(db,row)
            # Verify ownership/fence again before I/O, with no transaction left
            # open during the callback. The event quota is durable, not a flag.
            with self._write() as (db,_now):
                row=self._row(operation)
                if row['state'] in TERMINAL_STATES:
                    self._release_reservation(db,operation);return self._receipt(row)
                self._reserve_events(db,row)
            outcome=error=None
            try:outcome=send(message)
            except Exception as failure:error=failure
            with self._write() as (db,now):
                row=self._row(operation)
                if row['state'] in TERMINAL_STATES:
                    self._release_reservation(db,operation)
                    return self._receipt(row)
                db.execute('SAVEPOINT native_receipt')
                try:
                    if error is not None:raise error
                    self._native(db,row,outcome,now,reverse_reply)
                except BeforeWriteError as refused:
                    db.execute('ROLLBACK TO native_receipt')
                    self._release_reservation(db,operation)
                    # Keep the adapter's own fixed code (which check refused) when it is a
                    # valid identifier; otherwise the generic reason.
                    try:reason=identifier(refused.code)
                    except Exception:reason='native-before-write'
                    db.execute("UPDATE messages SET state='failed',last_confirmed='failed',reason=? WHERE id=?",(reason,operation))
                    self._event(db,operation,'failed')
                except Exception as uncertain:
                    db.execute('ROLLBACK TO native_receipt')
                    self._release_reservation(db,operation)
                    # The generic reason, with the adapter's own code appended when it has a
                    # valid one, so a live miss says which check after the write failed.
                    reason='native-outcome-uncertain'
                    try:reason=identifier(reason+':'+uncertain.code)
                    except Exception:pass
                    db.execute("UPDATE messages SET state='unknown',reason=? WHERE id=?",(reason,operation))
                    self._event(db,operation,'unknown')
                db.execute('RELEASE native_receipt')
                self._release_reservation(db,operation)
            return self.receipt(operation)

    @contextmanager
    def native_observation(self, operation):
        """Reserve outcome/refusal capacity before a native read; no callback in SQL."""
        with self._lock:
            with self._write() as (db,_now):
                row=self._row(operation)
                if row is None:raise DeliveryError('unknown-operation')
                self._reserve_events(db,row)
                self._start_reservation(db,row)
            try:yield
            finally:
                with self._write() as (db,_now):self._release_reservation(db,operation)

    def reply(self, operation):
        with self._lock:
            row = self._row(operation)
            if row is None:
                raise DeliveryError('unknown-operation')
            value = row['reply']
            if value is not None and hashlib.sha256(text_bytes(value)).hexdigest() != row['reply_sha']:
                raise DeliveryError('stored-reply-integrity')
            return value if row['state'] == 'reply-received' else None

    def recipient_reply(self, operation, text, *, reverse_reply=False):
        """Private authenticated-recipient API; preserves any native ID already known."""
        reply_sha = hashlib.sha256(text_bytes(text)).hexdigest()
        with self._lock:
            with self._write() as (db, now):
                row = self._row(operation)
                if row is None:
                    raise DeliveryError('unknown-operation')
                if row['reply_of'] is not None:
                    raise DeliveryError('reply-to-reply-delivery')
                if row['state'] == 'queued':
                    raise DeliveryError('reply-before-dispatch')
                if row['state'] in TERMINAL_STATES:
                    if row['state'] != 'reply-received' or row['reply_sha'] != reply_sha:
                        raise DeliveryError('terminal-receipt-conflict')
                    return self._receipt(row)
                db.execute("UPDATE messages SET state='reply-received',last_confirmed='reply-received',reply=?,reply_sha=?,reason=NULL WHERE id=?",
                           (text, reply_sha, operation))
                self._release_reservation(db,operation)
                self._event(db, operation, 'reply-received')
                self._record_reverse(db,self._row(operation),now,reverse_reply,strict=True)
            return self.receipt(operation)

    def _reply_link(self, operation):
        row=self._db.execute('SELECT * FROM reply_deliveries WHERE parent=?',(operation,)).fetchone()
        if row is None:return None
        parent=self._row(operation)
        if (parent is None or parent['reply_of'] is not None or parent['state']!='reply-received'
                or row['child']!=reply_operation_id(operation) or row['state'] not in ('pending','queued','suppressed')
                or type(row['expires']) not in (float,int) or not 0<row['expires']<=1e12
                or not math.isfinite(row['expires'])):
            raise DeliveryError('stored-reply-delivery-integrity')
        if hashlib.sha256(text_bytes(parent['reply'])).hexdigest()!=parent['reply_sha']:
            raise DeliveryError('stored-reply-integrity')
        if row['reason'] is not None:identifier(row['reason'])
        return row

    def reply_delivery(self, operation):
        """Private reverse-job decision/status link; parent receipt stays unchanged."""
        with self._lock:
            self._ready()
            identifier(operation)
            row=self._reply_link(operation)
            return dict(row) if row is not None else None

    def pending_replies(self, limit=32):
        if type(limit) is not int or not 1<=limit<=MAX_MESSAGES:
            raise DeliveryError('invalid-pending-limit')
        with self._lock:
            self._ready()
            return tuple(row[0] for row in self._db.execute(
                "SELECT parent FROM reply_deliveries WHERE state='pending' ORDER BY rowid LIMIT ?",(limit,)))

    def _record_reverse(self, db, row, now, enabled, strict=False):
        if row['reply_of'] is not None or db.execute(
                'SELECT 1 FROM reply_deliveries WHERE parent=?',(row['id'],)).fetchone():
            return  # Includes durable suppression: later route grants never replay.
        expires=min(now+REPLY_DELIVERY_SECONDS,1e12,row['expires']) if row['expires'] is not None else min(now+REPLY_DELIVERY_SECONDS,1e12)
        reason=None
        if enabled is not True:reason='reverse-route-not-selected'
        elif db.execute('SELECT 1 FROM refusals WHERE root=?',(row['root'],)).fetchone():reason='task-root-refused'
        elif now>=expires:reason='reply-delivery-expired'
        child=reply_operation_id(row['id'])
        db.execute('INSERT INTO reply_deliveries VALUES(?,?,?,?,?)',
            (row['id'],child,'suppressed' if reason else 'pending',expires,reason))
        if reason is None:self._try_queue_reply(db,row,strict=strict)

    def _try_queue_reply(self, db, parent, strict=False):
        link=self._reply_link(parent['id'])
        if link is None or link['state']!='pending':return
        db.execute('SAVEPOINT reverse_job')
        try:
            if self._row(link['child']) is not None:raise DeliveryError('reply-operation-conflict')
            if db.execute('SELECT COUNT(*) FROM messages WHERE reply_of IS NOT NULL').fetchone()[0]>=MAX_REPLY_DELIVERIES:
                raise DeliveryError('message-capacity')
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,'queued','queued',NULL,NULL,NULL,NULL,NULL,?)",
                (link['child'],parent['root'],parent['target'],parent['source'],parent['target_json'],
                 parent['source_json'],parent['reply'],parent['reply_sha'],link['expires'],parent['id']))
            self._new_queued_sibling(db,parent['root'])
            self._event(db,link['child'],'queued')
            db.execute("UPDATE reply_deliveries SET state='queued',reason=NULL WHERE parent=?",(parent['id'],))
        except DeliveryError as error:
            db.execute('ROLLBACK TO reverse_job')
            if error.code not in ('message-capacity','event-capacity','reply-operation-conflict'):raise
            if strict and error.code=='reply-operation-conflict':raise
            db.execute('UPDATE reply_deliveries SET state=?,reason=? WHERE parent=?',
                ('suppressed' if error.code=='reply-operation-conflict' else 'pending',error.code,parent['id']))
        except sqlite3.Error as error:
            # Only statement-local contention/constraint failures with the outer
            # transaction still alive can preserve its known reply. FULL, I/O,
            # corruption and transaction-ending errors are not logical capacity.
            code=getattr(error,'sqlite_errorcode',None)
            if (not db.in_transaction or type(code) is not int or (code & 255) not in
                    (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_CONSTRAINT)):
                raise
            db.execute('ROLLBACK TO reverse_job')
            db.execute("UPDATE reply_deliveries SET reason='reply-storage-error' WHERE parent=?",(parent['id'],))
        finally:
            if db.in_transaction:db.execute('RELEASE reverse_job')

    def promote_reply(self, operation):
        """Core holds a fresh reverse route guard; no callbacks inside storage."""
        with self._lock:
            with self._write() as (db,now):
                parent=self._row(operation)
                link=self._reply_link(operation)
                if parent is None or link is None or link['state']!='pending':return
                self._message(parent)
                reason=None
                if now>=link['expires']:reason='reply-delivery-expired'
                elif db.execute('SELECT 1 FROM refusals WHERE root=?',(parent['root'],)).fetchone():reason='task-root-refused'
                if reason:db.execute("UPDATE reply_deliveries SET state='suppressed',reason=? WHERE parent=?",(reason,operation))
                else:self._try_queue_reply(db,parent)

    def suppress_reply(self, operation, reason):
        identifier(reason)
        with self._lock:
            with self._write() as (db,_now):
                db.execute("UPDATE reply_deliveries SET state='suppressed',reason=? WHERE parent=? AND state='pending'",(reason,operation))

    def events(self, operation, after=0, limit=32):
        if type(after) is not int or not 0 <= after < 2**53 or type(limit) is not int or not 1 <= limit <= 128:
            raise DeliveryError('invalid-event-cursor')
        with self._lock:
            self._ready()
            identifier(operation)
            rows = self._db.execute('SELECT cursor,operation,state,epoch FROM events WHERE operation=? AND cursor>? ORDER BY cursor LIMIT ?',
                                    (operation, after, limit)).fetchall()
            return tuple(DeliveryEvent(*row) for row in rows)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._epoch = None

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()
