"""kitlib.ledger.vcs.commit_one retries add+commit while git's index.lock is busy, and only then.

Real git in a throwaway repository (tests/gitfixture.py); the lock is a file made in its git folder."""
import os
import threading
import unittest
from unittest import mock

from kitlib.ledger import vcs, write
from kitlib.ledger.errors import Refused
from tests.gitfixture import TempRepo, fixture_env

MODEL = 'Test Model 1'
WAIT = 1.0      # the lock bound the tests run with


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
        try:
            os.remove(self.lock)
        except FileNotFoundError:
            pass

    def test_a_lock_released_before_the_bound_gives_one_commit(self):
        self.hold()
        timer = threading.Timer(0.4, self.release)
        timer.start()
        self.addCleanup(timer.cancel)
        lines = vcs.finish(self.plan, self.got.id, None, 'Use this one.')
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
