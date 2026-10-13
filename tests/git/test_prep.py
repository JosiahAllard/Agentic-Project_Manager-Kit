"""kitlib.git.prep: the first-use repo prep (docs/new-worktree.md "First-use prep"; decisions 23 and 25
of docs/multi-host-plan.md), against throwaway repositories with a bare remote (tests/gitfixture.py).

Every case runs with the fixture's environment (no global or system git config) and its own
stand-in kit folder, so no case points a repository at this checkout's githooks.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import kitroot
from kitlib.git import cmd as gitcmd
from kitlib.git import prep
from tests import gitfixture

FOLDERS = ('.claude/worktrees', '.codex/worktrees')
LINES = ['/.claude/worktrees/', '/.codex/worktrees/']


def clean_env(extra=None):
    """The fixture's environment without config passed through the environment
    (GIT_CONFIG_COUNT/KEY_n/VALUE_n), which no repository of the user's would see either."""
    env = {k: v for k, v in gitfixture.fixture_env(extra).items()
           if not k.upper().startswith(('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))}
    return env


class PrepCase(unittest.TestCase):
    EXTRA_ENV = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kit-prep-')
        self.addCleanup(gitfixture.rmtree, self.tmp)
        self.kit = os.path.join(self.tmp, 'kit')
        os.makedirs(os.path.join(self.kit, 'githooks'))
        self.hooks = os.path.join(self.kit, 'githooks')
        env = mock.patch.dict(os.environ, clean_env(self.EXTRA_ENV), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.repo = self.fx.root

    def run_prep(self, path=None, folders=FOLDERS):
        return prep.run(path or self.repo, folders=folders, kit=self.kit)

    def exclude(self, name='info/exclude'):
        """The bytes of the common dir's file `name` (b'' when it is not there)."""
        try:
            with open(os.path.join(self.fx.common_dir(), *name.split('/')), 'rb') as f:
                return f.read()
        except FileNotFoundError:
            return b''

    def config(self, *args, cwd=None):
        """`git config <args>`'s output, run with this case's environment (the fixture's own git
        ignores GIT_CONFIG_GLOBAL)."""
        r = gitcmd.subprocess_run(('git', 'config') + args, cwd=cwd or self.repo, capture_output=True,
                                  text=True, encoding='utf-8', errors='replace', env=dict(os.environ))
        return r.stdout.strip()


class Exclude(PrepCase):

    def test_every_host_folder_gets_one_anchored_line(self):
        lines = self.run_prep()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn('/.claude/worktrees/ /.codex/worktrees/', lines[0])
        got = self.exclude().decode().splitlines()
        self.assertEqual([l for l in got if l in LINES], LINES)

    def test_the_default_folders_are_every_registered_host(self):
        prep.run(self.repo, kit=self.kit)
        got = self.exclude().decode().splitlines()
        for line in LINES:
            self.assertIn(line, got)

    def test_a_second_run_writes_nothing(self):
        self.run_prep()
        before = self.exclude()
        config = self.exclude(name='config')
        self.assertEqual(self.run_prep(), [])
        self.assertEqual(self.exclude(), before)
        self.assertEqual(self.exclude(name='config'), config)

    def test_existing_lines_and_line_endings_are_kept(self):
        path = os.path.join(self.fx.common_dir(), 'info', 'exclude')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'# mine\r\n*.log\r\n/.claude/worktrees')      # no trailing newline
        lines = self.run_prep()
        self.assertIn('/.codex/worktrees/', lines[0])
        self.assertNotIn('/.claude/worktrees/', lines[0])
        self.assertEqual(self.exclude(), b'# mine\r\n*.log\r\n/.claude/worktrees\r\n/.codex/worktrees/\r\n')

    def test_a_missing_info_folder_is_made(self):
        shutil.rmtree(os.path.join(self.fx.common_dir(), 'info'), ignore_errors=True)
        self.run_prep()
        self.assertIn(b'/.claude/worktrees/', self.exclude())

    def test_a_folder_outside_the_repository_is_never_written(self):
        self.run_prep(folders=('../elsewhere', '/abs', 'C:/x', '.claude/worktrees'))
        self.assertEqual(self.exclude().decode().splitlines()[-1:], ['/.claude/worktrees/'])
        self.assertNotIn(b'elsewhere', self.exclude())

    def test_a_linked_worktree_writes_the_common_exclude(self):
        wt = self.fx.worktree('w')
        self.run_prep(path=wt)
        self.assertIn(b'/.codex/worktrees/', self.exclude())
        own = os.path.join(self.fx.common_dir(), 'worktrees', 'w', 'info', 'exclude')
        self.assertFalse(os.path.exists(own))
        self.assertEqual(self.run_prep(path=self.repo), [])


class NothingTrackedChanges(PrepCase):

    def test_a_fresh_clone_with_a_worktree_has_a_clean_status_after_prep(self):
        self.fx.write('.claude/settings.json', '{}\n')
        self.fx.commit('settings', '.claude/settings.json')
        self.fx.git('push', '-q', 'origin', 'main')
        clone = os.path.join(self.tmp, 'clone')
        self.fx.git('clone', '-q', '-b', 'main', self.fx.remote, clone, cwd=self.tmp)
        self.fx.git('worktree', 'add', '-q', os.path.join(clone, '.claude', 'worktrees', 'w'),
                    '-b', 'kit/w', cwd=clone)
        self.assertIn('.claude/worktrees/', self.fx.git('status', '--porcelain', cwd=clone))
        head = self.fx.git('rev-parse', 'HEAD', cwd=clone)
        lines = prep.run(clone, folders=FOLDERS, kit=self.kit)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(self.fx.git('status', '--porcelain', '--ignored=no', cwd=clone), '')
        self.assertEqual(self.fx.git('rev-parse', 'HEAD', cwd=clone), head)

    def test_a_commit_in_a_worktree_pushes_to_its_remote_branch(self):
        """Decision 23's condition: the exclude hides the folder from the main checkout only."""
        self.run_prep()
        wt = self.fx.worktree('w', branch='fix/pushed')
        self.fx.write('src/a.txt', 'a\n', root=wt)
        self.fx.write('.claude/notes.md', 'n\n', root=wt)
        self.fx.commit('work', 'src/a.txt', '.claude/notes.md', cwd=wt)
        self.fx.git('push', '-q', 'origin', 'fix/pushed', cwd=wt)
        tree = self.fx.git('ls-tree', '-r', '--name-only', 'fix/pushed', cwd=self.fx.remote)
        self.assertIn('src/a.txt', tree.splitlines())
        self.assertIn('.claude/notes.md', tree.splitlines())
        self.assertEqual(self.fx.git('rev-parse', 'fix/pushed', cwd=self.fx.remote),
                         self.fx.head(cwd=wt))


class Hooks(PrepCase):

    def test_unset_everywhere_sets_the_local_hooks_path_to_the_kit(self):
        lines = self.run_prep()
        self.assertIn('set the local core.hooksPath', lines[-1])
        self.assertEqual(self.config('--local', '--get', 'core.hooksPath'), self.hooks)

    def test_the_kit_path_comes_from_the_module_location(self):
        self.assertEqual(prep.kit_githooks(), os.path.join(kitroot.root(), 'githooks'))
        self.assertTrue(os.path.isdir(prep.kit_githooks()))

    def test_an_existing_local_hooks_path_is_kept(self):
        mine = os.path.join(self.tmp, 'my-hooks')
        os.makedirs(mine)
        self.config('core.hooksPath', mine)
        lines = self.run_prep()
        self.assertFalse(any('hooksPath' in l for l in lines), lines)
        self.assertEqual(self.config('--get', 'core.hooksPath'), mine)

    def test_a_missing_folder_not_named_githooks_is_kept(self):
        gone = os.path.join(self.tmp, 'gone', 'hooks')
        self.config('core.hooksPath', gone)
        self.run_prep()
        self.assertEqual(self.config('--get', 'core.hooksPath'), gone)

    def test_a_relative_githooks_is_kept(self):
        self.config('core.hooksPath', 'githooks')
        self.run_prep()
        self.assertEqual(self.config('--get', 'core.hooksPath'), 'githooks')

    def test_a_stale_local_kit_githooks_is_repointed(self):
        old = os.path.join(self.tmp, 'old-kit', 'githooks')
        self.config('core.hooksPath', old)
        lines = self.run_prep()
        self.assertIn('repointed', lines[-1])
        self.assertIn(old, lines[-1])
        self.assertEqual(self.config('--get', 'core.hooksPath'), self.hooks)
        self.assertEqual(self.run_prep(), [])

    def test_a_linked_worktree_sets_the_repository_config(self):
        wt = self.fx.worktree('w')
        self.run_prep(path=wt)
        self.assertEqual(self.config('--local', '--get', 'core.hooksPath'), self.hooks)


class GlobalHooks(PrepCase):
    """A hooks path in the user's global config (a temp GIT_CONFIG_GLOBAL) is kept: nothing local
    is written, even when that folder is gone."""

    def setUp(self):
        fd, self.global_cfg = tempfile.mkstemp(prefix='kit-prep-global-')
        os.close(fd)
        self.addCleanup(os.remove, self.global_cfg)
        self.EXTRA_ENV = {'GIT_CONFIG_GLOBAL': self.global_cfg}
        super().setUp()

    def test_a_global_hooks_path_is_kept(self):
        mine = os.path.join(self.tmp, 'global-hooks')
        os.makedirs(mine)
        self.config('--global', 'core.hooksPath', mine)
        lines = self.run_prep()
        self.assertFalse(any('hooksPath' in l for l in lines), lines)
        self.assertEqual(self.config('--local', '--get', 'core.hooksPath'), '')
        self.assertEqual(self.config('--get', 'core.hooksPath'), mine)

    def test_a_stale_global_githooks_is_not_repointed(self):
        old = os.path.join(self.tmp, 'old-kit', 'githooks')
        self.config('--global', 'core.hooksPath', old)
        self.run_prep()
        self.assertEqual(self.config('--local', '--get', 'core.hooksPath'), '')


class Failures(PrepCase):

    def test_not_a_repository_is_a_line_not_an_error(self):
        lines = prep.run(self.tmp, folders=FOLDERS, kit=self.kit)
        self.assertEqual(len(lines), 1)
        self.assertIn('not a git checkout', lines[0])

    def test_an_unwritable_exclude_is_a_line_and_the_hooks_step_still_runs(self):
        target = os.path.join(self.fx.common_dir(), 'info', 'exclude')
        if os.path.isfile(target):          # real git's init writes one
            os.remove(target)
        os.makedirs(target)
        lines = self.run_prep()
        self.assertIn('could not add the worktree folders', lines[0])
        self.assertIn('set the local core.hooksPath', lines[-1])

    def test_a_git_that_fails_is_a_line(self):
        from kitlib.git import cmd
        failed = cmd.Result(None, b'', 'could not run git')
        with mock.patch.object(cmd, 'call', return_value=failed):
            lines = self.run_prep()
        self.assertEqual(len(lines), 1)
        self.assertIn('could not run git', lines[0])


if __name__ == '__main__':
    unittest.main()
