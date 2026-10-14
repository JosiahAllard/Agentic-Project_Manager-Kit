"""stable_bytes reads a file another handle may be renaming or deleting (optional/bridge/win/evidence.py)."""
import ctypes
import os
from pathlib import Path
import tempfile
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


if __name__ == '__main__':
    unittest.main()
