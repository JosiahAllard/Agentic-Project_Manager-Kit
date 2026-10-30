"""kitlib.fs.replace: os.replace retried while Windows refuses it (PermissionError), and only then."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.fs import replace as R

WAIT, PAUSE = 0.25, 0.0625      # exact in binary: tries at 0, 1/16, 1/8, 3/16 and 1/4 s, so 5


class FakeTime:
    """Stands in for kitlib.fs.replace's `time`: monotonic() reads `t` and sleep(s) adds s to it,
    so the retry loop runs on counted pauses, never on the machine's speed. `on_sleep` runs at
    each pause (a reader letting go)."""

    def __init__(self, on_sleep=None):
        self.t, self.sleeps, self.on_sleep = 0.0, [], on_sleep

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep is not None:
            self.on_sleep()


def fake_time(test, on_sleep=None):
    clock = FakeTime(on_sleep)
    patcher = mock.patch.object(R, 'time', clock)
    patcher.start()
    test.addCleanup(patcher.stop)
    return clock


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
        clock = fake_time(self, on_sleep=reader.close)  # the reader lets go at the first pause
        R.replace(self.src, self.dst)
        self.assertEqual(clock.sleeps, [R.PAUSE])       # refused once for real, then through
        self.assertEqual(self.read(), 'new')
        self.assertFalse(os.path.exists(self.src))

    def test_a_refusal_that_outlasts_the_wait_is_raised(self):
        clock = fake_time(self)
        calls = []

        def refuse(src, dst):
            calls.append(clock.t)
            raise PermissionError(13, 'Access is denied')
        with mock.patch.object(R.os, 'replace', side_effect=refuse):
            with self.assertRaises(PermissionError):
                R.replace(self.src, self.dst, wait=WAIT, pause=PAUSE)
        self.assertEqual(calls, [0.0, 0.0625, 0.125, 0.1875, 0.25])
        self.assertEqual(clock.sleeps, [PAUSE] * 4)
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

    def test_the_default_bound_is_wait_and_the_default_pause_is_pause(self):
        clock = fake_time(self)
        with mock.patch.object(R.os, 'replace', side_effect=PermissionError(13, 'denied')) as m, \
                mock.patch.object(R, 'WAIT', WAIT), mock.patch.object(R, 'PAUSE', PAUSE):
            with self.assertRaises(PermissionError):
                R.replace(self.src, self.dst)
        self.assertEqual(m.call_count, 5)
        self.assertEqual(clock.sleeps, [PAUSE] * 4)


class RemoveTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-fs-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.dst = os.path.join(self.dir, 'file')
        with open(self.dst, 'w', encoding='utf-8') as f:
            f.write('old')

    def test_a_reader_holding_the_file_open_is_waited_out(self):
        reader = open(self.dst, 'rb')
        self.addCleanup(reader.close)
        with self.assertRaises(PermissionError):
            os.remove(self.dst)                         # the condition the retry is for
        clock = fake_time(self, on_sleep=reader.close)  # the reader lets go at the first pause
        R.remove(self.dst)
        self.assertEqual(clock.sleeps, [R.PAUSE])
        self.assertFalse(os.path.exists(self.dst))

    def test_a_file_already_gone_is_success(self):
        clock = fake_time(self)
        R.remove(os.path.join(self.dir, 'nothing'), wait=WAIT)
        self.assertEqual(clock.sleeps, [])

    def test_a_refusal_that_outlasts_the_wait_is_raised(self):
        clock = fake_time(self)
        with mock.patch.object(R.os, 'remove', side_effect=PermissionError(13, 'denied')) as m:
            with self.assertRaises(PermissionError):
                R.remove(self.dst, wait=WAIT, pause=PAUSE)
        self.assertEqual(m.call_count, 5)
        self.assertEqual(clock.sleeps, [PAUSE] * 4)

    def test_any_other_error_is_raised_at_once(self):
        with mock.patch.object(R.os, 'remove', side_effect=OSError(5, 'io')) as m:
            with self.assertRaises(OSError):
                R.remove(self.dst, wait=5.0)
        self.assertEqual(m.call_count, 1)


if __name__ == '__main__':
    unittest.main()
