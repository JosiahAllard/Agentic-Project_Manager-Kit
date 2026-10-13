"""Real private Windows request/reply traffic with kernel client identity."""
import os
import threading
import time
import unittest
import uuid

from optional.bridge.core.messages import BeforeWriteError,DeliveryError
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.win.rpc import PipeServer,request
from optional.bridge.win.transport import PipeTarget
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

    def test_bad_handler_is_content_free_and_next_connection_still_works(self):
        def fail(peer,message): raise RuntimeError('private message must not enter logs')
        self.serve(fail)
        result=request(self.target,{'id':'bad'})
        self.assertEqual(result['error']['code'],'private-request-unavailable')
        self.threads[-1].join(timeout=3)
        self.serve(lambda peer,message:{'ok':True})
        self.assertEqual(request(self.target,{'id':'good'}),{'ok':True})
