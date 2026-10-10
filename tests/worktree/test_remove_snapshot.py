"""The window between worktree_remove's last check and git's removal (docs/worktree-remove.md).

Ported in spirit from another repository's cleanup-window snapshot tests. That script deleted
the tree itself from a snapshot of what its check had verified. The kit never deletes a tree
itself: every check runs, then `git worktree remove` (never `--force`) is the only deleter, so
the claim worth pinning is that anything which appears or changes inside the window keeps the
tree registered and intact, and that a failing git is reported as intact or DESTROYED, never
retried. The window is a seam: `remove.clear_disposable` is the last step before git.
"""

import json
import os
import shutil
import unittest
from unittest import mock

from kitlib.worktree import remove
from tests import gitfixture


class WindowCase(unittest.TestCase):
    """A landed worktree `win` in a throwaway repo with a bare origin."""

    CONFIG = {'worktree_remove': {'disposable_untracked': ['*.tmp']}}

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.repo = os.path.realpath(self.fx.root)
        self.fx.write('.kit.json', json.dumps(self.CONFIG, indent=2) + '\n')
        self.fx.write('.gitignore', '*.log\n')
        self.fx.commit('config', '.kit.json', '.gitignore')
        self.fx.git('push', '-q', 'origin', 'main')
        self.path = os.path.realpath(self.fx.worktree('win', None))
        self.fx.write('tracked.txt', 'work\n', root=self.path)
        self.fx.commit('work', 'tracked.txt', cwd=self.path)
        self.git('merge', '-q', '--no-ff', '-m', 'Merge kit/win', 'kit/win')
        self.git('push', '-q', 'origin', 'main')

    def git(self, *args):
        return self.fx.git(*args, cwd=self.repo)

    def branches(self):
        return self.git('branch', '--format=%(refname:short)').split()

    def registered(self):
        return [os.path.normcase(os.path.realpath(line[len('worktree '):]))
                for line in self.git('worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')]

    def run_in_window(self, action, after=True):
        """Run the remove verb with `action(path)` done inside the window: after the real
        clear_disposable (the last step before git), or before it when `after` is False."""
        real = remove.clear_disposable

        def seam(ctx, path):
            if not after:
                action(path)
            done = real(ctx, path)
            if after:
                action(path)
            return done

        with mock.patch.object(remove, 'clear_disposable', seam):
            got = remove.remove(self.repo, 'win')
        return got.code, '\n'.join(got.lines)

    def assert_kept_intact(self, out):
        self.assertTrue(os.path.isdir(self.path), out)
        self.assertIn(os.path.normcase(self.path), self.registered(), out)
        self.assertIn('kit/win', self.branches(), out)
        self.assertEqual(self.git('-C', self.path, 'rev-parse', '--abbrev-ref', 'HEAD'),
                         'kit/win', out)


class FileAppearsInTheWindow(WindowCase):

    def test_control_without_a_window_change_removes(self):
        got = remove.remove(self.repo, 'win')
        out = '\n'.join(got.lines)
        self.assertEqual(got.code, 0, out)
        self.assertFalse(os.path.exists(self.path))
        self.assertNotIn('kit/win', self.branches())

    def test_a_file_written_after_the_checks_survives_and_the_tree_stays_registered(self):
        late = os.path.join(self.path, 'window_work.txt')
        rc, out = self.run_in_window(lambda p: self.fx.write('window_work.txt', 'work\n', root=p))
        self.assertEqual(rc, 1, out)
        self.assertIn('failed    git worktree remove exited', out)
        self.assertIn('intact   the worktree is still registered; nothing was destroyed', out)
        self.assertTrue(os.path.isfile(late), out)
        self.assert_kept_intact(out)

    def test_a_tracked_file_edited_after_the_checks_keeps_its_edit(self):
        edited = os.path.join(self.path, 'tracked.txt')

        def edit(p):
            with open(os.path.join(p, 'tracked.txt'), 'a', encoding='utf-8') as f:
                f.write('more\n')

        rc, out = self.run_in_window(edit)
        self.assertEqual(rc, 1, out)
        self.assertIn('intact   the worktree is still registered', out)
        with open(edited, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'work\nmore\n')
        self.assert_kept_intact(out)

    def test_a_tracked_file_deleted_after_the_checks_is_a_refusal_not_a_crash(self):
        rc, out = self.run_in_window(lambda p: os.remove(os.path.join(p, 'tracked.txt')))
        self.assertEqual(rc, 1, out)
        self.assertNotIn('Traceback', out)
        self.assertIn('failed    git worktree remove exited', out)
        self.assert_kept_intact(out)

    def test_an_ignored_file_written_between_the_dirty_check_and_the_reread_stops_git(self):
        # After the refusal check, before clear_disposable's own status read.
        late = os.path.join(self.path, 'run.log')
        rc, out = self.run_in_window(lambda p: self.fx.write('run.log', 'mine\n', root=p),
                                     after=False)
        self.assertEqual(rc, 1, out)
        self.assertIn('now has ignored files that are not build output (1): !! run.log', out)
        self.assertIn('git was NOT run', out)
        self.assertNotIn('failed    git worktree remove', out)       # git never ran
        self.assertTrue(os.path.isfile(late), out)
        self.assert_kept_intact(out)


class DisposableInTheWindow(WindowCase):

    def test_only_the_disposable_files_the_status_named_are_cleared(self):
        self.fx.write('scratch.tmp', 'x\n', root=self.path)
        # A second *.tmp written after clear_disposable's status read is not in its list.
        rc, out = self.run_in_window(lambda p: self.fx.write('later.tmp', 'y\n', root=p))
        self.assertEqual(rc, 1, out)
        self.assertIn('cleared   1 untracked file(s) that are not work', out)
        self.assertIn('scratch.tmp', out)
        self.assertFalse(os.path.exists(os.path.join(self.path, 'scratch.tmp')))
        self.assertTrue(os.path.isfile(os.path.join(self.path, 'later.tmp')), out)
        self.assert_kept_intact(out)


@unittest.skipUnless(os.name == 'nt', 'junctions are Windows')
class JunctionAppearsInTheWindow(WindowCase):

    def test_a_junction_created_after_the_checks_never_costs_its_target(self):
        import _winapi
        target = os.path.join(self.fx.base, 'elsewhere')
        os.makedirs(target)
        keep = os.path.join(target, 'precious.txt')
        with open(keep, 'w', encoding='utf-8') as f:
            f.write('outside the tree\n')
        junction = os.path.join(self.path, 'late_link')

        rc, out = self.run_in_window(lambda p: _winapi.CreateJunction(target, junction))
        try:
            self.assertEqual(rc, 1, out)
            self.assertTrue(os.path.isfile(keep), out)
            self.assert_kept_intact(out)
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)                   # the junction itself, not its target


class GitFails(WindowCase):
    """`git worktree remove` failing is reported by what is left, never retried or forced."""

    def failing_git(self, err, destroy=False):
        real = remove.git
        calls = []

        def fake(cwd, *args, **kw):
            if args[:2] == ('worktree', 'remove'):
                calls.append(args)
                if destroy:
                    shutil.rmtree(os.path.join(self.repo, '.git', 'worktrees', 'win'))
                return 1, '', err
            return real(cwd, *args, **kw)

        fake.calls = calls
        return fake

    def run_failing(self, fake):
        with mock.patch.object(remove, 'git', fake):
            got = remove.remove(self.repo, 'win')
        return got.code, '\n'.join(got.lines)

    def test_a_failure_with_the_entry_surviving_is_intact_and_keeps_the_branch(self):
        fake = self.failing_git('error: cannot remove: Permission denied')
        rc, out = self.run_failing(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn('failed    git worktree remove exited 1: error: cannot remove: Permission denied',
                      out)
        self.assertIn('intact   the worktree is still registered; nothing was destroyed', out)
        self.assertNotIn('DESTROYED', out)
        self.assertEqual(len(fake.calls), 1, 'git must run once, never retried')
        self.assertFalse({'--force', '-f'} & set(fake.calls[0]), fake.calls[0])
        self.assertNotIn('deleted   branch', out)
        self.assert_kept_intact(out)

    def test_a_failure_that_destroyed_the_entry_says_so_and_keeps_the_branch(self):
        fake = self.failing_git('error: cannot remove: Permission denied', destroy=True)
        rc, out = self.run_failing(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn('DESTROYED the administrative entry is gone and the directory is still there.',
                      out)
        self.assertIn('Something took hold between the check and the removal.', out)
        self.assertEqual(len(fake.calls), 1, 'git must run once, never retried')
        self.assertNotIn('deleted   branch', out)
        self.assertTrue(os.path.isdir(self.path))
        self.assertIn('kit/win', self.branches())

    def test_gits_force_advice_is_called_a_trap(self):
        fake = self.failing_git("fatal: 'win' contains modified or untracked files, use --force to delete it")
        rc, out = self.run_failing(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn("that message is git's, and its advice is a trap here", out)
        self.assertFalse({'--force', '-f'} & set(fake.calls[0]), fake.calls[0])


if __name__ == '__main__':
    unittest.main()
