"""A write to the PM data branch's folder store commits its own record paths
(docs/backlog-store.md "The main backend"); other folder stores still commit nothing."""
import os
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.git import cmd
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Failure

from tests.backlog.helpers import BRANCH, priced
from tests.gitfixture import TempRepo

DIR = 'kit/backlog'
REASON = 'the work is finished'


class PmDataStore(unittest.TestCase):
    """A repository with a PM data record and a linked worktree with its branch checked out,
    holding a committed folder store under kit/backlog."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        root = self.repo.head()
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root)
        self.wt = self.repo.worktree('pm', branch='pmdata')
        for stem, text in (list(priced('BAR', session='kit-test').items())
                           + list(priced('BAZ').items())):
            self.repo.write('%s/%s.md' % (DIR, stem), text, root=self.wt)
        self.repo.commit('seed the store', DIR, cwd=self.wt)
        self.backend = smod.MainBackend(self.wt, DIR)
        self.store = smod.Store(self.backend, rmod.Rules(), session='kit-test')
        self.ctx = pmod.Context(self.store, self.wt, branch=BRANCH)

    def git(self, *args):
        return self.repo.git(*args, cwd=self.wt)

    def head(self):
        return self.git('rev-parse', 'HEAD')

    def status(self):
        """`git status --porcelain` lines for the whole worktree, leading space kept."""
        out, why = cmd.run_text(['status', '--porcelain', '--untracked-files=all'], self.wt,
                                strip=False)
        self.assertIsNotNone(out, why)
        return [l for l in out.split('\n') if l]

    def changed_in(self, sha):
        """`(status, path)` of each path `sha` changed against its parent."""
        out = self.git('diff-tree', '--no-commit-id', '--no-renames', '--name-status', '-r', sha)
        return sorted(tuple(l.split('\t', 1)) for l in out.split('\n') if l)

    def path(self, stem):
        return os.path.join(self.wt, *DIR.split('/'), stem + '.md')

    def assert_committed(self, before, tip, changed):
        """`tip` is HEAD, one commit on `before`, changing exactly `changed`, with a Session line,
        and the worktree is clean."""
        self.assertEqual(tip, self.head())
        self.assertEqual(self.git('rev-parse', tip + '^'), before)
        self.assertEqual(self.changed_in(tip), sorted(changed))
        self.assertIn('Session: kit-test', self.git('log', '-1', '--format=%B', tip))
        self.assertEqual(self.status(), [])

    # -- each write commits ----------------------------------------------------------------------

    def test_add_commits_the_new_record(self):
        before = self.head()
        change, tip = self.store.write(
            lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**']))
        self.assert_committed(before, tip, [('A', DIR + '/FOO.md')])
        self.assertEqual(self.git('log', '-1', '--format=%s', tip), change.subject)

    def test_set_commits_the_changed_record(self):
        before = self.head()
        _, tip = self.store.write(
            lambda s: pmod.plan_set(s, self.ctx, 'BAR', [('summary', ['renamed'])]))
        self.assert_committed(before, tip, [('M', DIR + '/BAR.md')])
        self.assertIn('summary: renamed', self.git('show', '%s:%s/BAR.md' % (tip, DIR)))

    def test_close_commits_the_deletion(self):
        before = self.head()
        _, tip = self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assertFalse(os.path.exists(self.path('BAR')))
        self.assert_committed(before, tip, [('D', DIR + '/BAR.md')])

    def test_pull_commits(self):
        before = self.head()
        _, tip = self.store.write(lambda s: pmod.plan_pull(s, self.ctx, 'BAR'))
        self.assert_committed(before, tip, [('M', DIR + '/BAR.md')])

    def test_only_the_writes_own_paths_are_committed(self):
        """Another record edited by hand and an untracked file stay exactly as they were:
        neither committed nor staged."""
        with open(self.path('BAZ'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nedited by hand\n')
        self.repo.write('notes.txt', 'scratch\n', root=self.wt)
        before = self.head()
        _, tip = self.store.write(
            lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**']))
        self.assertEqual(self.changed_in(tip), [('A', DIR + '/FOO.md')])
        self.assertEqual(self.git('rev-parse', tip + '^'), before)
        self.assertEqual(sorted(self.status()), [' M %s/BAZ.md' % DIR, '?? notes.txt'])

    def test_a_write_that_changes_nothing_in_git_makes_no_commit(self):
        """An uncommitted record closed: its file goes, HEAD never had it, no empty commit."""
        self.repo.write('%s/NEW.md' % DIR, priced('NEW')['NEW'], root=self.wt)
        before = self.head()
        _, tip = self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'NEW', REASON))
        self.assertIsNone(tip)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.status(), [])

    # -- races and failures ----------------------------------------------------------------------

    def test_a_head_moved_by_another_commit_is_built_on_not_lost(self):
        real = self.backend._head
        moved = []

        def head_then_move():
            sha = real()
            if not moved:
                self.repo.write('other.txt', 'other\n', root=self.wt)
                moved.append(self.repo.commit('someone else', 'other.txt', cwd=self.wt))
            return sha
        with mock.patch.object(self.backend, '_head', side_effect=head_then_move):
            _, tip = self.store.write(
                lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**']))
        self.assert_committed(moved[0], tip, [('A', DIR + '/FOO.md')])

    def test_a_write_that_fails_partway_commits_nothing(self):
        """The second file cannot be written: no commit holds the first alone."""
        os.makedirs(self.path('ZZZ'))                 # a directory where ZZZ.md would go
        before = self.head()
        with self.assertRaises(Failure) as cm:
            self.backend.publish(self.store.load(), {'AAA': priced('AAA')['AAA'],
                                                     'ZZZ': priced('ZZZ')['ZZZ']}, 'x\n')
        self.assertIn('not committed', str(cm.exception))
        self.assertIn('catch-up', str(cm.exception))
        self.assertEqual(self.head(), before)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertFalse(os.path.exists(self.path('AAA')))    # the first file was put back

    def test_a_commit_that_fails_is_reported(self):
        lock = os.path.join(self.repo.common_dir(), 'refs', 'heads', 'pmdata.lock')
        with open(lock, 'w') as f:
            f.write('')
        self.addCleanup(os.remove, lock)
        before = self.head()
        with mock.patch.object(smod.time, 'sleep'):
            with self.assertRaises(Failure) as cm:
                self.store.write(
                    lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**']))
        msg = str(cm.exception)
        self.assertIn('%s/FOO.md' % DIR, msg)
        self.assertIn('not committed', msg)
        self.assertIn('catch-up', msg)
        self.assertEqual(self.head(), before)
        self.assertTrue(os.path.isfile(self.path('FOO')))

    # -- which stores commit ---------------------------------------------------------------------

    def test_a_worktree_on_another_branch_commits_nothing(self):
        other = self.repo.worktree('elsewhere')
        backend = smod.MainBackend(other, DIR)
        store = smod.Store(backend, rmod.Rules(), session='kit-test')
        ctx = pmod.Context(store, other, branch=BRANCH)
        before = self.repo.git('rev-parse', 'HEAD', cwd=other)
        _, tip = store.write(lambda s: pmod.plan_add(s, ctx, 'FOO', 'a summary', ['area/foo/**']))
        self.assertIsNone(tip)
        self.assertEqual(self.repo.git('rev-parse', 'HEAD', cwd=other), before)
        self.assertIsNone(backend.commit_branch())
        self.assertEqual(self.backend.commit_branch(), 'pmdata')

    # -- catch-up --------------------------------------------------------------------------------

    def leave_pending(self):
        """One record added, one modified, one deleted by hand, and a non-record file."""
        self.repo.write('%s/NEW.md' % DIR, priced('NEW')['NEW'], root=self.wt)
        with open(self.path('BAZ'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nedited by hand\n')
        os.remove(self.path('BAR'))
        self.repo.write('%s/notes.txt' % DIR, 'scratch\n', root=self.wt)
        self.repo.write('elsewhere.txt', 'scratch\n', root=self.wt)

    def test_pending_lists_each_record_path_with_its_kind(self):
        self.leave_pending()
        self.assertEqual(self.backend.pending(), [
            ('deleted', DIR + '/BAR.md'), ('modified', DIR + '/BAZ.md'),
            ('added', DIR + '/NEW.md'), ('not a record', DIR + '/notes.txt')])

    def test_catch_up_commits_exactly_the_record_paths(self):
        self.leave_pending()
        before = self.head()
        paths = [p for kind, p in self.backend.pending() if kind != 'not a record']
        tip = self.backend.commit_paths(paths, 'backlog: catch up\n\nSession: kit-test\n')
        self.assertEqual(self.git('rev-parse', tip + '^'), before)
        self.assertEqual(self.changed_in(tip), [('A', DIR + '/NEW.md'), ('D', DIR + '/BAR.md'),
                                                ('M', DIR + '/BAZ.md')])
        self.assertEqual(sorted(self.status()), ['?? elsewhere.txt', '?? %s/notes.txt' % DIR])
        self.assertEqual(self.backend.pending(), [('not a record', DIR + '/notes.txt')])

    def test_catch_up_refuses_a_path_outside_the_store(self):
        self.leave_pending()
        before = self.head()
        with self.assertRaises(Failure):
            self.backend.commit_paths(['elsewhere.txt'], 'x\n')
        self.assertEqual(self.head(), before)


if __name__ == '__main__':
    unittest.main()
