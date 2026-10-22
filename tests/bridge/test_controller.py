"""Real supervisor, owner decisions and broker admission/dispatch assembly."""
from pathlib import Path
import tempfile
import unittest
import uuid
from dataclasses import replace
import hashlib

from optional.bridge.win.controller import Controller
from optional.bridge.core.messages import MessageIntent,SenderAuthority
from optional.bridge.win.leader import ServiceLeader
from tests.bridge.test_pairing import chat
from tests.bridge.test_dispatch import TestAdapter


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-controller-')
        self.root=Path(self.temp.name)
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(self.root,self.leader)
        self.a,self.b=chat('a'),chat('b',adapter='test-recipient')
        self.live={'a':self.a,'b':self.b}
        for peer in self.live.values():
            self.controller.enroll(peer,lambda binding:self.live.get(binding.identity.session_id)==binding,
                {'title':peer.identity.session_id,'workspace':'owned scratch'})
        self.adapter=TestAdapter('test-recipient')
        self.controller.adapter(self.adapter)
        self.server=self.controller.frontend(lambda metadata:SenderAuthority(self.a,'native-test-root'))

    def tearDown(self):
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()

    def test_real_controller_requires_owner_connection_then_dispatches_durable_message(self):
        from optional.bridge.core.messages import DeliveryError
        import hashlib
        intent=MessageIntent('op',self.b,hashlib.sha256(b'text').hexdigest())
        proof=self.server.calls.issue(intent,{})
        with self.assertRaises(ValueError): self.controller.core.admit('op',self.b,'text',proof)
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        proof=self.server.calls.issue(intent,{})
        self.assertEqual(self.controller.core.admit('op',self.b,'text',proof).state,'queued')
        receipts=self.controller.dispatch_pending()
        self.assertEqual(receipts[0].state,'reply-received')
        self.assertEqual(len(self.adapter.calls),1)
        # The authenticated native reply now has an automatic reverse job.
        # This fixture installs no adapter for a, so readiness remains queued.
        reply=self.controller.outbox.reply_delivery('op')
        pending=self.controller.dispatch_pending()
        self.assertEqual([(item.operation_id,item.state) for item in pending],[(reply['child'],'queued')])
        self.assertEqual(len(self.adapter.calls),1)

    def test_native_binding_drift_revokes_connection_and_unavailable_leadership_blocks_writes(self):
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self.live.pop('b')
        with self.assertRaises(ValueError): self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self.leader.close()
        self.assertFalse(self.leader.owns())
        with self.assertRaises(ValueError): self.controller.dispatch_pending()

    def test_state_is_persisted_and_new_owner_can_recover_without_replaying_completed_work(self):
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self.controller.close()
        self.controller=Controller(self.root,self.leader)
        self.assertEqual(self.controller.outbox.receipt('missing'),None)
        self.assertEqual(self.controller.dispatch_pending(),())

    def test_writer_lease_is_never_persisted_and_a_restart_drops_it(self):
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self.controller.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.assertEqual(self.controller.leases.writer(self.b),self.a)
        self.assertEqual(self.controller.owner.writers(),((self.a.reference,self.b.reference),))
        leases=self.controller.leases
        self.controller.close()
        self.assertEqual(leases.current(),())
        self.controller=Controller(self.root,self.leader)
        for peer in self.live.values():
            self.controller.enroll(peer,lambda item:self.live.get(item.identity.session_id)==item)
        # The owner's route survives the restart; the lease does not.
        self.assertEqual(set(self.controller.owner.connections()),
            {(self.a.reference,self.b.reference),(self.b.reference,self.a.reference)})
        self.assertIsNone(self.controller.leases.writer(self.b))
        self.assertEqual(self.controller.owner.writers(),())

    def test_no_agent_tool_can_mint_or_renew_a_writer_lease(self):
        server=self.server
        self.assertIsNotNone(server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}}))
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        names=[tool['name'] for tool in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['result']['tools']]
        self.assertTrue(names)
        self.assertFalse([name for name in names if 'lease' in name or 'writer' in name or 'owner' in name])
        for name in ('bridge_writer_lease','bridge_lease','bridge_owner_lease'):
            reply=server.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':name,'arguments':{
                'writer':self.a.reference,'recipient':self.b.reference}}})
            self.assertIn('error',reply)
        # The agent-facing server holds the broker and its caller proofs, never the owner or leases.
        self.assertEqual(set(vars(server))&{'owner','leases','decisions'},set())
        self.assertEqual(set(vars(server.core))&{'owner','leases','decisions'},set())
        self.assertEqual(self.controller.leases.current(),())

    def _admit(self,operation,recipient):
        intent=MessageIntent(operation,recipient,hashlib.sha256(b'text').hexdigest())
        proof=self.server.calls.issue(intent,{})
        return self.controller.core.admit(operation,recipient,'text',proof)

    def test_reopened_chats_refuse_old_queued_binding_then_dispatch_fresh_message(self):
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self._admit('old-process',self.b)
        original=self.controller.outbox.message('old-process')
        self.a=replace(self.a,identity=replace(self.a.identity,incarnation_id='reopened-a'))
        self.b=replace(self.b,identity=replace(self.b.identity,incarnation_id='reopened-b'))
        self.live={'a':self.a,'b':self.b}
        for peer in self.live.values():
            self.controller.enroll(peer,lambda item:self.live.get(item.identity.session_id)==item)
        # The existing owner choice restores; there is no new owner change.
        self._admit('fresh-process',self.b)
        receipts=self.controller.dispatch_pending()
        self.assertEqual([item.state for item in receipts],['refused','reply-received'])
        self.assertEqual([item.operation_id for item in self.adapter.calls],['fresh-process'])
        self.assertEqual(self.controller.outbox.message('old-process'),original)

    def test_unavailable_adapter_does_not_abort_dispatch_of_other_ready_chats(self):
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self._admit('unavailable-adapter',self.b)
        self.controller.core.adapters.pop('test-recipient')
        other=chat('c',adapter='ready-recipient')
        self.live['c']=other
        self.controller.enroll(other,lambda item:self.live.get(item.identity.session_id)==item)
        adapter=TestAdapter('ready-recipient')
        self.controller.adapter(adapter)
        self.controller.owner.change(self.a.reference,other.reference,enabled=True)
        self._admit('ready-adapter',other)
        receipts=self.controller.dispatch_pending()
        self.assertEqual([item.state for item in receipts],['queued','reply-received'])
        self.assertEqual([item.operation_id for item in adapter.calls],['ready-adapter'])

    def test_ownership_loss_during_adapter_check_still_stops_the_dispatch_batch(self):
        from optional.bridge.core.messages import DeliveryError
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self._admit('ownership-loss',self.b)
        def lost_owner():
            self.leader.close()
            raise DeliveryError('native-adapter-unavailable')
        self.adapter.describe=lost_owner
        with self.assertRaisesRegex(DeliveryError,'service-ownership-unverified'):
            self.controller.dispatch_pending()
        self.assertEqual(self.adapter.calls,[])
        self.assertEqual(self.controller.outbox.receipt('ownership-loss').state,'queued')

    def test_more_than_one_batch_of_unavailable_work_does_not_starve_ready_chat(self):
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        for index in range(32): self._admit('blocked-'+str(index),self.b)
        self.controller.core.adapters.pop('test-recipient')
        other=chat('c',adapter='ready-recipient')
        self.live['c']=other
        self.controller.enroll(other,lambda item:self.live.get(item.identity.session_id)==item)
        adapter=TestAdapter('ready-recipient')
        self.controller.adapter(adapter)
        self.controller.owner.change(self.a.reference,other.reference,enabled=True)
        self._admit('ready-after-batch',other)
        self.controller.dispatch_pending()
        self.controller.dispatch_pending()
        self.assertEqual([item.operation_id for item in adapter.calls],['ready-after-batch'])
        self.assertEqual(self.controller.outbox.receipt('blocked-0').state,'queued')

    def test_stop_between_operations_prevents_remaining_batch_native_calls(self):
        import threading
        stopped=threading.Event()
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self._admit('first-before-stop',self.b)
        self._admit('after-stop',self.b)
        original=self.adapter.send
        def send(message):
            result=original(message)
            stopped.set()
            return result
        self.adapter.send=send
        receipts=self.controller.dispatch_pending(continue_check=lambda:not stopped.is_set())
        self.assertEqual(len(receipts),1)
        self.assertEqual([item.operation_id for item in self.adapter.calls],['first-before-stop'])
        self.assertEqual(self.controller.outbox.receipt('after-stop').state,'queued')
