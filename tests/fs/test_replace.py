"""kitlib.fs.replace: os.replace retried while Windows refuses it (PermissionError), and only then."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.fs import replace as R


class ReplaceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-fs-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src = os.path.join(self.dir, 'new')
        self.dst = os.path.join(self.dir, 'file')
        for path, text in ((self.src, 'new'), (self.dst, 'old')):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)

    def read(self):
        with open(self.dst, encoding='utf-8') as f:
            return f.read()

    def test_a_reader_holding_the_target_open_is_waited_out(self):
        reader = open(self.dst, 'rb')                   # no delete sharing: os.replace is refused
        self.addCleanup(reader.close)
        with self.assertRaises(PermissionError):
            os.replace(self.src, self.dst)              # the condition the retry is for
        closer = threading.Timer(0.3, reader.close)
        self.addCleanup(closer.cancel)
        closer.start()
        start = time.monotonic()
        R.replace(self.src, self.dst)
        self.assertGreaterEqual(time.monotonic() - start, 0.2)
        self.assertEqual(self.read(), 'new')
        self.assertFalse(os.path.exists(self.src))

    def test_a_refusal_that_outlasts_the_wait_is_raised(self):
        calls = []

        def refuse(src, dst):
            calls.append(time.monotonic())
            raise PermissionError(13, 'Access is denied')
        with mock.patch.object(R.os, 'replace', side_effect=refuse):
            start = time.monotonic()
            with self.assertRaises(PermissionError):
                R.replace(self.src, self.dst, wait=0.2, pause=0.02)
        self.assertGreaterEqual(calls[-1] - start, 0.2)
        self.assertLess(time.monotonic() - start, 5.0)
        self.assertGreater(len(calls), 2)
        self.assertEqual(self.read(), 'old')

    def test_any_other_error_is_raised_at_once(self):
        calls = []

        def missing(src, dst):
            calls.append(1)
            raise FileNotFoundError(2, 'No such file')
        with mock.patch.object(R.os, 'replace', side_effect=missing):
            with self.assertRaises(FileNotFoundError):
                R.replace(self.src, self.dst, wait=5.0)
        self.assertEqual(len(calls), 1)

    def test_the_default_bound_is_wait(self):
        with mock.patch.object(R.os, 'replace', side_effect=PermissionError(13, 'denied')), \
                mock.patch.object(R, 'WAIT', 0.1):
            start = time.monotonic()
            with self.assertRaises(PermissionError):
                R.replace(self.src, self.dst)
        self.assertLess(time.monotonic() - start, 5.0)


if __name__ == '__main__':
    unittest.main()
