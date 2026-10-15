"""Listing and removing home worktrees (kitlib/launcher/home.py list_homes and remove,
kitlib/launcher/start.py homes, `launch.py homes`; docs/launcher.md "The home worktree"): what is
listed, every refusal (nothing changed), and on real git a clean home removed and a dirty one kept."""
import os
import shutil
import sys
import tempfile
import unittest

from kitlib.git import cmd
from kitlib.git import repo as gitrepo
from kitlib.launcher import home, start
from kitlib.launcher.settings import LaunchError
from tests.launcher.fakes import FakeHost, Home, session
from tests.realgit import real_git
from tests.worktree.test_worktree_sweep import SweepCase

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402

HEAD = 'a' * 40
OLD = 'b' * 40


class Git(object):
    """git as a script: the worktree list (the main checkout, then `entries`: [(path, lines)]) and
    each worktree's status (`status`: {path: text}); every call is recorded."""

    def __init__(self, main, entries=(), status=None, fail=()):
        self.main, self.entries, self.status, self.fail = main, list(entries), dict(status or {}), fail
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        key = ' '.join(args[:2])
        if key in self.fail:
            return cmd.Result(1, b'', 'fatal: %s refused' % key)
        if key == 'worktree list':
            out = 'worktree %s\nHEAD %s\nbranch refs/heads/main\n\n' % (self.main.replace(os.sep, '/'), HEAD)
            for path, lines in self.entries:
                out += 'worktree %s\n%s\n\n' % (path.replace(os.sep, '/'), lines)
            return cmd.Result(0, out.encode(), '')
        if args[0] == '--no-optional-locks':
            return cmd.Result(0, self.status.get(cwd, '').encode(), '')
        return cmd.Result(0, b'', '')

    def writes(self):
        return [a for a, _ in self.calls if a[0] == 'worktree' and a[1] != 'list']


class Case(unittest.TestCase):
    def setUp(self):
        self.main = tempfile.mkdtemp(prefix='kit-homes-')
        self.addCleanup(shutil.rmtree, self.main, True)
        self.a = home.home_path(self.main, 'alpha-coord')
        self.b = home.home_path(self.main, 'deep-x')
        for p in (self.a, self.b):
            os.makedirs(p)


class List(Case):
    def test_only_home_folders_are_listed_with_head_lock_and_dirty_files(self):
        other = os.path.join(self.main, home.folder(), 'kit-x')
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached\nlocked why' % HEAD),
                            (self.b, 'HEAD %s\ndetached' % OLD),
                            (other, 'HEAD %s\nbranch refs/heads/kit/x' % HEAD)],
                status={self.b: '?? stray.txt\n!! docs/ops/state.local.md\n'})
        got = home.list_homes(self.main, git=g)
        self.assertEqual([os.path.normcase(h['path']) for h in got], [os.path.normcase(self.a), os.path.normcase(self.b)])
        self.assertEqual([(h['head'], h['locked'] is not None, h['dirty']) for h in got],
                         [(HEAD, True, []), (OLD, False, ['stray.txt', 'docs/ops/state.local.md'])])
        self.assertEqual(g.writes(), [])

    def test_a_home_whose_folder_is_missing_is_listed_as_such(self):
        shutil.rmtree(self.b)
        g = Git(self.main, [(self.b, 'HEAD %s\ndetached\nlocked' % HEAD)])
        h, = home.list_homes(self.main, git=g)
        self.assertIsNone(h['dirty'])
        self.assertEqual([c for c in g.calls if c[0][0] == '--no-optional-locks'], [])

    def test_a_failed_list_is_a_launch_error(self):
        with self.assertRaisesRegex(LaunchError, 'git worktree list in .* failed'):
            home.list_homes(self.main, git=Git(self.main, fail=('worktree list',)))


class Remove(Case):
    def test_a_clean_home_is_unlocked_then_removed_without_force(self):
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached\nlocked why' % HEAD)])
        self.assertEqual(home.remove(self.main, self.a, git=g), ['Removed the home worktree %s.' % self.a])
        self.assertEqual(g.writes(), [('worktree', 'unlock', self.a), ('worktree', 'remove', self.a)])
        status = [a for a, _ in g.calls if a[0] == '--no-optional-locks'][0]
        self.assertIn('--ignored', status)      # an ignored stray write (a state note) counts as dirty
        self.assertIn('--untracked-files=all', status)

    def test_an_unlocked_home_is_removed_without_an_unlock(self):
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached' % HEAD)])
        home.remove(self.main, self.a, git=g)
        self.assertEqual(g.writes(), [('worktree', 'remove', self.a)])

    def test_what_if_writes_nothing(self):
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached\nlocked' % HEAD)])
        self.assertEqual(home.remove(self.main, self.a, whatif=True, git=g),
                         ['Would unlock and remove the home worktree %s.' % self.a])
        self.assertEqual(g.writes(), [])

    def test_refusals_change_nothing(self):
        other = os.path.join(self.main, home.folder(), 'kit-x')
        os.makedirs(other)
        cases = (
            (self.a, [], 'is not a worktree of'),
            (other, [(other, 'HEAD %s\ndetached' % HEAD)], 'is not a home worktree'),
            (self.a, [(self.a, 'HEAD %s\nbranch refs/heads/kit/x\nlocked' % HEAD)],
             'is on refs/heads/kit/x, not detached'),
        )
        for path, entries, msg in cases:
            g = Git(self.main, entries)
            with self.assertRaisesRegex(LaunchError, msg + '.*Nothing was removed'):
                home.remove(self.main, path, git=g)
            self.assertEqual(g.writes(), [], msg)

    def test_a_dirty_home_is_refused_and_its_files_named(self):
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached\nlocked' % HEAD)],
                status={self.a: '?? stray.txt\n M README.md\n!! docs/ops/state.local.md\n'})
        with self.assertRaises(LaunchError) as c:
            home.remove(self.main, self.a, git=g)
        self.assertIn('has 3 uncommitted, untracked or ignored file(s): stray.txt, README.md, '
                      'docs/ops/state.local.md', str(c.exception))
        self.assertIn('Nothing was removed', str(c.exception))
        self.assertEqual(g.writes(), [])

    def test_a_missing_folder_is_refused_with_the_prune_steps(self):
        shutil.rmtree(self.a)
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached\nlocked' % HEAD)])
        with self.assertRaisesRegex(LaunchError, 'its folder is missing; run .*worktree unlock.*worktree prune'):
            home.remove(self.main, self.a, git=g)
        self.assertEqual(g.writes(), [])

    def test_a_failed_remove_locks_it_again(self):
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached\nlocked' % HEAD)], fail=('worktree remove',))
        with self.assertRaisesRegex(LaunchError, 'git worktree remove .* failed: .*; it was locked again'):
            home.remove(self.main, self.a, git=g)
        self.assertEqual(g.writes(), [('worktree', 'unlock', self.a), ('worktree', 'remove', self.a),
                                      ('worktree', 'lock', '--reason', home.LOCK_REASON % 'home-alpha-coord', self.a)])

    def test_a_failed_unlock_removes_nothing(self):
        g = Git(self.main, [(self.a, 'HEAD %s\ndetached\nlocked' % HEAD)], fail=('worktree unlock',))
        with self.assertRaisesRegex(LaunchError, 'git worktree unlock .* failed.*Nothing was removed'):
            home.remove(self.main, self.a, git=g)
        self.assertEqual(g.writes(), [('worktree', 'unlock', self.a)])


class Verb(unittest.TestCase):
    """start.homes: the main checkout, a session name or a home-<name> folder, a running session."""

    def setUp(self):
        self.h = Home(self, repos=('alpha',))
        self.repo = self.h.repos['alpha']
        self.host = FakeHost(self.h.root)
        self.host.where = gitrepo.MAIN
        self.a = home.home_path(self.repo, 'alpha-coord')
        os.makedirs(self.a)
        self.git = Git(self.repo, [(self.a, 'HEAD %s\ndetached\nlocked' % HEAD)])

    def homes(self, **kw):
        args = dict(repo='alpha', git=self.git)
        args.update(kw)
        return start.homes(self.h.settings, self.host, **args)

    def test_list(self):
        out = self.homes()
        self.assertEqual(out[0], 'Home worktrees of %s:' % self.repo)
        self.assertIn('home-alpha-coord  at %s  locked  clean' % HEAD[:10], out[1])
        self.git.entries = []
        self.assertEqual(self.homes(), ['No home worktrees in %s.' % self.repo])

    def test_list_names_a_running_session(self):
        self.host.procs = [session(4242, 'alpha-coord')]
        self.assertIn('in use by the running session alpha-coord (PID 4242)', self.homes()[1])

    def test_remove_by_session_name_or_folder_name(self):
        for name in ('alpha-coord', 'home-alpha-coord', 'Alpha-Coord'):
            self.git.calls = []
            self.assertEqual(self.homes(remove=name, whatif=True),
                             ['Would unlock and remove the home worktree %s.' % self.a])
        self.assertEqual(self.homes(remove='alpha-coord'), ['Removed the home worktree %s.' % self.a])

    def test_a_running_sessions_home_is_refused(self):
        self.host.procs = [session(4242, 'Alpha-Coord')]
        with self.assertRaisesRegex(LaunchError, "a session named 'Alpha-Coord' is running \\(PID 4242\\) in its "
                                                 "home worktree .*; stop it first. Nothing was removed."):
            self.homes(remove='alpha-coord')
        self.assertEqual(self.git.writes(), [])

    def test_only_a_main_checkout_has_homes(self):
        for kind in (gitrepo.LINKED, gitrepo.NONE, gitrepo.UNKNOWN):
            self.host.where = kind
            with self.assertRaisesRegex(LaunchError, 'is not a main checkout'):
                self.homes()
        self.assertEqual(self.git.calls, [])

    def test_the_verb_parses_and_reaches_start_homes(self):
        a = launch.parser().parse_args(['homes', '--repo', 'alpha', '--remove', 'deep-x', '--what-if'])
        self.assertEqual((a.cmd, a.repo, a.repo_path, a.remove, a.what_if), ('homes', 'alpha', None, 'deep-x', True))
        a = launch.parser().parse_args(['homes', '--repo-path', self.repo])
        self.assertEqual((a.remove, a.what_if), (None, False))
        for bad in (['homes'], ['homes', '--repo', 'a', '--repo-path', 'b']):
            with self.assertRaises(SystemExit):
                launch.parser().parse_args(bad)


@real_git('worktree locking: the fake does not model git worktree lock')
class RealRemove(SweepCase):
    CONFIG = {}

    def setUp(self):
        super().setUp()
        self.path = home.home_path(self.repo, 'deep-x')
        home.ensure(self.repo, self.path)

    def test_a_clean_home_is_listed_then_removed(self):
        h, = home.list_homes(self.repo)
        self.assertEqual(os.path.normcase(os.path.normpath(h['path'])), os.path.normcase(self.path))
        self.assertIsNotNone(h['locked'])
        self.assertEqual(h['dirty'], [])
        self.assertEqual(home.remove(self.repo, self.path), ['Removed the home worktree %s.' % self.path])
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(home.list_homes(self.repo), [])

    def test_an_untracked_or_ignored_file_keeps_the_home(self):
        self.fx.write('.gitignore', 'ignored.txt\n')
        self.fx.commit('ignore', '.gitignore')
        home.ensure(self.repo, self.path)                # moved to the commit with the .gitignore
        for name in ('ignored.txt', 'stray.txt'):
            f = os.path.join(self.path, name)
            with open(f, 'w', encoding='utf-8') as fh:
                fh.write('a stray write\n')
            with self.assertRaisesRegex(LaunchError, "1 uncommitted, untracked or ignored file\\(s\\): %s" % name):
                home.remove(self.repo, self.path)
            self.assertTrue(os.path.isfile(f))
            self.assertIsNotNone(home._entry(self.repo, self.path, cmd.call)['locked'])
            os.remove(f)
        home.remove(self.repo, self.path)
        self.assertFalse(os.path.exists(self.path))


if __name__ == '__main__':
    unittest.main()
