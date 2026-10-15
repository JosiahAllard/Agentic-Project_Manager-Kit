"""A launched session's home worktree (kitlib/launcher/home.py, docs/launcher.md "The home
worktree"): the name and folder, ensure() against a scripted git, and on real git the worktree
itself, which the sweep, worktree_remove --check and worktree_remove never offer to remove."""
import os
import shutil
import tempfile
import unittest

from kitlib.git import cmd
from kitlib.launcher import home
from kitlib.launcher.settings import LaunchError
from tests.realgit import real_git
from tests.worktree.test_worktree_sweep import SweepCase

HEAD = 'a' * 40
OLD = 'b' * 40


class Names(unittest.TestCase):
    def test_the_stem_drops_the_recycle_count_then_the_account_tag(self):
        for name, stem in (('alpha-coordinator-a1', 'alpha-coordinator'),
                           ('alpha-coordinator-a2-r3', 'alpha-coordinator'),
                           ('alpha-coordinator-o2', 'alpha-coordinator'),
                           ('deep-net-race', 'deep-net-race'),
                           ('consultant-myrepo', 'consultant-myrepo'),
                           ('a1', 'a1')):
            self.assertEqual(home.stem_of(name), stem, name)

    def test_the_folder_is_under_the_hosts_worktrees_folder_with_a_safe_slug(self):
        main = os.path.join('Z:' + os.sep, 'repo')
        base = os.path.join(main, '.claude', 'worktrees')
        self.assertEqual(home.home_path(main, 'alpha-coord'), os.path.join(base, 'home-alpha-coord'))
        self.assertEqual(home.home_path(main + os.sep, 'Alpha Coord/x'), os.path.join(base, 'home-alpha-coord-x'))
        self.assertEqual(home.home_path(main, '..'), os.path.join(base, 'home-session'))


class ScriptedGit(object):
    """git as a script: HEAD, the worktree list and the home's status; every call is recorded and
    `worktree add` makes the folder."""

    def __init__(self, main, path, entry=None, status='', fail=()):
        self.main, self.path, self.entry, self.status, self.fail = main, path, entry, status, fail
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        key = ' '.join(args[:2])
        if key in self.fail:
            return cmd.Result(1, b'', 'fatal: %s refused' % key)
        if args[0] == 'rev-parse':
            return cmd.Result(0, (HEAD + '\n').encode(), '')
        if key == 'worktree list':
            out = 'worktree %s\nHEAD %s\nbranch refs/heads/main\n\n' % (self.main.replace(os.sep, '/'), HEAD)
            if self.entry is not None:
                out += 'worktree %s\n%s\n' % (self.path.replace(os.sep, '/'), self.entry)
            return cmd.Result(0, out.encode(), '')
        if key == 'worktree add':
            os.makedirs(args[-2])
            return cmd.Result(0, b'', '')
        if args[0] == '--no-optional-locks':
            return cmd.Result(0, self.status.encode(), '')
        return cmd.Result(0, b'', '')

    def writes(self):
        return [a for a, _ in self.calls if a[0] in ('worktree', 'checkout') and a[1] != 'list']


class Ensure(unittest.TestCase):
    def setUp(self):
        self.main = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.main, True)
        self.path = home.home_path(self.main, 'alpha-coord')

    def git(self, **kw):
        return ScriptedGit(self.main, self.path, **kw)

    def present(self):
        os.makedirs(self.path)

    def test_a_missing_home_is_added_detached_at_head_then_locked(self):
        g = self.git()
        out = home.ensure(self.main, self.path, git=g)
        self.assertEqual(g.writes(), [('worktree', 'add', '--detach', self.path, HEAD),
                                      ('worktree', 'lock', '--reason', home.LOCK_REASON % 'home-alpha-coord',
                                       self.path)])
        self.assertEqual(out, ['Created the home worktree %s, detached at %s.' % (self.path, HEAD[:10])])

    def test_what_if_writes_nothing(self):
        g = self.git()
        self.assertEqual(home.ensure(self.main, self.path, whatif=True, git=g),
                         ['Would create the home worktree %s, detached at %s, and lock it.' % (self.path, HEAD[:10])])
        self.assertEqual(g.writes(), [])
        self.assertFalse(os.path.exists(self.path))
        self.present()
        g = self.git(entry='HEAD %s\ndetached' % OLD)
        out = home.ensure(self.main, self.path, whatif=True, git=g)
        self.assertEqual(g.writes(), [])
        self.assertEqual(out, ['Would lock the home worktree %s.' % self.path,
                               'Would move the home worktree %s to %s.' % (self.path, HEAD[:10])])

    def test_a_clean_home_moves_to_head_and_a_current_one_is_left(self):
        self.present()
        g = self.git(entry='HEAD %s\ndetached\nlocked why' % OLD)
        self.assertEqual(home.ensure(self.main, self.path, git=g),
                         ['Moved the home worktree %s to %s.' % (self.path, HEAD[:10])])
        self.assertEqual(g.writes(), [('checkout', '-q', '--detach', HEAD)])
        self.assertEqual(g.calls[-1][1], self.path)
        g = self.git(entry='HEAD %s\ndetached\nlocked' % HEAD)
        self.assertEqual(home.ensure(self.main, self.path, git=g),
                         ['Home worktree %s is at %s.' % (self.path, HEAD[:10])])
        self.assertEqual(g.writes(), [])

    def test_an_unlocked_home_is_locked(self):
        self.present()
        g = self.git(entry='HEAD %s\ndetached' % HEAD)
        out = home.ensure(self.main, self.path, git=g)
        self.assertEqual(g.writes(), [('worktree', 'lock', '--reason', home.LOCK_REASON % 'home-alpha-coord',
                                       self.path)])
        self.assertEqual(out[0], 'Locked the home worktree %s.' % self.path)

    def test_a_dirty_home_is_named_and_never_moved(self):
        self.present()
        g = self.git(entry='HEAD %s\ndetached\nlocked' % OLD, status='?? stray.txt\n M README.md\n')
        out = home.ensure(self.main, self.path, git=g)
        self.assertEqual(g.writes(), [])
        self.assertEqual(len(out), 1)
        self.assertIn('has 2 uncommitted or untracked file(s), left as they are', out[0])
        self.assertIn('stray.txt, README.md', out[0])

    def test_a_failed_move_is_a_warning(self):
        self.present()
        g = self.git(entry='HEAD %s\ndetached\nlocked' % OLD, fail=('checkout -q',))
        out = home.ensure(self.main, self.path, git=g)
        self.assertIn('Warning: moving the home worktree', out[0])
        self.assertIn('it stays at %s' % OLD[:10], out[0])

    def test_refusals_change_nothing(self):
        cases = (
            (False, 'HEAD %s\ndetached\nlocked' % HEAD, 'is registered as a worktree but its folder is missing'),
            (True, None, 'exists but is not a worktree of'),
            (True, 'HEAD %s\nbranch refs/heads/kit/x' % HEAD, 'is on refs/heads/kit/x, not detached'),
        )
        for make, entry, msg in cases:
            if make and not os.path.exists(self.path):
                self.present()
            g = self.git(entry=entry)
            with self.assertRaises(LaunchError) as c:
                home.ensure(self.main, self.path, git=g)
            self.assertIn(msg, str(c.exception))
            self.assertEqual(g.writes(), [], msg)

    def test_a_failed_add_is_refused(self):
        g = self.git(fail=('worktree add',))
        with self.assertRaisesRegex(LaunchError, 'git worktree add .* failed'):
            home.ensure(self.main, self.path, git=g)


@real_git('worktree locking: the fake does not model git worktree lock')
class RealHome(SweepCase):
    """ensure() on a real repository with a bare origin; the home it makes is never a candidate
    for removal."""

    CONFIG = {}

    def setUp(self):
        super().setUp()
        self.path = home.home_path(self.repo, 'alpha-coord')

    def entry(self):
        return home._entry(self.repo, self.path, cmd.call)

    def test_it_is_made_detached_and_locked_then_follows_main_when_clean(self):
        out = home.ensure(self.repo, self.path)
        self.assertIn('Created the home worktree', out[0])
        e = self.entry()
        self.assertIsNone(e['branch'])
        self.assertIn('launcher home worktree', e['locked'])
        self.assertEqual(e['head'], self.fx.head())
        new = self.fx.write('later.txt', 'later\n')
        self.assertTrue(os.path.isfile(new))
        head = self.fx.commit('later', 'later.txt')
        self.assertEqual(home.ensure(self.repo, self.path), ['Moved the home worktree %s to %s.' % (self.path, head[:10])])
        self.assertTrue(os.path.isfile(os.path.join(self.path, 'later.txt')))
        self.assertEqual(home.ensure(self.repo, self.path), ['Home worktree %s is at %s.' % (self.path, head[:10])])

    def test_a_dirty_home_keeps_its_files_and_its_commit(self):
        home.ensure(self.repo, self.path)
        before = self.entry()['head']
        with open(os.path.join(self.path, 'stray.txt'), 'w', encoding='utf-8') as f:
            f.write('a relative write that missed the main checkout\n')
        self.fx.write('later.txt', 'later\n')
        self.fx.commit('later', 'later.txt')
        out = home.ensure(self.repo, self.path)
        self.assertIn('stray.txt', out[0])
        self.assertEqual(self.entry()['head'], before)
        self.assertTrue(os.path.isfile(os.path.join(self.path, 'stray.txt')))

    def test_the_sweep_check_and_remove_never_take_it(self):
        home.ensure(self.repo, self.path)
        self.git('push', '-q', 'origin', 'main')
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('skipped   %s [detached]: detached HEAD: no branch, never swept' % self.path, out)
        self.assertIn('removed 0, would remove 0', out)
        self.assertTrue(os.path.isdir(self.path), out)
        rc, out = self.run_verb('check')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('home-alpha-coord', out)
        rc, out = self.run_verb('remove', 'home-alpha-coord')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('cannot remove a locked working tree', out)
        self.assertTrue(os.path.isdir(self.path), out)
        self.assertIsNotNone(self.entry())


if __name__ == '__main__':
    unittest.main()
