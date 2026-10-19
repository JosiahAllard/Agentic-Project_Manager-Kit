"""Real private Windows request/reply traffic with kernel client identity."""
import io
import json
import os
import threading
import time
import unittest
import uuid

from optional.bridge.core.jsonwire import encode_frame
from optional.bridge.core.messages import BeforeWriteError,DeliveryError
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.win.rpc import PipeServer,encode,request
from optional.bridge.win.rpcworker import exchange
from optional.bridge.win.transport import PipeTarget
from kitlib.proc.pipe import connect
from kitlib.proc.process import own_ctime


class PipeRpcTests(unittest.TestCase):
    def setUp(self):
        self.leader=ServiceLeader(uuid.uuid4().hex)
        self.target=PipeTarget(self.leader.endpoint,os.getpid(),own_ctime())
        self.threads=[]

    def tearDown(self):
        for thread in self.threads: thread.join(timeout=4)
        self.leader.close()
        self.assertFalse(any(thread.is_alive() for thread in self.threads))

    def serve(self,handler=None):
        server=PipeServer(self.leader,handler=handler)
        failures=[]
        def run():
            try: server.once(timeout=3)
            except Exception as error: failures.append(getattr(error,'code',type(error).__name__))
        thread=threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return failures

    def test_large_private_request_and_reply_cross_real_pipe_with_kernel_identity(self):
        seen=[]
        def handler(peer,message):
            seen.append(peer)
            return {'id':message['id'],'result':message['text']}
        failures=self.serve(handler)
        text='private \U0001f30e\n' * 5000
        reply=request(self.target,{'id':'roundtrip','text':text},timeout=3)
        self.threads[-1].join(timeout=3)
        self.assertEqual(reply,{'id':'roundtrip','result':text})
        self.assertEqual(failures,[])
        self.assertEqual(len(seen),1)
        self.assertNotEqual(seen[0].pid,os.getpid())  # the owned I/O helper, not a claimed caller.
        self.assertGreater(seen[0].creation,0)

    def test_default_handler_cannot_turn_self_claimed_sender_or_owner_into_authority(self):
        self.serve()
        result=request(self.target,{'id':'claim','owner_approved':True,'sender':'fake-owner'})
        self.assertEqual(result['error']['code'],'private-client-unverified')

    def test_wrong_server_identity_is_rejected_before_request_bytes(self):
        self.serve(lambda peer,message:{'ok':True})
        with self.assertRaises(BeforeWriteError):
            request(PipeTarget(self.target.name,os.getpid(),own_ctime()+1),{'id':'private'},timeout=3)

    def test_blocked_response_is_uncertain_without_controller_hang(self):
        self.serve(lambda peer,message:(time.sleep(1.2) or {'ok':True}))
        start=time.monotonic()
        with self.assertRaises(DeliveryError) as caught:
            request(self.target,{'id':'timeout'},timeout=.5)
        self.assertNotIsInstance(caught.exception,BeforeWriteError)
        self.assertLess(time.monotonic()-start,4)

    def test_client_waits_for_the_busy_one_instance_pipe_instead_of_being_refused(self):
        # A hook's request meeting a frontend call already being served was refused
        # 'not-written' (ERROR_PIPE_BUSY), and the hook exits 0 with nothing enrolled.
        holder=connect(self.leader.endpoint,os.getpid(),own_ctime())
        self.leader.listener.accept(timeout=3)  # the one instance now serves another client
        packet=encode({'id':'queued'})
        header=encode_frame({'pipe':self.target.name,'pid':self.target.pid,'creation':self.target.creation,
            'length':len(packet),'wait':4000},max_frame_bytes=2048)
        output=io.BytesIO()
        worker=threading.Thread(target=exchange,args=(io.BytesIO(header+packet),output))
        self.threads.append(worker)
        worker.start()
        worker.join(timeout=.5)
        self.assertTrue(worker.is_alive(),output.getvalue())  # still waiting, not refused
        holder.close()
        self.leader.listener.reset()
        PipeServer(self.leader,handler=lambda peer,message:{'id':message['id'],'served':True}).once(timeout=3)
        worker.join(timeout=4)
        status,_,body=output.getvalue().partition(b'\n')
        self.assertEqual(json.loads(status)['state'],'reply')
        self.assertEqual(json.loads(body),{'id':'queued','served':True})

    def test_a_pipe_busy_for_the_whole_budget_is_reported_not_written(self):
        # The helper waited 10 s for a busy pipe, past every caller's budget, so the caller
        # killed it and reported an uncertain outcome although nothing had been written.
        holder=connect(self.leader.endpoint,os.getpid(),own_ctime())
        self.addCleanup(holder.close)
        self.leader.listener.accept(timeout=3)  # the one instance serves another client throughout
        start=time.monotonic()
        with self.assertRaises(BeforeWriteError) as caught:
            request(self.target,{'id':'busy'},timeout=4)
        self.assertEqual(caught.exception.code,'native-peer-unavailable-before-write')
        self.assertLess(time.monotonic()-start,4)

    def test_bad_handler_is_content_free_and_next_connection_still_works(self):
        def fail(peer,message): raise RuntimeError('private message must not enter logs')
        self.serve(fail)
        result=request(self.target,{'id':'bad'})
        self.assertEqual(result['error']['code'],'private-request-unavailable')
        self.threads[-1].join(timeout=3)
        self.serve(lambda peer,message:{'ok':True})
        self.assertEqual(request(self.target,{'id':'good'}),{'ok':True})
