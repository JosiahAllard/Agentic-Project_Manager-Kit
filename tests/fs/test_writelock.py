"""kitlib.fs.writelock: one writer of a file at a time; the lock's place, nesting and errors."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.fs import writelock as W
from kitlib.git import repo as gitrepo
from tests.gitfixture import TempRepo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A child that holds the lock of argv[2] (prefix argv[3]) until its stdin closes.
HOLDER = ('import sys; sys.path.insert(0, sys.argv[1]); from kitlib.fs import writelock as W\n'
          'with W.locked(sys.argv[2], sys.argv[3], 5):\n'
          '    print("held", flush=True); sys.stdin.read()\n')


class OutsideGitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-fs-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'sub', 'FILE.md')

    def test_the_lock_sits_beside_the_file_and_its_folder_is_made(self):
        lock = W.lock_path(self.path, 'kit-x')
        self.assertEqual(lock, os.path.join(self.dir, 'sub', '.FILE.md.lock'))
        with W.locked(self.path, 'kit-x', 1) as got:
            self.assertEqual(got, lock)
            self.assertTrue(os.path.isfile(lock))
            self.assertTrue(W.held(self.path, 'kit-x'))
        self.assertFalse(W.held(self.path, 'kit-x'))
        self.assertTrue(os.path.isfile(lock))           # never removed

    def test_nests_in_its_thread_and_excludes_another_thread(self):
        seen = []

        def other():
            try:
                with W.locked(self.path, 'kit-x', 0.2, pause=0.02):
                    seen.append('took it')
            except W.Busy as e:
                seen.append(e)
        with W.locked(self.path, 'kit-x', 1):
            with W.locked(self.path.upper(), 'kit-x', 1):
                self.assertTrue(W.held(self.path, 'kit-x'))
            self.assertTrue(W.held(self.path, 'kit-x'))
            t = threading.Thread(target=other)
            t.start()
            t.join(30)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], W.Busy)
        self.assertEqual(seen[0].wait, 0.2)
        self.assertIn('held by pid %d' % os.getpid(), seen[0].holder)

    def test_another_process_holding_it_is_busy_after_the_wait(self):
        proc = subprocess.Popen([sys.executable, '-c', HOLDER, ROOT, self.path, 'kit-x'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        self.addCleanup(proc.communicate, timeout=30)
        self.addCleanup(proc.stdin.close)
        self.assertEqual(proc.stdout.readline().strip(), 'held')
        start = time.monotonic()
        with self.assertRaises(W.Busy) as cm:
            with W.locked(self.path, 'kit-x', 0.3):
                self.fail('the block ran under a held lock')
        self.assertGreaterEqual(time.monotonic() - start, 0.3)
        self.assertIn('held by pid %d' % proc.pid, str(cm.exception))
        self.assertEqual(cm.exception.lock, W.lock_path(self.path, 'kit-x'))

    def test_a_lock_that_cannot_be_made_or_taken_says_which(self):
        with mock.patch.object(W.os, 'makedirs', side_effect=PermissionError(13, 'denied')):
            with self.assertRaises(W.CannotMake):
                with W.locked(self.path, 'kit-x', 1):
                    pass
        with mock.patch.object(W.TreeLock, 'acquire', side_effect=W.TreeLockError('broken')):
            with self.assertRaises(W.CannotTake) as cm:
                with W.locked(self.path, 'kit-x', 1):
                    pass
        self.assertEqual(str(cm.exception.detail), 'broken')
        self.assertFalse(W.held(self.path, 'kit-x'))


class InGitTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.path = self.repo.path('missing', 'FILE.md')

    def test_the_lock_is_in_the_common_dir_named_by_prefix_and_the_same_once_the_folder_exists(self):
        lock = W.lock_path(self.path, 'kit-x')
        common = os.path.normcase(os.path.realpath(self.repo.common_dir()))
        self.assertEqual(os.path.normcase(os.path.dirname(lock)), common)
        self.assertTrue(os.path.basename(lock).startswith('kit-x-file.md-'), lock)
        self.assertNotEqual(W.lock_path(self.path, 'kit-y'), lock)  # two features, two locks
        os.makedirs(os.path.dirname(self.path))
        self.assertEqual(W.lock_path(self.path, 'kit-x'), lock)

    def test_git_that_cannot_say_is_no_place_and_never_held(self):
        bad = gitrepo.Place(gitrepo.UNKNOWN, None, 'git did not answer', None, None)
        with mock.patch.object(gitrepo, 'classify', return_value=bad):
            with self.assertRaises(W.NoPlace) as cm:
                with W.locked(self.path, 'kit-x', 1):
                    pass
            self.assertEqual(cm.exception.detail, 'git did not answer')
            self.assertFalse(W.held(self.path, 'kit-x'))


if __name__ == '__main__':
    unittest.main()
