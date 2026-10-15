"""Concrete Codex source enrollment with owned native/frontend process fixtures."""
import base64
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

from optional.bridge.hosts.codex.runtime import CodexSources
from optional.bridge.core.messages import DeliveryError
from optional.bridge.win.controller import Controller
from optional.bridge.win.frontend import NativeFrontend
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.port import NativeSource
from kitlib.proc.job import Job
from kitlib.proc.pipe import process_image
from kitlib.proc.process import own_ctime, proc_ctime


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='codex-runtime-')
        self.root=Path(self.temp.name)
        self.profile=self.root/'native-profile'
        self.profile.mkdir()
        self.workspace=self.root/'workspace'
        self.workspace.mkdir()
        sessions=self.profile/'sessions'/'2026'/'10'/'12'
        sessions.mkdir(parents=True)
        self.thread,self.turn=str(uuid.uuid4()),str(uuid.uuid4())
        self.path=sessions/('rollout-fixture-'+self.thread+'.jsonl')
        self.metadata={'x-codex-turn-metadata':{'thread_id':self.thread,'turn_id':self.turn,
            'thread_source':'user','auto_review_enabled':True,'node_repl_auto_review_required':True}}
        self.session={'id':self.thread,'cwd':str(self.workspace),'originator':'Codex Desktop',
            'source':'vscode','thread_source':'user','creator_account_id':'fixture-account',
            'creator_user_id':'fixture-user'}
        self.context={'turn_id':self.turn,'cwd':str(self.workspace),'approval_policy':'never',
            'approvals_reviewer':'auto_review','sandbox_policy':{'type':'workspace-write'},
            'permission_profile':{'type':'managed','network':'restricted','file_system':{
                'type':'restricted','entries':[{'access':'write','path':{'type':'path','path':str(self.workspace)}}]}}}
        self.lifecycle={'type':'task_started','turn_id':self.turn}
        self.write()
        self.write_login()
        state=self.root/'state'
        state.mkdir()
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(state,self.leader)
        self.job=Job(kill_on_close=True)
        self.channel_hosts=[]
        self.child=self.job.start([sys.executable,'-I','-c','import time; time.sleep(60)'],
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.native=NativeFrontend(self.child.pid,proc_ctime(self.child.pid),os.getpid(),own_ctime(),
            'codex',Path(sys.executable).resolve())
        self.factory=CodexSources(self.controller,self.profile,'fixture-host')
        self.source=self.factory(self.native)

    def tearDown(self):
        self.factory.close()
        self.controller.close()
        self.leader.close()
        self.job.close()
        self.child.wait(timeout=2)
        for host in self.channel_hosts: host.wait(timeout=2)
        self.temp.cleanup()

    def write_login(self, user='fixture-user'):
        claims={'sub':user,'https://api.openai.com/auth':{'chatgpt_user_id':user,
            'chatgpt_account_id':'fixture-account'}}
        body=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        (self.profile/'config.toml').write_text('cli_auth_credentials_store = "file"\n',encoding='utf-8')
        (self.profile/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt','tokens':{
            'account_id':'fixture-account','id_token':'e30.'+body+'.c2ln',
            'access_token':'fixture-private-credential'}}),encoding='utf-8')

    def write(self):
        def line(kind,payload):
            return json.dumps({'timestamp':'2026-10-12T12:00:00Z','type':kind,'payload':payload},
                separators=(',',':')).encode()+b'\n'
        self.path.write_bytes(line('session_meta',self.session)+line('turn_context',self.context)+
            line('event_msg',self.lifecycle))

    def observe(self):
        return self.source.observe(self.metadata)

    def test_actual_source_enrolls_original_native_binding_through_real_controller(self):
        self.assertIs(type(self.source),NativeSource)
        self.assertIsNone(self.source.prepare)
        authority=self.observe()
        self.assertEqual(authority.sender.identity.native_session_id,self.thread)
        self.assertEqual(authority.sender.identity.adapter_id,'codex-desktop')
        self.assertEqual(self.controller.pairs.registered(),(authority.sender,))
        self.assertEqual(authority.task_root,self.thread)
        self.assertTrue(self.controller._live(authority.sender))
        self.assertIsNone(self.factory.native_channel(authority.sender))
        self.assertNotIn('fixture-private-credential',repr(authority))

    def test_real_frontend_server_consumes_native_meta_and_registers_without_owner_grant(self):
        server=self.controller.frontend(self.source.observe)
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        reply=server.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_connections','arguments':{},'_meta':self.metadata}})
        self.assertEqual(reply['result']['structuredContent'],{'chats':[]})
        self.assertEqual(len(self.controller.pairs.registered()),1)
        self.assertEqual(self.controller.pairs.owner_routes(),())

    def test_completed_original_turn_keeps_idle_liveness_but_cannot_be_active_caller(self):
        authority=self.observe()
        self.lifecycle['type']='task_completed'
        self.write()
        self.assertTrue(self.controller._live(authority.sender))
        with self.assertRaises(DeliveryError): self.observe()
        self.assertTrue(self.controller._live(authority.sender))

    def test_missing_native_review_flags_or_model_arguments_cannot_enroll(self):
        self.metadata['x-codex-turn-metadata'].pop('auto_review_enabled')
        for forged in (self.metadata,{'arguments':{'thread_id':self.thread,'turn_id':self.turn,
                'auto_review_enabled':True,'node_repl_auto_review_required':True}}):
            with self.assertRaises(DeliveryError): self.source.observe(forged)
        self.assertEqual(self.controller.pairs.registered(),())

    def test_changed_latest_context_suspends_until_new_actual_active_metadata(self):
        old=self.observe().sender
        self.context['approval_policy']='on-request'
        self.write()
        self.assertFalse(self.controller._live(old))
        new=self.observe().sender
        self.assertNotEqual(new,old)
        self.assertTrue(self.controller._live(new))
        self.assertFalse(self.controller._live(old))

    def test_new_uncaptured_turn_cannot_reuse_old_flags_even_with_same_permissions(self):
        old=self.observe().sender
        self.turn=str(uuid.uuid4())
        self.context['turn_id']=self.turn
        self.lifecycle.update(type='task_started',turn_id=self.turn)
        self.write()
        self.assertFalse(self.controller._live(old))
        self.metadata['x-codex-turn-metadata']['turn_id']=self.turn
        fresh=self.observe().sender
        self.assertEqual(fresh,old)
        self.assertTrue(self.controller._live(fresh))

    def test_additional_changed_context_does_not_silently_keep_captured_flag_authority(self):
        authority=self.observe()
        self.context['new_runtime_field']='changed'
        self.write()
        self.assertFalse(self.controller._live(authority.sender))

    def test_login_workspace_or_header_drift_denies_captured_idle_binding(self):
        authority=self.observe()
        self.write_login(user='other-user')
        self.assertFalse(self.controller._live(authority.sender))
        self.write_login()
        self.context['cwd']=str(self.root)
        self.write()
        self.assertFalse(self.controller._live(authority.sender))
        self.context['cwd']=str(self.workspace)
        self.session['creator_user_id']='other-user'
        self.write()
        self.assertFalse(self.controller._live(authority.sender))

    def test_current_login_must_match_session_creator_before_enrollment(self):
        self.write_login(user='other-user')
        with self.assertRaises(DeliveryError): self.observe()
        self.assertEqual(self.controller.pairs.registered(),())

    def test_closed_native_frontend_drops_idle_liveness_without_borrowed_host(self):
        authority=self.observe()
        self.job.close()
        self.child.wait(timeout=2)
        self.assertFalse(self.controller._live(authority.sender))
        with self.assertRaises(DeliveryError): self.observe()

    def test_terminal_source_enrolls_without_any_desktop_tools_channel(self):
        self.session.update(originator='codex_cli_rs',source='cli')
        self.write()
        authority=self.observe()
        self.assertEqual(authority.sender.identity.adapter_id,'codex-terminal')
        self.lifecycle['type']='task_complete'
        self.write()
        self.assertTrue(self.controller._live(authority.sender))
        self.assertIsNone(self.factory.native_channel(authority.sender))

    def test_wire_channel_fields_cannot_publish_app_owned_endpoint(self):
        self.metadata['CODEX_APP_TOOLS_PIPE_PATH']=r'\\.\pipe\untrusted-model-channel'
        self.metadata['channel']={'pipe':r'\\.\pipe\untrusted-model-channel','pid':os.getpid()}
        authority=self.observe()
        self.assertIsNone(self.factory.native_channel(authority.sender))

    def test_frontend_creation_parent_or_kind_mismatch_fails_before_source_use(self):
        for changed in (replace(self.native,creation=self.native.creation+1),
                replace(self.native,host_creation=self.native.host_creation+1),
                replace(self.native,kind='claude')):
            with self.assertRaises(DeliveryError): self.factory(changed)

    def test_frontend_whose_parent_is_not_the_host_fails_before_source_use(self):
        # The frontend runs, is this user's and has the right image and creation time, but was
        # started by another process: naming that process as host enrolls, naming this one fails.
        script=('import subprocess,sys,time;'
            'g=subprocess.Popen([sys.executable,"-I","-c","import time; time.sleep(60)"]);'
            'print(g.pid,flush=True);time.sleep(60)')
        middle=self.job.start([sys.executable,'-I','-c',script],stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(middle.wait,2)
        self.addCleanup(middle.stdout.close)
        grandchild=int(middle.stdout.readline())
        native=NativeFrontend(grandchild,proc_ctime(grandchild),os.getpid(),own_ctime(),
            'codex',Path(sys.executable).resolve())
        with self.assertRaises(DeliveryError): self.factory(native)
        own=self.factory(replace(native,host_pid=middle.pid,host_creation=proc_ctime(middle.pid),
            host_executable=process_image(middle.pid)))
        self.assertTrue(own.observe.__self__.alive())

    def test_liveness_check_takes_no_system_process_snapshot(self):
        # require() runs on every observe and liveness check: it reads the frontend's parent from
        # its own handle, so it holds when a Toolhelp snapshot would fail or be slow.
        runtime=self.source.observe.__self__
        self.assertTrue(runtime.alive())
        with patch('kitlib.proc.process.snapshot',side_effect=OSError('no snapshot')) as snapshot:
            self.assertTrue(runtime.alive())
            runtime.require()
        snapshot.assert_not_called()

    def test_factory_close_releases_source_authority_and_retained_native_handles(self):
        authority=self.observe()
        self.factory.close()
        self.assertFalse(self.controller._live(authority.sender))
        with self.assertRaises(DeliveryError): self.observe()

    def test_sender_mode_comes_from_current_verified_native_capture(self):
        authority=self.observe()
        self.assertEqual(self.factory.sender_mode(authority.sender),'prompting')
        self.context.update(permission_profile={'type':'disabled'},sandbox_policy={'type':'danger-full-access'},
            approvals_reviewer='user')
        self.write()
        fresh=self.observe()
        self.assertEqual(self.factory.sender_mode(fresh.sender),'bypass')
        self.lifecycle['type']='task_completed'
        self.write()
        self.assertEqual(self.factory.sender_mode(fresh.sender),'bypass')
        with self.assertRaises(DeliveryError): self.factory.sender_mode(authority.sender)

    def test_sender_mode_denies_unobserved_permission_account_or_controller_drift(self):
        from tests.bridge.test_pairing import chat
        with self.assertRaises(DeliveryError): self.factory.sender_mode(chat('not-native-captured'))
        authority=self.observe()
        self.context['approval_policy']='on-request'
        self.write()
        with self.assertRaises(DeliveryError): self.factory.sender_mode(authority.sender)
        self.context['approval_policy']='never'
        self.write()
        self.write_login(user='other-user')
        with self.assertRaises(DeliveryError): self.factory.sender_mode(authority.sender)
        self.write_login()
        self.controller.close()
        with self.assertRaises(DeliveryError): self.factory.sender_mode(authority.sender)

    def test_sender_mode_denies_ambiguous_fact_suppliers_without_choosing_one(self):
        from unittest.mock import patch
        authority=self.observe()
        runtime=self.source.observe.__self__
        with patch.object(self.factory,'_runtime_list',return_value=(runtime,runtime)):
            with self.assertRaises(DeliveryError): self.factory.sender_mode(authority.sender)

    def channel_source(self, *, wrong_creation=False):
        from kitlib.proc.pipe import PrivateListener
        from optional.bridge.win.channel import NativeChannel
        from optional.bridge.win.transport import PipeTarget
        name=r'\\.\pipe\LOCAL\codex-runtime-channel-'+uuid.uuid4().hex
        listener=PrivateListener(name)
        self.addCleanup(listener.close)
        command=('import subprocess,sys,time,json; p=subprocess.Popen([sys.executable,"-I","-c",'
            '"import time; time.sleep(60)"],creationflags=0x08000000); '
            'print(json.dumps({"frontend_pid":p.pid}),flush=True); time.sleep(60)')
        host=self.job.start([sys.executable,'-I','-c',command],stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        self.channel_hosts.append(host)
        self.addCleanup(host.stdout.close)
        data=json.loads(host.stdout.readline(1024))
        self.assertEqual(set(data),{'frontend_pid'})
        server_creation=own_ctime()+(1 if wrong_creation else 0)
        channel=NativeChannel(PipeTarget(name,os.getpid(),server_creation),Path(sys.executable).resolve())
        native=NativeFrontend(data['frontend_pid'],proc_ctime(data['frontend_pid']),host.pid,proc_ctime(host.pid),
            'codex',Path(sys.executable).resolve(),channel)
        source=self.factory(native)
        return source,native,listener

    def test_app_owned_channel_publication_uses_metadata_only_distinct_server_ancestor(self):
        from unittest.mock import patch
        from optional.bridge.win import channel
        from kitlib.proc.pipe import peek
        from ctypes import wintypes as w
        import ctypes
        source,native,listener=self.channel_source()
        binding=source.observe(self.metadata).sender
        queries=[]
        original=channel.create_file
        def metadata_query(*args):
            queries.append((args[1],args[5]))
            return original(*args)
        with patch('optional.bridge.win.channel.create_file',metadata_query):
            endpoint=self.factory.native_channel(binding)
        self.assertEqual(queries,[(0x00020080,0x40000000)])
        self.assertEqual(endpoint.pid,os.getpid())
        self.assertNotEqual(endpoint.pid,native.host_pid)
        self.assertEqual(endpoint.creation,own_ctime())
        self.assertEqual(endpoint.executable,str(Path(sys.executable).resolve()))
        listener.accept(timeout=2)
        available=w.DWORD()
        # Metadata publication sends no native request body or frame.
        if peek(listener.handle,None,0,None,ctypes.byref(available),None):
            self.assertEqual(available.value,0)

    def test_channel_descriptor_identity_drift_does_not_publish_or_grant_writer(self):
        source,native,listener=self.channel_source(wrong_creation=True)
        binding=source.observe(self.metadata).sender
        with self.assertRaises(DeliveryError): self.factory.native_channel(binding)
        self.assertEqual(self.controller.pairs.owner_routes(),())
        self.assertTrue(self.controller._live(binding))

    def test_failed_enrollment_does_not_restore_known_stale_native_flags(self):
        from unittest.mock import patch
        old=self.observe().sender
        self.metadata['x-codex-turn-metadata']['auto_review_enabled']=False
        with patch.object(self.controller,'enroll',side_effect=DeliveryError('injected-enrollment-failure')):
            with self.assertRaises(DeliveryError): self.observe()
        self.assertFalse(self.controller._live(old))
        fresh=self.observe().sender
        self.assertNotEqual(fresh,old)
        self.assertTrue(self.controller._live(fresh))

    def test_real_owner_change_and_native_source_refresh_finish_without_lock_inversion(self):
        with Job(kill_on_close=True) as job:
            child=job.start([sys.executable,'-I','-c',
                'import sys; sys.path.insert(0,sys.argv[1]); from tests.bridge.test_codex_runtime import concurrency_fixture; concurrency_fixture()',
                str(Path(__file__).resolve().parents[2])],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                output,_=child.communicate(timeout=10)
            finally:
                job.close()
                child.wait(timeout=2)
        self.assertEqual(child.returncode,0,output.decode('utf-8','replace'))
        self.assertEqual(json.loads(output),{'owner_completed':True,'source_completed':True,
            'both_threads_finished':True,'current_binding_live':True,'owner_route_selected':True})


def concurrency_fixture():
    """An actual owner/pairing callback races a native refresh in an owned process.

    The parent process bounds a failed deadlock and owns its complete process
    tree. No blocked test thread or native helper escapes fixture cleanup.
    """
    from tests.bridge.test_pairing import chat
    case=CodexRuntimeTests()
    case.setUp()
    initial=case.observe().sender
    other=chat('other-native-fixture')
    case.controller.enroll(other,lambda binding:binding==other)
    runtime=case.source.observe.__self__
    owner_inside_pairing,source_inside_capture=threading.Event(),threading.Event()
    original_live=case.controller.pairs._live_check
    original_observe=runtime.calls.observe
    barrier_used=False
    def live(binding):
        nonlocal barrier_used
        if threading.current_thread().name=='owner-change' and binding==initial and not barrier_used:
            barrier_used=True
            owner_inside_pairing.set()
            if not source_inside_capture.wait(timeout=3): return False
        return original_live(binding)
    def observe(metadata):
        source_inside_capture.set()
        return original_observe(metadata)
    case.controller.pairs._live_check=live
    runtime.calls.observe=observe
    result={'owner_completed':False,'source_completed':False}
    def owner():
        try:
            case.controller.owner.change(initial.reference,other.reference,enabled=True)
            result['owner_completed']=True
        except Exception: pass
    def refresh():
        try:
            case.observe()
            result['source_completed']=True
        except Exception: pass
    owner_thread=threading.Thread(target=owner,name='owner-change',daemon=True)
    source_thread=threading.Thread(target=refresh,name='native-refresh',daemon=True)
    owner_thread.start()
    if not owner_inside_pairing.wait(timeout=3):
        print(json.dumps({'barrier_reached':False}),flush=True)
        os._exit(2)
    source_thread.start()
    owner_thread.join(timeout=3)
    source_thread.join(timeout=3)
    result['both_threads_finished']=not owner_thread.is_alive() and not source_thread.is_alive()
    if not result['both_threads_finished']:
        print(json.dumps(result),flush=True)
        os._exit(2)
    result['current_binding_live']=case.controller._live(initial)
    try:
        case.controller.pairs.require_selected(initial,other)
        result['owner_route_selected']=True
    except Exception:
        result['owner_route_selected']=False
    case.tearDown()
    print(json.dumps(result),flush=True)
