"""Direct Codex-source delivery using populated runtime and owned native RPC pipes."""
import ctypes
from ctypes import wintypes as w
from dataclasses import replace
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import unittest
import uuid

from optional.bridge.hosts.codex.runtime import CodexSources
from optional.bridge.core.contracts import Capability
from optional.bridge.core.messages import BeforeWriteError,DeliveryError,OutboundMessage,reply_operation_id
from kitlib.proc.pipe import (INVALID,SecurityAttributes,connect_pipe,create_file,create_pipe,
    disconnect_pipe,from_sddl,local_free,peek,process_sids,read,write,close)
from optional.bridge.win.channel import NativeChannel
from optional.bridge.win.frontend import NativeFrontend
from optional.bridge.win.transport import PipeTarget
from kitlib.proc.process import own_ctime,proc_ctime
from tests.bridge import test_codex_runtime as runtime_fixtures
from tests.bridge.test_pairing import chat

ERROR_BROKEN_PIPE=109


class OwnedNativeRPC:
    def __init__(self, target):
        self.name=r'\\.\pipe\LOCAL\codex-runtime-rpc-'+uuid.uuid4().hex
        self.target,self.requests,self.errors,self.handles,self.buffers=target,[],[],[],{}
        self.stop=threading.Event()
        user=process_sids(os.getpid())[0]
        security=w.LPVOID()
        if not from_sddl('O:%sD:P(A;;GA;;;%s)'%(user,user),1,ctypes.byref(security),None):
            raise ValueError('fixture ACL')
        try:
            attributes=SecurityAttributes(ctypes.sizeof(SecurityAttributes),security,False)
            for index in range(16):
                handle=create_pipe(self.name,0x00080003 if index==0 else 3,1,16,4096,4096,0,ctypes.byref(attributes))
                if handle==INVALID: raise ValueError('fixture pipe')
                self.handles.append(handle)
                self.buffers[handle]=bytearray()
                connect_pipe(handle,None)
        finally: local_free(security)
        self.response='success'
        self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set():
                for handle in self.handles:
                    connect_pipe(handle,None)
                    available=w.DWORD()
                    if not peek(handle,None,0,None,ctypes.byref(available),None):
                        # A server peek also fails on an instance that is still listening, and a
                        # client may connect to it at any moment. Recycle only an instance whose
                        # client has closed its end (ERROR_BROKEN_PIPE); disconnecting any other
                        # cuts off a client mid-verification. A connected metadata-only client
                        # peeks fine, so it is never recycled while it is open.
                        if ctypes.get_last_error()==ERROR_BROKEN_PIPE:
                            disconnect_pipe(handle)
                            connect_pipe(handle,None)
                            self.buffers[handle].clear()
                        continue
                    if not available.value: continue
                    buffer=ctypes.create_string_buffer(min(4096,available.value))
                    count=w.DWORD()
                    if not read(handle,buffer,len(buffer),ctypes.byref(count),None): continue
                    data=self.buffers[handle]
                    data.extend(buffer.raw[:count.value])
                    if len(data)<4 or len(data)<4+struct.unpack('<I',data[:4])[0]: continue
                    request=json.loads(data[4:].decode('utf-8'))
                    self.requests.append(request)
                    result={'success':True,'structuredContent':{'threadId':self.target}}
                    if self.response=='malformed': result['content']=[{'type':'text','text':'{"isError":true}'}]
                    output=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
                    packet=struct.pack('<I',len(output))+output
                    written=w.DWORD()
                    write(handle,packet,len(packet),ctypes.byref(written),None)
                    data.clear()
                time.sleep(.002)
        except Exception as error:
            self.errors.append(type(error).__name__)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        for handle in self.handles: close(handle)
        if self.thread.is_alive() or self.errors: raise AssertionError('fixture server failed')


class DirectRuntimeDeliveryTests(unittest.TestCase):
    write=runtime_fixtures.CodexRuntimeTests.write
    write_login=runtime_fixtures.CodexRuntimeTests.write_login
    tearDown=runtime_fixtures.CodexRuntimeTests.tearDown
    def setUp(self):
        runtime_fixtures.CodexRuntimeTests.setUp(self)
        self.factory.close()
        self.factory=CodexSources(self.controller,self.profile,'fixture-host',desktop_executable=Path(sys.executable))
        self.source=self.factory(self.native)

    def populate(self):
        target_thread,target_turn=str(uuid.uuid4()),str(uuid.uuid4())
        self.rpc=OwnedNativeRPC(target_thread)
        self.addCleanup(self.rpc.close)
        command=('import subprocess,sys,time,json; p=subprocess.Popen([sys.executable,"-I","-c",'
            '"import time; time.sleep(60)"],creationflags=0x08000000); '
            'print(json.dumps({"frontend_pid":p.pid}),flush=True); time.sleep(60)')
        host=self.job.start([sys.executable,'-I','-c',command],stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        self.channel_hosts.append(host)
        self.addCleanup(host.stdout.close)
        child=json.loads(host.stdout.readline(1024))['frontend_pid']
        channel=NativeChannel(PipeTarget(self.rpc.name,os.getpid(),own_ctime()),Path(sys.executable).resolve())
        native=NativeFrontend(child,proc_ctime(child),host.pid,proc_ctime(host.pid),'codex',
            Path(sys.executable).resolve(),channel)
        self.direct_source=self.factory(native)
        self.sender=self.direct_source.observe(self.metadata).sender
        session={**self.session,'id':target_thread}
        context={**self.context,'turn_id':target_turn}
        self.target_path=self.path.with_name('rollout-target-'+target_thread+'.jsonl')
        def line(kind,payload):
            return json.dumps({'timestamp':'2026-10-12T12:00:00Z','type':kind,'payload':payload},
                separators=(',',':')).encode()+b'\n'
        self.target_path.write_bytes(line('session_meta',session)+line('turn_context',context)+
            line('event_msg',{'type':'task_started','turn_id':target_turn}))
        self.target_meta={'x-codex-turn-metadata':{**self.metadata['x-codex-turn-metadata'],
            'thread_id':target_thread,'turn_id':target_turn}}
        self.recipient=self.direct_source.observe(self.target_meta).sender
        with self.target_path.open('ab') as stream:
            stream.write(line('event_msg',{'type':'task_completed','turn_id':target_turn}))
        self.controller.owner.change(self.sender.reference,self.recipient.reference,enabled=True)
        self.message=OutboundMessage('direct-fixture-op',self.thread,self.sender,self.recipient,'owned fixture body')
        return self.factory.delivery_adapter()

    def test_concrete_provider_dispatches_native_frame_within_existing_core_selected_guard(self):
        adapter=self.populate()
        self.assertIn(Capability.SEND,adapter.describe().capabilities)
        with self.controller.pairs.selected_guard(self.sender,self.recipient):
            result=adapter.send(self.message)
        self.assertEqual(result.state,'recipient-accepted')
        self.assertEqual(len(self.rpc.requests),1)
        request=self.rpc.requests[0]
        self.assertEqual(request['params']['threadId'],self.sender.identity.native_session_id)
        arguments=request['params']['arguments']
        self.assertEqual(arguments['threadId'],self.recipient.identity.native_session_id)
        header,body=arguments['prompt'].split('\n\n',1)
        self.assertEqual(header.splitlines()[0],'Bridge operation: '+self.message.operation_id)
        self.assertIn('bridge_reply',header)
        self.assertEqual(body,self.message.text)
        self.assertEqual(request['params']['tool'],'send_message_to_thread')
        self.assertIsNone(adapter.reconcile(self.message,result.native_id))
        self.assertEqual(len(self.rpc.requests),1)

    def test_configured_image_or_verified_facility_absence_has_no_send_capability(self):
        self.assertNotIn(Capability.SEND,self.factory.delivery_adapter().describe().capabilities)
        other=CodexSources(self.controller,self.profile,'fixture-host')
        self.addCleanup(other.close)
        self.assertNotIn(Capability.SEND,other.delivery_adapter().describe().capabilities)

    def test_source_or_target_context_drift_prevents_any_native_message_bytes(self):
        adapter=self.populate()
        self.context['approval_policy']='on-request'
        self.write()
        with self.assertRaises(BeforeWriteError): adapter.send(self.message)
        self.assertEqual(self.rpc.requests,[])
        self.context['approval_policy']='never'
        self.write()
        self.target_path.write_bytes(self.target_path.read_bytes().replace(b'"approval_policy":"never"',
            b'"approval_policy":"on-request"'))
        with self.assertRaises(BeforeWriteError): adapter.send(self.message)
        self.assertEqual(self.rpc.requests,[])

    def test_known_capture_epoch_change_after_lease_invalidates_before_write(self):
        self.populate()
        with self.factory._delivery_lease(self.message) as lease:
            self.metadata['x-codex-turn-metadata']['auto_review_enabled']=False
            self.direct_source.observe(self.metadata)
            self.assertFalse(lease.recheck())
        self.assertEqual(self.rpc.requests,[])

    def test_self_target_or_different_account_cannot_acquire_native_executor(self):
        adapter=self.populate()
        for message in (replace(self.message,recipient=self.sender),
                replace(self.message,sender=replace(self.sender,account_ref='other-account'))):
            with self.assertRaises(BeforeWriteError): adapter.send(message)
        self.assertEqual(self.rpc.requests,[])

    def test_explicit_desktop_image_mismatch_and_channel_mismatch_have_no_native_write(self):
        adapter=self.populate()
        self.factory.desktop_executable=Path(__file__).resolve()
        self.assertNotIn(Capability.SEND,adapter.describe().capabilities)
        with self.assertRaises(BeforeWriteError): adapter.send(self.message)
        self.assertEqual(self.rpc.requests,[])

    def test_current_native_login_change_prevents_direct_write(self):
        adapter=self.populate()
        self.write_login(user='different-current-user')
        with self.assertRaises(BeforeWriteError): adapter.send(self.message)
        self.assertEqual(self.rpc.requests,[])

    def test_distinct_app_channel_cannot_be_used_for_selected_target(self):
        self.populate()
        second=OwnedNativeRPC(self.recipient.identity.native_session_id)
        self.addCleanup(second.close)
        # A second app-owned endpoint is still the wrong scheduling channel.
        runtime=self.direct_source.observe.__self__
        previous=runtime.native
        replacement=NativeChannel(PipeTarget(second.name,os.getpid(),own_ctime()),Path(sys.executable).resolve())
        runtime.native=replace(previous,channel=replacement)
        with self.factory._delivery_lease(self.message) as lease:
            runtime.native=previous
            self.assertFalse(lease.recheck())
        self.assertEqual(self.rpc.requests,[])
        self.assertEqual(second.requests,[])

    def test_different_target_frontend_app_endpoint_denies_before_any_native_body(self):
        adapter=self.populate()
        other_source,native,listener=runtime_fixtures.CodexRuntimeTests.channel_source(self)
        event={'timestamp':'2026-10-12T12:00:00Z','type':'event_msg','payload':{
            'type':'task_started','turn_id':self.target_meta['x-codex-turn-metadata']['turn_id']}}
        with self.target_path.open('ab') as stream: stream.write(json.dumps(event).encode()+b'\n')
        other_target=other_source.observe(self.target_meta).sender
        self.controller.pairs.require_selected(self.sender,other_target)
        message=replace(self.message,recipient=other_target)
        with self.assertRaises(BeforeWriteError): adapter.send(message)
        self.assertEqual(self.rpc.requests,[])

    def complete_source(self,kind='task_completed'):
        event={'timestamp':'2026-10-12T12:00:00Z','type':'event_msg','payload':{
            'type':kind,'turn_id':self.turn}}
        with self.path.open('ab') as stream: stream.write(json.dumps(event).encode()+b'\n')

    def test_completed_source_is_live_identity_but_cannot_be_native_executor(self):
        adapter=self.populate()
        self.complete_source()
        self.assertTrue(self.controller._live(self.sender))
        self.assertTrue(self.controller._live(self.recipient))
        with self.assertRaises(BeforeWriteError): adapter.send(self.message)
        self.assertEqual(self.rpc.requests,[])

    def test_source_finishing_after_lease_capture_invalidates_before_write(self):
        self.populate()
        with self.factory._delivery_lease(self.message) as lease:
            self.assertTrue(lease.recheck())
            self.complete_source()
            self.assertFalse(lease.recheck())
        self.assertEqual(self.rpc.requests,[])

    def test_source_finishes_after_helper_connect_and_no_body_is_dispatched(self):
        from unittest.mock import patch
        from optional.bridge.hosts.codex.delivery_transport import NativeToolsPipe
        adapter=self.populate()
        original=NativeToolsPipe.call
        def call(pipe,endpoint,request, *, before_write):
            def finish_source_then_check():
                self.complete_source('task_aborted')
                return before_write()
            return original(pipe,endpoint,request,before_write=finish_source_then_check)
        with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call',call):
            with self.assertRaises(BeforeWriteError): adapter.send(self.message)
        self.assertEqual(self.rpc.requests,[])

    def test_contradictory_native_result_remains_uncertain_after_single_message(self):
        adapter=self.populate()
        self.rpc.response='malformed'
        with self.assertRaises(DeliveryError) as caught: adapter.send(self.message)
        self.assertNotIsInstance(caught.exception,BeforeWriteError)
        self.assertEqual(len(self.rpc.requests),1)


class LeasedWriterDeliveryTests(unittest.TestCase):
    """Another host's sender reaches a Codex chat only through an owner-leased Codex writer."""
    write=DirectRuntimeDeliveryTests.write
    write_login=DirectRuntimeDeliveryTests.write_login
    tearDown=DirectRuntimeDeliveryTests.tearDown
    setUp=DirectRuntimeDeliveryTests.setUp
    populate=DirectRuntimeDeliveryTests.populate
    complete_source=DirectRuntimeDeliveryTests.complete_source

    def leased(self):
        # self.sender is the Codex writer W (an active turn); self.recipient is R.
        adapter=self.populate()
        self.other=chat('claude-sender',adapter='claude-desktop')
        self.controller.enroll(self.other,lambda binding:binding==self.other)
        self.controller.owner.change(self.other.reference,self.recipient.reference,enabled=True)
        self.claude_message=OutboundMessage('leased-fixture-op',self.thread,self.other,self.recipient,
            'owned fixture body')
        return adapter

    def grant(self,enabled=True):
        self.controller.owner.lease(self.sender.reference,self.recipient.reference,enabled=enabled)

    def refused(self,adapter,message,code=None):
        with self.assertRaises(BeforeWriteError) as caught: adapter.send(message)
        if code is not None: self.assertEqual(caught.exception.code,code)
        self.assertEqual(self.rpc.requests,[])

    def test_another_hosts_sender_without_a_lease_is_refused_before_any_native_bytes(self):
        adapter=self.leased()
        self.refused(adapter,self.claude_message,'codex-direct-route-unavailable')

    def test_leased_writer_delivers_another_hosts_message_as_the_native_executor(self):
        adapter=self.leased()
        self.grant()
        with self.controller.pairs.selected_guard(self.other,self.recipient):
            result=adapter.send(self.claude_message)
        self.assertEqual(result.state,'recipient-accepted')
        self.assertEqual(len(self.rpc.requests),1)
        params=self.rpc.requests[0]['params']
        self.assertEqual((params['threadId'],params['turnId']),(self.sender.identity.native_session_id,self.turn))
        self.assertEqual(params['arguments']['threadId'],self.recipient.identity.native_session_id)
        self.assertEqual(params['arguments']['prompt'].split('\n\n',1)[1],self.claude_message.text)

    def test_a_reply_return_from_another_host_needs_the_lease_too(self):
        adapter=self.leased()
        reply=OutboundMessage(reply_operation_id('parent-fixture-op'),self.thread,self.other,self.recipient,
            'owned reply body',reply_of='parent-fixture-op')
        self.refused(adapter,reply,'codex-direct-route-unavailable')
        self.grant()
        self.assertEqual(adapter.send(reply).state,'recipient-accepted')
        self.assertEqual(self.rpc.requests[0]['params']['threadId'],self.sender.identity.native_session_id)

    def test_writer_turn_ending_stops_delivery_before_any_bytes(self):
        adapter=self.leased()
        self.grant()
        self.complete_source()
        self.refused(adapter,self.claude_message,'codex-direct-context-unverified')
        self.assertEqual(self.controller.leases.writer(self.recipient),self.sender)  # the lease itself stays

    def test_revoked_lease_invalidates_an_open_scope_before_write_and_time_does_not(self):
        adapter=self.leased()
        self.grant()
        with self.factory._delivery_lease(self.claude_message) as lease:
            self.assertEqual(lease.writer,self.sender)
            self.assertTrue(lease.recheck())
            self.grant(False)
            self.assertFalse(lease.recheck())
        self.grant()
        with self.factory._delivery_lease(self.claude_message) as lease:
            self.assertTrue(lease.recheck())
            self.controller.leases.clock=lambda:time.time()+10*365*24*3600  # no expiry
            self.assertTrue(lease.recheck())
            self.assertTrue(self.controller.owner.revoke(self.sender.reference,self.recipient.reference,by='pm'))
            self.assertFalse(lease.recheck())
        self.refused(adapter,self.claude_message,'codex-direct-route-unavailable')

    def test_a_lease_is_no_route_so_a_deselected_sender_gets_no_bytes(self):
        adapter=self.leased()
        self.grant()
        self.controller.owner.change(self.other.reference,self.recipient.reference,enabled=False)
        self.refused(adapter,self.claude_message)

    def test_a_writer_on_another_account_or_the_recipient_itself_is_refused(self):
        adapter=self.leased()
        for writer in (replace(self.sender,account_ref='other-account'),self.recipient):
            self.controller.leases._leases[self.recipient.reference]=(writer,self.recipient,time.monotonic()+60)
            self.refused(adapter,self.claude_message,'codex-direct-route-unavailable')

    def test_codex_sender_is_unchanged_byte_for_byte_with_a_lease_present(self):
        adapter=self.leased()
        self.assertEqual(adapter.send(self.message).state,'recipient-accepted')
        # A lease naming another host's chat as writer would refuse if a Codex sender used it.
        self.controller.owner.lease(self.other.reference,self.recipient.reference,enabled=True)
        self.assertEqual(self.controller.leases.writer(self.recipient),self.other)
        with self.factory._delivery_lease(self.message) as lease:
            self.assertIsNone(lease.writer)
            self.assertEqual(lease.executor.thread_id,self.sender.identity.native_session_id)
        self.assertEqual(adapter.send(self.message).state,'recipient-accepted')
        self.assertEqual(len(self.rpc.requests),2)
        self.assertEqual(self.rpc.requests[0],self.rpc.requests[1])
        self.assertEqual(self.rpc.requests[1]['params']['threadId'],self.sender.identity.native_session_id)


class OwnedNativeRPCFixtureTests(unittest.TestCase):
    """The fixture must never cut off a client the product is verifying."""

    def test_only_instances_whose_client_closed_are_disconnected(self):
        # A listening instance also fails a server peek. Disconnecting one is a race: a client
        # that connects between that peek and the disconnect is cut off, and the product's
        # channel check then fails (codex-native-channel-unverified) - more often under load.
        from unittest.mock import patch
        module=sys.modules[__name__]
        real_peek,real_disconnect=module.peek,module.disconnect_pipe
        last_error,disconnected={},[]
        def recording_peek(handle,*arguments):
            ok=real_peek(handle,*arguments)
            error=ctypes.get_last_error()
            last_error[handle]=None if ok else error
            ctypes.set_last_error(error)
            return ok
        def recording_disconnect(handle):
            disconnected.append(last_error.get(handle))
            return real_disconnect(handle)
        with patch.object(module,'peek',recording_peek),patch.object(module,'disconnect_pipe',recording_disconnect):
            rpc=OwnedNativeRPC(str(uuid.uuid4()))
            try:
                time.sleep(.3)  # several fixture passes over the 16 listening instances
                client=create_file(rpc.name,0x00020080,0,None,3,0x40000000,None)
                self.assertNotIn(client,(None,INVALID),ctypes.get_last_error())
                close(client)
                deadline=time.monotonic()+5
                while ERROR_BROKEN_PIPE not in disconnected and time.monotonic()<deadline: time.sleep(.01)
            finally: rpc.close()
        self.assertIn(ERROR_BROKEN_PIPE,disconnected)  # the closed client's instance was recycled
        self.assertEqual(set(disconnected),{ERROR_BROKEN_PIPE})
