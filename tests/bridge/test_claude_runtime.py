"""Owned native records, process callers and actual private Claude runtime assembly."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from optional.bridge.hosts.claude.runtime import ClaudeSources
from optional.bridge.hosts.claude.hook import entry_command
from optional.bridge.core.messages import DeliveryError
from optional.bridge.win.controller import Controller
from optional.bridge.win.frontend import FrontendInspector,NativeFrontend
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.port import NativeSource
from optional.bridge.win.service import BrokerService
from kitlib.proc.job import Job
from kitlib.proc.process import close,open_process,own_ctime,proc_ctime
from tests.bridge.test_pairing import chat

RUNTIME_SOURCE='optional/bridge/hosts/claude/runtime.py'


class ClaudeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-claude-runtime-')
        self.root=Path(self.temp.name)
        self.kit=Path(__file__).resolve().parents[2]
        self.state=self.root/'state';self.state.mkdir()
        self.registry=self.root/'sessions';self.registry.mkdir()
        self.profile=self.root/'desktop';self.profile.mkdir()
        self.account=str(uuid.uuid4())
        self.cli=str(uuid.uuid4())
        self.task='local_'+str(uuid.uuid4())
        self.bridge=uuid.uuid4().hex
        self.folder=self.profile/'claude-code-sessions'/self.account/str(uuid.uuid4())
        self.folder.mkdir(parents=True)
        self.task_file=self.folder/(self.task+'.json')
        self.data={'sessionId':self.task,'cliSessionId':self.cli,'bridgeSessionIds':[self.bridge],
            'cwd':str(self.root),'isArchived':False,'permissionMode':'auto','title':'original desktop',
            'sessionPermissionUpdates':[],'chromePermissionMode':'default','alwaysAllowedReasons':[]}
        self.config={'lastKnownAccountUuid':self.account,'windowSizeWasSignedIn':True}
        self.terminal_account=self.root/'.claude.json'
        self.terminal_account.write_text(json.dumps({'oauthAccount':{'accountUuid':str(uuid.uuid4()),
            'organizationUuid':str(uuid.uuid4())}}),encoding='utf-8')
        self.projects=self.root/'projects';self.transcripts=self.projects/'workspace';self.transcripts.mkdir(parents=True)
        self.transcript=self.transcripts/(self.cli+'.jsonl');self.transcript.write_text('{}\n',encoding='utf-8')
        self.record={'pid':os.getpid(),'procStartFt':str(own_ctime()),'sessionId':self.cli,
            'bridgeSessionId':self.bridge,'hostSessionId':self.task,'cwd':str(self.root),
            'peerProtocol':1,'entrypoint':'claude-desktop','messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-runtime-owned'}
        self.write()
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(self.state,self.leader)
        self.marker=self.state/'service.json'
        self.marker.write_text(json.dumps({'protocol':1,'pipe':self.leader.endpoint,
            'pid':os.getpid(),'creation':own_ctime()}),encoding='utf-8')
        self.sources=ClaudeSources(self.controller,self.registry,sys.executable,'owned-host',
            self.profile,self.terminal_account,self.projects,self.marker,'kit')
        self.job=Job(kill_on_close=True)
        self.children=[]
        self.service=None;self.service_thread=None;self.stop=threading.Event()

    def write(self):
        (self.profile/'config.json').write_text(json.dumps(self.config),encoding='utf-8')
        self.task_file.write_text(json.dumps(self.data),encoding='utf-8')
        (self.registry/('%d.json'%os.getpid())).write_text(json.dumps(self.record),encoding='utf-8')

    def tearDown(self):
        self.job.close()
        for child in self.children:
            child.wait(timeout=3)
            for stream in (child.stdin,child.stdout,child.stderr):
                if stream is not None:stream.close()
        self.stop.set()
        if self.service_thread is not None:self.service_thread.join(timeout=4)
        if self.service is not None:self.service.close()
        self.sources.close()
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()
        if self.service_thread is not None:self.assertFalse(self.service_thread.is_alive())

    def cli_mode(self):
        self.record={key:value for key,value in self.record.items() if key not in ('bridgeSessionId','hostSessionId')}
        self.record['entrypoint']='cli'
        self.write()

    def frontend(self,actual=False):
        argv=([sys.executable,'-I','-B',str(self.kit/'optional/bridge/mcp_frontend.py'),str(self.marker)] if actual
            else [sys.executable,'-I','-c','import sys; sys.stdin.read()'])
        env=dict(os.environ);env.pop('CODEX_APP_TOOLS_PIPE_PATH',None)
        child=self.job.start(argv,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        self.children.append(child)
        native=NativeFrontend(child.pid,proc_ctime(child.pid),os.getpid(),own_ctime(),'claude',Path(sys.executable).resolve())
        return child,native

    def start_service(self):
        inspector=FrontendInspector(self.kit,self.marker,sys.executable,{'claude':sys.executable})
        self.service=BrokerService(self.controller,inspector,source_factory=self.sources,hook_sink=self.sources.hook_sink)
        self.service_thread=threading.Thread(target=self.service.run,args=(self.stop,),daemon=True)
        self.service_thread.start()
        child,native=self.frontend(actual=True)
        self.native_process=child
        result=self.call({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        self.assertIn('result',result)
        child.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n');child.stdin.flush()
        return native

    def call(self,message):
        child=self.native_process
        child.stdin.write(json.dumps(message).encode()+b'\n');child.stdin.flush()
        return json.loads(child.stdout.readline(1024*1024+1))

    def hook(self,name,args,use='toolu_owned_runtime',session=None):
        event={'session_id':self.cli if session is None else session,'prompt_id':str(uuid.uuid4()),
            'cwd':str(self.root),'transcript_path':str(self.transcript),'permission_mode':'auto',
            'hook_event_name':'PreToolUse','tool_name':'mcp__kit__'+name,'tool_input':args,
            'tool_use_id':use,'mcp_server':{'name':'kit','source':'plugin'}}
        child=self.job.start(entry_command(self.marker),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        self.children.append(child)
        output,error=child.communicate(json.dumps(event).encode(),timeout=8)
        self.assertEqual((child.returncode,output,error),(0,b'',b''))

    def test_desktop_refresh_registers_exact_original_binding_before_first_tool(self):
        observed=self.sources.refresh()
        self.assertEqual(len(observed),1)
        binding=observed[0]
        self.assertEqual(binding.identity.session_id,self.task)
        self.assertEqual(binding.identity.native_session_id,self.cli)
        self.assertTrue(self.controller._live(binding))
        self.assertEqual(self.controller._describe(binding),{'title':'original desktop'})
        self.assertEqual(self.controller.pairs.owner_routes(),())
        self.assertFalse(self.controller.core.adapters)

    def test_desktop_native_source_ignores_model_meta_and_reuses_actual_original_task(self):
        _,native=self.frontend()
        source=self.sources(native)
        self.assertIs(type(source),NativeSource)
        self.assertIsNone(source.prepare)
        authority=source.observe({'sender':'borrowed','task_root':'fake','owner':True})
        self.assertEqual(authority.sender.identity.session_id,self.task)
        self.assertEqual(authority.task_root,self.task)
        self.assertTrue(self.controller._live(authority.sender))
        self.assertIs(self.sources(native),source)

    def test_refresh_titles_have_no_authority_and_account_permission_archive_drift_revokes(self):
        before=self.sources.refresh()[0]
        self.data['title']='title claiming all permissions'
        self.write()
        self.assertEqual(self.sources.refresh()[0],before)
        self.assertEqual(self.controller._describe(before),{'title':'title claiming all permissions'})
        self.data['permissionMode']='default';self.write()
        changed=self.sources.refresh()[0]
        self.assertNotEqual(before,changed)
        self.assertFalse(self.controller._live(before))
        self.assertTrue(self.controller._live(changed))
        self.data['isArchived']=True;self.write()
        self.assertEqual(self.sources.refresh(),())
        self.assertFalse(self.controller._live(changed))

    def test_cli_without_pretooluse_capture_has_no_binding_and_meta_cannot_enable_it(self):
        self.cli_mode()
        self.assertEqual(self.sources.refresh(),())
        _,native=self.frontend()
        source=self.sources(native)
        self.assertIs(type(source),NativeSource)
        self.assertTrue(callable(source.prepare))
        with self.assertRaises(Exception):source.observe({'session_id':self.cli,'permission_mode':'auto','owner':True})
        with self.assertRaises(Exception):
            source.prepare({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'bridge_connections','arguments':{}}})
        self.assertEqual(self.controller.pairs.registered(),())

    def test_actual_hook_sink_enrolls_cli_once_and_native_source_queues_selected_operation(self):
        self.cli_mode();self.start_service()
        target=chat('fixture-target')
        self.controller.enroll(target,lambda item:item==target)
        args={'operation_id':'runtime-operation','recipient':target.reference,'text':'private runtime fixture'}
        self.hook('bridge_send',args)
        sender=next(item for item in self.controller.pairs.registered() if item.identity.native_session_id==self.cli)
        self.assertEqual(self.controller.pairs.owner_routes(),())  # Hook never chooses connections.
        self.controller.owner.change(sender.reference,target.reference,enabled=True)
        result=self.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_send','arguments':args,'_meta':{'sender':'fake','owner':True}}})
        self.assertFalse(result['result']['isError'],result)
        message=self.controller.outbox.message('runtime-operation')
        self.assertEqual(message.sender,sender)
        self.assertEqual(message.task_root,self.cli)
        self.assertEqual(message.text,args['text'])
        self.assertEqual(len(self.service.frontends),1)
        replay=self.call({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'bridge_send','arguments':args}})
        self.assertIn('error',replay)
        self.assertFalse(self.controller.core.adapters)  # Admission supplies no wake/return-peer lease.

    def test_wrong_session_hook_cannot_enable_current_frontend(self):
        self.cli_mode();self.start_service()
        self.hook('bridge_connections',{},session=str(uuid.uuid4()))
        self.assertEqual(self.controller.pairs.registered(),())
        result=self.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'bridge_connections','arguments':{}}})
        self.assertIn('error',result)

    def test_two_frontends_for_same_cli_make_hook_routing_explicitly_ambiguous(self):
        self.cli_mode()
        for _ in range(2):
            _,native=self.frontend()
            self.sources(native)
        self.start_service()
        self.hook('bridge_connections',{})
        self.assertEqual(self.controller.pairs.registered(),())

    def test_exited_frontend_held_open_by_another_handle_does_not_make_hook_routing_ambiguous(self):
        # An exited process keeps its creation time readable while any handle to it is open
        # anywhere on the machine; holding one here makes that window certain.
        self.cli_mode()
        child,native=self.frontend()
        self.sources(native)
        held=open_process(child.pid)
        try:
            self.assertTrue(held)
            child.terminate();child.wait(timeout=3)
            self.start_service()
            self.hook('bridge_connections',{})
            self.assertEqual([item.identity.native_session_id for item in self.controller.pairs.registered()],[self.cli])
        finally:close(held)

    def test_exited_frontend_held_open_by_another_handle_is_refused_and_dropped_from_cache(self):
        self.cli_mode()
        child,native=self.frontend()
        self.sources(native)
        held=open_process(child.pid)
        try:
            self.assertTrue(held)
            child.terminate();child.wait(timeout=3)
            with self.assertRaises(DeliveryError):self.sources(native)
            _,other=self.frontend()
            self.sources(other)
            self.assertEqual(list(self.sources._frontends),[(other.pid,other.creation)])
        finally:close(held)

    def test_runtime_lock_is_not_held_during_controller_or_pairing_callbacks(self):
        original=self.controller.enroll
        def enrolled(*args,**kwargs):
            self.assertFalse(self.sources._lock._is_owned())
            return original(*args,**kwargs)
        with patch.object(self.controller,'enroll',side_effect=enrolled):
            self.sources.refresh()
            _,native=self.frontend()
            self.sources(native).observe({})

    def test_foreign_frontend_identity_and_closed_provider_fail_without_native_adapters(self):
        _,native=self.frontend()
        with self.assertRaises(DeliveryError):self.sources(replace(native,host_creation=native.host_creation+1))
        self.sources.refresh()
        self.assertTrue(self.sources.directory._handles)
        self.sources.close()
        self.assertEqual(self.sources.directory._handles,{})
        with self.assertRaises(DeliveryError):self.sources(native)
        self.assertFalse(self.controller.core.adapters)
