"""kitlib.gate.cleanup: after a verified landing the landed branch goes - its remote branch, its
worktree (kitlib.worktree.remove, never forced) and its local branch (`git branch -d`). What main
does not contain, a worktree with work in it and a worktree in use are kept and named, and the
landing stands. Also the one-time sweep (`land.py --sweep-landed`) and the flags of both entry
points (docs/gate.md "Landing", "Branch cleanup").

Every repository, worktree and branch here is the test's own, in a temp folder with a bare remote."""
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land, result, runner
from kitlib.git import cmd
from kitlib.records import landing
from tests.gate import fixtures
from tests.platforms import windows_only

BRANCH = 'kit/feature'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAND_PY = os.path.join(KIT_ROOT, 'tools', 'land.py')
GATE_PY = os.path.join(KIT_ROOT, 'tools', 'gate.py')
WINDOWS = os.name == 'nt'
SHA = 'a' * 40
CLEAN_BRANCH = 'kitlib.gate.cleanup.clean_branch'


def sweep(*args, **kw):
    """kitlib.gate.cleanup.sweep, imported at the call (each test fails on its own without it)."""
    return importlib.import_module('kitlib.gate.cleanup').sweep(*args, **kw)


class CleanupCase(fixtures.GateRepoCase):
    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        env = mock.patch.dict(os.environ, fixtures.test_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def lander(self, branch, expect_main, expect_tip, **kw):
        kw.setdefault('git_env', self.env)
        kw.setdefault('book_kw', self.book_kw)
        kw.setdefault('log_dir', self.logs)
        fixtures.gate_full(self.repo.root, *(expect_tip if isinstance(expect_tip, list)
                                             else [expect_tip]))
        fixtures.queue_review(self.repo.root, branch, expect_tip)
        return land.Lander(self.repo.root, branch, expect_main, expect_tip, **kw)

    def pushed(self, name, files):
        """A worktree on kit/<name> with `files` committed, the branch pushed: (path, tip)."""
        wt, tip = self.branch(name, files)
        self.repo.git('push', '-q', 'origin', 'kit/%s' % name)
        return wt, tip

    def on_remote(self, branch):
        return self.repo.git('--git-dir', self.repo.remote, 'rev-parse', '--verify', '--quiet',
                             'refs/heads/%s' % branch, cwd=self.repo.base, check=False)

    def local(self, branch):
        return self.repo.git('rev-parse', '--verify', '--quiet', 'refs/heads/%s' % branch,
                             check=False)

    def registered(self):
        return [os.path.normcase(os.path.realpath(line[len('worktree '):]))
                for line in self.repo.git('worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')]

    def is_registered(self, wt):
        return os.path.normcase(os.path.realpath(wt)) in self.registered()

    def assert_all_kept(self, wt, branch, remote_sha):
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(self.is_registered(wt))
        self.assertTrue(self.local(branch))
        self.assertEqual(self.on_remote(branch), remote_sha)

    def assert_landed(self, out):
        self.assertEqual(out.code, G.OK, out.lines)
        landed = [l for l in out.lines if l.startswith('LANDED ')]
        self.assertEqual(len(landed), 1, out.lines)
        return out.facts['cleanup']


class AfterLanding(CleanupCase):
    def test_a_clean_landing_removes_all_three(self):
        wt, tip = self.pushed('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip, summary='add a').run()
        got = self.assert_landed(out)
        self.assertTrue(out.lines[-1].startswith('LANDED kit/feature on main'), out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.on_remote(BRANCH), '')            # the remote branch is gone
        self.assertFalse(os.path.exists(wt))                    # the worktree is gone
        self.assertFalse(self.is_registered(wt))
        self.assertEqual(self.local(BRANCH), '')                # the local branch is gone
        entry = got[BRANCH]
        self.assertEqual((entry['remote_branch'], entry['worktree'], entry['local_branch']),
                         ('deleted', 'removed', 'deleted'), out.lines)
        self.assertEqual(entry['kept'], [])
        text = '\n'.join(out.lines)
        self.assertIn('cleanup   kit/feature:', text)
        rec, why = result.read(self.repo.root, BRANCH, 'land')
        self.assertIsNone(why)
        self.assertEqual(rec['verdict'], 'pass')
        self.assertEqual(rec['cleanup'], entry)

    def test_a_dirty_worktree_is_kept_and_reported(self):
        for kind in ('untracked', 'modified'):
            with self.subTest(kind):
                name = 'dirty-%s' % kind
                branch = 'kit/%s' % name
                wt, tip = self.pushed(name, {'%s.py' % name: 'x = 1\n'})
                if kind == 'untracked':
                    self.repo.write('notes.txt', 'unsaved work\n', root=wt)
                else:
                    self.repo.write('%s.py' % name, 'x = 2\n', root=wt)
                main = self.repo.git('rev-parse', 'refs/heads/main')
                out = self.lander(branch, main, tip).run()
                entry = self.assert_landed(out)[branch]
                self.assertEqual(entry['worktree'], 'kept', out.lines)
                self.assertEqual(entry['local_branch'], 'kept', out.lines)
                self.assertEqual(entry['remote_branch'], 'deleted', out.lines)  # all in main
                self.assertTrue(os.path.isdir(wt))
                self.assertTrue(self.is_registered(wt))
                self.assertEqual(self.local(branch), tip)
                kept = '\n'.join(entry['kept'])
                self.assertIn('uncommitted or untracked', kept)
                self.assertIn(os.path.normcase(os.path.realpath(wt)),
                              os.path.normcase('\n'.join(out.lines)))
                self.assertIn('kept', '\n'.join(out.lines))
                rec, _ = result.read(self.repo.root, branch, 'land')
                self.assertEqual(rec['cleanup']['worktree'], 'kept')

    def test_an_unmerged_branch_is_kept(self):
        # The branch moves on (a commit and a push) while the landing gates: main never holds
        # that commit, so the worktree, the local branch and the remote branch all stay.
        wt, tip = self.pushed('feature', {'a.py': 'x = 1\n'})
        moved = []

        def merged_gate(lander):
            self.repo.write('b.py', 'later\n', root=wt)
            moved.append(self.repo.commit('later work', 'b.py', cwd=wt))
            self.repo.git('push', '-q', 'origin', BRANCH)
            # In the landing's own slot, as Lander.gate runs it: a second booking can wait
            # forever when confine(1) leaves one fake slot (under a gate's machine-lock run).
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      log_dir=lander.log_dir, book_kw=lander.book_kw,
                                      slot=lander.slot)
        out = self.lander(BRANCH, self.main, tip, merged_gate=merged_gate).run()
        entry = self.assert_landed(out)[BRANCH]
        self.assertEqual((entry['remote_branch'], entry['worktree'], entry['local_branch']),
                         ('kept', 'kept', 'kept'), out.lines)
        self.assert_all_kept(wt, BRANCH, moved[0])
        self.assertEqual(self.local(BRANCH), moved[0])
        kept = '\n'.join(entry['kept'])
        self.assertIn('not in main', kept)
        text = '\n'.join(out.lines)
        self.assertNotIn('branch -D', text)         # no hand command to discard work
        self.assertNotIn('--force', text)

    def test_a_remote_branch_ahead_of_the_landing_is_kept(self):
        wt, tip = self.pushed('feature', {'a.py': 'x = 1\n'})
        other = self.repo.worktree('elsewhere', branch='kit/elsewhere-base')
        self.repo.git('checkout', '-q', '--detach', tip, cwd=other)
        self.repo.write('c.py', 'pushed from elsewhere\n', root=other)
        ahead = self.repo.commit('elsewhere', 'c.py', cwd=other)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/%s' % (ahead, BRANCH))
        out = self.lander(BRANCH, self.main, tip).run()
        entry = self.assert_landed(out)[BRANCH]
        self.assertEqual(entry['remote_branch'], 'kept', out.lines)
        self.assertEqual(self.on_remote(BRANCH), ahead)
        self.assertIn('not in main', '\n'.join(entry['kept']))
        self.assertEqual((entry['worktree'], entry['local_branch']), ('removed', 'deleted'))

    def kill_the_delete_and_the_read_after_it(self, branch):
        """A patch of cmd._run under which `git branch -d` exits 1 having printed nothing (a git
        ended from outside, refs.KILLED_SHAPE), and so does every later rev-parse of the branch;
        every other git runs as before. That read said "gone"."""
        real = cmd.spawner()
        tried = []

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] == 'branch' and '-d' in argv:
                tried.append(argv)
                return 1, b'', b''
            if tried and argv[1] == 'rev-parse' and argv[-1].startswith('refs/heads/' + branch):
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        return mock.patch.object(cmd, '_run', side_effect=run), tried

    def assert_kept_unread(self, entry, lines, tip):
        self.assertEqual(entry['local_branch'], 'kept', lines)
        self.assertIn('local branch: whether it is gone could not be read', '\n'.join(entry['kept']))
        self.assertEqual(self.local(BRANCH), tip, 'the branch is still there')

    def test_a_killed_read_after_the_worktree_removal_is_not_reported_deleted(self):
        wt, tip = self.pushed('feature', {'a.py': 'x = 1\n'})
        patch, tried = self.kill_the_delete_and_the_read_after_it(BRANCH)
        with patch:
            out = self.lander(BRANCH, self.main, tip).run()
        entry = self.assert_landed(out)[BRANCH]
        self.assertTrue(tried, 'the cleanup ran no branch -d')
        self.assertEqual(entry['worktree'], 'removed', out.lines)
        self.assert_kept_unread(entry, out.lines, tip)

    def test_a_red_landing_removes_nothing(self):
        wt, tip = self.pushed('feature', {'RED': 'x\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assert_all_kept(wt, BRANCH, tip)
        self.assertIsNone(out.facts.get('cleanup'))
        self.assertIsNone(result.read(self.repo.root, BRANCH, 'land')[0]['cleanup'])

    def test_keep_branch_leaves_all_three(self):
        wt, tip = self.pushed('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip, keep_branch=True).run()
        entry = self.assert_landed(out)[BRANCH]
        self.assert_all_kept(wt, BRANCH, tip)
        self.assertEqual((entry['remote_branch'], entry['worktree'], entry['local_branch']),
                         ('skipped', 'skipped', 'skipped'), out.lines)
        self.assertIn('--keep-branch', '\n'.join(out.lines))
        rec, _ = result.read(self.repo.root, BRANCH, 'land')
        self.assertEqual(rec['cleanup'], entry)

    def test_a_cleanup_that_raises_is_a_warning_and_the_landing_stands(self):
        wt, tip = self.pushed('feature', {'a.py': 'x = 1\n'})
        with mock.patch(CLEAN_BRANCH, side_effect=RuntimeError('boom')):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        warn = [l for l in out.lines if l.startswith('WARNING: ') and 'cleanup' in l]
        self.assertTrue(warn, out.lines)
        self.assertIn('boom', warn[0])
        self.assertIn('--sweep-landed', warn[0])
        self.assertTrue(os.path.isdir(wt))


@windows_only('the holder probes are Windows ones')
class InUse(CleanupCase):
    def test_a_worktree_in_use_is_kept_and_reported(self):
        wt, tip = self.pushed('feature', {'a.py': 'x = 1\n'})
        with open(os.path.join(wt, 'a.py'), encoding='utf-8'):
            out = self.lander(BRANCH, self.main, tip).run()
        entry = self.assert_landed(out)[BRANCH]
        self.assertEqual((entry['worktree'], entry['local_branch']), ('kept', 'kept'), out.lines)
        self.assertEqual(entry['remote_branch'], 'deleted')
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(self.is_registered(wt))
        self.assertEqual(self.local(BRANCH), tip)
        self.assertIn('held', '\n'.join(entry['kept']))


class Sweep(CleanupCase):
    """The one-time sweep: the same rule for every branch main already contains."""

    def setUp(self):
        CleanupCase.setUp(self)
        self.done, _ = self.pushed('done', {'done.py': 'd\n'})
        self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge kit/done', 'kit/done')
        self.open_wt, self.open_tip = self.pushed('open', {'open.py': 'o\n'})     # not landed
        self.fresh = self.repo.worktree('fresh')                # no commits of its own
        self.repo.git('push', '-q', 'origin', 'kit/fresh')
        # landed long ago: only the remote branch and the local branch are left
        self.repo.git('branch', 'kit/old', self.repo.initial)
        old_wt = self.repo.path('.claude', 'worktrees', 'old')
        self.repo.git('worktree', 'add', '-q', old_wt, 'kit/old')
        self.repo.write('old.py', 'old\n', root=old_wt)
        self.old_tip = self.repo.commit('old work', 'old.py', cwd=old_wt)
        self.repo.git('push', '-q', 'origin', 'kit/old')
        self.repo.git('worktree', 'remove', old_wt)
        self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge kit/old', 'kit/old')
        # landed, and only on the remote
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/kit/remote-only' % self.old_tip)
        self.repo.git('push', '-q', 'origin', 'main')
        self.main = self.repo.git('rev-parse', 'main')

    def state(self):
        return {b: (self.local(b), self.on_remote(b))
                for b in ('kit/done', 'kit/open', 'kit/fresh', 'kit/old', 'kit/remote-only')}

    def test_a_dry_run_changes_nothing_and_names_what_would_go(self):
        before = self.state()
        out = sweep(self.repo.root, git_env=self.env)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.state(), before)
        self.assertTrue(os.path.isdir(self.done))
        for b in ('kit/done', 'kit/old', 'kit/remote-only'):
            self.assertIn('would', [l for l in out.lines if b + ':' in l][0], out.lines)
        for b in ('kit/open', 'kit/fresh'):         # not landed: never named as going
            self.assertFalse([l for l in out.lines if b + ':' in l and 'would' in l], out.lines)
        self.assertIn('--apply', out.lines[-1])

    def test_apply_cleans_what_main_contains_and_keeps_the_rest(self):
        out = sweep(self.repo.root, apply=True, git_env=self.env)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(os.path.exists(self.done))
        self.assertEqual(self.local('kit/done'), '')
        self.assertEqual(self.on_remote('kit/done'), '')
        self.assertEqual(self.local('kit/old'), '')
        self.assertEqual(self.on_remote('kit/old'), '')
        self.assertEqual(self.on_remote('kit/remote-only'), '')
        # not landed, and a fresh branch with no work of its own: untouched
        self.assertTrue(os.path.isdir(self.open_wt))
        self.assertEqual(self.local('kit/open'), self.open_tip)
        self.assertEqual(self.on_remote('kit/open'), self.open_tip)
        self.assertTrue(os.path.isdir(self.fresh))
        self.assertTrue(self.local('kit/fresh'))
        self.assertTrue(self.on_remote('kit/fresh'))
        self.assertEqual(self.local('main'), self.main)
        self.assertEqual(self.remote_main(), self.main)
        text = '\n'.join(out.lines)
        self.assertNotIn('branch -D', text)

    def test_the_sweep_waits_for_no_landing(self):
        lock = landing.LandingLock(self.repo.root)
        held = lock.take(['kit/someone'], pid=os.getpid())
        self.addCleanup(lock.release, held['token'])
        before = self.state()
        out = sweep(self.repo.root, apply=True, git_env=self.env)
        self.assertEqual(out.code, G.BUSY, out.lines)
        self.assertIn('landing lock', out.lines[0])
        self.assertEqual(self.state(), before)
        self.assertTrue(os.path.isdir(self.done))

    def test_the_entry_point_runs_the_sweep(self):
        r = subprocess.run([sys.executable, LAND_PY, '--sweep-landed', '-C', self.repo.root],
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           env=self.env, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertIn('kit/done:', r.stdout)
        self.assertTrue(os.path.isdir(self.done))           # a dry run
        r = subprocess.run([sys.executable, LAND_PY, '--sweep-landed', '--apply', '-C',
                            self.repo.root], capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=self.env, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertFalse(os.path.exists(self.done))


class Record(unittest.TestCase):
    def test_cleanup_is_null_unless_a_landing_ran_it(self):
        self.assertIsNone(result.build('kit/x', SHA, None, 'gate', G.OK)['cleanup'])
        entry = {'remote_branch': 'deleted', 'worktree': 'removed', 'local_branch': 'deleted',
                 'worktree_path': None, 'kept': []}
        self.assertEqual(result.build('kit/x', SHA, None, 'land', G.OK, cleanup=entry)['cleanup'],
                         entry)
        json.dumps(entry)


def load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Entry(unittest.TestCase):
    """Both entry points: --keep-branch reaches the landing; the sweep's usage errors exit 2."""

    def run_tool(self, *args):
        return subprocess.run([sys.executable, LAND_PY] + list(args), capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL,
                              env=fixtures.test_env())

    def test_sweep_usage(self):
        for args in (['--apply'], ['--sweep-landed', 'kit/x'],
                     ['--sweep-landed', '--expect-main', SHA], ['--sweep-landed', '--queue'],
                     ['--sweep-landed', '--keep-branch']):
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, G.USAGE, (args, r.stdout, r.stderr))
            self.assertIn('USAGE:', r.stderr)

    def landed(self, tool, argv):
        seen = {}

        def fake(root, branch, expect_main, expect_tip, **kw):
            seen.update(kw)
            return G.Outcome(G.OK, ['LANDED fake'])
        with mock.patch.object(land, 'land', fake), mock.patch('sys.stdout'):
            rc = tool.main(argv)
        self.assertEqual(rc, G.OK)
        return seen

    def test_keep_branch_reaches_the_landing(self):
        tip = ['kit/x', '--expect-main', SHA, '--expect-tip', 'b' * 40]
        land_py = load_tool(LAND_PY, 'kit_land_tool')
        self.assertIs(self.landed(land_py, tip + ['--keep-branch'])['keep_branch'], True)
        self.assertIs(self.landed(land_py, tip)['keep_branch'], False)
        gate_py = load_tool(GATE_PY, 'kit_gate_tool')
        self.assertIs(self.landed(gate_py, ['land'] + tip + ['--keep-branch'])['keep_branch'], True)
        self.assertIs(self.landed(gate_py, ['land'] + tip)['keep_branch'], False)


if __name__ == '__main__':
    unittest.main()
