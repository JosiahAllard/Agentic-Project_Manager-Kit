"""Owned Windows processes exercise terminal hook evidence and shared caller proofs."""
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from optional.bridge.hosts.claude.terminal import ClaudeTerminal, TerminalCalls, TerminalError
from optional.bridge.hosts.claude.sessions import ClaudeDirectory
from optional.bridge.hosts.claude.hook import produce
from optional.bridge.core.contracts import SessionIdentity, WorkspaceIdentity
from optional.bridge.core.messages import MessageIntent
from optional.bridge.core.pairing import ChatBinding
from kitlib.proc.process import own_ctime, proc_ctime, terminate_verified

NATIVE_SOURCE='optional/bridge/hosts/claude/terminal.py'
HOOK_SOURCE='optional/bridge/hosts/claude/hook.py'


@dataclass(frozen=True)
class Peer:
    pid:int
    creation:int


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-terminal-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.registry=self.root/'sessions'
        self.registry.mkdir()
        self.account_file=self.root/'.claude.json'
        self.account={'oauthAccount':{'accountUuid':str(uuid.uuid4()),
            'organizationUuid':str(uuid.uuid4()),'emailAddress':'private@example.invalid'}}
        self.account_file.write_text(json.dumps(self.account),encoding='utf-8')
        self.session=str(uuid.uuid4())
        self.record={'pid':os.getpid(),'procStartFt':str(own_ctime()),'sessionId':self.session,
            'cwd':str(self.root),'peerProtocol':1,'entrypoint':'cli',
            'messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-owned-terminal'}
        self.record_file=self.registry/('%d.json'%os.getpid())
        self.write_record()
        self.transcripts=self.root/'.claude'/'projects'
        self.transcript_dir=self.transcripts/'encoded-workspace'
        self.transcript_dir.mkdir(parents=True)
        self.transcript=self.transcript_dir/(self.session+'.jsonl')
        self.transcript.write_text('{}\n',encoding='utf-8')
        self.terminal=ClaudeTerminal(self.account_file,transcript_root=self.transcripts)
        self.directory=ClaudeDirectory(self.registry,sys.executable,account_probe=self.terminal.account_probe)
        self.addCleanup(self.directory.close)
        self.now=[100.0]
        self.recipient=ChatBinding(SessionIdentity('recipient','recipient','test-adapter','test-incarnation',
            WorkspaceIdentity('test-host','test-project','test-workspace')),'test-account',1)
        self.args={'operation_id':'operation-1','recipient':self.recipient.reference,'text':'private body'}
        self.intent=MessageIntent('operation-1',self.recipient,hashlib.sha256(b'private body').hexdigest())
        self.event={'session_id':self.session,'prompt_id':str(uuid.uuid4()),'cwd':str(self.root),
            'transcript_path':str(self.transcript),'permission_mode':'auto','hook_event_name':'PreToolUse','tool_name':'mcp__kit__bridge_send',
            'tool_input':self.args,'tool_use_id':'toolu_owned',
            'mcp_server':{'name':'kit','source':'plugin'}}
        # A real child runs the producer and remains alive for kernel checks.
        self.producer_code=("import sys; from optional.bridge.hosts.claude.hook import produce; "
            "produce(sys.stdin.buffer,lambda value: (sys.stdout.buffer.write(value+b'\\n'),"
            "sys.stdout.buffer.flush())); __import__('threading').Event().wait(30)")
        self.argv=[sys.executable,'-c',self.producer_code]
        self.calls=TerminalCalls(self.directory,self.terminal,'test-host',os.getpid(),own_ctime(),
            hook_argv=self.argv,bridge_tools={'mcp__kit__bridge_send':{'name':'kit','source':'plugin'}},
            clock=lambda:self.now[0])
        self.children=[]
        self.addCleanup(self.stop_children)

    def write_record(self):
        self.record_file.write_text(json.dumps(self.record),encoding='utf-8')

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.stdin.close()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()

    def capture(self,event=None):
        child=subprocess.Popen(self.argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parents[2],creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(child)
        payload=json.dumps(self.event if event is None else event).encode()
        child.stdin.write(payload+b'\n')
        child.stdin.close()
        envelope=child.stdout.readline()
        self.assertTrue(envelope,child.stderr.read() if child.poll() is not None else b'producer missing')
        peer=Peer(child.pid,proc_ctime(child.pid))
        return child,peer,envelope

    def test_native_account_discovery_is_read_only_and_does_not_guess_permissions(self):
        before=self.account_file.read_bytes()
        snapshot=self.directory.discover()[0]
        self.assertEqual(snapshot.kind,'terminal')
        self.assertEqual(snapshot.native_session_id,self.session)
        self.assertEqual(snapshot.account_ref,self.terminal.observe_account())
        with self.assertRaisesRegex(TerminalError,'hook-context-required'):
            self.calls.metadata('mcp__kit__bridge_send',self.args)
        self.assertEqual(before,self.account_file.read_bytes())

    def test_real_hook_producer_binds_exact_cli_and_consumes_shared_intent_proof(self):
        _,peer,envelope=self.capture()
        ticket=self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        authority=self.calls.observe(metadata)
        self.assertEqual(authority.sender.identity.session_id,self.session)
        self.assertEqual(authority.task_root,self.session)
        self.assertEqual(authority.sender.identity.adapter_id,'claude-peer')
        self.assertNotIn('private body',repr(metadata))
        proof=self.calls.issue(self.intent,metadata)
        self.assertEqual(self.calls.proofs.authorize(self.intent,proof),authority)
        self.assertIsNone(self.calls.proofs.authorize(self.intent,proof))
        with self.assertRaises(TerminalError): self.calls.issue(self.intent,metadata)

    def test_self_claimed_identity_and_wrong_process_creation_cannot_capture(self):
        _,peer,envelope=self.capture()
        with self.assertRaises(TerminalError):
            self.calls.observe_hook(Peer(peer.pid,peer.creation+1),envelope)
        with self.assertRaises(TerminalError):
            self.calls.observe_hook({'pid':peer.pid,'creation':peer.creation},envelope)
        with self.assertRaises(TerminalError):
            self.calls.observe({'sender':self.session,'owner':True,'task_root':self.session})

    def test_wrong_native_session_workspace_or_missing_mode_is_explicitly_unavailable(self):
        for key,value in [('session_id',str(uuid.uuid4())),('cwd',str(self.registry)),
                ('permission_mode',None),('permission_mode','plan'),('prompt_id',None),('tool_use_id',None)]:
            with self.subTest(field=key,value=value):
                event=dict(self.event,**{key:value})
                _,peer,envelope=self.capture(event)
                with self.assertRaises(TerminalError): self.calls.observe_hook(peer,envelope)

    def test_tool_provenance_input_sender_override_and_intent_mutation_are_denied(self):
        for event in [dict(self.event,mcp_server={'name':'kit','source':'project'}),
                dict(self.event,tool_name='mcp__other__bridge_send'),
                dict(self.event,tool_input=dict(self.args,sender=self.session))]:
            _,peer,envelope=self.capture(event)
            with self.assertRaises(TerminalError): self.calls.observe_hook(peer,envelope)
        _,peer,envelope=self.capture()
        ticket=self.calls.observe_hook(peer,envelope)
        with self.assertRaises(TerminalError):
            self.calls.metadata('mcp__kit__bridge_send',dict(self.args,text='altered'),ticket=ticket)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        wrong=MessageIntent(self.intent.operation_id,self.recipient,hashlib.sha256(b'altered').hexdigest())
        with self.assertRaises(TerminalError): self.calls.issue(wrong,metadata)

    def test_account_permission_and_native_registration_changes_invalidate_context(self):
        _,peer,envelope=self.capture()
        ticket=self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        initial=self.calls.observe(metadata).sender
        self.account['oauthAccount']['accountUuid']=str(uuid.uuid4())
        self.account_file.write_text(json.dumps(self.account),encoding='utf-8')
        with self.assertRaises(TerminalError): self.calls.observe(metadata)
        _,peer,envelope=self.capture(dict(self.event,permission_mode='bypassPermissions',tool_use_id='toolu_next'))
        ticket=self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        changed=self.calls.observe(metadata).sender
        self.assertNotEqual(initial,changed)
        self.record['sessionId']=str(uuid.uuid4())
        self.write_record()
        with self.assertRaises(TerminalError): self.calls.observe(metadata)

    def test_hook_tool_use_replay_and_expired_capture_are_rejected(self):
        _,peer,envelope=self.capture()
        ticket=self.calls.observe_hook(peer,envelope)
        with self.assertRaisesRegex(TerminalError,'hook-replay'):
            self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        self.now[0]+=5
        with self.assertRaisesRegex(TerminalError,'hook-context-expired-or-mismatched'): self.calls.observe(metadata)
        with self.assertRaisesRegex(TerminalError,'hook-replay'):
            self.calls.observe_hook(peer,envelope)

    def test_expired_hook_context_never_admits_a_later_request(self):
        _,peer,envelope=self.capture()
        self.calls.observe_hook(peer,envelope)
        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'bridge_send','arguments':self.args}}
        self.now[0]+=5
        with self.assertRaisesRegex(TerminalError,'hook-context-required'): self.calls.prepare_request(request)

    def test_private_request_can_observe_twice_but_consume_only_one_native_tool_call(self):
        _,peer,envelope=self.capture()
        self.calls.observe_hook(peer,envelope)
        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'bridge_send','arguments':self.args,'_meta':{'sender':'pretend-owner'}}}
        metadata,consume=self.calls.prepare_request(request)
        authority=self.calls.proofs.observe(metadata)
        self.assertEqual(self.calls.proofs.observe(metadata),authority)
        proof=self.calls.proofs.issue(self.intent,metadata)
        self.assertEqual(self.calls.proofs.authorize(self.intent,proof),authority)
        with self.assertRaises(TerminalError): self.calls.prepare_request(request)
        consume()
        consume()
        with self.assertRaises(TerminalError): self.calls.observe(metadata)
        with self.assertRaises(TerminalError): self.calls.prepare_request(request)

    def test_request_wrong_arguments_or_notification_never_borrows_pending_hook(self):
        _,peer,envelope=self.capture()
        self.calls.observe_hook(peer,envelope)
        for request in [
                {'jsonrpc':'2.0','method':'tools/call','params':{'name':'bridge_send','arguments':self.args}},
                {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'bridge_send',
                    'arguments':dict(self.args,text='changed')}}]:
            with self.assertRaises(TerminalError): self.calls.prepare_request(request)

    def new_calls(self,argv):
        return TerminalCalls(self.directory,self.terminal,'test-host',os.getpid(),own_ctime(),
            hook_argv=argv,bridge_tools={'mcp__kit__bridge_send':{'name':'kit','source':'plugin'}},
            clock=lambda:self.now[0])

    def script_producer(self,extra=''):
        script=self.root/'owned-hook.py'
        source='import sys; sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[2]))+'); '
        script.write_text(source+self.producer_code+extra,encoding='utf-8')
        self.argv=[sys.executable,'-I',str(script)]
        self.calls=self.new_calls(self.argv)
        return script

    def test_transcript_profile_path_session_file_and_presence_are_verified(self):
        outside=self.root/'other-profile'/self.transcript.name
        outside.parent.mkdir()
        outside.write_text('{}',encoding='utf-8')
        wrong=self.transcript_dir/(str(uuid.uuid4())+'.jsonl')
        wrong.write_text('{}',encoding='utf-8')
        for path in [None,str(outside),str(wrong),str(self.transcripts/self.transcript.name)]:
            _,peer,envelope=self.capture(dict(self.event,transcript_path=path))
            with self.assertRaises(TerminalError): self.calls.observe_hook(peer,envelope)
        self.transcript.unlink()
        _,peer,envelope=self.capture()
        with self.assertRaises(TerminalError): self.calls.observe_hook(peer,envelope)

    def test_transcript_replacement_after_capture_invalidates_private_metadata(self):
        _,peer,envelope=self.capture()
        ticket=self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        replacement=self.transcript.with_suffix('.replacement')
        replacement.write_text('{}',encoding='utf-8')
        replacement.replace(self.transcript)
        with self.assertRaises(TerminalError): self.calls.observe(metadata)

    def test_isolated_python_real_hook_script_is_pinned_against_mutation(self):
        script=self.script_producer()
        _,peer,envelope=self.capture()
        script.write_text(script.read_text(encoding='utf-8')+'\n# changed\n',encoding='utf-8')
        with self.assertRaisesRegex(TerminalError,'hook-producer-script-changed'):
            self.calls.observe_hook(peer,envelope)

    def test_real_rpc_helper_child_of_hook_is_allowed_by_exact_native_ancestry(self):
        rpc_code="import os,sys,threading; print(os.getpid(),flush=True); threading.Event().wait(30)"
        source=("import subprocess,sys; sys.path.insert(0,"+repr(str(Path(__file__).resolve().parents[2]))+"); "
            "from optional.bridge.hosts.claude.hook import produce; "
            "child=subprocess.Popen([sys.executable,'-c',"+repr(rpc_code)+"],stdout=subprocess.PIPE); "
            "sys.stdout.buffer.write(child.stdout.readline()); sys.stdout.buffer.flush(); "
            "produce(sys.stdin.buffer,lambda value: (sys.stdout.buffer.write(value+b'\\n'),sys.stdout.buffer.flush())); "
            "child.wait()")
        script=self.root/'owned-hook.py'
        script.write_text(source,encoding='utf-8')
        self.argv=[sys.executable,'-I',str(script)]
        self.calls=self.new_calls(self.argv)
        hook=subprocess.Popen(self.argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(hook)
        rpc_pid=int(hook.stdout.readline())
        rpc_creation=proc_ctime(rpc_pid)
        self.addCleanup(terminate_verified,rpc_pid,rpc_creation)
        hook.stdin.write(json.dumps(self.event).encode())
        hook.stdin.close()
        envelope=hook.stdout.readline()
        ticket=self.calls.observe_hook(Peer(rpc_pid,rpc_creation),envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        self.assertEqual(self.calls.observe(metadata).task_root,self.session)

    def test_unregistered_intermediate_parent_and_closer_registry_cannot_borrow_outer_chat(self):
        wrapper_code=('import subprocess,sys; child=subprocess.Popen('+repr(self.argv)+
            ',stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr); print(child.pid,flush=True); child.wait()')
        wrapper=subprocess.Popen([sys.executable,'-c',wrapper_code],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(wrapper)
        producer_pid=int(wrapper.stdout.readline())
        producer_creation=proc_ctime(producer_pid)
        self.addCleanup(terminate_verified,producer_pid,producer_creation)
        wrapper.stdin.write(json.dumps(self.event).encode())
        wrapper.stdin.close()
        envelope=wrapper.stdout.readline()
        with self.assertRaisesRegex(TerminalError,'hook-native-parent-mismatch'):
            self.calls.observe_hook(Peer(producer_pid,producer_creation),envelope)
        # A closer unverified registry must not be silently skipped for an outer native chat.
        (self.registry/('%d.json'%wrapper.pid)).write_text(json.dumps({
            'entrypoint':'cli','sessionId':'inner-unverified'}),encoding='utf-8')
        with self.assertRaisesRegex(TerminalError,'closest-native-caller-unverified'):
            self.calls._closest(producer_pid,producer_creation)

    def test_mode_change_alone_revokes_old_binding_but_new_prompt_keeps_same_mode_binding(self):
        _,peer,envelope=self.capture()
        ticket=self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        first=self.calls.observe(metadata).sender
        self.assertTrue(self.calls.require_live(first))
        self.calls.issue(self.intent,metadata)
        _,peer,envelope=self.capture(dict(self.event,prompt_id=str(uuid.uuid4()),tool_use_id='toolu_same_mode'))
        ticket=self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        self.assertEqual(first,self.calls.observe(metadata).sender)
        self.calls.issue(self.intent,metadata)
        _,peer,envelope=self.capture(dict(self.event,permission_mode='default',tool_use_id='toolu_changed_mode'))
        ticket=self.calls.observe_hook(peer,envelope)
        metadata=self.calls.metadata('mcp__kit__bridge_send',self.args,ticket=ticket)
        changed=self.calls.observe(metadata).sender
        self.assertNotEqual(first,changed)
        self.assertFalse(self.calls.require_live(first))
        self.assertTrue(self.calls.require_live(changed))
        self.account['oauthAccount']['organizationUuid']=str(uuid.uuid4())
        self.account_file.write_text(json.dumps(self.account),encoding='utf-8')
        self.assertFalse(self.calls.require_live(changed))

    def test_native_frontend_wrong_creation_and_unrelated_producer_command_are_denied(self):
        _,peer,envelope=self.capture()
        self.calls.creation+=1
        with self.assertRaises(TerminalError): self.calls.observe_hook(peer,envelope)
        self.calls.creation-=1
        self.calls.hook_command='unrelated command'
        with self.assertRaises(TerminalError): self.calls.observe_hook(peer,envelope)

    def test_account_missing_duplicate_or_foreign_native_entry_denies_discovery(self):
        for data in [b'{}',b'{"oauthAccount":{},"oauthAccount":{}}']:
            self.account_file.write_bytes(data)
            with self.assertRaises(TerminalError): self.terminal.observe_account()
            self.assertEqual(self.directory.discover(),())
        self.account_file.write_text(json.dumps(self.account),encoding='utf-8')
        self.record['entrypoint']='claude-desktop'
        self.write_record()
        self.assertEqual(self.directory.discover(),())


class ProducerTests(unittest.TestCase):
    def test_executable_hook_entry_preserves_native_flow_without_private_service(self):
        script=Path(__file__).resolve().parents[2]/'optional'/'bridge'/'hosts'/'claude'/'hook.py'
        child=subprocess.run([sys.executable,'-I',str(script)],input=b'{}',capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
        self.assertEqual(child.returncode,0)
        self.assertEqual(child.stdout,b'')
        self.assertEqual(child.stderr,b'')

    def test_bounded_native_envelope_is_private_and_does_not_allow_or_change_permissions(self):
        payload=b'{"hook_event_name":"PreToolUse","permission_mode":"auto"}'
        sink=[]
        self.assertEqual(produce(io.BytesIO(payload+b'\n'),sink.append),0)
        self.assertEqual(json.loads(sink[0]),json.loads(payload))
        self.assertNotIn(b'permissionDecision',sink[0])
        pretty=io.BytesIO(json.dumps(json.loads(payload),indent=2).encode())
        self.assertEqual(produce(pretty,sink.append),0)
        self.assertEqual(sink[0],sink[1])

    def test_malformed_duplicate_oversized_multiline_and_nonfinite_input_never_publish(self):
        for payload in [b'{}\n{}\n',b'{"x":1,"x":2}\n',b'{"x":NaN}\n',b'x'*262145,b'[]\n']:
            sink=[]
            with self.assertRaises(TerminalError): produce(io.BytesIO(payload),sink.append)
            self.assertEqual(sink,[])
