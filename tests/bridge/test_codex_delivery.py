"""Native-shaped Windows fixtures for the existing-chat Codex delivery path."""
from contextlib import contextmanager
from dataclasses import replace
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from optional.bridge.hosts.codex.context import CodexContext
from optional.bridge.hosts.codex.delivery import CodexDesktopAdapter, CodexTerminalAdapter, DesktopLease, native_id
from optional.bridge.hosts.codex.delivery_transport import MAX_FRAME, NativeEndpoint, NativeToolsPipe
from optional.bridge.core.contracts import Capability
from optional.bridge.core.messages import BeforeWriteError, DeliveryError, NativeResult, OutboundMessage
from optional.bridge.core.contracts import SessionIdentity, WorkspaceIdentity
from optional.bridge.core.pairing import ChatBinding
from kitlib.proc.errors import ProcError
from kitlib.proc.pipe import PrivateListener, peek, read, write
from kitlib.proc.job import Job
from kitlib.proc.process import own_ctime, proc_ctime

SERVE_GUARD = 30


class CodexDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.name = r'\\.\pipe\LOCAL\kit-codex-delivery-' + uuid.uuid4().hex
        self.listener = PrivateListener(self.name)
        self.endpoint = NativeEndpoint(self.name, os.getpid(), own_ctime(), str(Path(sys.executable).resolve()))
        self.context = CodexContext(str(uuid.uuid4()), str(uuid.uuid4()), 'desktop', 'prompting',
            str(Path.cwd()), (1, 2), 'a' * 64, 'b' * 64, os.getpid(), own_ctime())
        self.target_context = replace(self.context, thread_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()))
        self.recipient = self.target_context.binding('fixture-host')
        self.sender = self.context.binding('fixture-host')
        self.message = OutboundMessage('fixture-operation', 'fixture-task', self.sender, self.recipient,
            'private-content\n\U0001f30e\u00e9' * 1200)
        self.requests, self.errors, self.threads = [], [], []
        self.checks, self.check_times = 0, []
        self.done = threading.Event()

    def tearDown(self):
        self.done.set()
        for thread in self.threads:
            thread.join(timeout=4)
        self.listener.close()
        self.assertFalse(any(thread.is_alive() for thread in self.threads))
        self.assertEqual(self.errors, [])

    def check(self):
        self.checks += 1
        self.check_times.append(time.monotonic())
        return True

    def response_elapsed(self):
        """Seconds since the response phase began.

        The adapter checks the lease once on entry, then NativeToolsPipe.call checks it as
        before_write and sets the response deadline straight after, so the second check is
        the commit. The helper start before it has its own budget (start_timeout); timing
        from before send() made these response bounds also count a slow start under load."""
        self.assertGreaterEqual(len(self.check_times), 2, 'the call never reached the commit')
        return time.monotonic()-self.check_times[1]

    @contextmanager
    def lease(self, message):
        self.assertIs(message, self.message)
        yield DesktopLease(self.recipient, self.context, self.endpoint, self.check)

    def serve(self, response=None, *, wrong_id=False, stall=False, raw=None):
        # The fake server waits for the helper for as long as the test runs: when the helper
        # connects is bounded by the adapter's start_timeout, which is under test, never by
        # this fixture. A fixed accept window raced a slow helper start (3 s from serve(),
        # which test_slow_helper_start spent 2.5 s of). SERVE_GUARD only stops a broken test
        # from hanging the suite; tearDown ends the wait.
        def run():
            try:
                guard = time.monotonic()+SERVE_GUARD
                while True:
                    try:
                        self.listener.accept(timeout=.1)
                        break
                    except ProcError as error:
                        if error.code != 'private-pipe-accept-timeout':
                            raise
                    if self.done.is_set():
                        return  # No helper ever connected; the test's own assertions judge that.
                    if time.monotonic() > guard:
                        raise TimeoutError('serve-guard')
                data = bytearray()
                while not self.done.is_set():
                    if time.monotonic() > guard:
                        raise TimeoutError('serve-guard')
                    available = w.DWORD()
                    if not peek(self.listener.handle, None, 0, None, ctypes.byref(available), None):
                        return
                    if available.value:
                        buffer = ctypes.create_string_buffer(min(4096, available.value))
                        count = w.DWORD()
                        if not read(self.listener.handle, buffer, len(buffer), ctypes.byref(count), None):
                            return
                        data.extend(buffer.raw[:count.value])
                        if len(data) >= 4 and len(data) >= struct.unpack('<I', data[:4])[0]+4:
                            request = json.loads(data[4:].decode('utf-8'))
                            self.requests.append(request)
                            if stall:
                                time.sleep(1)
                                return
                            result = {'jsonrpc': '2.0', 'id': request['id']+'-wrong' if wrong_id else request['id'],
                                'result': response if response is not None else {'success': True,
                                    'structuredContent': {'threadId': self.target_context.thread_id}}}
                            payload = json.dumps(result, ensure_ascii=False).encode('utf-8')
                            packet = raw if raw is not None else struct.pack('<I', len(payload))+payload
                            # Real fragmented header and body, including UTF-8 split boundaries.
                            for piece in (packet[:2], packet[2:7], packet[7:]):
                                if piece:
                                    written = w.DWORD()
                                    write(self.listener.handle, piece, len(piece), ctypes.byref(written), None)
                                    time.sleep(.01)
                            return
                    else:
                        time.sleep(.005)
            except Exception as error:
                self.errors.append('%s: %s' % (type(error).__name__, getattr(error, 'code', '')))
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()

    def test_native_shaped_frame_targets_original_chat_with_exact_reply_correlation(self):
        self.serve()
        result = CodexDesktopAdapter(lease=self.lease, timeout=2).send(self.message)
        self.threads[0].join(3)
        self.assertEqual(result.state, 'recipient-accepted')
        self.assertEqual(result.operation_id, self.message.operation_id)
        self.assertEqual(result.recipient_ref, self.recipient.reference)
        self.assertGreaterEqual(self.checks, 2)
        request = self.requests[0]
        self.assertEqual(request['method'], 'tools/call')
        arguments=request['params']['arguments']
        self.assertEqual(set(arguments),{'threadId','prompt'})
        self.assertEqual(arguments['threadId'],self.target_context.thread_id)
        header,body=arguments['prompt'].split('\n\n',1)
        self.assertEqual(header.splitlines()[0],'Bridge operation: '+self.message.operation_id)
        self.assertIn('bridge_reply',header)
        self.assertEqual(body,self.message.text)
        self.assertEqual(request['params']['threadId'], self.context.thread_id)
        self.assertEqual(request['params']['turnId'], self.context.turn_id)
        self.assertEqual(request['params']['tool'], 'send_message_to_thread')
        self.assertEqual(request['params']['namespace'], 'codex_app')
        self.assertEqual(result.native_id, request['params']['callId'])
        self.assertNotIn('approvalPolicy', request['params'])
        self.assertNotIn('sandboxPolicy', request['params'])
        self.assertNotIn('model', request['params'])

    def test_unconfigured_and_terminal_descriptions_do_not_claim_send(self):
        for adapter in (CodexDesktopAdapter(), CodexTerminalAdapter()):
            self.assertNotIn(Capability.SEND, adapter.describe().capabilities)
            with self.assertRaises(BeforeWriteError):
                adapter.send(self.message)
        self.assertIn(Capability.SEND, CodexDesktopAdapter(lease=self.lease).describe().capabilities)

    def test_wrong_native_server_pid_is_before_write_and_no_packet_leaves_helper(self):
        self.serve()
        with Job(kill_on_close=True) as job:
            other = job.start([sys.executable, '-I', '-c', 'import time; time.sleep(10)'],
                creationflags=0x08000000)
            wrong = NativeEndpoint(self.name, other.pid, proc_ctime(other.pid), str(Path(sys.executable).resolve()))
            try:
                with self.assertRaises(BeforeWriteError):
                    NativeToolsPipe(timeout=2).call(wrong, {'id': 'fixture-id'}, before_write=self.check)
            finally:
                job.terminate(1)
                other.wait(timeout=2)
        self.threads[0].join(3)
        self.assertEqual(self.requests, [])

    def test_wrong_native_creation_and_executable_are_before_write(self):
        for endpoint in (
                NativeEndpoint(self.name, os.getpid(), own_ctime()+1, str(Path(sys.executable).resolve())),
                NativeEndpoint(self.name, os.getpid(), own_ctime(), str(Path.cwd() / 'wrong.exe'))):
            with self.assertRaises(BeforeWriteError):
                NativeToolsPipe(timeout=1).call(endpoint, {'id': 'fixture-id'}, before_write=self.check)
        self.assertEqual(self.requests, [])

    def test_lease_revocation_after_connect_prevents_native_write(self):
        self.serve()
        checks = iter((True, False))
        @contextmanager
        def lease(message):
            yield DesktopLease(self.recipient, self.context, self.endpoint, lambda: next(checks))
        with self.assertRaises(BeforeWriteError):
            CodexDesktopAdapter(lease=lease, timeout=2).send(self.message)
        self.threads[0].join(3)
        self.assertEqual(self.requests, [])

    def test_unrelated_executor_and_changed_recipient_are_denied_before_connect(self):
        unrelated = CodexContext(str(uuid.uuid4()), self.context.turn_id, 'desktop', 'prompting',
            self.context.workspace, (1, 2), 'a'*64, 'b'*64, os.getpid(), own_ctime())
        for recipient, executor in ((self.sender, self.context), (self.recipient, unrelated)):
            @contextmanager
            def lease(message):
                yield DesktopLease(recipient, executor, self.endpoint, self.check)
            with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call') as call:
                with self.assertRaises(BeforeWriteError):
                    CodexDesktopAdapter(lease=lease).send(self.message)
                call.assert_not_called()

    def test_wrong_rpc_id_error_and_ambiguous_success_stay_uncertain(self):
        self.serve(wrong_id=True)
        with self.assertRaises(DeliveryError) as caught:
            CodexDesktopAdapter(lease=self.lease, timeout=2).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('private-content', str(caught.exception))

    def test_correlated_false_success_or_wrong_target_does_not_make_acceptance(self):
        for result in ({'success': False}, {'success': True, 'isError': True},
                {'success': True, 'structuredContent': {'threadId': 'other'}},
                {'success': 'true'}):
            with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call', return_value=result):
                with self.assertRaises(DeliveryError) as caught:
                    CodexDesktopAdapter(lease=self.lease).send(self.message)
                self.assertNotIsInstance(caught.exception, BeforeWriteError)

    def test_stalled_response_is_bounded_and_owned_helper_is_stopped_without_payload_argv(self):
        self.serve(stall=True)
        original, children = Job.start, []
        def start(job, command, **options):
            self.assertFalse(any('private-content' in value for value in command))
            self.assertFalse(any('private-content' in value for value in options['env'].values()))
            child = original(job, command, **options)
            children.append(child)
            return child
        with patch('optional.bridge.hosts.codex.delivery_transport.Job.start', start):
            with self.assertRaises(DeliveryError) as caught:
                CodexDesktopAdapter(lease=self.lease, timeout=.7).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertLess(self.response_elapsed(), 4)
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_reconcile_never_sends_and_accepts_only_exact_host_observed_receipt(self):
        receipt = NativeResult(self.message.operation_id, self.recipient.reference,
            'reply-received', native_id(self.message), 'explicit reply')
        adapter = CodexDesktopAdapter(lease=self.lease, receipts=lambda message, native: receipt)
        with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call') as call:
            self.assertEqual(adapter.reconcile(self.message, receipt.native_id), receipt)
            call.assert_not_called()
        wrong = NativeResult('other-operation', self.recipient.reference, 'recipient-accepted', receipt.native_id)
        adapter = CodexDesktopAdapter(lease=self.lease, receipts=lambda message, native: wrong)
        with self.assertRaises(DeliveryError):
            adapter.reconcile(self.message, receipt.native_id)
        self.assertIsNone(CodexDesktopAdapter(lease=self.lease).reconcile(self.message, None))

    def test_remote_endpoints_nonfinite_deadlines_and_oversize_requests_fail_before_launch(self):
        with self.assertRaises(BeforeWriteError):
            NativeEndpoint(r'\\remote\pipe\fixture', os.getpid(), own_ctime(), sys.executable)
        for timeout in (True, 0, float('nan'), float('inf'), 11):
            with self.assertRaises(BeforeWriteError):
                NativeToolsPipe(timeout=timeout)
            with self.assertRaises(BeforeWriteError):
                NativeToolsPipe(start_timeout=timeout)
        with patch('optional.bridge.hosts.codex.delivery_transport.Job.start') as start:
            with self.assertRaises(BeforeWriteError):
                NativeToolsPipe().call(self.endpoint, {'id':'fixture', 'payload':'x'*(128*1024)},
                    before_write=self.check)
            start.assert_not_called()

    def test_self_target_and_cross_host_executor_substitution_are_before_write(self):
        for executor, sender in ((self.target_context, self.sender), (self.context,
                ChatBinding(SessionIdentity('claude', 'claude-native', 'claude-terminal', 'incarnation',
                    WorkspaceIdentity('fixture-host', 'project', 'workspace')), 'account', 1))):
            message = replace(self.message, sender=sender)
            @contextmanager
            def lease(message):
                yield DesktopLease(self.recipient, executor, self.endpoint, self.check)
            with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call') as call:
                with self.assertRaises(BeforeWriteError):
                    CodexDesktopAdapter(lease=lease).send(message)
                call.assert_not_called()

    def test_before_write_refusal_keeps_the_code_of_the_check_that_refused(self):
        @contextmanager
        def self_target(message):
            yield DesktopLease(self.recipient, self.target_context, self.endpoint, self.check)
        with self.assertRaises(BeforeWriteError) as caught:
            CodexDesktopAdapter(lease=self_target).send(self.message)
        self.assertEqual(caught.exception.code, 'codex-desktop-executor-not-selected')
        refusal = BeforeWriteError('codex-native-peer-unavailable-before-write')
        with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call', side_effect=refusal):
            with self.assertRaises(BeforeWriteError) as caught:
                CodexDesktopAdapter(lease=self.lease).send(self.message)
        self.assertEqual(caught.exception.code, 'codex-native-peer-unavailable-before-write')
        # Any other failure before the write keeps the generic code and hides its detail.
        @contextmanager
        def broken(message):
            raise OSError('private-detail')
            yield
        with self.assertRaises(BeforeWriteError) as caught:
            CodexDesktopAdapter(lease=broken).send(self.message)
        self.assertEqual(caught.exception.code, 'codex-native-context-unavailable-before-write')
        self.assertNotIn('private-detail', str(caught.exception))

    def test_before_write_refusal_after_a_possible_write_stays_uncertain(self):
        @contextmanager
        def lease(message):
            yield DesktopLease(self.recipient, self.context, self.endpoint, self.check)
            raise BeforeWriteError('codex-desktop-context-changed')
        with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call',
                return_value={'success': True}):
            with self.assertRaises(DeliveryError) as caught:
                CodexDesktopAdapter(lease=lease).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertEqual(caught.exception.code, 'codex-native-result-unconfirmed')

    def test_native_oversize_frame_is_uncertain_and_bounded(self):
        self.serve(raw=struct.pack('<I', MAX_FRAME+1))
        with self.assertRaises(DeliveryError) as caught:
            CodexDesktopAdapter(lease=self.lease, timeout=2).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertLess(self.response_elapsed(), 4)
        self.assertEqual(len(self.requests), 1)

    def test_duplicate_native_fields_never_form_correlated_receipt(self):
        payload = b'{"jsonrpc":"2.0","id":"first","id":"second","result":{"success":true}}'
        self.serve(raw=struct.pack('<I', len(payload))+payload)
        with self.assertRaises(DeliveryError) as caught:
            CodexDesktopAdapter(lease=self.lease, timeout=2).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)

    def test_structured_native_tool_error_in_text_cannot_be_success(self):
        response = {'success': True, 'contentItems': [{'type': 'inputText',
            'text': '{"isError":true,"threadId":"other","detail":"private-content"}'}]}
        with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call', return_value=response):
            with self.assertRaises(DeliveryError) as caught:
                CodexDesktopAdapter(lease=self.lease).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertNotIn('private-content', str(caught.exception))

    def test_helper_output_flood_is_before_write_and_helper_is_ended(self):
        original, children = Job.start, []
        def start(job, command, **options):
            child = original(job, [command[0], '-I', '-c',
                'import os,time; os.write(1,b"x"*300000); time.sleep(10)'], **options)
            children.append(child)
            return child
        began = time.monotonic()
        with patch('optional.bridge.hosts.codex.delivery_transport.Job.start', start):
            with self.assertRaises(BeforeWriteError):
                CodexDesktopAdapter(lease=self.lease, timeout=1, start_timeout=1).send(self.message)
        self.assertLess(time.monotonic()-began, 4)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual(self.requests, [])

    def test_blocked_helper_stdin_before_handshake_is_before_write_and_helper_ends(self):
        # start_timeout alone bounds the start: a long response budget does not extend it.
        original, children = Job.start, []
        def start(job, command, **options):
            child = original(job, [command[0], '-I', '-c', 'import time; time.sleep(10)'], **options)
            children.append(child)
            return child
        began = time.monotonic()
        with patch('optional.bridge.hosts.codex.delivery_transport.Job.start', start):
            with self.assertRaises(BeforeWriteError):
                CodexDesktopAdapter(lease=self.lease, timeout=10, start_timeout=.7).send(self.message)
        self.assertLess(time.monotonic()-began, 4)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual(self.requests, [])

    def test_slow_helper_start_does_not_spend_the_response_budget(self):
        # A helper start slower than the whole response budget (a loaded machine) still
        # delivers: the response budget counts from the commit, not from the spawn.
        self.serve()
        original = Job.start
        # The injected 3.2 s start exceeds both the 2 s response budget and the 3 s accept
        # window the serve fixture once had, so a fixed fixture window fails here every run;
        # start_timeout 6.7 keeps the 3.5 s left for the real helper start that 2.5/6 gave.
        def start(job, command, **options):
            time.sleep(3.2)
            return original(job, command, **options)
        with patch('optional.bridge.hosts.codex.delivery_transport.Job.start', start):
            result = CodexDesktopAdapter(lease=self.lease, timeout=2, start_timeout=6.7).send(self.message)
        self.threads[0].join(3)
        self.assertEqual(result.state, 'recipient-accepted')
        self.assertEqual(len(self.requests), 1)

    def test_blocked_native_writer_is_uncertain_and_helper_ends(self):
        original, children = Job.start, []
        def start(job, command, **options):
            child = original(job, command, **options)
            children.append(child)
            return child
        with patch('optional.bridge.hosts.codex.delivery_transport.Job.start', start):
            with self.assertRaises(DeliveryError) as caught:
                CodexDesktopAdapter(lease=self.lease, timeout=.7).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertLess(self.response_elapsed(), 4)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_receipt_reconciliation_recovers_exact_call_id_after_unknown_outcome(self):
        ids = []
        def observed(message, correlation):
            ids.append(correlation)
            return NativeResult(message.operation_id, message.recipient.reference,
                'recipient-accepted', correlation)
        adapter = CodexDesktopAdapter(lease=self.lease, receipts=observed)
        with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call') as call:
            result = adapter.reconcile(self.message, None)
            call.assert_not_called()
        self.assertEqual(ids, [native_id(self.message)])
        self.assertEqual(result.native_id, native_id(self.message))
        self.assertNotEqual(native_id(self.message), native_id(replace(self.message, text='different')))
        with self.assertRaises(DeliveryError):
            adapter.reconcile(self.message, 'uncorrelated-native-id')

    def test_thread_start_failure_closes_owned_job_and_never_joins_unstarted_thread(self):
        original_start, original_thread_start, children, jobs = Job.start, threading.Thread.start, [], []
        count = 0
        def start(job, command, **options):
            jobs.append(job)
            child = original_start(job, command, **options)
            children.append(child)
            return child
        def thread_start(thread):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('injected thread start failure')
            return original_thread_start(thread)
        try:
            with patch('optional.bridge.hosts.codex.delivery_transport.Job.start', start), patch(
                    'optional.bridge.hosts.codex.delivery_transport.threading.Thread.start', thread_start):
                with self.assertRaises(BeforeWriteError):
                    CodexDesktopAdapter(lease=self.lease, timeout=1).send(self.message)
            self.assertTrue(children)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertTrue(all(job.h is None for job in jobs))
        finally:
            for job in jobs:
                job.close()
            for child in children:
                child.wait(timeout=2)

    def test_failed_terminate_closes_job_before_stream_cleanup_and_ends_helper(self):
        self.serve(stall=True)
        original_start, children, jobs, closed_while_live = Job.start, [], [], []
        class GuardStream:
            def __init__(self, stream, child):
                self.stream, self.child = stream, child
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def close(self):
                if self.child.poll() is None:
                    closed_while_live.append(True)
                    raise AssertionError('stream close attempted before child ended')
                return self.stream.close()
        def start(job, command, **options):
            jobs.append(job)
            child = original_start(job, command, **options)
            children.append(child)
            child.stdin, child.stdout = GuardStream(child.stdin, child), GuardStream(child.stdout, child)
            return child
        try:
            with patch('optional.bridge.hosts.codex.delivery_transport.Job.start', start), patch(
                    'optional.bridge.hosts.codex.delivery_transport.Job.terminate', return_value=False):
                with self.assertRaises(DeliveryError):
                    CodexDesktopAdapter(lease=self.lease, timeout=.7).send(self.message)
            self.assertEqual(closed_while_live, [])
            self.assertLess(self.response_elapsed(), 4)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertTrue(all(job.h is None for job in jobs))
        finally:
            for job in jobs:
                job.close()
            for child in children:
                child.wait(timeout=2)

    def test_structured_and_text_native_representations_cannot_hide_each_others_error(self):
        valid = {'success': True, 'threadId': self.target_context.thread_id}
        invalid = {'isError': True, 'threadId': 'other', 'detail': 'private-content'}
        for response in (
                {'success': True, 'structuredContent': valid,
                    'contentItems': [{'type': 'inputText', 'text': json.dumps(invalid)}]},
                {'success': True, 'structuredContent': valid,
                    'content': [{'type': 'text', 'text': json.dumps(invalid)}]},
                {'success': True, 'contentItems': [{'type': 'inputText', 'text': json.dumps(valid)}],
                    'content': [{'type': 'text', 'text': json.dumps(invalid)}]}):
            with self.subTest(keys=tuple(response)):
                with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call', return_value=response):
                    with self.assertRaises(DeliveryError) as caught:
                        CodexDesktopAdapter(lease=self.lease).send(self.message)
                self.assertNotIsInstance(caught.exception, BeforeWriteError)
                self.assertNotIn('private-content', str(caught.exception))

    def test_multiple_native_representations_with_conflicting_target_cannot_be_success(self):
        valid = {'success': True, 'threadId': self.target_context.thread_id}
        wrong = {'success': True, 'threadId': 'other'}
        for field in ('contentItems', 'content'):
            response = {'success': True, 'structuredContent': valid,
                field: [{'type': 'inputText' if field == 'contentItems' else 'text',
                    'text': json.dumps(wrong)}]}
            with self.subTest(field=field):
                with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call', return_value=response):
                    with self.assertRaises(DeliveryError) as caught:
                        CodexDesktopAdapter(lease=self.lease).send(self.message)
                self.assertNotIsInstance(caught.exception, BeforeWriteError)

    def test_consistent_simultaneous_native_representations_are_accepted(self):
        valid = {'success': True, 'threadId': self.target_context.thread_id}
        response = {'success': True, 'structuredContent': valid,
            'contentItems': [{'type': 'inputText', 'text': json.dumps(valid)}],
            'content': [{'type': 'text', 'text': json.dumps(valid)}]}
        with patch('optional.bridge.hosts.codex.delivery.NativeToolsPipe.call', return_value=response):
            result = CodexDesktopAdapter(lease=self.lease).send(self.message)
        self.assertEqual(result.state, 'recipient-accepted')
        self.assertEqual(result.recipient_ref, self.recipient.reference)

    def test_lease_exit_before_write_error_after_real_native_write_stays_uncertain(self):
        self.serve()
        @contextmanager
        def lease(message):
            yield DesktopLease(self.recipient, self.context, self.endpoint, self.check)
            raise BeforeWriteError('lease-exit-denial')
        with self.assertRaises(DeliveryError) as caught:
            CodexDesktopAdapter(lease=lease, timeout=2).send(self.message)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertEqual(len(self.requests), 1)
