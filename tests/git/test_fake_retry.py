"""kitlib.git.fake.retry: a Windows sharing denial is waited out for a time, not a count of attempts.
Another handle opened with no sharing (CreateFileW, share mode 0) makes every open of the file fail
with PermissionError until it is closed, as an outside file holder does."""
import ctypes
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.git.fake import objects, refs, retry
from tests import platforms

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3


class _Holder:
    """Holds `path` exclusively from entry until `release()` or exit."""

    def __init__(self, path):
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k32 = k32
        self._h = k32.CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
        if self._h in (None, ctypes.c_void_p(-1).value):
            raise OSError('CreateFileW failed: %d' % ctypes.get_last_error())

    def release(self):
        if self._h is not None:
            self._k32.CloseHandle(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.release()


@platforms.windows_only('a sharing denial of an open file is a Windows behaviour')
class RetryDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'ref')
        with open(self.path, 'wb') as f:
            f.write(b'abc\n')

    def _release_later(self, holder, seconds):
        t = threading.Timer(seconds, holder.release)
        t.start()
        self.addCleanup(t.join)

    def test_read_waits_past_half_a_second(self):
        # 50 attempts x 10 ms gave up in about half a second; a time deadline outlasts the hold
        with _Holder(self.path) as h:
            self._release_later(h, 0.8)
            began = time.monotonic()
            self.assertEqual(refs._read_file(self.path), 'abc\n')
        self.assertGreaterEqual(time.monotonic() - began, 0.7)

    def test_read_loose_waits_past_half_a_second(self):
        with _Holder(self.path) as h:
            self._release_later(h, 0.8)
            self.assertEqual(objects._read_loose(self.path), b'abc\n')

    def test_read_error_names_the_time_waited(self):
        with mock.patch.object(retry, 'DEADLINE', 0.2), _Holder(self.path):
            with self.assertRaises(PermissionError) as cm:
                refs._read_file(self.path)
        self.assertIn('still denied after waiting 0.', str(cm.exception))

    def test_replace_waits_then_names_the_time(self):
        src = os.path.join(self.dir, 'src')
        with open(src, 'wb') as f:
            f.write(b'new\n')
        with _Holder(self.path) as h:
            self._release_later(h, 0.8)
            refs._replace(src, self.path)
        with open(self.path, 'rb') as f:
            self.assertEqual(f.read(), b'new\n')
        with open(src, 'wb') as f:
            f.write(b'newer\n')
        with mock.patch.object(retry, 'DEADLINE', 0.2), _Holder(self.path):
            with self.assertRaisesRegex(PermissionError, 'still denied after waiting'):
                refs._replace(src, self.path)


if __name__ == '__main__':
    unittest.main()
