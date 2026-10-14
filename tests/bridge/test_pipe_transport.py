"""Real private Windows pipes, blocked readers and owned-helper cleanup."""
import os
import threading
import time
import unittest

from tests.bridge import pipes
from unittest.mock import patch
import uuid

from optional.bridge.core.jsonwire import encode_frame
from optional.bridge.core.messages import BeforeWriteError, DeliveryError
from kitlib.proc.pipe import PrivateListener
from optional.bridge.win.transport import PipeTarget, write_peer
from kitlib.proc.job import Job
from kitlib.proc.process import own_ctime

WORKER_SOURCE = 'optional/bridge/tools/bridge_pipe_worker.py'


class PipeTransportTests(unittest.TestCase):
    def setUp(self):
        self.name = r'\\.\pipe\LOCAL\cc-msg-kit-test-' + uuid.uuid4().hex
        self.listener = PrivateListener(self.name)
        self.target = PipeTarget(self.name, os.getpid(), own_ctime())
        self.received, self.errors, self.threads = [], [], []

    def tearDown(self):
        for thread in self.threads:
            thread.join(timeout=4)
        self.listener.close()
        self.assertFalse(any(thread.is_alive() for thread in self.threads))
        self.assertEqual(self.errors, [])

    def drain(self):
        def run():
            try:
                self.listener.accept(timeout=5)
                frames, closed = pipes.frames_until_close(self.listener,5)
                self.received.extend(frames)
                self.assertTrue(closed)
            except Exception as error:
                self.errors.append(type(error).__name__)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()

    def test_large_unicode_payload_reaches_original_pipe_and_write_is_not_acceptance(self):
        frame = {'message': 'bridge\n\U0001f30e\u00e9' * 4000}
        packet = encode_frame(frame)
        self.assertGreater(len(packet), 4096)
        self.drain()
        receipt = write_peer(self.target, packet, timeout=5)
        self.threads[0].join(timeout=3)
        self.assertEqual(self.received, [frame])
        self.assertEqual(receipt.bytes_written, len(packet))
        self.assertFalse(hasattr(receipt, 'recipient_accepted'))

    def test_wrong_server_process_is_rejected_before_any_native_bytes(self):
        self.drain()
        with self.assertRaises(BeforeWriteError):
            write_peer(PipeTarget(self.name, os.getpid()+1, own_ctime()), b'secret\n', timeout=3)
        self.threads[0].join(timeout=3)
        self.assertEqual(self.received, [])

    def test_wrong_creation_time_is_rejected_before_any_native_bytes(self):
        self.drain()
        with self.assertRaises(BeforeWriteError):
            write_peer(PipeTarget(self.name, os.getpid(), own_ctime()+1), b'secret\n', timeout=3)
        self.threads[0].join(timeout=3)
        self.assertEqual(self.received, [])

    def test_stalled_native_reader_times_out_and_owned_helper_is_gone(self):
        original = Job.start
        children = []
        def start(job, command, **options):
            child = original(job, command, **options)
            children.append(child)
            return child
        began = time.monotonic()
        with patch('optional.bridge.win.transport.Job.start', start):
            with self.assertRaises(DeliveryError) as caught:
                write_peer(self.target, b'sensitive-text' * 9000, timeout=.7)
        self.assertNotIsInstance(caught.exception, BeforeWriteError)
        self.assertLess(time.monotonic()-began, 5)
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertNotIn('sensitive-text', str(caught.exception))

    def test_remote_pipe_and_oversize_payload_are_rejected_without_launch(self):
        with self.assertRaises(BeforeWriteError):
            PipeTarget(r'\\remote\pipe\cc-msg-test', os.getpid(), own_ctime())
        with patch('optional.bridge.win.transport.Job.start') as start:
            with self.assertRaises(BeforeWriteError):
                write_peer(self.target, b'x' * (256*1024+1))
            start.assert_not_called()

    def test_blocked_worker_stdin_does_not_block_controller_or_expose_packet_in_command(self):
        original = Job.start
        children = []
        def start(job, command, **options):
            self.assertFalse(any('private-content' in arg for arg in command))
            child = original(job, [command[0], '-c', 'import time; time.sleep(30)'], **options)
            children.append(child)
            return child
        began = time.monotonic()
        with patch('optional.bridge.win.transport.Job.start', start):
            with self.assertRaises(DeliveryError):
                write_peer(self.target, b'private-content' * 10000, timeout=.7)
        self.assertLess(time.monotonic()-began, 5)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_output_flood_is_bounded_and_owned_worker_ends(self):
        original = Job.start
        children = []
        def start(job, command, **options):
            child = original(job, [command[0], '-c',
                'import os; os.write(1, b"x"*65536); import time; time.sleep(30)'], **options)
            children.append(child)
            return child
        began = time.monotonic()
        with patch('optional.bridge.win.transport.Job.start', start):
            with self.assertRaises(DeliveryError):
                write_peer(self.target, b'private-content', timeout=3)
        self.assertLess(time.monotonic()-began, 5)
        self.assertTrue(all(child.poll() is not None for child in children))
