"""Durable reverse reply jobs exercise storage, route fencing and the real worker."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from optional.bridge.core.messages import BeforeWriteError,DeliveryError,NativeResult,SenderAuthority
from optional.bridge.core.outbox import Outbox
from optional.bridge.core.prompt import delivery_prompt
from tests.bridge.test_dispatch import DispatchTests,TestAdapter

LEGACY_SCHEMA = '\nCREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,last_clock REAL NOT NULL);\nINSERT INTO meta VALUES(1,0,0);\nCREATE TABLE refusals(root TEXT PRIMARY KEY,reason TEXT NOT NULL);\nCREATE TABLE messages(\n id TEXT PRIMARY KEY,root TEXT NOT NULL,source TEXT NOT NULL,target TEXT NOT NULL,\n source_json TEXT NOT NULL,target_json TEXT NOT NULL,body TEXT NOT NULL,body_sha TEXT NOT NULL,\n expires REAL,state TEXT NOT NULL,last_confirmed TEXT NOT NULL,dispatch_epoch INTEGER,\n native_id TEXT,reply TEXT,reply_sha TEXT,reason TEXT\n);\nCREATE TABLE events(\n cursor INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT NOT NULL,state TEXT NOT NULL,epoch INTEGER NOT NULL\n);\nCREATE INDEX events_operation ON events(operation,cursor);\n'


class ReplyDeliveryTests(DispatchTests):
    def setUp(self):
        super().setUp()
        self.reverse=TestAdapter(self.a.identity.adapter_id)
        def send(message):
            self.reverse.calls.append(message)
            if self.reverse.fail is not None:raise self.reverse.fail
            return NativeResult(message.operation_id,message.recipient.reference,'recipient-accepted','return-native')
        self.reverse.send=send
        self.core.adapters[self.a.identity.adapter_id]=self.reverse
        self.proofs[b'recipient']=SenderAuthority(self.b,'recipient-own-task')

    def accepted(self,operation='op',**kw):
        self.admit(operation,**kw)
        self.adapter.response=NativeResult(operation,self.b.reference,'recipient-accepted','parent-native')
        self.core.dispatch_once(operation)

    def child(self,operation='op'):
        link=self.store.reply_delivery(operation)
        return link['child']

    def reopen(self):
        self.store.close()
        self.store=self.open_store()
        self.store.claim()
        self.core=self.open_core()
        self.core.adapters[self.a.identity.adapter_id]=self.reverse

    def test_explicit_reply_atomically_queues_exact_reverse_message_without_native_call(self):
        self.accepted()
        original=self.store.message('op')
        self.core.reply_to('op','exact reply \u03bb',b'recipient')
        self.assertEqual(len(self.store.pending()),1)  # Missing before the change.
        child=self.store.message(self.child())
        self.assertEqual((child.sender,child.recipient,child.task_root),(self.b,self.a,self.root))
        self.assertEqual((child.text,child.reply_of),('exact reply \u03bb','op'))
        self.assertEqual(child.intent.body_sha256,hashlib.sha256(child.text.encode()).hexdigest())
        self.assertEqual(self.store.message('op'),original)
        self.assertEqual(self.store.receipt('op').native_id,'parent-native')
        self.assertEqual(self.reverse.calls,[])
        self.core.dispatch_once(child.operation_id)
        self.assertEqual(self.reverse.calls,[child])
        self.assertEqual(self.store.receipt('op').state,'reply-received')
        self.assertEqual(self.store.receipt(child.operation_id).state,'recipient-accepted')

    def test_concurrent_identical_replies_and_lost_mcp_ack_have_one_child(self):
        self.accepted()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.core.reply_to('op','reply',b'recipient'),range(8)))
        self.assertTrue(all(result.state=='reply-received' for result in results))
        self.assertEqual(len(self.store.pending()),1)
        child=self.child()
        self.core.dispatch_once(child)
        self.core.reply_to('op','reply',b'recipient')
        self.core.dispatch_once(child)
        self.assertEqual(len(self.reverse.calls),1)
        with self.assertRaisesRegex(DeliveryError,'terminal-receipt-conflict'):
            self.core.reply_to('op','changed',b'recipient')
        self.assertEqual(self.child(),child)

    def test_native_send_reply_and_poll_reply_each_queue_once(self):
        self.admit()
        self.core.dispatch_once('op')  # TestAdapter returns an authenticated reply.
        self.assertEqual(len(self.store.pending()),1)
        self.assertEqual(self.store.message(self.child()).text,'test reply')
        self.admit('late')
        self.adapter.fail=TimeoutError('owned uncertain effect')
        self.core.dispatch_once('late')
        self.adapter.reconcile=lambda message,native:NativeResult('late',self.b.reference,
            'reply-received','observed-late','late body')
        self.assertEqual(self.core.poll_native('late',b'test-caller-proof').state,'reply-received')
        self.assertEqual(self.store.message(self.child('late')).text,'late body')
        self.core.poll_native('late',b'test-caller-proof')
        self.assertEqual(len(self.store.pending()),2)

    def test_native_reply_without_reverse_route_is_permanently_suppressed(self):
        self.select(False)
        self.pairings.owner_select(self.pairings.propose('a','b',bidirectional=False),b'test-owner-proof')
        self.admit()
        self.core.dispatch_once('op')
        self.assertEqual(self.store.reply('op'),'test reply')
        self.assertEqual(self.store.pending(),())
        self.assertEqual(self.store.reply_delivery('op')['state'],'suppressed')
        self.select(True)
        self.core.promote_replies()
        self.assertEqual(self.store.pending(),())

    def test_restart_queued_child_dispatches_but_unknown_child_never_resends(self):
        self.accepted()
        self.core.reply_to('op','reply',b'recipient')
        child=self.child()
        self.reopen()
        self.reverse.fail=TimeoutError('owned lost ack')
        self.assertEqual(self.core.dispatch_once(child).state,'unknown')
        self.reopen()
        self.core.reply_to('op','reply',b'recipient')
        self.assertEqual(self.core.dispatch_once(child).state,'unknown')
        self.assertEqual(len(self.reverse.calls),1)
        self.assertEqual(self.store.reply('op'),'reply')

    def test_child_crash_after_owned_effect_is_unknown_on_restart(self):
        self.accepted()
        self.core.reply_to('op','reply',b'recipient')
        child=self.child()
        self.store.close()
        marker=Path(self.temp.name)/'return-effect.txt'
        code="""import os,pathlib,sys
sys.path.insert(0,sys.argv[1])
from optional.bridge.core.outbox import Outbox
box=Outbox(pathlib.Path(sys.argv[2]),leadership_check=lambda:True,clock=lambda:1000)
box.claim()
def send(message):
    pathlib.Path(sys.argv[4]).write_text('one return effect',encoding='utf-8')
    os._exit(23)
box.run_dispatch(sys.argv[3],send)
"""
        from kitlib.proc.job import Job
        with Job(kill_on_close=True) as job:
            process=job.start([sys.executable,'-I','-S','-c',code,str(Path(__file__).resolve().parents[2]),
                str(self.path),child,str(marker)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            output,error=process.communicate(timeout=10)
        self.assertEqual(process.returncode,23,error)
        process.stdout.close();process.stderr.close()
        self.assertEqual(marker.read_text(encoding='utf-8'),'one return effect')
        self.store=self.open_store();self.store.claim();self.core=self.open_core()
        self.core.adapters[self.a.identity.adapter_id]=self.reverse
        self.assertEqual(self.core.dispatch_once(child).state,'unknown')
        self.assertEqual(self.reverse.calls,[])
        self.assertEqual(self.store.reply('op'),'reply')

    def test_revocation_and_reopened_bindings_deny_child_before_bytes(self):
        for mutation in ('route','binding'):
            with self.subTest(mutation=mutation):
                op='op-'+mutation
                self.accepted(op)
                self.core.reply_to(op,'reply',b'recipient')
                child=self.child(op)
                if mutation=='route':self.select(False)
                else:
                    changed=replace(self.a,identity=replace(self.a.identity,incarnation_id='reopened'))
                    self.live['a']=changed;self.pairings.observe(changed)
                self.assertEqual(self.core.dispatch_once(child).state,'refused')
                self.assertEqual(self.reverse.calls,[])
                self.assertEqual(self.store.reply(op),'reply')
                if mutation=='route':self.select(True)

    def test_refusal_root_cancels_reverse_job_and_cannot_be_reset_by_reply_id(self):
        self.accepted()
        self.core.reply_to('op','reply',b'recipient')
        child=self.child()
        self.accepted('sibling')
        self.store.append_native('sibling',NativeResult('sibling',self.b.reference,'refused','parent-native'))
        self.assertEqual(self.store.receipt(child).state,'refused')
        self.assertEqual(self.core.dispatch_once(child).state,'refused')
        self.assertEqual(self.reverse.calls,[])

    def test_child_reply_is_denied_and_native_child_reply_never_creates_grandchild(self):
        self.accepted()
        self.core.reply_to('op','reply',b'recipient')
        child=self.child()
        self.core.dispatch_once(child)
        self.proofs[b'original']=SenderAuthority(self.a,self.root)
        with self.assertRaisesRegex(DeliveryError,'reply-to-reply-delivery'):
            self.core.reply_to(child,'ack',b'original')
        self.store.append_native(child,NativeResult(child,self.a.reference,'reply-received','return-native','native ack'),
            reverse_reply=True)
        self.assertEqual(self.store.pending(),())
        self.assertIsNone(self.store.reply_delivery(child))
        rendered=delivery_prompt(self.store.message(child))
        self.assertIn('Reply to bridge operation: op',rendered)
        self.assertNotIn('Use bridge_reply',rendered)
        self.assertTrue(rendered.endswith('reply'))

    def test_reserved_child_namespace_denies_ordinary_send(self):
        operation='bridge-reply-'+('a'*64)
        with self.assertRaisesRegex(DeliveryError,'reserved-reply-operation'):
            self.admit(operation)
        self.assertIsNone(self.store.receipt(operation))

    def test_fixed_expiry_is_not_refreshed_and_expired_reply_keeps_parent_body(self):
        self.accepted(expires_at=1002)
        self.core.reply_to('op','reply',b'recipient')
        child=self.child()
        self.assertEqual(self.store.message(child).expires_at,1002)
        self.now=1003
        self.reopen()
        self.assertEqual(self.core.dispatch_once(child).state,'expired')
        self.assertEqual(self.store.reply('op'),'reply')
        self.assertEqual(self.reverse.calls,[])

    def test_full_ordinary_capacity_still_allows_its_bounded_return_job(self):
        self.accepted()
        with patch('optional.bridge.core.outbox.MAX_MESSAGES',1):
            self.core.reply_to('op','reply',b'recipient')
            self.assertEqual(len(self.store.pending()),1)
            self.assertEqual(self.store.reply_delivery('op')['state'],'queued')
            with self.assertRaisesRegex(DeliveryError,'message-capacity'):self.admit('new')

    def test_child_savepoint_capacity_failure_preserves_native_reply_and_pending_intent(self):
        self.admit()
        original_event=self.store._event
        def child_full(db,operation,state,epoch=None):
            if operation.startswith('bridge-reply-'):raise DeliveryError('event-capacity')
            return original_event(db,operation,state,epoch)
        with patch.object(self.store,'_event',side_effect=child_full):
            self.assertEqual(self.core.dispatch_once('op').state,'reply-received')
        self.assertEqual(self.store.reply('op'),'test reply')
        self.assertEqual(self.store.reply_delivery('op')['state'],'pending')
        self.assertIsNone(self.store.receipt(self.child()))
        self.reopen()
        self.core.promote_replies()
        self.assertEqual(self.store.reply_delivery('op')['state'],'queued')
        self.core.dispatch_once(self.child())
        self.assertEqual(len(self.reverse.calls),1)

    def test_pending_reply_route_change_suppresses_it_permanently(self):
        self.admit()
        event=self.store._event
        def unavailable(db,operation,state,epoch=None):
            if operation.startswith('bridge-reply-'):raise DeliveryError('event-capacity')
            return event(db,operation,state,epoch)
        with patch.object(self.store,'_event',side_effect=unavailable):self.core.dispatch_once('op')
        self.select(False)
        self.core.promote_replies()
        self.select(True)
        self.core.promote_replies()
        self.assertEqual(self.store.reply_delivery('op')['state'],'suppressed')
        self.assertEqual(self.store.pending(),())

    def test_before_write_and_malformed_child_receipt_keep_parent_reply(self):
        self.accepted()
        self.core.reply_to('op','reply',b'recipient')
        child=self.child()
        self.reverse.fail=BeforeWriteError('owned no bytes')
        self.assertEqual(self.core.dispatch_once(child).state,'failed')
        self.core.reply_to('op','reply',b'recipient')
        self.assertEqual(self.store.reply('op'),'reply')
        self.assertEqual(len(self.reverse.calls),1)

    def test_explicit_capacity_or_stale_fence_failure_is_atomic(self):
        self.accepted()
        count=len(self.store.events('op'))
        with patch('optional.bridge.core.outbox.MAX_EVENTS',count):
            with self.assertRaisesRegex(DeliveryError,'event-capacity'):
                self.core.reply_to('op','reply',b'recipient')
        self.assertEqual(self.store.receipt('op').state,'recipient-accepted')
        self.assertIsNone(self.store.reply_delivery('op'))
        other=self.open_store()
        try:
            other.claim()
            with self.assertRaisesRegex(DeliveryError,'stale-broker-fence'):
                self.core.reply_to('op','reply',b'recipient')
        finally:other.close()
        self.assertIsNone(self.store.reply_delivery('op'))
        self.assertEqual(self.store.pending(),())

    def test_pending_old_binding_and_fixed_deadline_are_never_rebound_or_refreshed(self):
        self.admit()
        event=self.store._event
        def unavailable(db,operation,state,epoch=None):
            if operation.startswith('bridge-reply-'):raise DeliveryError('event-capacity')
            return event(db,operation,state,epoch)
        with patch.object(self.store,'_event',side_effect=unavailable):self.core.dispatch_once('op')
        expires=self.store.reply_delivery('op')['expires']
        changed=replace(self.a,identity=replace(self.a.identity,incarnation_id='reopened'))
        self.live['a']=changed;self.pairings.observe(changed)
        self.core.promote_replies()
        self.assertEqual(self.store.reply_delivery('op')['state'],'suppressed')
        self.assertEqual(self.store.reply_delivery('op')['expires'],expires)
        self.assertEqual(self.store.pending(),())

    def test_malformed_child_receipt_cannot_change_known_parent_or_repeat_send(self):
        self.accepted();self.core.reply_to('op','reply',b'recipient')
        child=self.child()
        def malformed(message):
            self.reverse.calls.append(message)
            return NativeResult('wrong-operation',message.recipient.reference,'recipient-accepted','return-native')
        self.reverse.send=malformed
        self.assertEqual(self.core.dispatch_once(child).state,'unknown')
        self.reopen()
        self.core.reply_to('op','reply',b'recipient')
        self.assertEqual(self.core.dispatch_once(child).state,'unknown')
        self.assertEqual(len(self.reverse.calls),1)
        self.assertEqual(self.store.reply('op'),'reply')
        self.assertEqual(self.store.receipt('op').native_id,'parent-native')

    def test_legacy_reserved_id_collision_denies_explicit_but_keeps_known_native_reply(self):
        from optional.bridge.core.messages import reply_operation_id
        self.accepted()
        columns=[row[1] for row in self.store._db.execute('PRAGMA table_info(messages)')][:16]
        row=list(self.store._db.execute('SELECT '+','.join(columns)+" FROM messages WHERE id='op'").fetchone())
        collision=row.copy();collision[0]=reply_operation_id('op');collision[6]='legacy collision body'
        collision[7]=hashlib.sha256(collision[6].encode()).hexdigest()
        collision[9]=collision[10]='queued';collision[11]=collision[12]=None
        legacy=Path(self.temp.name)/'collision-v1.sqlite'
        with closing(sqlite3.connect(legacy)) as db,db:
            db.executescript(LEGACY_SCHEMA)
            db.executemany('INSERT INTO messages VALUES('+','.join('?' for _ in range(16))+')',[row,collision])
            db.execute('PRAGMA application_id=1112496177');db.execute('PRAGMA user_version=1')
        self.store.close();self.path=legacy;self.store=self.open_store();self.store.claim()
        self.core=self.open_core();self.core.adapters[self.a.identity.adapter_id]=self.reverse
        with self.assertRaisesRegex(DeliveryError,'reply-operation-conflict'):
            self.core.reply_to('op','reply',b'recipient')
        self.assertIsNone(self.store.reply('op'))
        self.assertIsNone(self.store.reply_delivery('op'))
        self.adapter.reconcile=lambda message,native:NativeResult('op',self.b.reference,
            'reply-received','parent-native','known native reply')
        self.core.poll_native('op',b'test-caller-proof')
        self.assertEqual(self.store.reply('op'),'known native reply')
        self.assertEqual(self.store.reply_delivery('op')['state'],'suppressed')
        self.assertEqual(self.store.message(collision[0]).text,'legacy collision body')
        self.assertEqual(self.reverse.calls,[])

    def test_inline_authenticated_reply_survives_late_acknowledgement_or_error(self):
        for outcome in ('accepted','before-write','uncertain'):
            with self.subTest(outcome=outcome):
                operation='inline-'+outcome
                self.admit(operation)
                def reply_then_ack(message):
                    self.store.recipient_reply(message.operation_id,'inline known reply',reverse_reply=True)
                    if outcome=='before-write':raise BeforeWriteError('late no bytes claim')
                    if outcome=='uncertain':raise TimeoutError('late lost acknowledgement')
                    return NativeResult(message.operation_id,message.recipient.reference,'recipient-accepted','late-native')
                self.adapter.send=reply_then_ack
                self.assertEqual(self.core.dispatch_once(operation).state,'reply-received')
                self.assertEqual(self.store.reply(operation),'inline known reply')
                self.assertEqual(self.store.reply_delivery(operation)['state'],'queued')
                self.assertIsNone(self.store.receipt(operation).native_id)

    def test_durable_outcome_event_slot_cannot_be_taken_by_reentrant_admission(self):
        from optional.bridge.core.messages import OutboundMessage
        self.admit()
        denied=[]
        def native(message):
            try:self.store.admit(OutboundMessage('last-slot',self.root,self.a,self.b,'new body'))
            except DeliveryError as error:denied.append(error.code)
            return NativeResult('op',self.b.reference,'reply-received','native','known after I/O')
        self.adapter.send=native
        with patch('optional.bridge.core.outbox.MAX_EVENTS',3):
            self.assertEqual(self.core.dispatch_once('op').state,'reply-received')
        self.assertEqual(denied,['event-capacity'])
        self.assertEqual(self.store.reply('op'),'known after I/O')
        self.assertIsNone(self.store.receipt('last-slot'))
        self.assertEqual(self.store.reply_delivery('op')['state'],'pending')
        self.core.promote_replies()
        self.assertEqual(self.store.reply_delivery('op')['state'],'queued')

    def _poll_reentrant_sibling(self, state, outcome):
        from optional.bridge.core.messages import OutboundMessage
        self.root='poll-root-'+state+'-'+outcome
        operation='poll-'+state+'-'+outcome
        sibling=operation+'-sibling'
        self.admit(operation)
        if state=='unknown':self.adapter.fail=TimeoutError('owned uncertain dispatch')
        else:
            self.adapter.fail=None
            self.adapter.response=NativeResult(operation,self.b.reference,state,'parent-native')
        self.core.dispatch_once(operation)
        denied=[]
        reads=[]
        def reconcile(message,native):
            reads.append(message.operation_id)
            try:self.store.admit(OutboundMessage(sibling,self.root,self.a,self.b,'new same-root body'))
            except DeliveryError as error:denied.append(error.code)
            return NativeResult(operation,self.b.reference,outcome,'parent-native',
                'known polled reply' if outcome=='reply-received' else None)
        self.adapter.reconcile=reconcile
        count=self.store._db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        with patch('optional.bridge.core.outbox.MAX_EVENTS',count+2):
            receipt=self.core.poll_native(operation,b'test-caller-proof')
        self.assertEqual(receipt.state,outcome)
        self.assertEqual(reads,[operation])
        self.assertEqual(denied,['event-capacity'])
        self.assertIsNone(self.store.receipt(sibling))
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM event_reservations').fetchone()[0],0)
        if outcome=='reply-received':
            self.assertEqual(self.store.reply(operation),'known polled reply')
            self.assertEqual(self.store.reply_delivery(operation)['state'],'queued')
        else:
            with self.assertRaisesRegex(DeliveryError,'task-root-refused'):
                self.admit(operation+'-after-refusal')

    def test_poll_reservation_preserves_known_reply_against_near_capacity_sibling_admission(self):
        for state in ('recipient-accepted','held','unknown'):
            with self.subTest(state=state):self._poll_reentrant_sibling(state,'reply-received')

    def test_poll_reservation_preserves_root_refusal_against_near_capacity_sibling_admission(self):
        for state in ('recipient-accepted','held','unknown'):
            with self.subTest(state=state):self._poll_reentrant_sibling(state,'refused')

    def test_refusal_suppresses_pending_return_intent_without_later_root_revival(self):
        self.admit()
        event=self.store._event
        def unavailable(db,operation,state,epoch=None):
            if operation.startswith('bridge-reply-'):raise DeliveryError('event-capacity')
            return event(db,operation,state,epoch)
        with patch.object(self.store,'_event',side_effect=unavailable):self.core.dispatch_once('op')
        self.assertEqual(self.store.reply_delivery('op')['state'],'pending')
        self.accepted('sibling')
        self.store.append_native('sibling',NativeResult('sibling',self.b.reference,'refused','parent-native'))
        self.assertEqual(self.store.reply_delivery('op')['state'],'suppressed')
        self.reopen();self.core.promote_replies()
        self.assertEqual(self.store.pending(),())
        self.assertEqual(self.store.reply('op'),'test reply')

    def test_process_crash_after_known_reply_and_pending_intent_recovers_without_parent_replay(self):
        self.admit()
        self.store.close()
        marker=Path(self.temp.name)/'forward-effect.txt'
        code="""import os,pathlib,sys
sys.path.insert(0,sys.argv[1])
from optional.bridge.core.outbox import Outbox
from optional.bridge.core.messages import DeliveryError,NativeResult
box=Outbox(pathlib.Path(sys.argv[2]),leadership_check=lambda:True,clock=lambda:1000)
box.claim()
event=box._event
def unavailable(db,operation,state,epoch=None):
    if operation.startswith('bridge-reply-'):raise DeliveryError('event-capacity')
    return event(db,operation,state,epoch)
box._event=unavailable
def send(message):
    pathlib.Path(sys.argv[3]).write_text('one forward effect',encoding='utf-8')
    return NativeResult(message.operation_id,message.recipient.reference,'reply-received','parent-native','durable native reply')
box.run_dispatch('op',send,reverse_reply=True)
os._exit(31)
"""
        from kitlib.proc.job import Job
        with Job(kill_on_close=True) as job:
            process=job.start([sys.executable,'-I','-S','-c',code,str(Path(__file__).resolve().parents[2]),
                str(self.path),str(marker)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            output,error=process.communicate(timeout=10)
        self.assertEqual(process.returncode,31,error)
        process.stdout.close();process.stderr.close()
        self.assertEqual(marker.read_text(encoding='utf-8'),'one forward effect')
        self.store=self.open_store();self.store.claim();self.core=self.open_core()
        self.core.adapters[self.a.identity.adapter_id]=self.reverse
        self.assertEqual(self.store.reply('op'),'durable native reply')
        self.assertEqual(self.store.reply_delivery('op')['state'],'pending')
        self.assertEqual(self.core.dispatch_once('op').state,'reply-received')
        self.assertEqual(self.adapter.calls,[])
        self.core.promote_replies()
        self.core.dispatch_once(self.child())
        self.assertEqual(len(self.reverse.calls),1)
        self.assertEqual(self.store.message(self.child()).text,'durable native reply')

    def test_v2_migration_preserves_known_unknown_links_and_refusals_without_parent_replay(self):
        self.accepted();self.core.reply_to('op','v2 known reply',b'recipient')
        child=self.child()
        self.admit('v2-unknown');self.adapter.fail=TimeoutError('owned lost acknowledgement')
        self.core.dispatch_once('v2-unknown')
        self.root='v2-refused-root';self.adapter.fail=None
        self.accepted('v2-refused')
        self.store.append_native('v2-refused',NativeResult('v2-refused',self.b.reference,'refused','parent-native'))
        before={name:self.store.message(name) for name in ('op',child,'v2-unknown','v2-refused')}
        link=self.store.reply_delivery('op')
        rows=list(self.store._db.execute('SELECT * FROM messages'))
        events=list(self.store._db.execute('SELECT * FROM events'))
        refusals=list(self.store._db.execute('SELECT * FROM refusals'))
        links=list(self.store._db.execute('SELECT * FROM reply_deliveries'))
        meta=self.store._db.execute('SELECT epoch,last_clock FROM meta WHERE id=1').fetchone()
        legacy=Path(self.temp.name)/'v2.sqlite'
        with closing(sqlite3.connect(legacy)) as db,db:
            db.executescript(LEGACY_SCHEMA)
            db.execute('ALTER TABLE messages ADD COLUMN reply_of TEXT')
            db.execute('CREATE TABLE reply_deliveries(parent TEXT PRIMARY KEY,child TEXT UNIQUE NOT NULL,'+
                'state TEXT NOT NULL,expires REAL NOT NULL,reason TEXT)')
            db.executemany('INSERT INTO messages VALUES('+','.join('?' for _ in range(17))+')',rows)
            db.executemany('INSERT INTO events VALUES(?,?,?,?)',events)
            db.executemany('INSERT INTO refusals VALUES(?,?)',refusals)
            db.executemany('INSERT INTO reply_deliveries VALUES(?,?,?,?,?)',links)
            db.execute('UPDATE meta SET epoch=?,last_clock=? WHERE id=1',tuple(meta))
            db.execute('PRAGMA application_id=1112496177');db.execute('PRAGMA user_version=2')
        self.store.close();self.path=legacy;self.store=self.open_store()
        self.assertEqual(self.store._db.execute('PRAGMA user_version').fetchone()[0],2)
        self.store.claim();self.core=self.open_core()
        self.core.adapters[self.a.identity.adapter_id]=self.reverse
        self.assertEqual(self.store._db.execute('PRAGMA user_version').fetchone()[0],3)
        self.assertEqual({name:self.store.message(name) for name in before},before)
        self.assertEqual(self.store.reply('op'),'v2 known reply')
        self.assertEqual(self.store.receipt('op').native_id,'parent-native')
        self.assertEqual(self.store.receipt('v2-unknown').state,'unknown')
        self.assertEqual(self.store.receipt('v2-refused').state,'refused')
        self.assertEqual(self.store.reply_delivery('op'),link)
        self.assertEqual(self.store.pending(),(child,))
        self.assertEqual(self.store.pending_replies(),())
        count=len(self.adapter.calls)
        self.core.dispatch_once('op');self.core.dispatch_once('v2-unknown')
        self.assertEqual(len(self.adapter.calls),count)
        with self.assertRaisesRegex(DeliveryError,'task-root-refused'):self.admit('v2-refusal-new-id')
        self.core.dispatch_once(child)
        self.assertEqual(len(self.reverse.calls),1)

    def test_recoverable_child_sql_failure_preserves_known_native_reply_and_pending_reason(self):
        self.admit()
        self.store._db.execute("CREATE TRIGGER owned_child_failure BEFORE INSERT ON messages "
            "WHEN NEW.reply_of IS NOT NULL BEGIN SELECT RAISE(FAIL,'owned child allocation failure'); END")
        self.assertEqual(self.core.dispatch_once('op').state,'reply-received')
        self.assertEqual(self.store.reply('op'),'test reply')
        link=self.store.reply_delivery('op')
        self.assertEqual((link['state'],link['reason']),('pending','reply-storage-error'))
        self.assertIsNone(self.store.receipt(link['child']))
        self.assertFalse(self.store._db.in_transaction)
        self.store._db.execute('DROP TRIGGER owned_child_failure')
        self.core.promote_replies()
        self.core.dispatch_once(link['child'])
        self.assertEqual(len(self.reverse.calls),1)
        self.assertEqual(self.store.reply('op'),'test reply')

    def test_v1_migration_requires_claim_preserves_unknown_and_suppresses_old_replies(self):
        self.accepted()
        self.store.recipient_reply('op','old reply')
        self.admit('uncertain');self.adapter.fail=TimeoutError('owned lost ack');self.core.dispatch_once('uncertain')
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            columns=[row[1] for row in db.execute('PRAGMA table_info(messages)')][:16]
            rows=list(db.execute('SELECT '+','.join(columns)+' FROM messages WHERE '+
                "id NOT LIKE 'bridge-reply-%'"))
            events=list(db.execute('SELECT * FROM events'))
            refusals=list(db.execute('SELECT * FROM refusals'))
        legacy=Path(self.temp.name)/'v1.sqlite'
        with closing(sqlite3.connect(legacy)) as db,db:
            db.executescript(LEGACY_SCHEMA)
            db.executemany('INSERT INTO messages VALUES('+','.join('?' for _ in range(16))+')',rows)
            db.executemany('INSERT INTO events VALUES(?,?,?,?)',events)
            db.executemany('INSERT INTO refusals VALUES(?,?)',refusals)
            db.execute('PRAGMA application_id=1112496177');db.execute('PRAGMA user_version=1')
        denied=Outbox(legacy,leadership_check=lambda:False,clock=lambda:1000)
        with self.assertRaisesRegex(DeliveryError,'broker-ownership-unverified'):denied.claim()
        denied.close()
        with closing(sqlite3.connect(legacy)) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
        box=Outbox(legacy,leadership_check=lambda:True,clock=lambda:1000)
        try:
            box.claim()
            self.assertEqual(box.receipt('uncertain').state,'unknown')
            self.assertEqual(box.reply('op'),'old reply')
            self.assertEqual(box.reply_delivery('op')['state'],'suppressed')
            self.assertEqual(box.pending_replies(),())
            self.assertEqual(box.pending(),())
        finally:box.close()


from tests.bridge.test_controller import ControllerTests


class WorkerReplyTests(ControllerTests):
    def test_authenticated_mcp_reply_reaches_original_sender_via_actual_worker_without_polling(self):
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self.adapter.response=NativeResult('op',self.b.reference,'recipient-accepted','parent-native')
        self._admit('op',self.b)
        self.controller.dispatch_pending()
        reverse=TestAdapter(self.a.identity.adapter_id)
        reverse.send=lambda message:(reverse.calls.append(message) or NativeResult(
            message.operation_id,message.recipient.reference,'recipient-accepted','return-native'))
        self.controller.adapter(reverse)
        server=self.controller.frontend(lambda metadata:SenderAuthority(self.b,'recipient-native-task'))
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        message={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_reply','arguments':{'operation_id':'op','text':'exact MCP reply'},'_meta':{}}}
        response=server.handle(message)
        self.assertFalse(response['result']['isError'])
        self.assertEqual(reverse.calls,[])  # MCP storage does no synchronous native I/O.
        link=self.controller.outbox.reply_delivery('op')
        self.controller.dispatch_pending()
        self.assertEqual(len(reverse.calls),1)
        child=reverse.calls[0]
        self.assertEqual((child.operation_id,child.sender,child.recipient),(link['child'],self.b,self.a))
        self.assertEqual((child.task_root,child.reply_of,child.text),('native-test-root','op','exact MCP reply'))
        self.assertFalse(server.handle(message)['result']['isError'])  # Same reply after lost caller ack.
        self.controller.dispatch_pending()
        self.assertEqual(len(reverse.calls),1)
        self.assertEqual(self.controller.outbox.receipt('op').state,'reply-received')
        self.assertEqual(self.controller.outbox.receipt(child.operation_id).state,'recipient-accepted')

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(kind(name) for kind in (ReplyDeliveryTests,WorkerReplyTests)
        for name in sorted(kind.__dict__) if name.startswith('test_'))
