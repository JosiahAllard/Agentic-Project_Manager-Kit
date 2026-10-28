"""Launch mark: read once at frontend start, carried in the private frame, bound to the frontend."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from kitlib import agentname
from kitlib.proc.job import Job
from kitlib.proc.process import own_ctime
from optional.bridge import mcp_frontend
from optional.bridge.core.messages import DeliveryError,SenderAuthority,launch_mark
from optional.bridge.win import frontend as frontend_module
from optional.bridge.win.controller import Controller
from optional.bridge.win.frontend import FrontendInspector,NativeFrontend,serve_frontend
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.win.service import BrokerService
from tests.bridge.test_pairing import chat

INITIALIZE={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}}
INITIALIZED={'jsonrpc':'2.0','method':'notifications/initialized'}


class FakeService:
    """Stands in for the private pipe: records every frame the frontend sends."""
    def __init__(self):
        self.frames=[]

    def __call__(self,target,frame):
        self.frames.append(json.loads(json.dumps(frame)))
        message=frame['request']
        if 'id' not in message: return {'protocol':1,'response':None}
        result={'protocolVersion':'2025-06-18','serverInfo':{},'capabilities':{}}
        return {'protocol':1,'response':{'jsonrpc':'2.0','id':message['id'],'result':result}}


class FrontendFrameTests(unittest.TestCase):
    def run_frontend(self,messages,mark,generations=None):
        service=FakeService()
        generation=iter(generations) if generations is not None else None
        def current(marker):
            return next(generation) if generation is not None else ('target','v1')
        lines=b''.join(json.dumps(item).encode()+b'\n' for item in messages)
        output=io.BytesIO()
        with mock.patch.object(frontend_module,'request',service),\
                mock.patch.object(frontend_module,'capture_inherited_channel',lambda:None),\
                mock.patch.object(frontend_module,'_generation',current):
            serve_frontend(io.BytesIO(lines),output,Path('unused-marker'),mark=mark)
        return service.frames,output.getvalue()

    def test_every_frame_carries_the_start_mark(self):
        frames,_=self.run_frontend([INITIALIZE,INITIALIZED,{'jsonrpc':'2.0','id':2,'method':'tools/list'}],'coordinator')
        self.assertEqual(len(frames),3)
        for frame in frames:
            self.assertEqual(set(frame),{'protocol','request','mark'})
            self.assertEqual(frame['mark'],'coordinator')

    def test_replayed_handshake_after_a_new_service_carries_the_same_mark(self):
        # The service marker changes before the tool call: initialize and initialized are replayed.
        generations=[('target','v1')]*4+[('target','v2')]*8
        frames,_=self.run_frontend([INITIALIZE,INITIALIZED,{'jsonrpc':'2.0','id':2,'method':'tools/list'}],
            'pm',generations)
        methods=[frame['request']['method'] for frame in frames]
        self.assertEqual(methods,['initialize','notifications/initialized','initialize',
            'notifications/initialized','tools/list'])
        self.assertTrue(all(frame['mark']=='pm' for frame in frames))

    def test_unmarked_frontend_sends_frames_with_no_mark_key(self):
        frames,_=self.run_frontend([INITIALIZE,INITIALIZED],None)
        self.assertEqual(len(frames),2)
        self.assertTrue(all(set(frame)=={'protocol','request'} for frame in frames))

    def test_a_mark_that_is_not_a_plain_name_is_not_sent(self):
        for bad in ('two words','other:agent','x'*65,'-lead','été',''):
            frames,_=self.run_frontend([INITIALIZE],bad)
            self.assertNotIn('mark',frames[0],bad)

    def test_main_reads_the_mark_once_at_start(self):
        seen={}
        def serve(input_stream,output_stream,marker,mark=None):
            # The session changes its own environment after start: the frontend's mark stays.
            os.environ['PM_KIT_ROLE']='pm'
            seen['mark']=mark
            seen['later']=mcp_frontend.sessionmark.mark(os.environ)
        environ={**os.environ,'PM_KIT_ROLE':agentname.PREFIX+'coordinator'}
        with mock.patch.dict(os.environ,environ,clear=True),\
                mock.patch.object(sys,'argv',['mcp_frontend.py',str(Path(tempfile.gettempdir())/'service.json')]),\
                mock.patch.object(mcp_frontend,'serve_frontend',serve):
            self.assertEqual(mcp_frontend.main(),0)
        self.assertEqual(seen,{'mark':'coordinator','later':'pm'})

    def test_main_passes_no_mark_for_an_unmarked_session(self):
        seen={}
        environ={key:value for key,value in os.environ.items() if key.upper()!='PM_KIT_ROLE'}
        with mock.patch.dict(os.environ,environ,clear=True),\
                mock.patch.object(sys,'argv',['mcp_frontend.py',str(Path(tempfile.gettempdir())/'service.json')]),\
                mock.patch.object(mcp_frontend,'serve_frontend',lambda *args,mark=None:seen.setdefault('mark',mark)):
            self.assertEqual(mcp_frontend.main(),0)
        self.assertEqual(seen,{'mark':None})


class LaunchMarkTests(unittest.TestCase):
    def test_plain_marks_are_kept_exactly(self):
        for value in (None,'pm','coordinator','marked-owner','deep_worker-2','A'*64):
            self.assertEqual(launch_mark(value),value)
            self.assertEqual(SenderAuthority(chat('a'),'root',launched=value).launched,value)

    def test_anything_else_is_refused(self):
        for value in ('','x'*65,'two words','pm\n','_pm','-pm','other:agent','é',5,b'pm',['pm']):
            with self.assertRaises(DeliveryError,msg=repr(value)) as caught: launch_mark(value)
            self.assertEqual(caught.exception.code,'invalid-launch-mark')
            with self.assertRaises(DeliveryError): SenderAuthority(chat('a'),'root',launched=value)

    def test_authority_is_unmarked_by_default(self):
        self.assertIsNone(SenderAuthority(chat('a'),'root').launched)


class ControllerMarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-launch-mark-')
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(Path(self.temp.name),self.leader)
        self.a,self.b=chat('a'),chat('b')
        self.peers={'a':self.a,'b':self.b}
        for peer in self.peers.values():
            self.controller.enroll(peer,lambda item:self.peers.get(item.identity.session_id)==item)

    def tearDown(self):
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()

    def attest(self,binding,mark):
        server=self.controller.frontend(lambda meta:SenderAuthority(binding,'root'),mark=mark)
        return server,server.calls.observe({})

    def test_authority_carries_the_frontend_mark_and_the_exact_live_binding_has_it(self):
        _,authority=self.attest(self.a,'coordinator')
        self.assertEqual(authority,SenderAuthority(self.a,'root',launched='coordinator'))
        self.assertEqual(self.controller.launched(self.a),'coordinator')
        self.assertIsNone(self.controller.launched(self.b))

    def test_only_the_exact_binding_has_the_mark(self):
        self.attest(self.a,'pm')
        self.assertIsNone(self.controller.launched(replace(self.a,permission_revision=2)))
        self.assertIsNone(self.controller.launched(self.a.identity))

    def test_a_chat_that_is_no_longer_live_has_no_mark(self):
        self.attest(self.a,'pm')
        self.peers['a']=replace(self.a,permission_revision=2)
        self.assertIsNone(self.controller.launched(self.a))

    def test_a_detached_frontend_leaves_no_mark(self):
        server,_=self.attest(self.a,'pm')
        self.controller.detach_frontend(server)
        self.assertIsNone(self.controller.launched(self.a))

    def test_an_unmarked_frontend_attesting_the_chat_clears_its_mark(self):
        self.attest(self.a,'pm')
        _,authority=self.attest(self.a,None)
        self.assertIsNone(authority.launched)
        self.assertIsNone(self.controller.launched(self.a))

    def test_an_unenrolled_chat_is_never_recorded(self):
        stranger=chat('stranger')
        _,authority=self.attest(stranger,'pm')
        self.assertEqual(authority.launched,'pm')
        self.peers['stranger']=stranger
        self.controller.enroll(stranger,lambda item:self.peers.get(item.identity.session_id)==item)
        self.assertIsNone(self.controller.launched(stranger))

    def test_a_bad_mark_or_observer_cannot_make_a_frontend(self):
        with self.assertRaises(DeliveryError): self.controller.frontend(lambda meta:None,mark='two words')
        with self.assertRaises(DeliveryError): self.controller.frontend('not callable',mark='pm')
        with self.assertRaises(DeliveryError): self.controller.frontend(None).calls.observe({})


class StubInspector:
    """The kernel checks are FrontendInspector's (tested apart); this passes this process off as the frontend."""
    def __init__(self): self.calls=[]

    def inspect(self,peer,channel=None,mark=None):
        self.calls.append((channel,mark))
        return NativeFrontend(os.getpid(),own_ctime(),os.getpid(),own_ctime(),'codex',
            Path(sys.executable),None,launch_mark(mark))

    def verify_helper(self,peer): raise DeliveryError('frontend-process-unverified')


class ServiceMarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-service-mark-')
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(Path(self.temp.name),self.leader)
        self.a=chat('a')
        self.controller.enroll(self.a,lambda item:item==self.a)
        self.inspector=StubInspector()
        self.service=BrokerService(self.controller,self.inspector,
            source_factory=lambda native:lambda meta:SenderAuthority(self.a,'root'))

    def tearDown(self):
        self.service.close()
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()

    def test_old_frontend_frame_without_mark_is_still_served(self):
        # Exactly the frame a frontend started before the mark existed sends.
        result=self.service.handle(None,{'protocol':1,'request':INITIALIZE})
        self.assertIn('result',result['response'])
        self.assertEqual(self.inspector.calls,[(None,None)])
        self.assertIsNone(next(iter(self.service.frontends.values()))[0].mark)

    def test_marked_frame_binds_the_mark_to_the_frontend(self):
        result=self.service.handle(None,{'protocol':1,'request':INITIALIZE,'mark':'coordinator'})
        self.assertIn('result',result['response'])
        self.assertEqual(next(iter(self.service.frontends.values()))[0].mark,'coordinator')

    def test_malformed_mark_fields_are_refused(self):
        for frame in ({'protocol':1,'request':INITIALIZE,'mark':None},
                {'protocol':1,'request':INITIALIZE,'mark':5},
                {'protocol':1,'request':INITIALIZE,'mark':'pm','extra':1},
                {'protocol':1,'mark':'pm'}):
            with self.assertRaises(DeliveryError) as caught: self.service.handle(None,frame)
            self.assertEqual(caught.exception.code,'invalid-private-request')
        with self.assertRaises(DeliveryError) as caught:
            self.service.handle(None,{'protocol':1,'request':INITIALIZE,'mark':'two words'})
        self.assertEqual(caught.exception.code,'invalid-launch-mark')
        self.assertEqual(self.service.frontends,{})

    def test_the_same_frontend_cannot_change_or_drop_its_mark(self):
        self.service.handle(None,{'protocol':1,'request':INITIALIZE,'mark':'pm'})
        for frame in ({'protocol':1,'request':INITIALIZE,'mark':'coordinator'},
                {'protocol':1,'request':INITIALIZE}):
            with self.assertRaises(DeliveryError) as caught: self.service.handle(None,frame)
            self.assertEqual(caught.exception.code,'frontend-mark-changed')

    def test_real_inspector_checks_the_mark_before_anything_else(self):
        kit=Path(__file__).resolve().parents[2]
        inspector=FrontendInspector(kit,Path(self.temp.name)/'service.json',sys.executable,{'codex':sys.executable})
        with self.assertRaises(DeliveryError) as caught: inspector.inspect(None,mark='two words')
        self.assertEqual(caught.exception.code,'invalid-launch-mark')
        with self.assertRaises(DeliveryError) as caught: inspector.inspect(None,mark='pm')
        self.assertEqual(caught.exception.code,'frontend-process-unverified')


class RealFrontendMarkTests(unittest.TestCase):
    """The actual frontend process, its kernel-verified parent and the shared controller."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-frontend-mark-')
        self.root=Path(self.temp.name)
        self.kit=Path(__file__).resolve().parents[2]
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.controller=Controller(self.root,self.leader)
        self.a=chat('a')
        self.controller.enroll(self.a,lambda item:item==self.a)
        self.marker=self.root/'service.json'
        self.marker.write_text(json.dumps({'protocol':1,'pipe':self.leader.endpoint,
            'pid':os.getpid(),'creation':own_ctime()}),encoding='utf-8')
        self.service=BrokerService(self.controller,
            FrontendInspector(self.kit,self.marker,sys.executable,{'codex':sys.executable}),
            source_factory=lambda native:lambda meta:SenderAuthority(self.a,'root'))
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.service.run,args=(self.stop,),daemon=True)
        self.thread.start()
        self.job=Job(kill_on_close=True)
        self.process=None

    def tearDown(self):
        self.job.close()
        if self.process is not None:
            self.process.wait(timeout=3)
            for stream in (self.process.stdin,self.process.stdout): stream.close()
        self.stop.set()
        self.thread.join(timeout=4)
        self.service.close()
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()
        self.assertFalse(self.thread.is_alive())

    def start(self,role):
        environ={key:value for key,value in os.environ.items() if key.upper()!='PM_KIT_ROLE'}
        if role is not None: environ['PM_KIT_ROLE']=role
        self.process=self.job.start([sys.executable,'-I','-B',str(self.kit/'optional/bridge/mcp_frontend.py'),
            str(self.marker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            env=environ,creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)

    def call(self,message):
        self.process.stdin.write(json.dumps(message).encode()+b'\n')
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline(1024*1024+1))

    def connect(self):
        self.assertIn('result',self.call(INITIALIZE))
        self.process.stdin.write(json.dumps(INITIALIZED).encode()+b'\n')
        self.process.stdin.flush()
        result=self.call({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'bridge_connections','arguments':{}}})
        self.assertFalse(result['result']['isError'],result)
        return next(iter(self.service.frontends.values()))[0]

    def test_launched_session_frontend_carries_its_mark_to_the_exact_chat(self):
        self.start(agentname.PREFIX+'coordinator')
        native=self.connect()
        self.assertEqual(native.pid,self.process.pid)
        self.assertEqual(native.mark,'coordinator')
        self.assertEqual(self.controller.launched(self.a),'coordinator')

    def test_unmarked_frontend_still_connects_and_gives_no_mark(self):
        self.start(None)
        native=self.connect()
        self.assertIsNone(native.mark)
        self.assertIsNone(self.controller.launched(self.a))


if __name__=='__main__':
    unittest.main()
