"""Real private Windows request/reply traffic with kernel client identity."""
import ctypes
from ctypes import wintypes as w
import io
import json
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from optional.bridge.core.jsonwire import encode_frame
from optional.bridge.core.messages import BeforeWriteError,DeliveryError
from optional.bridge.win import rpcworker
from optional.bridge.win.leader import ServiceLeader
from optional.bridge.win.rpc import PipeServer,encode,request
from optional.bridge.win.rpcworker import exchange
from optional.bridge.win.transport import PipeTarget
from kitlib.proc.errors import ProcError
from kitlib.proc.pipe import connect,write
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
        # request() leaves half its timeout for starting the helper before the pipe is
        # reached; at timeout=.5 a helper start over 250 ms on a fully loaded machine made
        # this a not-written refusal that never reached the blocked handler. timeout=2
        # gives the start 1 s; the handler still blocks well past the request's deadline.
        self.serve(lambda peer,message:(time.sleep(3.5) or {'ok':True}))
        start=time.monotonic()
        with self.assertRaises(DeliveryError) as caught:
            request(self.target,{'id':'timeout'},timeout=2)
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

    def busy_request_with_slow_helper_start(self,start_seconds):
        """request() at timeout=4 against a pipe that stays busy, where starting the helper
        takes start_seconds on a fake clock. Returns (outcome, clock when the helper gives up
        or None if it never waits). The fake helper waits exactly the 'wait' it is sent, and is
        killed (uncertain) if that runs past the caller's timeout, as run_worker would."""
        class Clock:
            now=100.0
            def monotonic(self): return self.now
        clock=Clock()
        start,timeout,gave_up=clock.now,4,[]
        def run_worker(name,data,*,limit,timeout,prefix=None):
            clock.now+=start_seconds  # the helper process starting
            if prefix is not None: data=prefix()+data
            meta=json.loads(data.partition(b'\n')[0])
            clock.now+=meta['wait']/1000  # waited for the busy pipe, then gave up
            gave_up.append(clock.now)
            if clock.now>start+timeout: raise DeliveryError('native-write-uncertain')
            return encode_frame({'state':'not-written'},max_frame_bytes=2048)
        with patch('optional.bridge.win.rpc.time',clock),patch('optional.bridge.win.rpc.run_worker',run_worker):
            try: request(self.target,{'id':'busy'},timeout=timeout)
            except DeliveryError as error: outcome=error
        return outcome,(gave_up[0]-start if gave_up else None)

    def test_busy_wait_is_measured_from_the_start_of_the_request(self):
        # The helper's busy wait was half the timeout from its own start, so a slow start
        # pushed its give-up past half the caller's budget, and past all of it at 2.5 s.
        outcome,gave_up=self.busy_request_with_slow_helper_start(1.5)
        self.assertIsInstance(outcome,BeforeWriteError)
        self.assertEqual(outcome.code,'native-peer-unavailable-before-write')
        self.assertLessEqual(gave_up,2.0)
        outcome,gave_up=self.busy_request_with_slow_helper_start(2.5)
        self.assertIsInstance(outcome,BeforeWriteError)
        self.assertIsNone(gave_up)  # the budget was spent starting it: nothing sent

    def early_reject(self):
        """The server's first client check fails, so it replies before reading the request."""
        return patch.object(self.leader.listener,'verify_client',
            side_effect=ProcError('pipe-client-process-mismatch'))

    def test_early_reject_reply_reaches_a_client_slow_to_read_it(self):
        # The server took the unread request for an extra frame and disconnected at once,
        # discarding its reply before the client read it: private-outcome-uncertain.
        real=rpcworker.read_frame
        def slow(*args,**kwargs):
            time.sleep(.5)  # the request is written; the client is descheduled before reading
            return real(*args,**kwargs)
        packet=encode({'id':'early'})
        header=encode_frame({'pipe':self.target.name,'pid':self.target.pid,'creation':self.target.creation,
            'length':len(packet),'wait':2500},max_frame_bytes=2048)
        output=io.BytesIO()
        with self.early_reject(),patch.object(rpcworker,'read_frame',slow):
            self.serve()
            exchange(io.BytesIO(header+packet),output)
            self.threads[-1].join(timeout=5)
        status,_,body=output.getvalue().partition(b'\n')
        self.assertEqual(json.loads(status)['state'],'reply',output.getvalue())
        self.assertEqual(json.loads(body),{'error':{'code':'private-client-unavailable'}})

    def test_early_reject_of_a_request_past_the_pipe_buffer_still_replies(self):
        # The client is still blocked writing a request larger than the 4 KB pipe buffer
        # when the server, which never reads it, disconnects.
        with self.early_reject():
            self.serve()
            result=request(self.target,{'id':'large','text':'x'*16384},timeout=5)
            self.threads[-1].join(timeout=5)
        self.assertEqual(result,{'error':{'code':'private-client-unavailable'}})

    def test_client_that_never_reads_or_closes_holds_the_server_only_for_a_bound(self):
        holder=connect(self.leader.endpoint,os.getpid(),own_ctime())
        self.addCleanup(holder.close)
        packet,count=encode({'id':'stuck'}),w.DWORD()
        self.assertTrue(write(holder.handle,packet,len(packet),ctypes.byref(count),None))
        start=time.monotonic()
        with self.early_reject():
            try: PipeServer(self.leader).once(timeout=3)
            except DeliveryError: pass
        # The drain bound (3 s) plus slack; never the client's own lifetime.
        self.assertLess(time.monotonic()-start,5)
        self.serve(lambda peer,message:{'ok':True})
        self.assertEqual(request(self.target,{'id':'next'}),{'ok':True})

    def test_bad_handler_is_content_free_and_next_connection_still_works(self):
        def fail(peer,message): raise RuntimeError('private message must not enter logs')
        self.serve(fail)
        result=request(self.target,{'id':'bad'})
        self.assertEqual(result['error']['code'],'private-request-unavailable')
        self.threads[-1].join(timeout=3)
        self.serve(lambda peer,message:{'ok':True})
        self.assertEqual(request(self.target,{'id':'good'}),{'ok':True})
