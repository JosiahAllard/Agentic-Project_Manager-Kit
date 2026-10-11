"""A write to the PM data branch's folder store commits its own record paths
(docs/backlog-store.md "The main backend"); other folder stores still commit nothing."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.git import cmd
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog import errors
from kitlib.backlog.errors import Failure, Uncommitted, Usage
from tools import backlog as backlog_tool

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
        self.assertNotIsInstance(cm.exception, Uncommitted)  # put back: nothing left written
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
        self.assertIsInstance(cm.exception, Uncommitted)    # the files are written
        msg = str(cm.exception)
        self.assertIn('%s/FOO.md' % DIR, msg)
        self.assertIn('not committed', msg)
        self.assertIn('catch-up', msg)
        self.assertEqual(self.head(), before)
        self.assertTrue(os.path.isfile(self.path('FOO')))

    def test_a_retry_never_overwrites_another_writers_commit_of_the_same_record(self):
        """Two writers on BAR: the other commits while this one is between its first HEAD read
        and its update-ref. The retry sees HEAD's BAR changed since its first base and stops,
        Uncommitted, naming the path; the other writer's commit stands."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._head
        theirs = []

        def head_then_the_other_writes():
            sha = real()
            if not theirs:
                theirs.append(other.write(lambda s: pmod.plan_set(
                    s, other_ctx, 'BAR', [('summary', ['theirs'])]))[1])
            return sha
        with mock.patch.object(self.backend, '_head', side_effect=head_then_the_other_writes), \
                mock.patch.object(smod.time, 'sleep'):
            with self.assertRaises(Uncommitted) as cm:
                self.store.write(lambda s: pmod.plan_set(s, self.ctx, 'BAR', [('summary', ['mine'])]))
        self.assertIsNotNone(theirs[0])
        self.assertEqual(self.head(), theirs[0])
        self.assertIn('summary: theirs', self.git('show', 'HEAD:%s/BAR.md' % DIR))
        self.assertEqual(cm.exception.paths, [DIR + '/BAR.md'])
        self.assertIn('another commit changed %s/BAR.md' % DIR, str(cm.exception))

    def worktree_index(self):
        """The worktree's own index file (its admin folder's `index`)."""
        gitdir = self.git('rev-parse', '--git-dir')
        index = os.path.join(gitdir if os.path.isabs(gitdir) else os.path.join(self.wt, gitdir),
                             'index')
        self.assertTrue(os.path.isfile(index), index)
        return index

    def test_an_index_left_stale_after_the_commit_is_its_own_result(self):
        """The commit lands but the real index cannot be updated (index.lock held): IndexStale,
        not Uncommitted, saying HEAD has the commit, and its repair command makes status clean."""
        lock = self.worktree_index() + '.lock'
        with open(lock, 'w') as f:
            f.write('')
        before = self.head()
        try:
            with mock.patch.object(smod.time, 'sleep'):
                with self.assertRaises(Failure) as cm:
                    self.store.write(
                        lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**']))
        finally:
            os.remove(lock)
        exc = cm.exception
        self.assertEqual(type(exc).__name__, 'IndexStale', str(exc))
        self.assertIsInstance(exc, errors.IndexStale)
        self.assertNotIsInstance(exc, Uncommitted)
        tip = self.head()
        self.assertEqual(self.git('rev-parse', tip + '^'), before)
        self.assertEqual(self.changed_in(tip), [('A', DIR + '/FOO.md')])
        self.assertEqual((exc.commit, exc.paths), (tip, [DIR + '/FOO.md']))
        self.assertEqual(exc.repair, 'git -C "%s" reset -q -- %s/FOO.md' % (self.wt, DIR))
        msg = str(exc)
        self.assertIn('HEAD has the commit %s' % tip[:10], msg)
        self.assertIn('a plain `git commit` there would revert', msg)
        self.assertIn(exc.repair, msg)
        self.assertNotEqual(self.status(), [])           # stale: staged backwards
        self.git('reset', '-q', '--', DIR + '/FOO.md')    # the repair, as it names it
        self.assertEqual(self.status(), [])
        self.assertEqual(self.head(), tip)

    def test_an_inherited_git_index_file_is_not_the_one_a_write_updates(self):
        """GIT_INDEX_FILE, GIT_DIR and GIT_WORK_TREE inherited from the caller (a hook's
        environment) take no part: the worktree's own index is updated, the stray one never made."""
        tmp = tempfile.mkdtemp(prefix='kit-stray-')
        self.addCleanup(shutil.rmtree, tmp, True)
        stray = os.path.join(tmp, 'index')
        before = self.head()
        with mock.patch.dict(os.environ, {'GIT_INDEX_FILE': stray,
                                          'GIT_DIR': os.path.join(tmp, 'no-repo'),
                                          'GIT_WORK_TREE': tmp}):
            _, tip = self.store.write(
                lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**']))
            pending = self.backend.pending()
        self.assertFalse(os.path.exists(stray))
        self.assertEqual(pending, [])
        self.assert_committed(before, tip, [('A', DIR + '/FOO.md')])

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

    def test_catch_up_apply_commits_only_the_paths_it_names(self):
        self.leave_pending()
        before = self.head()
        tip = smod.catch_up(self.backend, [DIR + '\\NEW.md'], 'kit-test')
        self.assertEqual(self.git('rev-parse', tip + '^'), before)
        self.assertEqual(self.changed_in(tip), [('A', DIR + '/NEW.md')])
        self.assertEqual(self.git('log', '-1', '--format=%B', tip),
                         'backlog: catch up 1 uncommitted record path\n\nSession: kit-test')
        self.assertEqual([p for _, p in self.backend.pending()],
                         [DIR + '/BAR.md', DIR + '/BAZ.md', DIR + '/notes.txt'])

    def test_catch_up_apply_refuses_what_is_not_an_uncommitted_record(self):
        self.leave_pending()
        before = self.head()
        for paths in ([], [DIR + '/notes.txt'], [DIR + '/QUX.md'], ['elsewhere.txt'],
                      [DIR + '/NEW.md', DIR + '/notes.txt']):
            with self.assertRaises(Usage, msg=paths):
                smod.catch_up(self.backend, paths, 'kit-test')
        self.assertEqual(self.head(), before)
        self.assertEqual(len(self.backend.pending()), 4)

    # -- the catch-up verb's arguments and exit codes --------------------------------------------

    def tool(self, *args):
        cfg = os.path.join(self.wt, '.kit.json')
        if not os.path.exists(cfg):
            self.repo.write('.kit.json', '{"backlog": {"store": "%s"}}\n' % DIR, root=self.wt)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = backlog_tool.main(['-C', self.wt, '--session', 'kit-test'] + list(args))
        return rc, out.getvalue()

    def test_the_catch_up_verb_lists_then_applies(self):
        self.leave_pending()
        rc, out = self.tool('catch-up')
        self.assertEqual(rc, 0, out)
        self.assertIn('  deleted      %s/BAR.md\n' % DIR, out)
        self.assertIn('  not a record %s/notes.txt\n' % DIR, out)
        self.assertIn('4 path(s), 1 added, 1 deleted, 1 modified, 1 not a record\n', out)
        self.assertIn('catch-up --apply %s/BAR.md %s/BAZ.md %s/NEW.md\n' % (DIR, DIR, DIR), out)
        before = self.head()
        rc, out = self.tool('catch-up', '--apply', DIR + '/notes.txt')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: ', out)
        self.assertEqual(self.head(), before)
        rc, out = self.tool('catch-up', '--apply', DIR + '/BAR.md', DIR + '/BAZ.md')
        self.assertEqual(rc, 0, out)
        self.assertIn('CAUGHT UP 2 record path(s)', out)
        self.assertEqual(self.changed_in(self.head()), [('D', DIR + '/BAR.md'),
                                                        ('M', DIR + '/BAZ.md')])


if __name__ == '__main__':
    unittest.main()
