"""Actual MCP frontend process, private pipe and shared controller ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from optional.bridge.core.messages import SenderAuthority
from optional.bridge.win.controller import Controller
from optional.bridge.win.frontend import FrontendInspector
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.win.service import BrokerService
from kitlib.proc.job import Job
from kitlib.proc.process import own_ctime
from tests.bridge.test_pairing import chat

FRONTEND_SOURCE='optional/bridge/mcp_frontend.py'


class PrivateFrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-private-frontend-')
        self.root=Path(self.temp.name)
        self.kit=Path(__file__).resolve().parents[2]
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(self.root,self.leader)
        self.a,self.b=chat('a'),chat('b')
        self.peers={'a':self.a,'b':self.b}
        for peer in self.peers.values(): self.controller.enroll(peer,lambda item:self.peers.get(item.identity.session_id)==item)
        self.controller.owner.change(self.a.reference,self.b.reference,enabled=True)
        self.marker=self.root/'service.json'
        self.marker.write_text(json.dumps({'protocol':1,'pipe':self.leader.endpoint,
            'pid':os.getpid(),'creation':own_ctime()}),encoding='utf-8')
        self.inspector=FrontendInspector(self.kit,self.marker,sys.executable,{'codex':sys.executable})
        self.service=BrokerService(self.controller,self.inspector,
            source_factory=lambda native:lambda meta:SenderAuthority(self.a,'native-test-root'))
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.service.run,args=(self.stop,),daemon=True)
        self.thread.start()
        self.job=Job(kill_on_close=True)
        self.process=self.job.start([sys.executable,'-I','-B',str(self.kit/FRONTEND_SOURCE),str(self.marker)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)

    def tearDown(self):
        self.job.close()
        self.process.wait(timeout=3)
        for stream in (self.process.stdin,self.process.stdout): stream.close()
        self.stop.set()
        self.thread.join(timeout=4)
        self.service.close()
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()
        self.assertFalse(self.thread.is_alive())

    def call(self,message):
        self.process.stdin.write(json.dumps(message).encode()+b'\n')
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline(1024*1024+1))

    def initialize(self):
        result=self.call({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        self.assertIn('result',result)
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def test_real_frontend_queues_message_through_shared_broker_with_no_identity_argument(self):
        self.initialize()
        result=self.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_send','arguments':{'operation_id':'private-test','recipient':self.b.reference,
            'text':'private native frontend body'}}})
        self.assertEqual(result['result']['structuredContent']['state'],'queued')
        message=self.controller.outbox.message('private-test')
        self.assertEqual(message.sender,self.a)
        self.assertEqual(message.text,'private native frontend body')
        self.assertEqual(message.task_root,'native-test-root')

    def test_normal_rpc_helper_cannot_impersonate_frontend_by_claiming_parent_fields(self):
        from optional.bridge.win.rpc import request
        from optional.bridge.win.transport import PipeTarget
        result=request(PipeTarget(self.leader.endpoint,os.getpid(),own_ctime()),{
            'protocol':1,'request':{'jsonrpc':'2.0','id':9,'method':'tools/list'},
            'frontend_pid':self.process.pid,'owner_approved':True})
        self.assertIn('error',result)

    def test_repeated_requests_reuse_the_exact_frontend_protocol_session(self):
        self.initialize()
        first=self.call({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        self.assertTrue(first['result']['tools'])
        self.assertEqual(len(self.service.frontends),1)
        # Helpers reconnect, but the exact frontend retains its MCP session.
        second=self.call({'jsonrpc':'2.0','id':3,'method':'tools/list'})
        self.assertTrue(second['result']['tools'])
        self.assertEqual(len(self.service.frontends),1)

    def test_actual_parent_matches_second_explicit_native_image(self):
        alternate=Path(os.environ['WINDIR'])/'System32/cmd.exe'
        self.service.inspector=FrontendInspector(self.kit,self.marker,sys.executable,
            {'codex':[alternate,Path(sys.executable)]})
        self.initialize()
        self.assertEqual(next(iter(self.service.frontends.values()))[0].kind,'codex')

    def test_unlisted_actual_native_parent_cannot_initialize_a_source(self):
        alternate=Path(os.environ['WINDIR'])/'System32/cmd.exe'
        self.service.inspector=FrontendInspector(self.kit,self.marker,sys.executable,{'codex':[alternate]})
        result=self.call({'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2025-06-18'}})
        self.assertIn('error',result)
        self.assertFalse(self.service.frontends)

    def test_malformed_private_client_does_not_stop_the_broker(self):
        from optional.bridge.core.jsonwire import encode_frame
        from optional.bridge.win.transport import run_worker
        for packet in (b'{\n',b'[]\n',b'\xff\n',b'{"x":1,"x":2}\n'):
            header=encode_frame({'pipe':self.leader.endpoint,'pid':os.getpid(),
                'creation':own_ctime(),'length':len(packet)},max_frame_bytes=2048)
            run_worker('bridge_rpc_worker.py',header+packet,limit=1024*1024+2048,timeout=5)
            self.assertTrue(self.thread.is_alive())
        self.initialize()

    def test_client_identity_race_does_not_stop_the_broker(self):
        from kitlib.proc.errors import ProcError
        from optional.bridge.win.rpc import request
        from optional.bridge.win.transport import PipeTarget
        with mock.patch.object(self.leader.listener,'verify_client',side_effect=ProcError('pipe-client-process-mismatch')):
            request(PipeTarget(self.leader.endpoint,os.getpid(),own_ctime()),{
                'protocol':1,'request':{'jsonrpc':'2.0','id':8,'method':'ping'}})
        self.assertTrue(self.thread.is_alive())
        self.initialize()

    def test_valid_envelope_from_ordinary_helper_reaches_and_fails_ancestry_check(self):
        from optional.bridge.win.rpc import request
        from optional.bridge.win.transport import PipeTarget
        with mock.patch.object(self.inspector,'inspect',wraps=self.inspector.inspect) as inspected:
            result=request(PipeTarget(self.leader.endpoint,os.getpid(),own_ctime()),{
                'protocol':1,'request':{'jsonrpc':'2.0','id':8,'method':'ping'}})
            self.assertIn('error',result)
            inspected.assert_called_once()
        self.assertFalse(self.service.frontends)

    def test_frontend_without_bytecode_flag_cannot_establish_native_authority(self):
        self.process.terminate()
        self.process.wait(timeout=3)
        for stream in (self.process.stdin,self.process.stdout): stream.close()
        self.process=self.job.start([sys.executable,'-I',str(self.kit/FRONTEND_SOURCE),str(self.marker)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        response=self.call({'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2025-06-18'}})
        self.assertIn('error',response)
        self.assertFalse(self.service.frontends)
        self.assertFalse(self.controller._sources)

    def test_exited_frontend_is_removed_and_its_authority_detached(self):
        self.initialize()
        old=next(iter(self.service.frontends.values()))[1].calls
        self.process.terminate()
        self.process.wait(timeout=3)
        for stream in (self.process.stdin,self.process.stdout): stream.close()
        self.process=self.job.start([sys.executable,'-I','-B',str(self.kit/FRONTEND_SOURCE),str(self.marker)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        self.initialize()
        self.assertEqual(len(self.service.frontends),1)
        self.assertNotIn(old,self.controller._sources)

    def test_private_prepared_metadata_replaces_wire_claims_and_is_consumed_on_refusal(self):
        from optional.bridge.port import NativeSource
        captured=object()
        consumed=[]
        def observe(metadata):
            if metadata is not captured: raise ValueError('wrong provenance')
            return SenderAuthority(self.a,'native-test-root')
        def prepare(message):
            return captured,lambda:consumed.append(True)
        self.service.source_factory=lambda native:NativeSource(observe,prepare)
        self.initialize()
        result=self.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_send','arguments':{'operation_id':'unselected','recipient':'unknown','text':'x'},
            '_meta':{'sender':'borrowed','ticket':'fabricated'}}})
        self.assertTrue(result['result']['isError'])
        self.assertEqual(consumed,[True])
        result=self.call({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
            'name':'bridge_connections','arguments':{},'_meta':{'sender':'borrowed'}}})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(consumed,[True,True])

    def test_hook_channel_defaults_to_denial(self):
        from optional.bridge.win.rpc import request
        from optional.bridge.win.transport import PipeTarget
        result=request(PipeTarget(self.leader.endpoint,os.getpid(),own_ctime()),{
            'protocol':1,'hook':{'hook_event_name':'PreToolUse','session_id':'borrowed'}})
        self.assertIn('error',result)
        self.assertFalse(self.service.frontends)

    def test_executable_claude_hook_and_frontend_share_private_native_context(self):
        from optional.bridge.hosts.claude.sessions import ClaudeDirectory
        from optional.bridge.hosts.claude.terminal import ClaudeTerminal,TerminalCalls
        from optional.bridge.hosts.claude.hook import entry_command
        from optional.bridge.port import NativeSource
        session=str(uuid.uuid4())
        registry=self.root/'native-sessions'
        registry.mkdir()
        (registry/('%d.json'%os.getpid())).write_text(json.dumps({
            'pid':os.getpid(),'procStartFt':str(own_ctime()),'sessionId':session,
            'cwd':str(self.root),'peerProtocol':1,'entrypoint':'cli',
            'messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-owned-frontend-test'}),encoding='utf-8')
        account=self.root/'.claude.json'
        account.write_text(json.dumps({'oauthAccount':{'accountUuid':str(uuid.uuid4()),
            'organizationUuid':str(uuid.uuid4())}}),encoding='utf-8')
        projects=self.root/'projects'
        transcripts=projects/'native-workspace'
        transcripts.mkdir(parents=True)
        transcript=transcripts/(session+'.jsonl')
        transcript.write_text('{}\n',encoding='utf-8')
        terminal=ClaudeTerminal(account,transcript_root=projects)
        directory=ClaudeDirectory(registry,sys.executable,account_probe=terminal.account_probe)
        try:
            holder=[]
            argv=entry_command(self.marker)
            def factory(native):
                calls=TerminalCalls(directory,terminal,'owned-host',native.pid,native.creation,
                    hook_argv=argv,bridge_tools={'mcp__kit__bridge_send':{'name':'kit','source':'plugin'}})
                holder.append(calls)
                return NativeSource(calls.observe,calls.prepare_request)
            self.service.source_factory=factory
            self.initialize()
            arguments={'operation_id':'native-hook-send','recipient':self.b.reference,'text':'native hook body'}
            event={'session_id':session,'prompt_id':str(uuid.uuid4()),'cwd':str(self.root),
                'transcript_path':str(transcript),'permission_mode':'default','hook_event_name':'PreToolUse',
                'tool_name':'mcp__kit__bridge_send','tool_input':arguments,'tool_use_id':'toolu_owned_frontend',
                'mcp_server':{'name':'kit','source':'plugin'}}
            def sink(peer,payload):
                calls=holder[0]
                ticket=calls.observe_hook(peer,payload)
                authority=calls.observe(calls.metadata(event['tool_name'],arguments,ticket=ticket))
                self.controller.enroll(authority.sender,calls.require_live)
                # Explicit fixture owner choice; production sink never selects routes.
                self.controller.owner.change(authority.sender.reference,self.b.reference,enabled=True)
            self.service.hook_sink=sink
            child=self.job.start(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
            stdout,stderr=child.communicate(json.dumps(event).encode(),timeout=8)
            self.assertEqual((child.returncode,stdout,stderr),(0,b'',b''))
            result=self.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
                'name':'bridge_send','arguments':arguments,'_meta':{'sender':'fabricated'}}})
            self.assertFalse(result['result']['isError'],result)
            message=self.controller.outbox.message('native-hook-send')
            self.assertEqual(message.sender.identity.native_session_id,session)
            self.assertEqual(message.text,'native hook body')
            replay=self.call({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
                'name':'bridge_send','arguments':arguments}})
            self.assertIn('error',replay)
        finally: directory.close()
