"""kitlib.fs.treelock on every platform (docs/linux-support-plan.md section 8, slice 3): an OS lock
another process or another descriptor in the same process cannot take, that a killed holder frees,
and that the holder's own read of its record does not drop. On Linux it is a flock: an
fcntl.lockf lock belongs to the process, so the same-process and own-read cases would fail."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from kitlib.fs import treelock as T

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A child that takes the lock at argv[2], says so, and holds it until its stdin closes.
HOLDER = ('import sys; sys.path.insert(0, sys.argv[1]); from kitlib.fs import treelock as T\n'
          'T.TreeLock(sys.argv[2], "a test holder").acquire(); print("held", flush=True)\n'
          'sys.stdin.read()\n')
# A child that tries the lock at argv[2] once and says "busy" or "free".
TRY = ('import sys; sys.path.insert(0, sys.argv[1]); from kitlib.fs import treelock as T\n'
       'try:\n'
       '    T.TreeLock(sys.argv[2]).acquire().release(); print("free")\n'
       'except T.TreeBusy:\n'
       '    print("busy")\n')


class TreeLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-treelock-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'kit-tree-x.lock')

    def holder(self):
        p = subprocess.Popen([sys.executable, '-c', HOLDER, ROOT, self.path],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._end, p)
        self.assertEqual(p.stdout.readline().strip(), 'held')
        return p

    @staticmethod
    def _end(p):
        if p.poll() is None:
            p.kill()             # only this test's own child, by its handle
        p.wait(30)
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except OSError:
                pass

    def another_process_sees(self):
        r = subprocess.run([sys.executable, '-c', TRY, ROOT, self.path], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_a_second_process_is_busy_until_the_holder_ends(self):
        p = self.holder()
        with self.assertRaises(T.TreeBusy) as cm:
            T.TreeLock(self.path).acquire()
        self.assertEqual(cm.exception.record.get('pid'), p.pid)
        p.stdin.close()
        p.wait(30)
        lock = T.TreeLock(self.path).acquire()
        try:
            self.assertEqual(lock.took_over.get('pid'), p.pid)   # it ended without a release
        finally:
            lock.release()

    def test_a_killed_holder_is_taken_over(self):
        p = self.holder()
        p.kill()
        p.wait(30)
        lock = T.TreeLock(self.path).acquire()
        try:
            self.assertEqual(lock.took_over.get('what'), 'a test holder')
        finally:
            lock.release()

    def test_a_second_lock_in_the_same_process_is_busy(self):
        first = T.TreeLock(self.path, 'first').acquire()
        try:
            with self.assertRaises(T.TreeBusy) as cm:
                T.TreeLock(self.path).acquire()
            self.assertEqual(cm.exception.record.get('what'), 'first')
        finally:
            first.release()
        second = T.TreeLock(self.path).acquire()
        try:
            self.assertEqual(second.took_over, {})          # the release emptied the record
        finally:
            second.release()

    def test_the_holders_own_read_of_its_record_keeps_the_lock(self):
        lock = T.TreeLock(self.path, 'mine').acquire()
        try:
            self.assertEqual(T.read_record(self.path).get('what'), 'mine')
            self.assertEqual(self.another_process_sees(), 'busy')
        finally:
            lock.release()
        self.assertEqual(self.another_process_sees(), 'free')


if __name__ == '__main__':
    unittest.main()
