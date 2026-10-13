"""Native channel metadata from actual owned pipe and frontend ancestry."""
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from optional.bridge import registry
from optional.bridge.core.messages import DeliveryError
from kitlib.proc.errors import ProcError
from kitlib.proc.pipe import PrivateListener,peek
from optional.bridge.win.channel import NativeChannel,revalidate_channel
from optional.bridge.win.controller import Controller
from optional.bridge.win.frontend import FrontendInspector
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.win.service import BrokerService
from optional.bridge.win.transport import PipeTarget
from kitlib.proc.job import Job
from kitlib.proc.process import own_ctime
import ctypes
from ctypes import wintypes as w

FRONTEND_SOURCE='optional/bridge/win/frontend.py'
SERVICE_SOURCE='optional/bridge/win/service.py'
CHANNEL_SOURCE='optional/bridge/win/channel.py'


class NativeChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-native-channel-')
        self.root=Path(self.temp.name)
        self.kit=Path(__file__).resolve().parents[2]
        self.native_name=r'\\.\pipe\LOCAL\owned-app-channel-'+uuid.uuid4().hex
        self.native_listener=PrivateListener(self.native_name)
        self.native_stop=threading.Event()
        self.native_frames=[]
        self.native_accepts=0
        self.native_errors=[]
        self.native_thread=threading.Thread(target=self.native_loop,daemon=True)
        self.native_thread.start()
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(self.root,self.leader)
        self.marker=self.root/'service.json'
        self.marker.write_text(json.dumps({'protocol':1,'pipe':self.leader.endpoint,
            'pid':os.getpid(),'creation':own_ctime()}),encoding='utf-8')
        self.inspector=FrontendInspector(self.kit,self.marker,sys.executable,{'codex':sys.executable})
        self.observed=[]
        self.service=BrokerService(self.controller,self.inspector,
            source_factory=lambda native:self.observed.append(native))
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.service.run,args=(self.stop,),daemon=True)
        self.thread.start()
        self.job=Job(kill_on_close=True)
        self.process=None
        self.siblings=[]

    def native_loop(self):
        while not self.native_stop.is_set():
            try:self.native_listener.accept(timeout=.1)
            except ProcError as error:
                if error.code=='private-pipe-accept-timeout':continue
                self.native_errors.append(error.code)
                return
            self.native_accepts+=1
            end=time.monotonic()+2
            while time.monotonic()<end and not self.native_stop.is_set():
                available=w.DWORD()
                if not peek(self.native_listener.handle,None,0,None,ctypes.byref(available),None):break
                if available.value:self.native_frames.append(available.value);break
                time.sleep(.002)
            try:self.native_listener.reset()
            except ProcError as error:
                if not self.native_stop.is_set():self.native_errors.append(error.code)
                return

    def tearDown(self):
        self.job.close()
        if self.process is not None:
            self.process.wait(timeout=3)
            for stream in (self.process.stdin,self.process.stdout):stream.close()
        for sibling in self.siblings:
            sibling.wait(timeout=3)
            for stream in (sibling.stdin,sibling.stdout):stream.close()
        self.stop.set()
        self.thread.join(timeout=4)
        self.service.close()
        self.controller.close()
        self.leader.close()
        self.native_stop.set()
        self.native_thread.join(timeout=3)
        self.native_listener.close()
        self.temp.cleanup()
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.native_thread.is_alive())
        self.assertEqual(self.native_frames,[])
        self.assertEqual(self.native_errors,[])

    def start(self,endpoint):
        if endpoint is not None and 'codex' not in registry.names():
            # The frontend subprocess reads the kit's own hosts.json; it nominates Codex's app
            # channel only once Codex is registered there (plan step 9; optional/bridge/STATUS.md).
            self.skipTest('Codex is not registered in hosts.json yet, so its channel is not read')
        env=dict(os.environ)
        env.pop('CODEX_APP_TOOLS_PIPE_PATH',None)
        if endpoint is not None:env['CODEX_APP_TOOLS_PIPE_PATH']=endpoint
        # The app-owned pipe server is this owned test process. A distinct native
        # host child starts the real installed frontend, just as app -> codex -> MCP.
        argv=[sys.executable,'-I','-B',str(self.kit/'optional/bridge/mcp_frontend.py'),str(self.marker)]
        code='import subprocess,sys; p=subprocess.Popen('+repr(argv)+',stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,close_fds=True); p.wait()'
        self.process=self.job.start([sys.executable,'-I','-c',code],env=env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)

    def call(self,message):
        self.process.stdin.write(json.dumps(message).encode()+b'\n')
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline(1024*1024+1))

    def initialize(self):
        response=self.call({'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2025-06-18'}})
        self.assertIn('result',response)
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()
        return self.observed[0]

    def test_real_inherited_pipe_is_published_separately_and_zero_frames_are_sent(self):
        self.start(self.native_name)
        native=self.initialize()
        channel=native.channel
        self.assertIs(type(channel),NativeChannel)
        self.assertEqual(channel.target,PipeTarget(self.native_name,os.getpid(),own_ctime()))
        self.assertEqual(channel.executable,Path(sys.executable).resolve())
        self.assertEqual(native.host_pid,self.process.pid)
        self.assertNotEqual(native.host_pid,channel.target.pid)
        with self.assertRaises(FrozenInstanceError):channel.executable=Path('fake.exe')
        self.assertNotIn(self.native_name,repr(channel))
        self.assertGreaterEqual(self.native_accepts,2)  # Frontend capture and broker revalidation.

    def test_repeated_requests_keep_channel_identity_and_mcp_session_stable(self):
        self.start(self.native_name)
        native=self.initialize()
        for identity in (2,3):
            result=self.call({'jsonrpc':'2.0','id':identity,'method':'tools/list'})
            self.assertTrue(result['result']['tools'])
        self.assertEqual(len(self.observed),1)
        self.assertEqual(len(self.service.frontends),1)
        self.assertEqual(next(iter(self.service.frontends.values()))[0],native)

    def test_missing_environment_keeps_legacy_frontend_channel_none(self):
        self.start(None)
        native=self.initialize()
        self.assertIsNone(native.channel)
        self.assertEqual(self.native_accepts,0)

    def test_tool_json_and_meta_cannot_replace_the_captured_native_channel(self):
        self.start(self.native_name)
        native=self.initialize()
        fake={'pipe':r'\\.\pipe\LOCAL\unrelated','pid':1,'creation':1,'executable':'fake.exe'}
        result=self.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_connections','arguments':{},'_meta':{'channel':fake}}})
        self.assertTrue(result['result']['isError'])  # Channel metadata grants no source authority.
        self.assertEqual(self.observed[0],native)
        with self.assertRaises(DeliveryError):
            revalidate_channel({'channel':fake},native.host_pid,native.host_creation,self.inspector.user)

    def test_actual_sibling_pipe_server_is_rejected_before_publication(self):
        name=r'\\.\pipe\LOCAL\owned-sibling-channel-'+uuid.uuid4().hex
        code=("import sys; sys.path.insert(0,"+repr(str(self.kit))+"); "
            "from kitlib.proc.pipe import PrivateListener; "
            "p=PrivateListener("+repr(name)+"); print('ready',flush=True); sys.stdin.readline(); p.close()")
        sibling=self.job.start([sys.executable,'-I','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        self.siblings.append(sibling)
        self.assertEqual(sibling.stdout.readline(),b'ready\r\n')
        self.start(name)
        result=self.call({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})
        self.assertIn('error',result)
        self.assertEqual(self.observed,[])
        self.assertEqual(self.service.frontends,{})

    def test_wrong_creation_executable_and_disconnected_endpoint_fail_revalidation(self):
        self.start(self.native_name)
        native=self.initialize()
        wire=native.channel.to_wire()
        self.assertEqual(revalidate_channel(wire,native.host_pid,native.host_creation,self.inspector.user),native.channel)
        wrong=dict(wire,creation=wire['creation']+1)
        with self.assertRaises(DeliveryError):revalidate_channel(wrong,native.host_pid,native.host_creation,self.inspector.user)
        fake_exe=self.root/'different.exe'
        fake_exe.write_bytes(b'owned-image-name-only')
        with self.assertRaises(DeliveryError):
            revalidate_channel(dict(wire,executable=str(fake_exe)),native.host_pid,native.host_creation,self.inspector.user)
        with self.assertRaises(DeliveryError):
            revalidate_channel(dict(wire,pipe=r'\\.\pipe\LOCAL\absent-'+uuid.uuid4().hex),native.host_pid,native.host_creation,self.inspector.user)

    def test_remote_or_malformed_environment_endpoint_cannot_publish(self):
        self.start(r'\\remote\pipe\owned-channel')
        result=self.call({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})
        self.assertIn('error',result)
        self.assertEqual(self.observed,[])

    def test_ordinary_rpc_helper_cannot_publish_channel_without_verified_frontend(self):
        from optional.bridge.win.rpc import request
        result=request(PipeTarget(self.leader.endpoint,os.getpid(),own_ctime()),{'protocol':1,
            'request':{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            'channel':{'pipe':self.native_name,'pid':os.getpid(),'creation':own_ctime(),
                'executable':str(Path(sys.executable).resolve())}})
        self.assertIn('error',result)
        self.assertEqual(self.observed,[])
        self.assertEqual(self.native_accepts,0)
