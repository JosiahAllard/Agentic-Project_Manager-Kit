"""A write to the PM data branch's folder store commits its own record paths
(docs/backlog-store.md "The main backend"); other folder stores still commit nothing."""
import contextlib
import io
import os
import re
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
        """Two writers on BAR: the other commits while this one is between its first attempt's
        HEAD read and its update-ref. The retry sees HEAD's BAR changed since its base and stops,
        Overtaken (an Uncommitted), naming the path; the other writer's commit stands."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._head
        theirs = []
        calls = []

        def head_then_the_other_writes():
            sha = real()
            calls.append(sha)
            if len(calls) == 2:                       # 1: publish's base; 2: the first attempt
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
        self.assertIsInstance(cm.exception, errors.Overtaken)

    def test_a_commit_between_the_read_and_the_first_attempt_is_never_overwritten(self):
        """Two writers on BAR: this one has checked the files against what it read, then the
        other writes and commits BAR before this one's first commit attempt reads HEAD. The base
        is HEAD from before the check, so the first attempt sees BAR changed and stops, Overtaken;
        the other writer's commit stands and the error names no catch-up to run."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._write_files
        theirs = []

        def the_other_writes_then_write(*args):
            theirs.append(other.write(lambda s: pmod.plan_set(
                s, other_ctx, 'BAR', [('summary', ['theirs'])]))[1])
            return real(*args)
        with mock.patch.object(self.backend, '_write_files',
                               side_effect=the_other_writes_then_write), \
                mock.patch.object(smod.time, 'sleep'):
            with self.assertRaises(Uncommitted) as cm:
                self.store.write(lambda s: pmod.plan_set(s, self.ctx, 'BAR', [('summary', ['mine'])]))
        self.assertEqual(len(theirs), 1)
        self.assertIsNotNone(theirs[0])
        self.assertEqual(self.head(), theirs[0])
        self.assertIn('summary: theirs', self.git('show', 'HEAD:%s/BAR.md' % DIR))
        exc = cm.exception
        self.assertIsInstance(exc, errors.Overtaken)
        self.assertEqual((exc.paths, exc.changed), ([DIR + '/BAR.md'], [DIR + '/BAR.md']))
        self.assertEqual((exc.head, exc.left, exc.compare), (theirs[0], [], ''))  # all put back
        self.assertIn('another commit changed %s/BAR.md' % DIR, str(exc))
        self.assertIn('nothing of it remains', str(exc))
        self.assertNotIn('catch-up', str(exc))           # no catch-up command at all

    def overtaken(self, theirs, mine, after_my_files=None):
        """The Overtaken of this store's write `mine(snap, ctx)`, another store's `theirs(snap,
        ctx)` having been written and committed after this one checked the files and before its
        first commit attempt. `after_my_files()` runs once this write's files are on disk."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._write_files

        def the_other_writes_then_write(*args):
            self.winner = other.write(lambda s: theirs(s, other_ctx))[1]
            real(*args)
            if after_my_files is not None:
                after_my_files()
        with mock.patch.object(self.backend, '_write_files',
                               side_effect=the_other_writes_then_write), \
                mock.patch.object(smod.time, 'sleep'):
            with self.assertRaises(errors.Overtaken) as cm:
                self.store.write(lambda s: mine(s, self.ctx))
        self.assertIsNotNone(self.winner)
        self.assertEqual(self.head(), self.winner)
        return cm.exception

    def test_an_overtaken_write_is_put_back_so_a_read_and_the_next_write_keep_the_winner(self):
        """The refused write's file goes back to HEAD's content: `load` returns the winner's
        record, the worktree is clean, and the next ordinary write to that record (which finds
        HEAD where it began, so no compare runs) carries the winner's change, not the loser's."""
        exc = self.overtaken(
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])]),
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['mine'])]))
        self.assertEqual(self.store.load().get('BAR').get('summary'), 'theirs')
        self.assertEqual(self.status(), [])
        _, tip = self.store.write(lambda s: pmod.plan_pull(s, self.ctx, 'BAR'))
        self.assert_committed(self.winner, tip, [('M', DIR + '/BAR.md')])
        after = self.git('show', 'HEAD:%s/BAR.md' % DIR)
        self.assertIn('summary: theirs', after)
        self.assertNotIn('summary: mine', after)
        self.assertEqual((exc.paths, exc.changed, exc.left), ([DIR + '/BAR.md'],) * 2 + ([],))
        self.assertIn('put back (%s/BAR.md to what HEAD holds' % DIR, str(exc))
        self.assertNotIn('catch-up', str(exc))

    def test_an_overtaken_write_of_a_record_the_winner_deleted_removes_its_file(self):
        """HEAD lacks the path (the winner closed the record): the refused write's file is
        removed, so the record does not come back."""
        exc = self.overtaken(
            lambda s, ctx: pmod.plan_close(s, ctx, 'BAR', REASON),
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['mine'])]))
        self.assertFalse(os.path.exists(self.path('BAR')))
        self.assertIsNone(self.store.load().get('BAR'))
        self.assertEqual(self.status(), [])
        self.assertEqual(exc.left, [])

    def test_an_overtaken_file_changed_since_it_was_written_is_left_and_named(self):
        """The file no longer holds what the refused write wrote (a hand edit after it): it is
        left exactly as it is and the error names it, with the compare against HEAD."""
        def edit_by_hand():
            with open(self.path('BAR'), 'ab') as f:
                f.write(b'\nedited by hand\n')
        exc = self.overtaken(
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])]),
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['mine'])]),
            after_my_files=edit_by_hand)
        with open(self.path('BAR'), 'rb') as f:
            on_disk = f.read()
        self.assertIn(b'summary: mine', on_disk)
        self.assertTrue(on_disk.endswith(b'\nedited by hand\n'))
        self.assertIn('summary: theirs', self.git('show', 'HEAD:%s/BAR.md' % DIR))
        self.assertEqual(exc.left, [DIR + '/BAR.md'])
        self.assertEqual(exc.compare, 'git -C "%s" diff HEAD -- "%s/BAR.md"' % (self.wt, DIR))
        self.assertIn('Left as it is', str(exc))
        self.assertIn(exc.compare, str(exc))

    def overtaken_publish(self, theirs, writes, **patches):
        """The Overtaken of this backend's publish of `writes` ({stem: text or None}) on what it
        reads now, another store's `theirs(snap, ctx)` having been written and committed after
        this one checked the files. `patches`: backend methods replaced for the publish."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._write_files

        def the_other_writes_then_write(*args):
            self.winner = other.write(lambda s: theirs(s, other_ctx))[1]
            real(*args)
        snap = self.store.load()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.backend, '_write_files',
                                                  side_effect=the_other_writes_then_write))
            stack.enter_context(mock.patch.object(smod.time, 'sleep'))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(self.backend, name, side_effect=value))
            with self.assertRaises(errors.Overtaken) as cm:
                self.backend.publish(snap, writes, 'two records\n')
        self.assertEqual(self.head(), self.winner)
        return cm.exception

    def read(self, stem):
        with open(self.path(stem), 'rb') as f:
            return f.read()

    def test_an_overtaken_write_gives_a_path_no_commit_changed_its_earlier_bytes(self):
        """A write of BAR and BAZ, BAZ holding uncommitted content: another commit changes BAR
        alone. BAR goes back to HEAD's content; BAZ, which no commit changed, goes back to the
        bytes it held before the write, and catch-up still lists it."""
        with open(self.path('BAZ'), 'ab') as f:
            f.write(b'\nedited by hand\n')
        dirty = self.read('BAZ')
        exc = self.overtaken_publish(
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])]),
            {'BAR': priced('BAR', summary='mine')['BAR'],
             'BAZ': priced('BAZ', summary='mine')['BAZ']})
        self.assertEqual((exc.changed, exc.left), ([DIR + '/BAR.md'], []))
        self.assertEqual(self.read('BAZ'), dirty)
        self.assertEqual(self.backend.pending(), [('modified', DIR + '/BAZ.md')])
        self.assertEqual(self.store.load().get('BAR').get('summary'), 'theirs')
        self.assertIn('%s/BAR.md to what HEAD holds' % DIR, str(exc))
        self.assertIn('held before the write', str(exc))

    def test_an_overtaken_delete_of_a_path_no_commit_changed_brings_the_file_back(self):
        """A write that deleted NEW (never committed) and BAZ (hand edited) and changed BAR,
        another commit changing BAR alone: NEW.md and BAZ.md are back with their earlier bytes."""
        self.repo.write('%s/NEW.md' % DIR, priced('NEW')['NEW'], root=self.wt)
        with open(self.path('BAZ'), 'ab') as f:
            f.write(b'\nedited by hand\n')
        new, dirty = self.read('NEW'), self.read('BAZ')
        exc = self.overtaken_publish(
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])]),
            {'BAR': priced('BAR', summary='mine')['BAR'], 'BAZ': None, 'NEW': None})
        self.assertEqual(exc.left, [])
        self.assertEqual((self.read('NEW'), self.read('BAZ')), (new, dirty))
        self.assertEqual(self.backend.pending(), [('modified', DIR + '/BAZ.md'),
                                                  ('added', DIR + '/NEW.md')])

    def test_an_overtaken_write_removes_a_record_it_created_that_no_commit_changed(self):
        """A write that created NEW alongside a change to BAR, another commit changing BAR
        alone: NEW.md, absent before the write and not on HEAD, is removed again."""
        exc = self.overtaken_publish(
            lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])]),
            {'BAR': priced('BAR', summary='mine')['BAR'], 'NEW': priced('NEW')['NEW']})
        self.assertEqual((exc.changed, exc.left), ([DIR + '/BAR.md'], []))
        self.assertFalse(os.path.exists(self.path('NEW')))
        self.assertEqual(self.status(), [])
        self.assertEqual(os.listdir(os.path.dirname(self.path('NEW'))), ['BAR.md', 'BAZ.md'])

    def test_an_overtaken_file_that_cannot_be_put_back_keeps_what_the_write_wrote(self):
        """The put back fails on the disk: the file is left whole, holding exactly what the
        refused write wrote (never a part of HEAD's content), it is named as left, and no
        other file stays in the store folder."""
        mine = priced('BAR', summary='mine')['BAR']
        real = os.replace

        def replace(src, dst):
            if os.path.basename(dst) == 'BAR.md':
                raise OSError('disk full')
            return real(src, dst)
        with mock.patch.object(smod.os, 'replace', replace):
            exc = self.overtaken_publish(
                lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])]),
                {'BAR': mine})
        self.assertEqual(exc.left, [DIR + '/BAR.md'])
        with open(self.path('BAR'), 'rb') as f:
            self.assertEqual(f.read(), mine.encode('utf-8'))
        self.assertEqual(os.listdir(os.path.dirname(self.path('BAR'))), ['BAR.md', 'BAZ.md'])

    def test_an_overtaken_path_whose_head_content_cannot_be_read_is_left(self):
        """git gives neither HEAD's entries (ls-tree) nor, in the second case, the blob
        (cat-file): the overtaken path is left and named; the other path is still put back."""
        real_entries, real_run = self.backend._entries, cmd.run
        calls = []

        def entries_fail_third(rev, paths):
            calls.append(rev)
            if len(calls) == 3:                       # 1, 2: the retry's compare; 3: _put_back
                raise Failure('git ls-tree: ended from outside')
            return real_entries(rev, paths)

        def no_blob(args, *rest, **kw):
            if list(args[:2]) == ['cat-file', 'blob']:
                return None, 'ended from outside'
            return real_run(args, *rest, **kw)
        baz = self.read('BAZ')
        writes = {'BAR': priced('BAR', summary='mine')['BAR'],
                  'BAZ': priced('BAZ', summary='mine')['BAZ']}
        theirs = lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])])
        exc = self.overtaken_publish(theirs, writes, _entries=entries_fail_third)
        self.assertEqual(len(calls), 3)
        self.assertEqual(exc.left, [DIR + '/BAR.md'])
        self.assertIn(b'summary: mine', self.read('BAR'))
        self.assertEqual(self.read('BAZ'), baz)
        self.git('checkout', 'HEAD', '--', DIR)
        theirs = lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs again'])])
        with mock.patch.object(smod.cmd, 'run', side_effect=no_blob):
            exc = self.overtaken_publish(theirs, writes)
        self.assertEqual(exc.left, [DIR + '/BAR.md'])
        self.assertIn(b'summary: mine', self.read('BAR'))
        self.assertEqual(self.read('BAZ'), baz)

    def test_an_unreadable_head_writes_nothing_and_says_so(self):
        """publish reads HEAD before it checks or writes any file: when git cannot give it, the
        ERROR says nothing was written, and no file, commit or index entry changed."""
        real = cmd.run_text

        def no_head(args, *rest, **kw):
            if list(args[:1]) == ['rev-parse'] and 'HEAD^{commit}' in args:
                return None, 'git ended from outside'
            return real(args, *rest, **kw)
        before = self.head()
        with open(self.path('BAR'), 'rb') as f:
            bar = f.read()
        with mock.patch.object(smod.cmd, 'run_text', side_effect=no_head):
            with self.assertRaises(Failure) as cm:
                self.store.write(lambda s: pmod.plan_set(s, self.ctx, 'BAR',
                                                         [('summary', ['mine'])]))
        self.assertNotIsInstance(cm.exception, Uncommitted)
        self.assertEqual(str(cm.exception), 'cannot read HEAD in %s: git ended from outside; '
                         'nothing was written' % self.wt)
        with open(self.path('BAR'), 'rb') as f:
            self.assertEqual(f.read(), bar)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path('BAR')))),
                         ['BAR.md', 'BAZ.md'])
        self.assertEqual(self.head(), before)
        self.assertEqual(self.status(), [])

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
        self.assertEqual(exc.repair, 'git -C "%s" reset -q -- "%s/FOO.md"' % (self.wt, DIR))
        msg = str(exc)
        self.assertIn('HEAD has the commit %s' % tip[:10], msg)
        self.assertIn('a plain `git commit` there would revert', msg)
        self.assertIn(exc.repair, msg)
        self.assertNotEqual(self.status(), [])           # stale: staged backwards
        self.git('reset', '-q', '--', DIR + '/FOO.md')    # the repair, as it names it
        self.assertEqual(self.status(), [])
        self.assertEqual(self.head(), tip)

    def test_the_printed_commands_quote_a_store_folder_with_a_space(self):
        """A store under `kit/back log`: the IndexStale repair, split as a shell splits it, makes
        status clean, and the Uncommitted message's catch-up command quotes the path."""
        spaced = 'kit/back log'
        backend = smod.MainBackend(self.wt, spaced)
        store = smod.Store(backend, rmod.Rules(), session='kit-test')
        ctx = pmod.Context(store, self.wt, branch=BRANCH)
        lock = self.worktree_index() + '.lock'
        with open(lock, 'w') as f:
            f.write('')
        try:
            with mock.patch.object(smod.time, 'sleep'):
                with self.assertRaises(errors.IndexStale) as cm:
                    store.write(lambda s: pmod.plan_add(s, ctx, 'FOO', 'a summary',
                                                        ['area/foo/**']))
        finally:
            os.remove(lock)
        repair = cm.exception.repair
        self.assertEqual(repair, 'git -C "%s" reset -q -- "%s/FOO.md"' % (self.wt, spaced))
        self.assertNotEqual(self.status(), [])
        words = [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', repair)]   # as a shell splits
        self.assertEqual(words[0], 'git')
        done = cmd.call(words[1:], self.repo.root, env=smod._store_env())
        self.assertEqual(done.rc, 0, done.err)
        self.assertEqual(self.status(), [])
        self.assertEqual(smod.quoted(['a b/C.md', 'd/E.md']), '"a b/C.md" "d/E.md"')
        self.assertTrue(str(backend._uncommitted([spaced + '/FOO.md'], 'why')).endswith(
            '`backlog.py catch-up --apply "%s/FOO.md"` commits them' % spaced))

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
        self.assertIn('catch-up --apply "%s/BAR.md" "%s/BAZ.md" "%s/NEW.md"\n' % (DIR, DIR, DIR),
                      out)
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

    def test_a_set_through_the_tool_commits_with_the_session_it_was_given(self):
        """A clean index and `backlog.py set`: `main` opens the store (opener.open_store) and
        plans the verb (opener.planner); the write is one commit of BAR.md carrying the
        `Session:` line of `--session`, and the index is left clean."""
        self.repo.write('.kit.json', '{"backlog": {"store": "%s"}}\n' % DIR, root=self.wt)
        self.repo.commit('configure the store', '.kit.json', cwd=self.wt)
        self.assertEqual(self.status(), [])
        before = self.head()
        with mock.patch.object(backlog_tool, 'open_store', wraps=backlog_tool.open_store) as opened, \
                mock.patch.object(backlog_tool, 'planner', wraps=backlog_tool.planner) as planned:
            rc, out = self.tool('set', 'BAR', '--summary', 'renamed by the tool')
        self.assertEqual(rc, 0, out)
        self.assertEqual(opened.call_args.kwargs['session'], 'kit-test')
        self.assertEqual(planned.call_count, 1)
        tip = self.head()
        self.assertIn('WROTE %s -> %s' % (self.backend.describe(), tip[:10]), out.splitlines())
        self.assert_committed(before, tip, [('M', DIR + '/BAR.md')])
        self.assertIn('summary: renamed by the tool',
                      self.git('show', '%s:%s/BAR.md' % (tip, DIR)))
        self.assertEqual(self.git('log', '-1', '--format=%B', tip).splitlines()[-1],
                         'Session: kit-test')

    def test_a_write_committed_with_a_stale_index_is_reported_written_and_exits_0(self):
        """The index is locked: `add` and `catch-up --apply` still commit, so each prints its
        WROTE / CAUGHT UP line with the commit, then a WARNING carrying the repair; exit 0."""
        self.tool('catch-up')                             # writes .kit.json
        self.repo.write('%s/NEW.md' % DIR, priced('NEW')['NEW'], root=self.wt)
        lock = self.worktree_index() + '.lock'
        with open(lock, 'w') as f:
            f.write('')
        try:
            with mock.patch.object(smod.time, 'sleep'):
                rc, out = self.tool('add', 'FOO', '--summary', 'a summary', '--scope',
                                    'area/foo/**')
                added = self.head()
                rc2, out2 = self.tool('catch-up', '--apply', DIR + '/NEW.md')
        finally:
            os.remove(lock)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.changed_in(added), [('A', DIR + '/FOO.md')])
        lines = out.splitlines()
        self.assertIn('WROTE %s -> %s' % (self.backend.describe(), added[:10]), lines)
        self.assertFalse([l for l in lines if l.startswith('ERROR')], out)
        warn = [l for l in lines if l.startswith('WARNING: ')]
        self.assertEqual(len(warn), 1, out)
        self.assertGreater(lines.index(warn[0]), lines.index(
            'WROTE %s -> %s' % (self.backend.describe(), added[:10])))
        self.assertIn('HEAD has the commit %s' % added[:10], warn[0])
        self.assertTrue(warn[0].endswith(
            'git -C "%s" reset -q -- "%s/FOO.md"' % (self.wt, DIR)), warn[0])
        tip = self.head()
        self.assertEqual(rc2, 0, out2)
        self.assertEqual(self.changed_in(tip), [('A', DIR + '/NEW.md')])
        lines = out2.splitlines()
        self.assertIn('CAUGHT UP 1 record path(s) in %s -> %s'
                      % (self.backend.describe(), tip[:10]), lines)
        self.assertTrue(lines[-1].startswith('WARNING: '), out2)
        self.assertTrue(lines[-1].endswith(
            'git -C "%s" reset -q -- "%s/NEW.md"' % (self.wt, DIR)), out2)


if __name__ == '__main__':
    unittest.main()
