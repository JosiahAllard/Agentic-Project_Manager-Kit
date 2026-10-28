"""kitlib.pmstart.kit_sync (docs/pm-data-branch.md "Session start: keep the kit current") on scratch
repositories with a bare remote; no network."""
import datetime
import os
import unittest
from unittest import mock

from kitlib import pmstart
from kitlib.pmstart import fglock
from kitlib.git import cmd as gitcmd
from kitlib.records import landing
from tests import gitfixture
from tests.home import temp_home
from tests.pmstart import HANDOFF_FAKE, keep_off_launcher_repos
from tests.realgit import real_git


def setUpModule():
    """The checklist must not read this machine's foreground lock value (fglock has its own tests)."""
    global _orig_read_live
    _orig_read_live = fglock.read_live
    fglock.read_live = lambda user32=None: 0
    global _orig_beat
    _orig_beat = pmstart.read_heartbeat
    pmstart.read_heartbeat = lambda: None     # nor this machine's pm_watch heartbeat
    keep_off_launcher_repos()


def tearDownModule():
    fglock.read_live = _orig_read_live
    pmstart.read_heartbeat = _orig_beat


class Base(unittest.TestCase):
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
        # the push also moved refs/remotes/origin/main here; put it back so the check must fetch
        self.repo.git('update-ref', 'refs/remotes/origin/main', self.repo.initial)
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

    def spy(self, fail=None):
        """Patch kitlib.git.cmd.run: record (args, timeout) of each call and pass it on; a call whose
        subcommand is `fail` answers (None, 'boom')."""
        calls = []
        real = gitcmd.run

        def run(args, cwd, **kw):
            calls.append((list(args), kw.get('timeout')))
            if fail and gitcmd.subcommand(args) == fail:
                return None, 'boom'
            return real(args, cwd, **kw)
        patcher = mock.patch.object(gitcmd, 'run', run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def main_tip(self):
        return self.repo.git('rev-parse', 'refs/heads/main')

    def refs(self):
        return self.repo.git('for-each-ref', '--format=%(refname) %(objectname)')



class KitSyncTest(Base):
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
        landing.LandingLock(self.repo.root).take(['kit/x'])
        self.assertLeft(self.run_check(), 'landing lock is held')
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/main'), self.repo.initial)

    def test_offline_warns(self):
        self.repo.git('remote', 'set-url', 'origin', os.path.join(self.repo.base, 'missing.git'))
        calls = self.spy()
        out = self.run_check()
        self.assertLeft(out, 'kit not updated: ')
        self.assertEqual([c for c, _t in calls if c[0] == 'fetch'][0][:2], ['fetch', '--no-tags'])
        self.assertNotIn('uncommitted', out[0])
        self.assertNotIn('in progress', out[0])

    def test_fetch_failure_wording_and_timeout(self):
        self.remote_ahead()
        calls = self.spy(fail='fetch')
        self.assertEqual(self.run_check(), ['kit not updated: boom'])
        fetches = [(c, t) for c, t in calls if c[0] == 'fetch']
        self.assertEqual(fetches, [(['fetch', '--no-tags', 'origin',
                                     'refs/heads/main:refs/remotes/origin/main'],
                                    pmstart.FETCH_TIMEOUT)])
        self.assertEqual(pmstart.FETCH_TIMEOUT, 3)
        self.assertEqual(self.main_tip(), self.repo.initial)

    def test_ff_only_failure_reported(self):
        self.remote_ahead()
        self.spy(fail='merge')
        self.assertEqual(self.run_check(), ['kit not updated: boom'])
        self.assertEqual(self.main_tip(), self.repo.initial)

    def test_unreadable_landing_lock_counts_as_held(self):
        self.remote_ahead()
        with open(os.path.join(self.repo.common_dir(), landing.LOCK_NAME), 'w') as f:
            f.write('not json')
        self.assertLeft(self.run_check(), 'landing lock cannot be read')
        self.assertEqual(self.main_tip(), self.repo.initial)

    def test_cherry_pick_revert_rebase_left_alone(self):
        self.remote_ahead()
        gd = self.repo.common_dir()
        for name, text, is_dir in (('CHERRY_PICK_HEAD', 'cherry-pick', False),
                                   ('REVERT_HEAD', 'revert', False),
                                   ('rebase-merge', 'rebase', True),
                                   ('rebase-apply', 'rebase', True)):
            path = os.path.join(gd, name)
            if is_dir:
                os.mkdir(path)
            else:
                with open(path, 'w') as f:
                    f.write(self.repo.initial + '\n')
            with self.subTest(name):
                self.assertLeft(self.run_check(), text)
                self.assertEqual(self.main_tip(), self.repo.initial)
            if is_dir:
                os.rmdir(path)
            else:
                os.remove(path)

    def test_diverged_not_merged(self):
        self.remote_ahead()
        self.repo.write('local.txt', 'l\n')
        mine = self.repo.commit('local work', 'local.txt')
        self.assertLeft(self.run_check(), 'diverged')
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/main'), mine)

    def test_in_checklist_and_failure_isolated(self):
        fakes = dict(handoff=HANDOFF_FAKE, reminders={'read': lambda p: '[]'},
                     liveness={'read': lambda p: '{}'})
        now = datetime.datetime(2026, 10, 6, 9, 0)
        sha = self.remote_ahead()
        temp_home(self)     # the hub check's current account reads an empty machine config
        txt = pmstart.checklist({'standing': ['x']}, now, procs=[], pm_data=lambda: self.settings,
                                **fakes)
        self.assertIn('kit updated to %s' % sha[:9], txt)

        def boom():
            raise RuntimeError('no config')
        txt = pmstart.checklist({'standing': ['x']}, now, procs=[], pm_data=boom, **fakes)
        self.assertIn('could not check kit_sync: RuntimeError: no config', txt)
        self.assertIn('standing rule: x', txt)


@real_git('fetch and merge --ff-only against a real bare remote: which refs and files really change')
class RealKitSyncTest(Base):
    def setUp(self):
        super().setUp()
        # the production 3 s fetch limit (asserted in KitSyncTest) is for a session start; a real
        # fetch on a fully booked machine can exceed it, so this test gives the fetch room
        patcher = mock.patch.object(pmstart, 'FETCH_TIMEOUT', 60)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fast_forward_moves_only_the_tracking_ref_and_main(self):
        sha = self.remote_ahead()
        # a tag on the remote that a default fetch would follow into refs/tags
        self.repo.git('tag', 'remote-tag', sha)
        self.repo.git('push', '-q', 'origin', 'remote-tag')
        self.repo.git('tag', '-d', 'remote-tag')
        before = dict(l.split(' ') for l in self.refs().splitlines())
        out = self.run_check()          # also asserts .git/config is byte-identical
        self.assertEqual(out, ['kit updated to %s' % sha[:9]])
        after = dict(l.split(' ') for l in self.refs().splitlines())
        moved = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(moved, {'refs/remotes/origin/main', 'refs/heads/main'})
        self.assertEqual([k for k in after if k.startswith('refs/tags/')], [])


if __name__ == '__main__':
    unittest.main()
