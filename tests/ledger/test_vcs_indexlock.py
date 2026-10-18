"""kitlib.ledger.vcs.commit_one retries add+commit while git's index.lock is busy, and only then.

Real git in a throwaway repository (tests/gitfixture.py); the lock is a file made in its git folder."""
import ctypes
import os
import time
import unittest
from ctypes import wintypes
from unittest import mock

from kitlib.git import cmd
from kitlib.ledger import vcs, write
from kitlib.ledger.errors import Refused
from tests.gitfixture import TempRepo, fixture_env

MODEL = 'Test Model 1'
WAIT = 1.0      # the lock bound the tests run with
GONE_WAIT = 2.0  # seconds release() waits for a delete Windows still has pending

_K32 = ctypes.WinDLL('kernel32', use_last_error=True)     # its own instance: argtypes stay local
_K32.CreateFileW.restype = wintypes.HANDLE
_K32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_K32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                            wintypes.DWORD]
_K32.CloseHandle.argtypes = [wintypes.HANDLE]
_DELETE, _SHARE_ALL, _OPEN_EXISTING, _FILE_DISPOSITION_INFO = 0x00010000, 7, 3, 4
_INVALID = wintypes.HANDLE(-1).value


class IndexLockTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.env = fixture_env()
        self.path = self.repo.path('rulings', 'LEDGER.md')
        self.lock = os.path.join(self.repo.root, '.git', 'index.lock')
        patch = mock.patch.object(vcs, 'INDEX_LOCK_WAIT', WAIT, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.plan = vcs.prepare(self.path, branch_ok=False, model=MODEL, env=self.env)
        self.got = write.add(self.path, repo='alpha', prov='selected', q='q', a='a', decides='Use this one.',
                             source='s', when='2026-10-06 08:00Z')
        self.before = self.count()

    def count(self):
        return int(self.repo.git('rev-list', '--count', 'HEAD'))

    def hold(self):
        with open(self.lock, 'w') as f:
            f.write('')
        self.addCleanup(self.release)

    def release(self):
        """Remove the lock. While a delete of it is still pending (another handle, a virus
        scanner's say, is open on the file) Windows denies the remove (WinError 5) until the file
        goes: that wait is bounded by GONE_WAIT."""
        deadline = time.monotonic() + GONE_WAIT
        while True:
            try:
                os.remove(self.lock)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def pending(self):
        """Put the held lock in the state a delete leaves it in while another handle is still open
        on it (the gate's failures of 2026-10-14): the file stays listed, and creating it is denied
        (PermissionError, errno 13), until that handle closes. Returns the close."""
        h = _K32.CreateFileW(self.lock, _DELETE, _SHARE_ALL, None, _OPEN_EXISTING, 0, None)
        self.assertNotIn(h, (None, _INVALID), 'open: WinError %d' % ctypes.get_last_error())
        flag = ctypes.c_ubyte(1)
        self.assertTrue(_K32.SetFileInformationByHandle(h, _FILE_DISPOSITION_INFO, ctypes.byref(flag), 1),
                        'mark deleted: WinError %d' % ctypes.get_last_error())
        closed = []

        def close():
            if not closed:
                closed.append(_K32.CloseHandle(h))
        self.addCleanup(close)
        self.assertTrue(os.path.exists(self.lock))
        with self.assertRaises(PermissionError):         # what git's create of the lock meets
            os.close(os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY))
        return close

    def free_at_first_pause(self, action):
        """Run action the first time commit_file pauses to retry, so the lock is present at the
        first try and gone before the next: an order, not a wall-clock race. The bound gets
        GONE_WAIT of headroom for a delete another handle lags on. Later pauses really sleep."""
        real, fired = time.sleep, []

        def pause(seconds):
            if not fired:
                fired.append(seconds)
                action()
            real(seconds)
        bound = mock.patch.object(vcs, 'INDEX_LOCK_WAIT', GONE_WAIT + WAIT)
        bound.start()
        self.addCleanup(bound.stop)
        sleep = mock.patch.object(vcs.time, 'sleep', side_effect=pause)
        sleep.start()
        self.addCleanup(sleep.stop)
        return fired

    def test_a_lock_still_being_deleted_is_busy_to_git_as_a_held_one(self):
        """Real git (2.55.0.windows.5, probed 2026-10-14) reports it as an existing lock."""
        self.hold()
        self.pending()
        _, why = cmd.run(['add', '--', self.plan.rel], self.plan.root, env=self.plan.env)
        self.assertIsNotNone(why)
        self.assertIn('Unable to create', why)
        self.assertIn("index.lock': File exists.", why)

    def test_a_lock_still_being_deleted_at_the_release_gives_one_commit(self):
        self.hold()
        fired = self.free_at_first_pause(self.pending())
        lines = vcs.finish(self.plan, self.got.id, None, 'Use this one.')
        self.assertEqual(fired, [vcs.INDEX_LOCK_PAUSE], 'the first sleep was not the retry pause')
        self.assertTrue(lines[0].startswith('commit '))
        self.assertEqual(self.count(), self.before + 1)
        self.assertEqual(self.repo.git('show', '--name-only', '--format=', 'HEAD'), 'rulings/LEDGER.md')
        self.assertEqual(self.repo.git('status', '--porcelain', '--', 'rulings/LEDGER.md'), '')
        self.assertFalse(os.path.exists(self.lock))

    def test_a_lock_released_before_the_bound_gives_one_commit(self):
        self.hold()
        fired = self.free_at_first_pause(self.release)
        lines = vcs.finish(self.plan, self.got.id, None, 'Use this one.')
        self.assertEqual(fired, [vcs.INDEX_LOCK_PAUSE], 'the first sleep was not the retry pause')
        self.assertTrue(lines[0].startswith('commit '))
        self.assertEqual(self.count(), self.before + 1)
        self.assertEqual(self.repo.git('show', '--name-only', '--format=', 'HEAD'), 'rulings/LEDGER.md')
        self.assertEqual(self.repo.git('status', '--porcelain', '--', 'rulings/LEDGER.md'), '')

    def test_a_lock_held_past_the_bound_is_refused_naming_index_lock(self):
        self.hold()
        with self.assertRaises(Refused) as cm:
            vcs.finish(self.plan, self.got.id, None, 'Use this one.')
        text = str(cm.exception)
        self.assertIn('index.lock busy', text)
        self.assertIn('NOT committed', text)
        self.release()
        self.assertEqual(self.count(), self.before)

    def test_a_failure_that_is_not_the_lock_is_not_retried(self):
        plan = self.plan._replace(rel='rulings/missing.md')
        with mock.patch.object(vcs.time, 'sleep') as sleep:
            with self.assertRaises(Refused) as cm:
                vcs.commit_one(plan, 'ledger: x', 'the entry')
        sleep.assert_not_called()
        self.assertNotIn('index.lock', str(cm.exception))
        self.assertEqual(self.count(), self.before)


if __name__ == '__main__':
    unittest.main()
