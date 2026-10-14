"""Host worktree folders: a second registered host gets its own folder, and the default host keeps
"repo".worktrees_dir (kitlib.worktree.hostdirs). The registry here is a test hosts.json holding a
second host, `codex`, with its own folder."""
import json
import os
import shutil
import tempfile
from unittest import mock

from kitlib.config import load
from kitlib.host import registry
from kitlib.worktree import hostdirs, new, remove
from tests.worktree.test_new_worktree import WorktreeCase


def two_hosts():
    """The kit's hosts.json plus a second, non-default host with its own worktree folder."""
    with open(registry.PATH, encoding='utf-8') as f:
        data = json.load(f)
    second = json.loads(json.dumps(next(iter(data.values()))))
    second.pop('default', None)
    second['adapter'] = 'second.adapter'
    second['shell'] = 'second'
    second['profile']['worktrees_directory'] = '.codex/worktrees'
    data['codex'] = second
    return data


class HostWorktrees(WorktreeCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.mkdtemp(prefix='hosts-')
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, 'hosts.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(two_hosts(), f)
        patch = mock.patch.object(registry, 'PATH', path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_second_host_creation_uses_shared_checks_and_its_own_folder(self):
        result = new.create(self.repo, 'codex/worker', host='codex')
        self.assertEqual(result.code, 0, result.lines)
        self.assertTrue(os.path.isdir(self.slug_path('worker', '.codex/worktrees')))
        self.assertFalse(os.path.exists(self.slug_path('worker', '.claude/worktrees')))
        self.assertEqual(self.git('branch', '--show-current')[1], self.BASE)

    def test_default_host_is_repo_worktrees_dir_and_an_override_wins_for_its_host_only(self):
        self.configure({'repo': {'worktrees_dir': 'claude-workers',
                                 'worktrees_by_host': {'codex': 'codex-workers'}}})
        result = new.create(self.repo, 'codex/worker', host='codex')
        self.assertEqual(result.code, 0, result.lines)
        self.assertTrue(os.path.isdir(self.slug_path('worker', 'codex-workers')))
        result = new.create(self.repo, 'kit/other', host=registry.default())
        self.assertEqual(result.code, 0, result.lines)
        self.assertTrue(os.path.isdir(self.slug_path('other', 'claude-workers')))
        self.assertEqual(hostdirs.folders(load(self.repo)), ('claude-workers', 'codex-workers'))

    def test_no_host_keeps_repo_worktrees_dir(self):
        result = new.create(self.repo, 'kit/plain')
        self.assertEqual(result.code, 0, result.lines)
        self.assertTrue(os.path.isdir(self.slug_path('plain')))

    def test_unknown_host_refuses_before_fetching_or_creating(self):
        real_git = new.git
        calls = []

        def observed(root, *args, **kw):
            calls.append(args)
            return real_git(root, *args, **kw)
        with mock.patch.object(new, 'git', side_effect=observed):
            result = new.create(self.repo, 'codex/worker', host='unknown')
        self.assertEqual(result.code, new.CONFIG)
        self.assertIn('unknown host', '\n'.join(result.lines))
        self.assertFalse(any(args[0] in {'fetch', 'worktree'} for args in calls))
        self.assertEqual(self.worktree_paths(), [])

    def test_second_host_cleanup_accepts_only_the_registered_host_worktree(self):
        result = new.create(self.repo, 'codex/worker', host='codex')
        self.assertEqual(result.code, 0, result.lines)
        path = self.slug_path('worker', '.codex/worktrees')
        ctx = remove.Ctx(self.repo, load(self.repo), cwd=self.repo)
        self.assertIsInstance(remove.registered_target(ctx, path), tuple)
        elsewhere = os.path.join(self.repo, 'not-a-host', 'worker')
        self.assertEqual(remove.registered_target(ctx, elsewhere), remove.REFUSED)
        with mock.patch.object(remove.holders, 'blockers', return_value=[]):
            result = remove.remove(self.repo, path, keep_branch=True, cwd=self.repo)
        self.assertEqual(result.code, 0, result.lines)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.git('branch', '--show-current')[1], self.BASE)

    def test_an_unreadable_registry_leaves_cleanup_to_repo_worktrees_dir(self):
        with mock.patch.object(registry, 'PATH', os.path.join(self.repo, 'missing.json')):
            self.assertEqual(remove.host_folders(load(self.repo)),
                             (os.path.normpath('.claude/worktrees'),))
