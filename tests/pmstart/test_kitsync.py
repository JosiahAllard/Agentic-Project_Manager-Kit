"""kitlib.pmstart.kit_sync (docs/pm-data-branch.md "Session start: keep the kit current") on scratch
repositories with a bare remote; no network."""
import os
import unittest

from kitlib import pmstart
from kitlib.records import landing
from tests import gitfixture


class KitSyncTest(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.data = os.path.join(self.repo.base, 'data')      # the data worktree: the kit is its main checkout
        self.repo.git('worktree', 'add', '-q', self.data, '-b', 'pmdata')
        self.settings = {'path': self.data, 'branch': 'pmdata', 'remote': 'private'}

    def remote_ahead(self):
        """Commit on a side worktree and push it to the remote's main; the commit."""
        side = os.path.join(self.repo.base, 'side-%d' % len(os.listdir(self.repo.base)))
        self.repo.git('worktree', 'add', '-q', side, '-b', os.path.basename(side), 'main')
        self.repo.write('remote.txt', 'r\n', root=side)
        sha = self.repo.commit('remote work', 'remote.txt', cwd=side)
        self.repo.git('push', '-q', 'origin', '%s:main' % os.path.basename(side), cwd=side)
        return sha

    def config(self):
        with open(os.path.join(self.repo.common_dir(), 'config'), encoding='utf-8') as f:
            return f.read()

    def run_check(self):
        before = self.config()
        out = pmstart.kit_sync(self.settings)
        self.assertEqual(self.config(), before, '.git/config changed')
        return out

    def assertLeft(self, out, text):
        self.assertEqual(len(out), 1, out)
        self.assertIn(text, out[0])
        self.assertTrue(out[0].startswith('kit not updated') or 'diverged' in out[0], out)

    def test_unset_gives_nothing(self):
        self.assertEqual(pmstart.kit_sync(None), [])

    def test_up_to_date_prints_nothing(self):
        self.assertEqual(self.run_check(), [])

    def test_clean_fast_forward(self):
        sha = self.remote_ahead()
        out = self.run_check()
        self.assertEqual(out, ['kit updated to %s' % sha[:9]])
        self.assertEqual(self.repo.head(), sha)

    def test_dirty_left_alone(self):
        self.remote_ahead()
        before = self.repo.head()
        self.repo.write('README.md', 'changed\n')
        self.assertLeft(self.run_check(), 'uncommitted')
        self.assertEqual(self.repo.head(), before)

    def test_off_main_left_alone(self):
        self.remote_ahead()
        self.repo.git('checkout', '-q', '-b', 'side')
        self.assertLeft(self.run_check(), 'on side')
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/main'), self.repo.initial)

    def test_mid_merge_left_alone(self):
        self.remote_ahead()
        with open(os.path.join(self.repo.common_dir(), 'MERGE_HEAD'), 'w') as f:
            f.write(self.repo.initial + '\n')
        self.assertLeft(self.run_check(), 'merge is in progress')
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/main'), self.repo.initial)

    def test_landing_held_left_alone(self):
        self.remote_ahead()
        landing.LandingLock(self.repo.root).take('full', ['kit/x'])
        self.assertLeft(self.run_check(), 'landing lock is held')
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/main'), self.repo.initial)

    def test_offline_warns(self):
        self.repo.git('remote', 'set-url', 'origin', os.path.join(self.repo.base, 'missing.git'))
        self.assertLeft(self.run_check(), '')

    def test_diverged_not_merged(self):
        self.remote_ahead()
        self.repo.write('local.txt', 'l\n')
        mine = self.repo.commit('local work', 'local.txt')
        self.assertLeft(self.run_check(), 'diverged')
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/main'), mine)

    def test_in_checklist_and_failure_isolated(self):
        fakes = dict(handoff={'globber': lambda p: []}, reminders={'read': lambda p: '[]'},
                     liveness={'read': lambda p: '{}'})
        import datetime
        now = datetime.datetime(2026, 10, 6, 9, 0)
        sha = self.remote_ahead()
        txt = pmstart.checklist({'standing': ['x']}, now, procs=[], pm_data=lambda: self.settings,
                                **fakes)
        self.assertIn('kit updated to %s' % sha[:9], txt)

        def boom():
            raise RuntimeError('no config')
        txt = pmstart.checklist({'standing': ['x']}, now, procs=[], pm_data=boom, **fakes)
        self.assertIn('could not check kit_sync: RuntimeError: no config', txt)
        self.assertIn('standing rule: x', txt)


if __name__ == '__main__':
    unittest.main()
