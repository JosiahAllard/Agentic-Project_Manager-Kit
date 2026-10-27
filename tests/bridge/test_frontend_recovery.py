"""Same live native frontend re-handshakes only initialization after broker replacement."""
import base64
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from optional.bridge.hosts.codex.runtime import CodexSources
from optional.bridge.win.frontend import serve_frontend,service_target
from optional.bridge.win.lifecycle import Supervisor
from kitlib.proc.job import Job
from kitlib.proc.process import own_ctime
from tests.bridge.registered import no_refused_pairs


class FrontendRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='frontend-recovery-')
        self.root=Path(self.temp.name)
        self.state=self.root/'private-state'
        self.profile=self.root/'native-profile'
        sessions=self.profile/'sessions'/'2026'/'10'/'12'
        sessions.mkdir(parents=True)
        self.workspace=self.root/'workspace'
        self.workspace.mkdir()
        self.kit=Path(__file__).resolve().parents[2]
        # The durable-queue test sends between two Codex threads, a pair the bridge refuses;
        # recovery, not the host-pair rule, is under test here (test_host_pairs.py covers that).
        self.enterContext(no_refused_pairs())
        self.thread_id,self.turn=str(uuid.uuid4()),str(uuid.uuid4())
        self.meta={'x-codex-turn-metadata':{'thread_id':self.thread_id,'turn_id':self.turn,
            'thread_source':'user','auto_review_enabled':True,'node_repl_auto_review_required':True}}
        session={'id':self.thread_id,'cwd':str(self.workspace),'originator':'Codex Desktop',
            'source':'vscode','thread_source':'user','creator_account_id':'fixture-account','creator_user_id':'fixture-user'}
        context={'turn_id':self.turn,'cwd':str(self.workspace),'approval_policy':'never',
            'approvals_reviewer':'auto_review','sandbox_policy':{'type':'workspace-write'},
            'permission_profile':{'type':'managed','network':'restricted','file_system':{'type':'restricted',
                'entries':[{'access':'write','path':{'type':'path','path':str(self.workspace)}}]}}}
        def record(kind,payload):
            return json.dumps({'timestamp':'2026-10-12T12:00:00Z','type':kind,'payload':payload},
                separators=(',',':')).encode()+b'\n'
        (sessions/('rollout-owned-'+self.thread_id+'.jsonl')).write_bytes(record('session_meta',session)+
            record('turn_context',context)+record('event_msg',{'type':'task_started','turn_id':self.turn}))
        body=base64.urlsafe_b64encode(json.dumps({'sub':'fixture-user'}).encode()).decode().rstrip('=')
        (self.profile/'config.toml').write_text('cli_auth_credentials_store = "file"\n',encoding='utf-8')
        (self.profile/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt','tokens':{
            'account_id':'fixture-account','id_token':'e30.'+body+'.c2ln'}}),encoding='utf-8')
        self.start_broker()
        self.job=Job(kill_on_close=True)
        self.frontend=self.job.start([sys.executable,'-I','-B',str(self.kit/'optional/bridge/mcp_frontend.py'),str(self.state/'service.json')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
        self.output=queue.Queue()
        def read_output():
            for raw in iter(self.frontend.stdout.readline,b''):
                self.output.put(raw)
        self.reader=threading.Thread(target=read_output,daemon=True)
        self.reader.start()
        self.next_id=0

    def start_broker(self):
        self.factory=None
        def build(controller):
            self.factory=CodexSources(controller,self.profile,'owned-recovery-host')
            return type('OwnedRuntime',(),{'source_factory':self.factory,'hook_sink':None,
                'close':lambda runtime:self.factory.close()})()
        self.supervisor=Supervisor(self.state,self.kit,sys.executable,{'codex':sys.executable},runtime_builder=build)
        self.service_thread=threading.Thread(target=self.supervisor.run,daemon=True)
        self.service_thread.start()

    def stop_broker(self):
        self.supervisor.stop.set()
        self.service_thread.join(timeout=4)
        self.assertFalse(self.service_thread.is_alive())
        self.supervisor.close()

    def restart(self):
        old_target=service_target(self.state/'service.json')
        old_marker=(self.state/'service.json').stat().st_ino
        self.stop_broker()
        self.start_broker()
        self.assertEqual(service_target(self.state/'service.json'),old_target)
        self.assertNotEqual((self.state/'service.json').stat().st_ino,old_marker)

    def tearDown(self):
        self.job.close()
        self.frontend.wait(timeout=3)
        self.reader.join(timeout=2)
        self.frontend.stdin.close()
        self.frontend.stdout.close()
        self.stop_broker()
        self.temp.cleanup()

    def send(self,message):
        self.frontend.stdin.write(json.dumps(message).encode()+b'\n')
        self.frontend.stdin.flush()

    def call(self,method,params=None):
        self.next_id+=1
        message={'jsonrpc':'2.0','id':self.next_id,'method':method}
        if params is not None:message['params']=params
        self.send(message)
        raw=self.output.get(timeout=8)
        response=json.loads(raw)
        self.assertEqual(response['id'],self.next_id)
        return response

    def initialize(self):
        response=self.call('initialize',{'protocolVersion':'2025-06-18',
            'clientInfo':{'name':'owned-native-client','version':'fixture'},'capabilities':{}})
        self.assertIn('result',response)
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})

    def connections(self,meta=None):
        return self.call('tools/call',{'name':'bridge_connections','arguments':{},
            '_meta':self.meta if meta is None else meta})

    def test_same_live_frontend_recovers_initialized_session_after_real_supervisor_restart(self):
        self.initialize()
        first=self.connections()
        self.assertFalse(first['result']['isError'])
        old_pid=self.frontend.pid
        self.restart()
        second=self.connections()
        self.assertFalse(second['result']['isError'],second)
        self.assertEqual(self.frontend.pid,old_pid)
        self.assertEqual(len(self.supervisor.controller.pairs.registered()),1)

    def test_fresh_actual_metadata_is_not_replaced_by_cached_previous_call(self):
        self.initialize()
        self.assertFalse(self.connections()['result']['isError'])
        self.restart()
        response=self.connections(meta={})
        self.assertTrue(response['result']['isError'])
        self.assertEqual(self.supervisor.controller.pairs.registered(),())
        self.assertFalse(self.connections()['result']['isError'])

    def test_failed_actual_request_is_not_retried_or_cached_during_recovery(self):
        self.initialize()
        self.assertFalse(self.connections()['result']['isError'])
        self.stop_broker()
        response=self.call('tools/call',{'name':'bridge_send','arguments':{
            'operation_id':'never-replayed','recipient':'missing-recipient','text':'owned fixture body'},'_meta':self.meta})
        self.assertIn('error',response)
        self.start_broker()
        self.assertFalse(self.connections()['result']['isError'])
        self.assertIsNone(self.supervisor.controller.outbox.receipt('never-replayed'))

    def test_client_without_actual_initialized_notification_is_not_promoted_on_restart(self):
        response=self.call('initialize',{'protocolVersion':'2025-06-18'})
        self.assertIn('result',response)
        self.restart()
        response=self.connections()
        self.assertIn('error',response)
        self.assertEqual(self.supervisor.controller.pairs.registered(),())

    def test_new_broker_failed_or_malformed_initialize_does_not_forward_actual_tool(self):
        self.initialize()
        self.assertFalse(self.connections()['result']['isError'])
        self.restart()
        original=self.supervisor.service.server.handler
        attempts=[]
        def refuse(peer,frame):
            method=frame.get('request',{}).get('method')
            attempts.append(method)
            if method=='initialize':
                message=frame['request']
                return {'protocol':1,'response':{'jsonrpc':'2.0','id':message['id'],
                    'result':{'protocolVersion':'2025-06-18'}}}  # Missing actual server contract.
            return original(peer,frame)
        self.supervisor.service.server.handler=refuse
        response=self.connections()
        self.assertIn('error',response)
        self.assertEqual(attempts,['initialize'])
        self.assertEqual(self.supervisor.controller.pairs.registered(),())

    def test_failed_recovery_notification_retries_only_notification_then_fresh_call(self):
        self.initialize()
        self.assertFalse(self.connections()['result']['isError'])
        self.restart()
        original=self.supervisor.service.server.handler
        methods,metadata=[],[]
        failed=False
        def fail_once(peer,frame):
            nonlocal failed
            message=frame['request']
            methods.append(message['method'])
            if message['method']=='notifications/initialized' and not failed:
                failed=True
                raise TimeoutError('owned notification unavailable once')
            if message['method']=='tools/call':metadata.append(message['params']['_meta'])
            return original(peer,frame)
        self.supervisor.service.server.handler=fail_once
        self.assertIn('error',self.connections())
        self.assertEqual(self.supervisor.controller.pairs.registered(),())
        # This new actual call has different metadata; the failed call is absent.
        response=self.connections(meta={})
        self.assertTrue(response['result']['isError'],response)
        self.assertFalse(self.connections()['result']['isError'])
        self.assertEqual(methods,['initialize','notifications/initialized',
            'notifications/initialized','tools/call','tools/call'])
        self.assertEqual(metadata,[{},self.meta])

    def test_actual_initialized_after_generation_change_retries_failed_notification(self):
        self.assertIn('result',self.call('initialize',{'protocolVersion':'2025-06-18'}))
        self.restart()
        original=self.supervisor.service.server.handler
        methods=[]
        failed=False
        def fail_once(peer,frame):
            nonlocal failed
            method=frame['request']['method']
            methods.append(method)
            if method=='notifications/initialized' and not failed:
                failed=True
                raise TimeoutError('owned notification unavailable once')
            return original(peer,frame)
        self.supervisor.service.server.handler=fail_once
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        response=self.connections()
        self.assertFalse(response['result']['isError'],response)
        self.assertEqual(methods,['initialize','notifications/initialized',
            'notifications/initialized','tools/call'])
        self.assertEqual(len(self.supervisor.controller.pairs.registered()),1)

    def test_lost_initialize_acknowledgement_does_not_imply_known_initialization(self):
        self.initialize()
        self.assertFalse(self.connections()['result']['isError'])
        self.restart()
        original=self.supervisor.service.server.handler
        methods=[]
        failed=False
        def lose_once(peer,frame):
            nonlocal failed
            methods.append(frame['request']['method'])
            response=original(peer,frame)
            if frame['request']['method']=='initialize' and not failed:
                failed=True
                raise TimeoutError('owned initialize acknowledgement lost')
            return response
        self.supervisor.service.server.handler=lose_once
        self.assertIn('error',self.connections())
        self.assertIn('error',self.connections())
        self.assertEqual(methods,['initialize','initialize'])
        self.assertEqual(self.supervisor.controller.pairs.registered(),())

    def test_queued_and_unknown_operations_survive_restart_without_tool_replay(self):
        self.initialize()
        self.connections()
        original=(self.profile/'sessions'/'2026'/'10'/'12'/('rollout-owned-'+self.thread_id+'.jsonl')).read_bytes()
        target,turn=str(uuid.uuid4()),str(uuid.uuid4())
        copy=original.replace(self.thread_id.encode(),target.encode()).replace(self.turn.encode(),turn.encode())
        (self.profile/'sessions'/'2026'/'10'/'12'/('rollout-owned-'+target+'.jsonl')).write_bytes(copy)
        target_meta={'x-codex-turn-metadata':{**self.meta['x-codex-turn-metadata'],'thread_id':target,'turn_id':turn}}
        self.assertFalse(self.connections(target_meta)['result']['isError'])
        peers={peer.identity.session_id:peer for peer in self.supervisor.controller.pairs.registered()}
        self.supervisor.controller.owner.change(peers[self.thread_id].reference,peers[target].reference,enabled=True)
        for operation in ('durable-queued','durable-unknown'):
            response=self.call('tools/call',{'name':'bridge_send','arguments':{'operation_id':operation,
                'recipient':peers[target].reference,'text':'owned durable body'},'_meta':self.meta})
            self.assertEqual(response['result']['structuredContent']['state'],'queued')
        effects=[]
        def lose_receipt(message):
            effects.append(message.operation_id)
            raise TimeoutError('owned uncertain result')
        self.assertEqual(self.supervisor.controller.outbox.run_dispatch('durable-unknown',lose_receipt).state,'unknown')
        self.restart()
        response=self.call('tools/call',{'name':'bridge_receipt','arguments':{'operation_id':'durable-queued'},'_meta':self.meta})
        self.assertEqual(response['result']['structuredContent']['state'],'queued')
        response=self.call('tools/call',{'name':'bridge_receipt','arguments':{'operation_id':'durable-unknown'},'_meta':self.meta})
        self.assertEqual(response['result']['structuredContent']['state'],'unknown')
        self.assertEqual(effects,['durable-unknown'])
        self.assertEqual(self.supervisor.controller.outbox.pending(),('durable-queued',))


class RecoveryProtocolTests(unittest.TestCase):
    def run_frontend(self,messages,targets,replies):
        encoded=b''.join(json.dumps(message).encode()+b'\n' for message in messages)
        output=io.BytesIO()
        calls=[]
        def forwarded(target,frame):
            calls.append((target,frame))
            return replies(frame,len(calls))
        with patch('optional.bridge.win.frontend.capture_inherited_channel',return_value=None),patch(
                'optional.bridge.win.frontend._generation',side_effect=targets),patch(
                'optional.bridge.win.frontend.request',side_effect=forwarded):
            serve_frontend(io.BytesIO(encoded),output,Path('owned-fixture-marker'))
        return [json.loads(line) for line in output.getvalue().splitlines()],calls

    def test_bounded_initialize_cache_does_not_store_oversize_params(self):
        from optional.bridge.win.transport import PipeTarget
        target=PipeTarget(r'\\.\pipe\LOCAL\owned-protocol-fixture',os.getpid(),own_ctime())
        init={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','large':'x'*20000}}
        response,calls=self.run_frontend([init],[(target,('first',))],lambda frame,count:None)
        self.assertIn('error',response[0])
        self.assertEqual(calls,[])

    def test_marker_generation_change_during_handshake_never_forwards_actual_tool(self):
        from optional.bridge.win.transport import PipeTarget
        target=PipeTarget(r'\\.\pipe\LOCAL\owned-protocol-fixture',os.getpid(),own_ctime())
        first,second,third=(target,('one',)),(target,('two',)),(target,('three',))
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'bridge_send','arguments':{}}}]
        def reply(frame,count):
            message=frame['request']
            if 'id' not in message:return {'protocol':1,'response':None}
            return {'protocol':1,'response':{'jsonrpc':'2.0','id':message['id'],'result':{
                'protocolVersion':'2025-06-18','serverInfo':{'name':'owned','version':'1'},'capabilities':{}}}}
        outputs,calls=self.run_frontend(messages,[first,first,first,first,second,third],reply)
        self.assertIn('error',outputs[-1])
        self.assertEqual([frame['request']['method'] for _,frame in calls],
            ['initialize','notifications/initialized','initialize'])
