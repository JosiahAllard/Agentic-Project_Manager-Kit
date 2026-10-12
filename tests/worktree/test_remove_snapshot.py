"""The window between worktree_remove's last check and git's removal (docs/worktree-remove.md).

Ported in spirit from another repository's cleanup-window snapshot tests. That script deleted
the tree itself from a snapshot of what its check had verified. The kit never deletes a tree
itself: every check runs, then `git worktree remove` (never `--force`) is the only deleter, so
the claim worth pinning is that anything which appears or changes inside the window keeps the
tree registered and intact, and that a failing git is reported as intact or DESTROYED, never
retried. The window has two seams. `remove.clear_disposable` is the last step of the verbs before
`remove.git_remove`; what appears after it is caught by `remove.last_look`, the status read
inside `git_remove`. What appears after `last_look` is left to git's own refusal, which names
modified and untracked files and never an ignored one.
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
    IGNORE = '*.log\n'

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.repo = os.path.realpath(self.fx.root)
        self.fx.write('.kit.json', json.dumps(self.CONFIG, indent=2) + '\n')
        self.fx.write('.gitignore', self.IGNORE)
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
        clear_disposable (the verb's last step before git_remove), or before it when `after` is
        False."""
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

    def run_after_last_look(self, action):
        """Run the remove verb with `action(path)` done after the real last_look, the status read
        inside git_remove: nothing but git's own refusal is left to keep the tree."""
        real = remove.last_look

        def seam(ctx, path):
            clean = real(ctx, path)
            action(path)
            return clean

        with mock.patch.object(remove, 'last_look', seam):
            got = remove.remove(self.repo, 'win')
        return got.code, '\n'.join(got.lines)

    def git_calls(self):
        """A stand-in for `remove.git` that records each `worktree remove` and runs every call."""
        real = remove.git
        calls = []

        def spy(cwd, *args, **kw):
            if args[:2] == ('worktree', 'remove'):
                calls.append(args)
            return real(cwd, *args, **kw)

        spy.calls = calls
        return spy

    def assert_last_look_refuses(self, action, named):
        """Run the remove verb with `action(path)` done after clear_disposable and assert that
        `last_look` refused it: its line carries the one status line `named`, and `git worktree
        remove` was never called. Returns the output."""
        spy = self.git_calls()
        with mock.patch.object(remove, 'git', spy):
            rc, out = self.run_in_window(action)
        self.assertEqual(rc, 1, out)
        self.assertEqual(spy.calls, [], 'git worktree remove must not run')
        self.assertIn('failed    %s changed after it was checked - uncommitted or untracked files'
                      ' (1): %s' % (self.path, named), out)
        self.assertIn('git was NOT run', out)
        self.assertNotIn('removed   ', out)
        self.assert_kept_intact(out)
        return out

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
        rc, out = self.run_after_last_look(
            lambda p: self.fx.write('window_work.txt', 'work\n', root=p))
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

        rc, out = self.run_after_last_look(edit)
        self.assertEqual(rc, 1, out)
        self.assertIn('intact   the worktree is still registered', out)
        with open(edited, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'work\nmore\n')
        self.assert_kept_intact(out)

    def test_a_tracked_file_deleted_after_the_checks_is_a_refusal_not_a_crash(self):
        rc, out = self.run_after_last_look(lambda p: os.remove(os.path.join(p, 'tracked.txt')))
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

    def test_a_file_written_after_clear_disposable_is_named_before_git_runs(self):
        late = os.path.join(self.path, 'window_work.txt')
        rc, out = self.run_in_window(lambda p: self.fx.write('window_work.txt', 'work\n', root=p))
        self.assertTrue(os.path.isfile(late), out)
        self.assertEqual(rc, 1, out)
        self.assertIn('failed    %s changed after it was checked - uncommitted or untracked files'
                      ' (1): ?? window_work.txt; git was NOT run' % self.path, out)
        self.assertNotIn('failed    git worktree remove', out)       # git never ran
        self.assert_kept_intact(out)

    def test_a_tracked_file_edited_after_clear_disposable_is_named_before_git_runs(self):
        def edit(p):
            with open(os.path.join(p, 'tracked.txt'), 'a', encoding='utf-8') as f:
                f.write('more\n')

        out = self.assert_last_look_refuses(edit, ' M tracked.txt; git was NOT run')
        with open(os.path.join(self.path, 'tracked.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'work\nmore\n', out)

    def test_a_tracked_file_deleted_after_clear_disposable_is_named_before_git_runs(self):
        out = self.assert_last_look_refuses(lambda p: os.remove(os.path.join(p, 'tracked.txt')),
                                            ' D tracked.txt; git was NOT run')
        self.assertNotIn('Traceback', out)


class FolderGoneInTheWindow(WindowCase):

    def test_a_folder_gone_after_clear_disposable_is_refused_and_stays_registered(self):
        # No status can be read where the folder was, so git is not run: the entry is left for the
        # next run to report, never dropped unseen.
        moved = os.path.join(self.fx.base, 'moved_away')
        spy = self.git_calls()
        with mock.patch.object(remove, 'git', spy):
            rc, out = self.run_in_window(lambda p: os.rename(p, moved))
        self.assertEqual(rc, 1, out)
        self.assertEqual(spy.calls, [], 'git worktree remove must not run')
        self.assertIn('failed    %s: its status could not be read again (' % self.path, out)
        self.assertIn('git was NOT run', out)
        self.assertNotIn('removed   ', out)
        self.assertFalse(os.path.exists(self.path), out)
        self.assertTrue(os.path.isfile(os.path.join(moved, 'tracked.txt')), out)
        self.assertIn(os.path.normcase(self.path), self.registered(), out)
        self.assertIn('kit/win', self.branches(), out)


class IgnoredAfterTheLastStatusRead(WindowCase):
    """A file git ignores, written after the verb's last status read: `git worktree remove` does
    not list it and deletes it with the tree, so `git_remove` reads the status itself first."""

    CONFIG = {'worktree_remove': {'disposable_untracked': ['*.tmp'], 'build_output': ['out/']}}
    IGNORE = '*.log\nout/\n'

    def test_an_ignored_file_written_after_clear_disposable_survives_and_git_is_not_run(self):
        late = os.path.join(self.path, 'run.log')
        spy = self.git_calls()
        with mock.patch.object(remove, 'git', spy):
            rc, out = self.run_in_window(lambda p: self.fx.write('run.log', 'mine\n', root=p))
        self.assertTrue(os.path.isfile(late), out)
        self.assertEqual(rc, 1, out)
        self.assertEqual(spy.calls, [], 'git worktree remove must not run')
        self.assertIn('failed    %s changed after it was checked - ignored files that are not build'
                      ' output (1): !! run.log' % self.path, out)
        self.assertIn('git was NOT run', out)
        self.assertNotIn('removed   ', out)
        self.assert_kept_intact(out)

    def test_a_keeper_written_into_build_output_after_clear_disposable_survives(self):
        late = os.path.join(self.path, 'out', 'dev.db')
        rc, out = self.run_in_window(lambda p: self.fx.write('out/dev.db', 'rows\n', root=p))
        self.assertTrue(os.path.isfile(late), out)
        self.assertEqual(rc, 1, out)
        self.assertIn('!! out/dev.db (a keeper inside build output)', out)
        self.assertIn('git was NOT run', out)
        self.assertNotIn('removed   ', out)
        self.assert_kept_intact(out)

    def test_build_output_written_after_clear_disposable_goes_with_the_tree(self):
        rc, out = self.run_in_window(lambda p: self.fx.write('out/app.bin', 'built\n', root=p))
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s' % self.path, out)
        self.assertFalse(os.path.exists(self.path))
        self.assertNotIn('kit/win', self.branches())

    def test_git_remove_itself_refuses_a_tree_that_holds_an_ignored_file(self):
        # No verb in front: every caller of git_remove gets the read, whatever it checked itself.
        late = os.path.join(self.path, 'run.log')
        ctx, refused = remove.open_ctx(self.repo)
        self.assertIsNone(refused)
        registered_path = remove.registrations(ctx.main)[remove.canon(self.path)][0]
        self.fx.write('run.log', 'mine\n', root=self.path)
        gone = remove.git_remove(ctx, self.path, registered_path)
        out = '\n'.join(ctx.out)
        self.assertTrue(os.path.isfile(late), out)
        self.assertIs(gone, False, out)
        self.assertIn('ignored files that are not build output (1): !! run.log', out)
        self.assertIn('git was NOT run', out)
        self.assert_kept_intact(out)

    def test_git_remove_refuses_a_tree_whose_status_cannot_be_read(self):
        ctx, refused = remove.open_ctx(self.repo)
        self.assertIsNone(refused)
        registered_path = remove.registrations(ctx.main)[remove.canon(self.path)][0]
        spy = self.git_calls()
        unread = (None, None, None, 'git status exited 128: fatal: index file corrupt')
        with mock.patch.object(remove, 'git', spy), \
                mock.patch.object(remove, 'dirty', lambda ctx, path: unread):
            gone = remove.git_remove(ctx, self.path, registered_path)
        out = '\n'.join(ctx.out)
        self.assertTrue(os.path.isdir(self.path), out)
        self.assertIs(gone, False, out)
        self.assertEqual(spy.calls, [], 'git worktree remove must not run')
        self.assertIn('failed    %s: its status could not be read again (git status exited 128:'
                      ' fatal: index file corrupt); git was NOT run' % self.path, out)
        self.assert_kept_intact(out)

    def test_an_abandon_keeps_the_tree_and_the_tag_when_an_ignored_file_appears_late(self):
        path = os.path.realpath(self.fx.worktree('open', None))
        self.fx.write('open.txt', 'unlanded\n', root=path)
        tip = self.fx.commit('unlanded', 'open.txt', cwd=path)
        late = os.path.join(path, 'run.log')
        real = remove.clear_disposable

        def seam(ctx, p):
            done = real(ctx, p)
            self.fx.write('run.log', 'mine\n', root=p)
            return done

        with mock.patch.object(remove, 'clear_disposable', seam):
            got = remove.abandon(self.repo, 'open', 'superseded')
        out = '\n'.join(got.lines)
        self.assertTrue(os.path.isfile(late), out)
        self.assertEqual(got.code, 1, out)
        self.assertIn('tagged    archive/open -> %s' % tip[:10], out)
        self.assertIn('ignored files that are not build output (1): !! run.log', out)
        self.assertIn('git was NOT run', out)
        self.assertIn('  kept     tag archive/open stays on %s, and branch kit/open' % tip[:10], out)
        self.assertNotIn('removed   ', out)
        self.assertIn(os.path.normcase(path), self.registered(), out)
        self.assertIn('kit/open', self.branches(), out)
        self.assertEqual(self.git('rev-parse', 'archive/open^{commit}'), tip)


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

    def test_a_disposable_file_written_after_clear_disposable_is_named_and_git_is_not_run(self):
        # last_look lists it as untracked: the verbs have cleared such files by then.
        out = self.assert_last_look_refuses(lambda p: self.fx.write('later.tmp', 'y\n', root=p),
                                            '?? later.tmp; git was NOT run')
        self.assertTrue(os.path.isfile(os.path.join(self.path, 'later.tmp')), out)


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

        rc, out = self.run_after_last_look(lambda p: _winapi.CreateJunction(target, junction))
        try:
            self.assertEqual(rc, 1, out)
            self.assertTrue(os.path.isfile(keep), out)
            self.assert_kept_intact(out)
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)                   # the junction itself, not its target

    def test_a_junction_created_after_clear_disposable_is_named_before_git_runs(self):
        import _winapi
        target = os.path.join(self.fx.base, 'elsewhere')
        os.makedirs(target)
        keep = os.path.join(target, 'precious.txt')
        with open(keep, 'w', encoding='utf-8') as f:
            f.write('outside the tree\n')
        junction = os.path.join(self.path, 'late_link')
        try:
            # The status lists what the junction leads to as untracked; that is what stops git.
            out = self.assert_last_look_refuses(
                lambda p: _winapi.CreateJunction(target, junction), '?? late_link/')
            self.assertTrue(os.path.isfile(keep), out)
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
