"""Claude runtime delivery resolves actual native bindings before owned peer writes."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid

import tests.bridge.test_claude_runtime as fixture
from optional.bridge.hosts.claude.peer import ClaudePeerEndpoint,native_message_id,peer_address
from optional.bridge.hosts.claude.hook import entry_command
from optional.bridge.core.messages import BeforeWriteError,DeliveryError,OutboundMessage
from kitlib.proc.process import own_ctime,proc_ctime

RUNTIME_SOURCE='optional/bridge/hosts/claude/runtime.py'


class ClaudeRuntimeDeliveryTests(unittest.TestCase):
    setUp=fixture.ClaudeRuntimeTests.setUp
    tearDown=fixture.ClaudeRuntimeTests.tearDown
    write=fixture.ClaudeRuntimeTests.write
    cli_mode=fixture.ClaudeRuntimeTests.cli_mode
    frontend=fixture.ClaudeRuntimeTests.frontend
    start_service=fixture.ClaudeRuntimeTests.start_service
    call=fixture.ClaudeRuntimeTests.call
    hook=fixture.ClaudeRuntimeTests.hook

    def recipient(self):
        # Setup, discovery and the helper's start all fall between 'ready' and the write's connect;
        # a loaded gate took longer than 5 s there (KIT-BRIDGE-CLAUDE-RUNTIME-FLAKE). The child is
        # killed with the job at tearDown, so a test that never writes does not wait out the 30 s.
        pipe=r'\\.\pipe\LOCAL\cc-msg-runtime-recipient-'+uuid.uuid4().hex
        code=("import sys; sys.path.insert(0,"+repr(str(self.kit))+" ); "
            "import json,os; from urllib.parse import unquote; from kitlib.proc.pipe import PrivateListener,connect; from tests.bridge.pipes import send,frames_until_close; "
            "listener=PrivateListener("+repr(pipe)+"); print('ready',flush=True); "
            "listener.accept(timeout=30); frames,closed=frames_until_close(listener,5); "
            "print(json.dumps({'frames':frames,'closed':closed}),flush=True); "
            "status=json.loads(sys.stdin.readline()); "
            "connection=connect(status['pipe'],status['pid'],status['creation']); "
            "send(connection,[{'type':'auth','token':status['token']},{'type':'control','action':'peer_message_status',"
            "'status':'held','orig_msg_id':frames[1]['msg_id'],'from':status['from']}]); connection.close(); "
            "sys.stdin.readline(); listener.close()")
        child=self.job.start([sys.executable,'-I','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline(),b'ready\r\n')
        session=str(uuid.uuid4());task='local_'+str(uuid.uuid4())
        cwd=self.root/'recipient-workspace';cwd.mkdir()
        record={'pid':child.pid,'procStartFt':str(proc_ctime(child.pid)),'sessionId':session,'hostSessionId':task,
            'cwd':str(cwd),'peerProtocol':1,'entrypoint':'claude-desktop','messagingSocketPath':pipe}
        self.recipient_record=self.registry/('%d.json'%child.pid)
        self.recipient_record.write_text(json.dumps(record),encoding='utf-8')
        folder=self.profile/'claude-code-sessions'/self.account/str(uuid.uuid4());folder.mkdir()
        self.recipient_task=folder/(task+'.json')
        self.recipient_data={'sessionId':task,'cliSessionId':session,'cwd':str(cwd),'isArchived':False,
            'permissionMode':'auto','title':'owned recipient','sessionPermissionUpdates':[]}
        self.recipient_task.write_text(json.dumps(self.recipient_data),encoding='utf-8')
        key=self.registry/('%d.%s.key'%(child.pid,hashlib.sha256(pipe.lower().encode()).hexdigest()))
        key.write_text(json.dumps({'peerToken':'b'*32,'procStartFt':record['procStartFt']}),encoding='utf-8')
        bindings=self.sources.refresh()
        sender=next(binding for binding in bindings if binding.identity.session_id==self.task)
        receiver=next(binding for binding in bindings if binding.identity.session_id==task)
        self.receiver_process=child
        # The assembled adapter's final boundary rechecks the standing selection.
        self.controller.owner.change(sender.reference,receiver.reference,enabled=True)
        return sender,receiver,pipe

    def endpoint(self):
        registry=self.root/'owned-return-registry';registry.mkdir()
        endpoint=ClaudePeerEndpoint(registry,self.root,self.sources.directory)
        self.addCleanup(endpoint.close)
        return endpoint

    def capture_mode(self,mode,use):
        event={'session_id':self.cli,'prompt_id':str(uuid.uuid4()),'cwd':str(self.root),
            'transcript_path':str(self.transcript),'permission_mode':mode,'hook_event_name':'PreToolUse',
            'tool_name':'mcp__kit__bridge_connections','tool_input':{},'tool_use_id':use,
            'mcp_server':{'name':'kit','source':'plugin'}}
        child=self.job.start(entry_command(self.marker),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        self.children.append(child)
        stdout,stderr=child.communicate(json.dumps(event).encode(),timeout=8)
        self.assertEqual((child.returncode,stdout,stderr),(0,b'',b''))
        return next(binding for binding in self.controller.pairs.registered() if binding.identity.native_session_id==self.cli)

    def test_exact_desktop_resolution_and_mode_change_reject_stale_full_binding(self):
        binding=self.sources.refresh()[0]
        snapshot=self.sources.resolve(binding)
        self.assertEqual(snapshot.native_session_id,self.cli)
        self.assertEqual(self.sources.sender_mode(binding),'prompting')
        self.data['permissionMode']='bypassPermissions';self.write()
        with self.assertRaises(DeliveryError):self.sources.resolve(binding)
        with self.assertRaises(DeliveryError):self.sources.sender_mode(binding)
        changed=self.sources.refresh()[0]
        self.assertEqual(self.sources.sender_mode(changed),'bypass')
        with self.assertRaises(DeliveryError):self.sources.resolve(replace(changed,account_ref='f'*64))

    def test_cli_mode_lookup_requires_validated_hook_and_full_current_binding(self):
        self.cli_mode();self.start_service()
        before=self.capture_mode('auto','toolu_mode_auto')
        self.assertEqual(self.sources.resolve(before).kind,'terminal')
        self.assertEqual(self.sources.sender_mode(before),'prompting')
        after=self.capture_mode('bypassPermissions','toolu_mode_bypass')
        self.assertEqual(self.sources.sender_mode(after),'bypass')
        with self.assertRaises(DeliveryError):self.sources.sender_mode(before)
        with self.assertRaises(DeliveryError):self.sources.resolve(replace(after,permission_revision=after.permission_revision+1))

    def test_actual_owned_peer_bytes_remain_unknown_then_correlated_hold_is_observed(self):
        sender,receiver,pipe=self.recipient()
        endpoint=self.endpoint()
        before=set(self.registry.iterdir())
        callback=[]
        def mode(binding):
            self.assertFalse(self.sources._lock._is_owned())
            callback.append(binding)
            return self.sources.sender_mode(binding)
        adapter=self.sources.delivery_adapter(endpoint,mode)
        self.assertEqual(set(self.registry.iterdir()),before)
        self.assertFalse(self.controller.core.adapters)
        message=OutboundMessage('runtime-delivery',self.task,sender,receiver,'owned peer body')
        with self.assertRaisesRegex(DeliveryError,'native-outcome-unconfirmed'):adapter.send(message)
        packet=json.loads(self.receiver_process.stdout.readline())
        self.assertTrue(packet['closed'])
        self.assertEqual(packet['frames'][0],{'type':'auth','token':'b'*32})
        self.assertEqual(packet['frames'][1]['msg_id'],native_message_id(message))
        self.assertIn('from-mode="prompting"',packet['frames'][1]['message']['content'])
        self.assertIn(message.text,packet['frames'][1]['message']['content'])
        self.assertEqual(callback,[sender])
        self.assertIsNone(adapter.reconcile(message,None))
        token=json.loads(endpoint.key_path.read_text())['peerToken']
        self.receiver_process.stdin.write(json.dumps({'pipe':endpoint.pipe,'pid':os.getpid(),'creation':own_ctime(),
            'token':token,'from':peer_address(pipe)}).encode()+b'\n');self.receiver_process.stdin.flush()
        deadline=time.monotonic()+1
        result=None
        while time.monotonic()<deadline:
            result=adapter.reconcile(message,None)
            if result is not None:break
            time.sleep(.005)
        self.assertIsNotNone(result)
        self.assertEqual(result.state,'held')
        self.assertNotEqual(result.state,'recipient-accepted')

    def test_root_mode_resolver_source_drift_is_rejected_before_native_bytes(self):
        sender,receiver,_=self.recipient()
        endpoint=self.endpoint()
        def mode(binding):
            self.assertFalse(self.sources._lock._is_owned())
            current=self.sources.sender_mode(binding)
            # The permission class is still prompting. The full native binding
            # nevertheless changed, which only an independent live check sees.
            self.data['permissionMode']='default'
            self.write()
            return current
        adapter=self.sources.delivery_adapter(endpoint,mode)
        message=OutboundMessage('source-drift-delivery',self.task,sender,receiver,'must stay private')
        with self.assertRaises(BeforeWriteError):adapter.send(message)
        self.assertEqual(endpoint._pending,{})

    def test_root_mode_resolver_recipient_drift_is_checked_again_before_native_bytes(self):
        sender,receiver,_=self.recipient()
        endpoint=self.endpoint()
        def mode(binding):
            self.assertFalse(self.sources._lock._is_owned())
            current=self.sources.sender_mode(binding)
            self.recipient_data['permissionMode']='default'
            self.recipient_task.write_text(json.dumps(self.recipient_data),encoding='utf-8')
            return current
        adapter=self.sources.delivery_adapter(endpoint,mode)
        message=OutboundMessage('drift-delivery',self.task,sender,receiver,'must stay private')
        with self.assertRaises(BeforeWriteError):adapter.send(message)
        self.assertEqual(endpoint._pending,{})

    def test_missing_external_sender_mode_is_denied_without_schema_or_default_override(self):
        sender,receiver,_=self.recipient()
        endpoint=self.endpoint()
        message=OutboundMessage('unknown-mode',self.task,sender,receiver,'must stay private')
        with self.assertRaises(DeliveryError):self.sources.delivery_adapter(endpoint,None)
        adapter=self.sources.delivery_adapter(endpoint,lambda binding:None)
        with self.assertRaises(BeforeWriteError):adapter.send(message)
        self.assertEqual(endpoint._pending,{})

    def test_closed_shared_controller_cannot_supply_native_resolution_or_mode(self):
        binding=self.sources.refresh()[0]
        self.controller.close()
        with self.assertRaises(DeliveryError):self.sources.resolve(binding)
        with self.assertRaises(DeliveryError):self.sources.sender_mode(binding)

    def test_account_workspace_and_closed_provider_make_resolution_unavailable(self):
        binding=self.sources.refresh()[0]
        self.config['windowSizeWasSignedIn']=False;self.write()
        with self.assertRaises(DeliveryError):self.sources.resolve(binding)
        self.config['windowSizeWasSignedIn']=True;self.data['cwd']=str(self.state);self.write()
        with self.assertRaises(DeliveryError):self.sources.resolve(binding)
        self.sources.close()
        with self.assertRaises(DeliveryError):self.sources.sender_mode(binding)
