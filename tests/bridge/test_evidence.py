"""stable_bytes reads a file another handle may be renaming or deleting (optional/bridge/win/evidence.py)."""
import ctypes
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from kitlib.proc.pipe import INVALID, close, create_file
from optional.bridge.win import evidence
from optional.bridge.win.evidence import EvidenceError, stable_bytes

DELETE = 0x00010000
SHARE_ALL = 7  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING = 3


class StableBytesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-evidence-')
        self.path = Path(self.temp.name) / 'service.json'
        self.path.write_bytes(b'{"protocol":1}')

    def tearDown(self):
        self.temp.cleanup()

    def test_reads_while_another_handle_holds_delete_access(self):
        # MoveFileEx (os.replace) holds a DELETE-access handle on the file for a moment after its
        # new name appears; a reader that does not share delete gets a sharing violation then.
        holder = create_file(str(self.path), DELETE, SHARE_ALL, None, OPEN_EXISTING, 0, None)
        self.assertNotIn(holder, (None, INVALID), ctypes.get_last_error())
        try:
            self.assertEqual(stable_bytes(self.path, 2048), b'{"protocol":1}')
        finally:
            close(holder)

    def test_open_reader_does_not_block_unlinking_the_file(self):
        # Supervisor.close unlinks the marker while frontends may be reading it: the reader must
        # not make that unlink fail, and must then not return the bytes as current.
        real_fstat = os.fstat
        calls = []

        def fstat(fd):
            calls.append(fd)
            if len(calls) == 2:
                self.path.unlink()
            return real_fstat(fd)
        with mock.patch.object(evidence.os, 'fstat', fstat):
            with self.assertRaises(FileNotFoundError):
                stable_bytes(self.path, 2048)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.path.exists())

    def test_missing_file_is_file_not_found(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            stable_bytes(self.path, 2048)

    def test_open_shared_reports_createfile_failure_as_oserror(self):
        # CreateFileW's own failure branch: a missing name is FileNotFoundError (winerror 2), and
        # a holder that shares nothing is a sharing violation (winerror 32), both naming the path.
        missing = self.path.with_name('missing.json')
        with self.assertRaises(FileNotFoundError) as caught:
            evidence._open_shared(missing)
        self.assertEqual((caught.exception.winerror, caught.exception.filename), (2, str(missing)))
        holder = create_file(str(self.path), evidence.GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
        self.assertNotIn(holder, (None, INVALID), ctypes.get_last_error())
        try:
            with self.assertRaises(PermissionError) as caught:
                evidence._open_shared(self.path)
            self.assertEqual((caught.exception.winerror, caught.exception.filename), (32, str(self.path)))
        finally:
            close(holder)


class MarkerReplaceTests(unittest.TestCase):
    """The lifecycle publishes service.json with a replace while frontends may be reading it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-evidence-')
        self.root = Path(self.temp.name)
        self.marker = self.root / 'service.json'
        self.marker.write_bytes(b'{"protocol":1,"old":true}')

    def tearDown(self):
        self.temp.cleanup()

    def publish(self):
        # Supervisor._publish itself, on a stand-in carrying only what it reads.
        from optional.bridge.win.lifecycle import Supervisor
        owner = SimpleNamespace(root=self.root, marker=self.marker, packet=None,
            leader=SimpleNamespace(endpoint=r'\\.\pipe\bridge-evidence-test'), _same_root=lambda: None)
        Supervisor._publish(owner)
        return owner.packet

    def test_publish_waits_out_a_reader_holding_the_marker(self):
        # A frontend reading the marker holds it open; replacing a file with an open handle fails
        # with WinError 5 under any share mode. The reader lets go on the writer's first wait.
        reader = evidence._open_shared(self.marker)
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            if not reader.closed: reader.close()
        try:
            with mock.patch('time.sleep', sleep):
                packet = self.publish()
        finally:
            reader.close()
        self.assertEqual(self.marker.read_bytes(), packet)
        self.assertEqual(len(waits), 1)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_publish_still_fails_when_the_reader_never_lets_go(self):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
        with evidence._open_shared(self.marker):
            with mock.patch('time.sleep', sleep), mock.patch('time.monotonic', lambda: clock[0]):
                with self.assertRaises(PermissionError) as caught:
                    self.publish()
        self.assertEqual(caught.exception.winerror, 5)
        self.assertGreater(clock[0], 0)
        self.assertLessEqual(clock[0], 1.0)
        self.assertEqual(self.marker.read_bytes(), b'{"protocol":1,"old":true}')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_replace_does_not_retry_other_errors(self):
        with mock.patch('time.sleep') as sleep:
            with self.assertRaises(FileNotFoundError):
                evidence.replace(self.root / 'absent.tmp', self.marker)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
