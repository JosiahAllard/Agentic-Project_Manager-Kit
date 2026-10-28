"""Observed Claude peer framing against real owned pipes, not live model chats."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.bridge import pipes
import uuid

from hosts.claude.adapter import peer as claude_peer

from optional.bridge.hosts.claude.peer import ClaudePeerAdapter, ClaudePeerEndpoint, native_message_id, peer_address
from optional.bridge.hosts.claude.sessions import ClaudeDirectory, NativeSessionError
from optional.bridge.core.contracts import SessionIdentity, WorkspaceIdentity
from optional.bridge.core.dispatch import CoreBroker
from optional.bridge.core.jsonwire import encode_frame
from optional.bridge.core.messages import BeforeWriteError, DeliveryError, OutboundMessage, SenderAuthority
from optional.bridge.core.outbox import Outbox
from optional.bridge.core.pairing import ChatBinding, PairingStore
from kitlib.proc.pipe import PrivateListener, connect
from optional.bridge.win.transport import PipeTarget, write_peer
from kitlib.proc.errors import ProcError
from kitlib.proc.process import own_ctime
from tests.bridge.test_pairing import chat

NATIVE_SOURCE = 'optional/bridge/hosts/claude/peer.py'


class ClaudePeerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-peer-')
        self.root = Path(self.temp.name)
        self.registry = self.root / 'native'
        self.own_registry = self.root / 'bridge'
        self.registry.mkdir()
        self.own_registry.mkdir()
        self.pipe = r'\\.\pipe\LOCAL\cc-msg-test-' + uuid.uuid4().hex
        self.listener = PrivateListener(self.pipe)
        self.record = {'pid':os.getpid(),'procStartFt':str(own_ctime()),
            'sessionId':'native-session','cwd':str(self.root),'peerProtocol':1,
            'entrypoint':'cli','messagingSocketPath':self.pipe}
        self.write_record()
        canonical = self.pipe.lower()
        key = self.registry / ('%d.%s.key' % (os.getpid(),hashlib.sha256(canonical.encode()).hexdigest()))
        key.write_text(json.dumps({'peerToken':'b'*32,'procStartFt':str(own_ctime())}), encoding='utf-8')
        self.directory = ClaudeDirectory(self.registry, sys.executable,
            account_probe=lambda record,root:'a'*64)
        self.snapshot = self.directory.discover()[0]
        self.recipient = ChatBinding(SessionIdentity('recipient','native-session','claude-peer',
            'test-incarnation',WorkspaceIdentity('test-host','test-project','test-workspace')), 'a'*64, 1)
        self.message = OutboundMessage('test-op','test-root',chat('sender'),self.recipient,'hello private peer')
        self.endpoint = ClaudePeerEndpoint(self.own_registry,self.root,self.directory)
        self.adapter = ClaudePeerAdapter(self.directory,self.endpoint,
            resolve=lambda binding:self.snapshot if binding == self.recipient else None,
            sender_mode=lambda message:'prompting')
        self.received, self.errors = [], []

    def tearDown(self):
        self.endpoint.close()
        self.directory.close()
        self.listener.close()
        self.temp.cleanup()

    def write_record(self):
        (self.registry / ('%d.json' % os.getpid())).write_text(json.dumps(self.record),encoding='utf-8')

    def listen(self):
        def receive():
            try:
                self.listener.accept(timeout=5)
                frames, closed = pipes.frames_until_close(self.listener,5)
                self.received.extend(frames)
                self.assertTrue(closed)
            except Exception as error:
                self.errors.append(type(error).__name__)
        thread = threading.Thread(target=receive)
        thread.start()
        return thread

    def dispatch(self):
        thread = self.listen()
        try:
            with self.assertRaisesRegex(DeliveryError,'native-outcome-unconfirmed'):
                self.adapter.send(self.message)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors,[])

    def status(self, state, *, token=None, address=None, operation=None, **fields):
        token = token or json.loads(self.endpoint.key_path.read_text(encoding='utf-8'))['peerToken']
        frames = [{'type':'auth','token':token},{
            'type':'control','action':'peer_message_status','status':state,
            'orig_msg_id':operation or native_message_id(self.message),
            'from':address or peer_address(self.snapshot.pipe),**fields}]
        # Simulate the verified native process, not our separate I/O helper.
        pipe = pipes.connect_waiting(self.endpoint.pipe,os.getpid(),own_ctime())
        try:
            pipes.send(pipe,frames)
        finally:
            pipe.close()
        deadline = time.monotonic()+.3
        expected = 'refused' if fields.get('status_detail') == 'refused' else state
        while time.monotonic() < deadline:
            result = self.adapter.reconcile(self.message,None)
            if result is not None and result.state == expected:
                return result
            time.sleep(.01)
        return None

    def test_a_client_meeting_the_busy_one_instance_pipe_waits_instead_of_failing(self):
        # Under load the fake native client opened the pipe while its one instance still served
        # another client: ERROR_PIPE_BUSY, 'pipe-connect-failed', no retry.
        holder = connect(self.pipe,os.getpid(),own_ctime())
        self.addCleanup(holder.close)
        self.listener.accept(timeout=3)
        with self.assertRaises(ProcError):
            connect(self.pipe,os.getpid(),own_ctime())      # the bare open is refused as busy
        opened = []
        worker = threading.Thread(target=lambda: opened.append(
            pipes.connect_waiting(self.pipe,os.getpid(),own_ctime(),timeout=5)))
        worker.start()
        worker.join(timeout=.5)
        self.assertTrue(worker.is_alive())                  # still waiting, not failed
        holder.close()
        self.listener.reset()
        worker.join(timeout=5)
        self.assertEqual(len(opened),1)
        opened[0].close()

    def test_a_pipe_busy_past_the_deadline_gives_a_clear_failure(self):
        holder = connect(self.pipe,os.getpid(),own_ctime())
        self.addCleanup(holder.close)
        self.listener.accept(timeout=3)
        with self.assertRaisesRegex(AssertionError,'still busy after'):
            pipes.connect_waiting(self.pipe,os.getpid(),own_ctime(),timeout=.3)

    def test_native_auth_and_exact_operation_reach_original_chat_but_write_is_unconfirmed(self):
        self.dispatch()
        self.assertEqual(self.received[0],{'type':'auth','token':'b'*32})
        frame = self.received[1]
        self.assertEqual(frame['msg_id'],native_message_id(self.message))
        self.assertEqual(frame['uuid'],frame['msg_id'])
        self.assertEqual(frame['from'],peer_address(self.endpoint.pipe))
        self.assertIn('from-mode="prompting"',frame['message']['content'])
        self.assertIn(self.message.operation_id,frame['message']['content'])
        self.assertIn('Use bridge_reply with this operation ID for a correlated reply.',frame['message']['content'])
        self.assertIn(self.message.text,frame['message']['content'])
        self.assertIsNone(self.adapter.reconcile(self.message,None))
        with self.assertRaises(DeliveryError):
            self.adapter.send(self.message)  # direct use must not blindly retry either.

    def test_the_frames_and_the_write_are_the_hub_adapters(self):
        # One copy of the wire format and of the pipe write (hosts.claude.adapter.peer): a copy
        # here never reaches these wrapped functions, and this fails.
        with mock.patch.object(claude_peer, 'message_frames', wraps=claude_peer.message_frames) as frames, \
                mock.patch.object(claude_peer, 'write_once', wraps=claude_peer.write_once) as write:
            self.dispatch()
        frames.assert_called_once_with('b'*32, peer_address(self.endpoint.pipe), mock.ANY,
                                       native_message_id(self.message), 'prompting')
        write.assert_called_once_with(self.snapshot.pipe, self.snapshot.pid, self.snapshot.creation, mock.ANY)
        self.assertEqual(write.call_args.args[3], b''.join(encode_frame(f) for f in self.received))

    def test_a_write_never_made_is_before_write_and_an_unknown_one_stays_attempted(self):
        with mock.patch.object(claude_peer, 'write_once', return_value=claude_peer.push.NOT_WRITTEN):
            with self.assertRaisesRegex(BeforeWriteError, 'native-peer-unavailable-before-write'):
                self.adapter.send(self.message)
        with mock.patch.object(claude_peer, 'write_once', return_value=claude_peer.push.UNCERTAIN):
            with self.assertRaisesRegex(DeliveryError, 'native-write-uncertain'):
                self.adapter.send(self.message)     # not refused as attempted: the first was forgotten
        with self.assertRaisesRegex(DeliveryError, 'native-operation-already-attempted'):
            self.adapter.send(self.message)

    def test_authenticated_correlated_hold_and_refusal_are_returned_without_resend(self):
        self.dispatch()
        result = self.status('held')
        self.assertIsNotNone(result)
        self.assertEqual(result.state,'held')
        self.assertEqual(result.native_id,native_message_id(self.message))
        self.status('dropped',status_detail='refused')
        self.assertEqual(self.adapter.reconcile(self.message,result.native_id).state,'refused')
        self.assertEqual(len(self.received),2)

    def test_wrong_auth_address_and_original_id_never_become_delivery_evidence(self):
        self.dispatch()
        self.assertIsNone(self.status('held',token='c'*32))
        self.assertIsNone(self.status('held',address=peer_address(r'\\.\pipe\LOCAL\cc-msg-other')))
        self.assertIsNone(self.status('held',operation=str(uuid.uuid4())))
        self.assertEqual(self.status('held').state,'held')

    def test_ordinary_text_reply_is_not_guessed_as_a_reply_to_pending_operation(self):
        self.dispatch()
        token = json.loads(self.endpoint.key_path.read_text())['peerToken']
        frames = [{'type':'auth','token':token},{
            'type':'user','from':peer_address(self.snapshot.pipe),
            'message':{'role':'user','content':'uncorrelated private reply'}}]
        pipe = pipes.connect_waiting(self.endpoint.pipe,os.getpid(),own_ctime())
        try:
            pipes.send(pipe,frames)
        finally:
            pipe.close()
        time.sleep(.1)
        self.assertIsNone(self.adapter.reconcile(self.message,None))

    def test_separate_client_process_cannot_claim_the_native_session_with_its_return_key(self):
        self.dispatch()
        token = json.loads(self.endpoint.key_path.read_text())['peerToken']
        packet = encode_frame({'type':'auth','token':token}) + encode_frame({
            'type':'control','action':'peer_message_status','status':'held',
            'orig_msg_id':native_message_id(self.message),'from':peer_address(self.snapshot.pipe)})
        write_peer(PipeTarget(self.endpoint.pipe,os.getpid(),own_ctime()),packet)
        time.sleep(.1)
        self.assertIsNone(self.adapter.reconcile(self.message,None))

    def test_current_native_account_and_permission_mode_are_required_before_write(self):
        self.adapter.sender_mode=None
        with self.assertRaises(BeforeWriteError):
            self.adapter.send(self.message)
        self.adapter.sender_mode=lambda message:'prompting'
        self.directory.account_probe=lambda record,root:'d'*64
        with self.assertRaises(BeforeWriteError):
            self.adapter.send(self.message)

    def test_desktop_uses_same_transport_and_changed_native_identity_rejects_late_status(self):
        self.record['entrypoint']='claude-desktop'
        self.write_record()
        self.snapshot=self.directory.discover()[0]
        self.dispatch()
        self.record['sessionId']='replacement-session'
        self.write_record()
        with self.assertRaises(NativeSessionError):
            self.status('held')

    def test_endpoint_publishes_own_peer_only_and_removes_only_owned_files(self):
        native_before=set(self.registry.iterdir())
        owned=list(self.own_registry.iterdir())
        self.assertEqual(len(owned),2)
        record=json.loads(self.endpoint.registry_path.read_text())
        self.assertEqual(record['messagingSocketPath'],self.endpoint.pipe)
        self.assertEqual(record['entrypoint'],'kit-agent-bridge')
        self.endpoint.close()
        self.assertEqual(list(self.own_registry.iterdir()),[])
        self.assertEqual(set(self.registry.iterdir()),native_before)

    def test_existing_registration_is_never_overwritten_or_deleted(self):
        self.endpoint.close()
        path=self.own_registry / ('%d.json' % os.getpid())
        path.write_text('{"existing":"preserve"}',encoding='utf-8')
        with self.assertRaises(DeliveryError) as raised:
            ClaudePeerEndpoint(self.own_registry,self.root,self.directory)
        self.assertEqual(raised.exception.code,'peer-registration-already-exists')
        self.assertEqual(path.read_text(),'{"existing":"preserve"}')

    def test_a_failed_start_names_its_step_and_leaves_no_files(self):
        # Each start step's failure reaches the service's refusal line as its own code.
        self.endpoint.close()
        module='optional.bridge.hosts.claude.peer.'
        real_publish=ClaudePeerEndpoint._publish
        def publish_failing(index):
            calls=[]
            def publish(endpoint,path,value):
                calls.append(path)
                if len(calls)==index: raise OSError('injected')
                return real_publish(endpoint,path,value)
            return publish
        cases=(
            ('peer-process-identity-unavailable',mock.patch(module+'own_ctime',return_value=0)),
            ('peer-endpoint-start-failed-own-ctime',mock.patch(module+'own_ctime',side_effect=OSError('x'))),
            ('peer-endpoint-start-failed-listener',mock.patch(module+'PrivateListener',side_effect=OSError('x'))),
            ('peer-endpoint-start-failed-publish-key',
                mock.patch.object(ClaudePeerEndpoint,'_publish',publish_failing(1))),
            ('peer-endpoint-start-failed-publish-registry',
                mock.patch.object(ClaudePeerEndpoint,'_publish',publish_failing(2))),
            ('peer-endpoint-start-failed-thread',
                mock.patch.object(threading.Thread,'start',side_effect=RuntimeError('x'))))
        for code,patch in cases:
            with self.subTest(code=code):
                with patch, self.assertRaises(DeliveryError) as raised:
                    ClaudePeerEndpoint(self.own_registry,self.root,self.directory)
                self.assertEqual(raised.exception.code,code)
                self.assertEqual(list(self.own_registry.iterdir()),[])

    def test_shared_core_records_one_native_write_unknown_hold_then_root_refusal(self):
        # Host authority ports are explicit test fixtures; native transport and
        # durable policy/receipt storage are the actual implementations.
        with PairingStore(self.root/'pairings.sqlite',live_check=lambda binding:True,
                owner_check=lambda selection,proof:proof == b'test-owner') as pairs:
            pairs.observe(self.message.sender)
            pairs.observe(self.recipient)
            pairs.owner_select(pairs.propose('sender','recipient'),b'test-owner')
            with Outbox(self.root/'outbox.sqlite',leadership_check=lambda:True) as outbox:
                outbox.claim()
                core=CoreBroker(pairs,outbox,{'claude-peer':self.adapter},
                    authorize=lambda intent,proof:SenderAuthority(self.message.sender,'test-root')
                        if proof == b'test-source' else None,
                    dispatch_check=lambda message:True)
                core.admit('test-op',self.recipient,self.message.text,b'test-source')
                core.admit('queued-sibling',self.recipient,'later same-root work',b'test-source')
                thread=self.listen()
                try:
                    self.assertEqual(core.dispatch_once('test-op').state,'unknown')
                finally:
                    thread.join(timeout=5)
                self.assertEqual(self.errors,[])
                self.assertFalse(thread.is_alive())
                self.assertEqual(core.dispatch_once('test-op').state,'unknown')
                self.assertEqual(self.status('held').state,'held')
                self.assertEqual(core.poll_native('test-op',b'test-source').state,'held')
                self.assertEqual(self.status('dropped',status_detail='refused').state,'refused')
                self.assertEqual(core.poll_native('test-op',b'test-source').state,'refused')
                self.assertEqual(core.receipt('queued-sibling',b'test-source').state,'refused')
                self.assertEqual(len(self.received),2)  # auth + exactly one native message
