"""A write to the PM data branch's folder store commits its own record paths
(docs/backlog-store.md "The main backend"); other folder stores still commit nothing."""
import contextlib
import errno
import io
import os
import re
import shutil
import tempfile
import threading
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

from tests.backlog.helpers import ADD_FLAGS, ADD_NONE, BRANCH, priced
from tests.gitfixture import TempRepo, fixture_env

DIR = 'kit/backlog'
REASON = 'the work is finished'


def commit_beside(wt, files, message):
    """Commit `files` ({path as git spells it: text or bytes, or None to delete}) on HEAD in the
    worktree `wt` without git's index lock there, as a writer that moves HEAD by plumbing alone does
    (`update-ref`): the files are written or removed, the commit is built in an index of its own
    and HEAD moved by compare-and-swap. A store write there holds the index lock from before it
    reads HEAD's commit (MainBackend._lock_index), so a `git commit` or another store write in the
    middle of one now fails or waits; this is the other commit such a write can still meet. The
    worktree's own index is left as it was: stale for those paths (`git reset -q -- <path>` is
    the mover's own repair). Returns the new commit."""
    tmp = tempfile.mkdtemp(prefix='kit-beside-')
    env = fixture_env({'GIT_INDEX_FILE': os.path.join(tmp, 'index')})

    def git(*args):
        r = cmd.call(list(args), wt, env=env)
        if r.rc != 0:
            raise AssertionError('git %s failed: %s' % (' '.join(args), r.err))
        return r.out.decode('utf-8').strip()
    try:
        for path, text in files.items():
            full = os.path.join(wt, *path.split('/'))
            if text is None:
                os.remove(full)
            else:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                data = text if isinstance(text, bytes) else text.encode('utf-8')
                with open(full, 'wb') as f:
                    f.write(data)
        old = git('rev-parse', 'HEAD')
        git('read-tree', old)
        lines = []
        for path, text in sorted(files.items()):
            if text is None:
                lines.append('0 %s\t%s' % ('0' * 40, path))
            else:
                lines.append('100644 %s\t%s' % (git('hash-object', '-w', '--', path), path))
        r = cmd.call(['update-index', '--index-info'], wt, env=env,
                     stdin=('\n'.join(lines) + '\n').encode('utf-8'))
        if r.rc != 0:
            raise AssertionError('git update-index failed: %s' % r.err)
        new = git('commit-tree', git('write-tree'), '-p', old, '-m', message)
        git('update-ref', 'HEAD', new, old)
        return new
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def commit_change(store, ctx, plan, message):
    """commit_beside of what `plan(snap, ctx)` would write in `store` (a MainBackend store):
    another writer's change to records, committed while a write of this store's holds the lock."""
    change = plan(store.load(), ctx)
    return commit_beside(store.backend.root, dict(
        (store.backend.path_of(stem), text) for stem, text in change.writes.items()), message)


def index_move_fails(index, error=None):
    """A patch under which moving a file over the worktree's index file `index`
    (MainBackend._sync_index's last step, _IndexLock.install; nothing else moves a file over it
    while a write holds its lock) raises `error` (default an OSError), every other move done. It
    stands in for what a held index.lock gave before the write took the lock itself: the commit
    made and the index not updated."""
    real = os.replace
    index = os.path.normcase(os.path.abspath(index))

    def replace(src, dst):
        if os.path.normcase(os.path.abspath(dst)) == index:
            raise error or OSError('the index is in use')
        return real(src, dst)
    return mock.patch.object(smod.os, 'replace', replace)


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
            lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE))
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
            lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE))
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

    # -- a clean backend writes over no uncommitted change ----------------------------------------

    def clean_store(self):
        backend = smod.MainBackend(self.wt, DIR, clean=True)
        return smod.Store(backend, rmod.Rules(), session='kit-test')

    def test_a_clean_write_over_an_uncommitted_edit_is_dirty_and_writes_nothing(self):
        with open(self.path('BAR'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nedited by hand\n')
        before, status = self.head(), self.status()
        with self.assertRaises(errors.Dirty) as cm:
            self.clean_store().write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        exc = cm.exception
        self.assertIsInstance(exc, Failure)
        self.assertNotIsInstance(exc, Uncommitted)
        self.assertEqual(exc.paths, [DIR + '/BAR.md'])
        self.assertEqual(exc.compare, 'git -C "%s" diff HEAD -- "%s/BAR.md"' % (self.wt, DIR))
        self.assertIn('nothing was written', str(exc))
        self.assertIn(exc.compare, str(exc))
        self.assertEqual((self.head(), self.status()), (before, status))
        with open(self.path('BAR'), encoding='utf-8') as f:
            self.assertIn('edited by hand', f.read())

    def test_a_clean_write_names_only_the_paths_that_differ(self):
        """A record never committed and a staged change are dirty too; a path the write touches
        that matches HEAD is not named."""
        self.repo.write('%s/NEW.md' % DIR, priced('NEW')['NEW'], root=self.wt)
        with open(self.path('BAZ'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nstaged by hand\n')
        self.git('add', '--', DIR + '/BAZ.md')

        def plan(s):
            change = pmod.plan_close(s, self.ctx, 'NEW', REASON)
            change.writes.update(pmod.plan_close(s, self.ctx, 'BAZ', REASON).writes)
            change.writes.update(pmod.plan_close(s, self.ctx, 'BAR', REASON).writes)
            return change
        with self.assertRaises(errors.Dirty) as cm:
            self.clean_store().write(plan)
        self.assertEqual(cm.exception.paths, [DIR + '/BAZ.md', DIR + '/NEW.md'])
        self.assertEqual(cm.exception.compare, 'git -C "%s" status --short -- "%s/BAZ.md" '
                         '"%s/NEW.md"' % (self.wt, DIR, DIR))
        self.assertTrue(os.path.exists(self.path('BAR')))

    def test_a_clean_write_over_clean_paths_commits_as_ever(self):
        before = self.head()
        _, tip = self.clean_store().write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assert_committed(before, tip, [('D', DIR + '/BAR.md')])

    def test_a_clean_write_where_only_the_index_is_stale_names_the_index_repair(self):
        """HEAD moved by a command that left the index alone (commit_beside): the worktree holds
        what HEAD holds and the index does not. The write is refused all the same, nothing
        written, but the Dirty names the index repair, not a compare with HEAD that would show
        nothing; that repair makes status clean, and the write then commits."""
        commit_beside(self.wt, {DIR + '/BAR.md': self.read('BAR') + b'\nmoved by plumbing\n'},
                      'moved by plumbing')
        before, status = self.head(), self.status()
        self.assertNotEqual(status, [])
        with self.assertRaises(errors.Dirty) as cm:
            self.clean_store().write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        exc = cm.exception
        repair = 'git -C "%s" reset -q -- "%s/BAR.md"' % (self.wt, DIR)
        self.assertEqual((exc.paths, exc.compare, exc.repair), ([DIR + '/BAR.md'], '', repair))
        self.assertEqual(str(exc), (
            '{d}/BAR.md not written in {wt}, and nothing was written: the index there is stale '
            'for {d}/BAR.md (the worktree holds what HEAD holds and the index does not: a store '
            'write whose index update failed, or a command that moved HEAD and left the index '
            'alone), so a plain `git commit` there would revert it; repair the index first (the '
            'worktree files are left alone): {r}').format(d=DIR, wt=self.wt, r=repair))
        self.assertEqual((self.head(), self.status()), (before, status))
        self.assertTrue(os.path.exists(self.path('BAR')))
        self.git('reset', '-q', '--', DIR + '/BAR.md')     # the repair, as it names it
        self.assertEqual(self.status(), [])
        _, tip = self.clean_store().write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assert_committed(before, tip, [('D', DIR + '/BAR.md')])

    def test_a_clean_write_with_no_index_file_names_a_whole_index_repair(self):
        """No index file at all (every tracked file then reads as a staged deletion): the repair
        is a reset of the whole index, since one of the write's paths alone would leave every
        other file staged as deleted."""
        os.remove(self.worktree_index())
        before = self.head()
        with self.assertRaises(errors.Dirty) as cm:
            self.clean_store().write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assertEqual((cm.exception.compare, cm.exception.repair),
                         ('', 'git -C "%s" reset -q' % self.wt))
        self.assertEqual(self.head(), before)
        self.git('reset', '-q')
        self.assertEqual(self.status(), [])

    def test_a_write_that_is_not_clean_still_writes_over_an_edit(self):
        """`clean` is the landing's (at_landing._pm_data_store): a backlog.py write keeps
        committing the record as the worktree holds it."""
        with open(self.path('BAR'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nedited by hand\n')
        before = self.head()
        _, tip = self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assert_committed(before, tip, [('D', DIR + '/BAR.md')])

    # -- races and failures ----------------------------------------------------------------------

    def test_a_head_moved_by_another_commit_is_built_on_not_lost(self):
        """The other commit moves HEAD without the index lock (commit_beside): the write builds
        on it, and the index the write did not touch stays as that commit left it."""
        real = self.backend._head
        moved = []

        def head_then_move():
            sha = real()
            if not moved:
                moved.append(commit_beside(self.wt, {'other.txt': 'other\n'}, 'someone else'))
            return sha
        with mock.patch.object(self.backend, '_head', side_effect=head_then_move):
            _, tip = self.store.write(
                lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**'],
                                        **ADD_NONE))
        self.assertEqual(self.git('ls-files', '--', 'other.txt'), '')   # the mover's own index
        self.git('reset', '-q', '--', 'other.txt')                      # its repair
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
                    lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**'],
                                            **ADD_NONE))
        self.assertIsInstance(cm.exception, Uncommitted)    # the files are written
        msg = str(cm.exception)
        self.assertIn('%s/FOO.md' % DIR, msg)
        self.assertIn('not committed', msg)
        self.assertIn('catch-up', msg)
        self.assertEqual(self.head(), before)
        self.assertTrue(os.path.isfile(self.path('FOO')))

    def test_a_retry_never_overwrites_another_writers_commit_of_the_same_record(self):
        """Two writers on BAR: the other (one that moves HEAD without the index lock,
        commit_beside) commits while this one is between its first attempt's HEAD read and its
        update-ref. The retry sees HEAD's BAR changed since its base and stops,
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
                theirs.append(commit_change(other, other_ctx, lambda s, ctx: pmod.plan_set(
                    s, ctx, 'BAR', [('summary', ['theirs'])]), 'theirs\n'))
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
        other (without the index lock, commit_beside) writes and commits BAR before this one's first commit attempt reads HEAD. The base
        is HEAD from before the check, so the first attempt sees BAR changed and stops, Overtaken;
        the other writer's commit stands and the error names no catch-up to run."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._write_files
        theirs = []

        def the_other_writes_then_write(*args):
            theirs.append(commit_change(other, other_ctx, lambda s, ctx: pmod.plan_set(
                s, ctx, 'BAR', [('summary', ['theirs'])]), 'theirs\n'))
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
        ctx)` having been written and committed (without the index lock, commit_change) after
        this one checked the files and before its first commit attempt. `after_my_files()` runs
        once this write's files are on disk."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._write_files
        index = self.git('ls-files', '-s', '--', DIR)

        def the_other_writes_then_write(*args):
            self.winner = commit_change(other, other_ctx, theirs, 'theirs\n')
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
        self.repair_beside(index, cm.exception.changed)
        return cm.exception

    def repair_beside(self, index, paths):
        """The index after an Overtaken write is `index` (the store's entries before it: the
        refused write staged nothing); then the winner's own repair (commit_beside left the index
        stale for `paths`) makes it HEAD's for them."""
        self.assertEqual(self.git('ls-files', '-s', '--', DIR), index)
        self.git('reset', '-q', '--', *paths)

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
        reads now, another store's `theirs(snap, ctx)` having been written and committed (without
        the index lock, commit_change) after this one checked the files. `patches`: backend methods replaced for the publish."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._write_files

        def the_other_writes_then_write(*args):
            self.winner = commit_change(other, other_ctx, theirs, 'theirs\n')
            real(*args)
        index = self.git('ls-files', '-s', '--', DIR)
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
        self.repair_beside(index, cm.exception.changed)
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
        real, moves = os.replace, []

        def replace(src, dst):                       # the write's own move goes through
            if os.path.basename(dst) == 'BAR.md':
                moves.append(src)
                if len(moves) > 1:
                    raise OSError('disk full')
            return real(src, dst)
        with mock.patch.object(smod.os, 'replace', replace):
            exc = self.overtaken_publish(
                lambda s, ctx: pmod.plan_set(s, ctx, 'BAR', [('summary', ['theirs'])]),
                {'BAR': mine})
        self.assertEqual(len(moves), 2)                 # the write, then the refused put back
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
        """The commit lands but the real index cannot be updated (the updated copy cannot be moved
        over it): IndexStale, not Uncommitted, saying HEAD has the commit, the lock file is gone,
        and its repair command makes status clean."""
        before = self.head()
        with index_move_fails(self.worktree_index()):
            with self.assertRaises(Failure) as cm:
                self.store.write(
                    lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**'],
                                            **ADD_NONE))
        self.assertFalse(os.path.exists(self.worktree_index() + '.lock'))
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
        with index_move_fails(self.worktree_index()):
            with self.assertRaises(errors.IndexStale) as cm:
                store.write(lambda s: pmod.plan_add(s, ctx, 'FOO', 'a summary',
                                                    ['area/foo/**'], **ADD_NONE))
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
                lambda s: pmod.plan_add(s, self.ctx, 'FOO', 'a summary', ['area/foo/**'],
                                        **ADD_NONE))
            pending = self.backend.pending()
        self.assertFalse(os.path.exists(stray))
        self.assertEqual(pending, [])
        self.assert_committed(before, tip, [('A', DIR + '/FOO.md')])

    # -- git's index lock -------------------------------------------------------------------------

    def lock_path(self):
        return self.worktree_index() + '.lock'

    def test_a_git_commit_during_a_close_cannot_commit_the_index_it_has_not_updated(self):
        """A `git commit` in the PM data worktree between the close's commit and its index update
        would commit an index still holding the closed record (putting it back on HEAD) and leave
        its deletion staged for whoever commits next. The write holds git's index lock from
        before it reads HEAD's commit until the index holds the write, so that commit stops: the
        close stands, a change staged there stays staged, and the lock is given up."""
        self.repo.write('notes.txt', 'staged\n', root=self.wt)
        self.git('add', '--', 'notes.txt')
        store = self.clean_store()
        real = store.backend._publish_commit
        lock = self.lock_path()
        seen = []

        def commit_then_intrude(*args, **kw):
            done = real(*args, **kw)
            seen.append(os.path.exists(lock))
            seen.append(cmd.call(['commit', '-q', '-m', 'intruder'], self.wt, env=fixture_env()))
            return done
        before = self.head()
        with mock.patch.object(store.backend, '_publish_commit', side_effect=commit_then_intrude):
            _, tip = store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        held, intruder = seen
        self.assertNotEqual(intruder.rc, 0, 'the intruding git commit was made')
        self.assertIn('index.lock', intruder.err)
        self.assertTrue(held)
        self.assertEqual(self.head(), tip)
        self.assertEqual(self.git('rev-parse', tip + '^'), before)
        self.assertEqual(self.changed_in(tip), [('D', DIR + '/BAR.md')])
        self.assertEqual(self.status(), ['A  notes.txt'])
        self.assertFalse(os.path.exists(lock))

    def test_a_held_index_lock_is_busy_and_nothing_is_written(self):
        """Another git command holds the lock for all of INDEX_WAIT: Busy (a Failure, not an
        Uncommitted), naming the lock; no file, commit or index entry changed, and the lock file
        is left as it was found."""
        lock = self.lock_path()
        with open(lock, 'w') as f:
            f.write('another git\n')
        self.addCleanup(os.remove, lock)
        before, status = self.head(), self.status()
        with mock.patch.object(smod, 'INDEX_WAIT', 0.2):
            with self.assertRaises(errors.Busy) as cm:
                self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        exc = cm.exception
        self.assertIsInstance(exc, Failure)
        self.assertNotIsInstance(exc, Uncommitted)
        self.assertEqual(os.path.normcase(exc.lock), os.path.normcase(lock))
        self.assertIn(exc.lock, str(exc))
        self.assertIn('nothing was written', str(exc))
        self.assertEqual((self.head(), self.status()), (before, status))
        self.assertTrue(os.path.exists(self.path('BAR')))
        with open(lock) as f:
            self.assertEqual(f.read(), 'another git\n')

    def test_a_lock_given_up_while_the_write_waits_is_taken(self):
        lock = self.lock_path()
        with open(lock, 'w') as f:
            f.write('')
        timer = threading.Timer(0.3, os.remove, [lock])
        timer.start()
        self.addCleanup(timer.join)
        before = self.head()
        with mock.patch.object(smod, 'INDEX_WAIT', 30):
            _, tip = self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assert_committed(before, tip, [('D', DIR + '/BAR.md')])

    def test_a_second_write_waits_for_the_first_and_is_busy_when_it_does_not_end(self):
        """Two store writes in the PM data worktree run one after the other: one that starts
        while another holds the lock waits INDEX_WAIT seconds for it, then stops Busy with
        nothing written; the first commits as ever."""
        other = smod.Store(smod.MainBackend(self.wt, DIR), rmod.Rules(), session='kit-other')
        other_ctx = pmod.Context(other, self.wt, branch=BRANCH)
        real = self.backend._write_files
        refused = []

        def the_other_writes_then_write(*args):
            with self.assertRaises(errors.Busy) as cm:
                other.write(lambda s: pmod.plan_set(s, other_ctx, 'BAZ', [('summary', ['theirs'])]))
            refused.append(cm.exception)
            return real(*args)
        before = self.head()
        with mock.patch.object(smod, 'INDEX_WAIT', 0.2), \
                mock.patch.object(self.backend, '_write_files',
                                  side_effect=the_other_writes_then_write):
            _, tip = self.store.write(lambda s: pmod.plan_set(s, self.ctx, 'BAR',
                                                              [('summary', ['mine'])]))
        self.assertEqual(len(refused), 1)
        self.assertIn('%s/BAZ.md not written' % DIR, str(refused[0]))
        self.assert_committed(before, tip, [('M', DIR + '/BAR.md')])
        self.assertNotIn(b'theirs', self.read('BAZ'))

    def test_catch_up_apply_holds_the_index_lock_too(self):
        self.leave_pending()
        lock = self.lock_path()
        real = self.backend._publish_commit
        held = []

        def commit(*args, **kw):
            held.append(os.path.exists(lock))
            return real(*args, **kw)
        with mock.patch.object(self.backend, '_publish_commit', side_effect=commit):
            tip = self.backend.commit_paths([DIR + '/NEW.md'], 'backlog: catch up\n')
        self.assertEqual(held, [True])
        self.assertFalse(os.path.exists(lock))
        self.assertEqual(self.changed_in(tip), [('A', DIR + '/NEW.md')])
        with open(lock, 'w') as f:
            f.write('')
        self.addCleanup(os.remove, lock)
        with mock.patch.object(smod, 'INDEX_WAIT', 0.2):
            with self.assertRaises(errors.Busy):
                self.backend.commit_paths([DIR + '/BAZ.md'], 'backlog: catch up\n')
        self.assertEqual(self.head(), tip)
        self.assertIn(('modified', DIR + '/BAZ.md'), self.backend.pending())

    def test_a_move_over_the_index_refused_for_a_moment_is_retried(self):
        """Windows refuses the move while a reader has the index open: it is tried again, and
        the write ends clean."""
        real = os.replace
        refused = []

        index = os.path.normcase(self.worktree_index())

        def replace(src, dst):
            if os.path.normcase(os.path.abspath(dst)) == index and len(refused) < 2:
                refused.append(dst)
                raise PermissionError(13, 'Access is denied', dst)
            return real(src, dst)
        before = self.head()
        with mock.patch.object(smod.os, 'replace', replace):
            _, tip = self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assertEqual(len(refused), 2)
        self.assert_committed(before, tip, [('D', DIR + '/BAR.md')])
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_a_move_over_the_index_refused_for_good_is_index_stale(self):
        """Still refused after REPLACE_WAIT: the write is committed, so IndexStale (not Busy,
        which says nothing was written), and the lock file is removed."""
        before = self.head()
        with index_move_fails(self.worktree_index(), PermissionError(13, 'Access is denied')), \
                mock.patch.object(smod, 'REPLACE_WAIT', 0.2):
            with self.assertRaises(errors.IndexStale) as cm:
                self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        exc = cm.exception
        self.assertIn('Access is denied', str(exc))
        self.assertEqual(self.git('rev-parse', exc.commit + '^'), before)
        self.assertEqual(exc.commit, self.head())
        self.assertFalse(os.path.exists(self.lock_path()))
        self.assertNotEqual(self.status(), [])
        self.git('reset', '-q', '--', DIR + '/BAR.md')
        self.assertEqual(self.status(), [])

    def test_a_write_with_no_index_file_is_index_stale_with_a_whole_index_repair(self):
        """No index file to copy into the lock file: the write is committed and IndexStale, and
        its repair resets the whole index, which makes status clean; a reset of the write's own
        path would build an index of that path alone."""
        index = self.worktree_index()
        os.remove(index)
        before = self.head()
        with self.assertRaises(errors.IndexStale) as cm:
            self.store.write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        exc = cm.exception
        self.assertEqual(exc.repair, 'git -C "%s" reset -q' % self.wt)
        self.assertIn('cannot copy the index into its lock file', str(exc))
        self.assertTrue(str(exc).endswith(exc.repair), str(exc))
        self.assertEqual(self.git('rev-parse', exc.commit + '^'), before)
        self.assertEqual(self.changed_in(exc.commit), [('D', DIR + '/BAR.md')])
        self.assertFalse(os.path.exists(index + '.lock'))
        self.git('reset', '-q', '--', DIR + '/BAR.md')     # what the repair said before
        self.assertIn('D  %s/BAZ.md' % DIR, self.status())
        os.remove(index)
        self.git('reset', '-q')                            # the repair, as it names it
        self.assertEqual(self.status(), [])

    def test_a_lock_file_that_cannot_be_removed_is_named_on_the_refusal(self):
        """The write stops (Dirty here) and the lock file it made cannot be removed: the
        refusal says so, since no git command can write that index until it is gone."""
        with open(self.path('BAR'), 'a', encoding='utf-8', newline='\n') as f:
            f.write('\nedited by hand\n')
        lock = self.lock_path()
        real = os.remove

        def remove(path):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(lock):
                raise PermissionError(13, 'Access is denied', path)
            return real(path)
        with mock.patch.object(smod.os, 'remove', remove), \
                mock.patch.object(smod, 'REPLACE_WAIT', 0.2):
            with self.assertRaises(errors.Dirty) as cm:
                self.clean_store().write(lambda s: pmod.plan_close(s, self.ctx, 'BAR', REASON))
        self.assertTrue(os.path.exists(lock))
        os.remove(lock)
        held = os.path.normpath(self.git('rev-parse', '--path-format=absolute', '--git-path',
                                         'index')) + '.lock'
        self.assertEqual(os.path.normcase(held), os.path.normcase(lock))
        self.assertEqual(str(cm.exception), (
            '{d}/BAR.md not written in {wt}, and nothing was written: {d}/BAR.md differs from '
            'HEAD there (a change made there and not committed: a hand edit, a staged change or '
            'a record never committed), which this write would delete or commit over; compare '
            'with HEAD first: git -C "{wt}" diff HEAD -- "{d}/BAR.md"; and git\'s index lock '
            '{h} could not be removed ({e}): no git command can write that index until it is; '
            'remove it by hand').format(d=DIR, wt=self.wt, h=held,
                                        e=PermissionError(13, 'Access is denied', held)))
        self.assertEqual(cm.exception.paths, [DIR + '/BAR.md'])

    # -- which stores commit ---------------------------------------------------------------------

    def test_a_worktree_on_another_branch_commits_nothing(self):
        other = self.repo.worktree('elsewhere')
        backend = smod.MainBackend(other, DIR)
        store = smod.Store(backend, rmod.Rules(), session='kit-test')
        ctx = pmod.Context(store, other, branch=BRANCH)
        before = self.repo.git('rev-parse', 'HEAD', cwd=other)
        _, tip = store.write(lambda s: pmod.plan_add(s, ctx, 'FOO', 'a summary', ['area/foo/**'],
                                                     **ADD_NONE))
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
        """The index cannot be updated after the commit: `add` and `catch-up --apply` still
        commit, so each prints its WROTE / CAUGHT UP line with the commit, then a WARNING carrying
        the repair; exit 0."""
        self.tool('catch-up')                             # writes .kit.json
        self.repo.write('%s/NEW.md' % DIR, priced('NEW')['NEW'], root=self.wt)
        with index_move_fails(self.worktree_index()):
            rc, out = self.tool('add', 'FOO', '--summary', 'a summary', '--scope',
                                'area/foo/**', *ADD_FLAGS)
            added = self.head()
            rc2, out2 = self.tool('catch-up', '--apply', DIR + '/NEW.md')
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


class _FullDisk(object):
    """A file opened for writing whose every write fails as a full disk does."""

    def __init__(self, f):
        self.f = f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def write(self, data):
        raise OSError(errno.ENOSPC, 'No space left on device')

    def close(self):
        self.f.close()


def nth_write_fails(n):
    """Patches under which the `n`th file the store module opens for writing (by `open` or
    `os.fdopen`) is opened, so a truncating open has truncated it, and then refuses every write."""
    count = [0]
    real_open, real_fdopen = open, os.fdopen

    def wrap(f, mode):
        if 'w' in mode:
            count[0] += 1
            if count[0] == n:
                return _FullDisk(f)
        return f
    return (mock.patch.object(smod, 'open', create=True,
                              new=lambda p, mode='r', *a, **k: wrap(real_open(p, mode, *a, **k),
                                                                   mode)),
            mock.patch.object(smod.os, 'fdopen',
                              new=lambda fd, mode='r', *a, **k: wrap(real_fdopen(fd, mode, *a, **k),
                                                                    mode)))


class WriteWhole(unittest.TestCase):
    """A store write that fails part way leaves every record whole and names the one it could not
    write (MainBackend._write_files, _restore, _replace_file)."""

    setUp = PmDataStore.setUp
    git = PmDataStore.git
    head = PmDataStore.head

    def leftovers(self):
        return [n for n in os.listdir(os.path.join(self.wt, *DIR.split('/')))
                if n.endswith('.put-back')]

    def test_a_write_that_fails_leaves_the_record_whole_and_named(self):
        """BAR is written, then the write into BAZ fails: BAZ still holds its bytes (never a
        truncated file), BAR is put back, the Failure names BAZ, and no temp file is left."""
        snap = self.backend.load(rmod.Rules())
        before = dict(snap.raw)
        writes = dict((stem, before[stem].decode('utf-8') + '\nchanged\n') for stem in ('BAR', 'BAZ'))
        head = self.head()
        first, second = nth_write_fails(2)
        with first, second, self.assertRaises(Failure) as cm:
            self.backend.publish(snap, writes, 'both\n')
        now = self.backend._read()
        self.assertEqual(now['BAZ'], before['BAZ'])
        self.assertEqual(now['BAR'], before['BAR'])
        self.assertIn('%s/BAZ.md' % DIR, str(cm.exception))
        self.assertIn('No space left on device', str(cm.exception))
        self.assertIn('the files already written were put back', str(cm.exception))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.head(), head)

    def test_a_restore_that_fails_leaves_the_record_whole_and_named(self):
        """BAR is written, BAZ's write fails, and putting BAR back fails too: BAR holds the new
        content whole (not a truncated file), BAZ its old bytes, and the Failure names BAZ as the
        record it could not write and BAR as not put back."""
        snap = self.backend.load(rmod.Rules())
        before = dict(snap.raw)
        writes = dict((stem, before[stem].decode('utf-8') + '\nchanged\n') for stem in ('BAR', 'BAZ'))
        with mock.patch.object(smod, '_replace_file', side_effect=self.fail_from_second()):
            with self.assertRaises(Failure) as cm:
                self.backend.publish(snap, writes, 'both\n')
        now = self.backend._read()
        self.assertEqual(now['BAZ'], before['BAZ'])
        self.assertEqual(now['BAR'], writes['BAR'].encode('utf-8'))
        self.assertIn('cannot write %s/BAZ.md' % DIR, str(cm.exception))
        self.assertIn('could not be put back: %s/BAR.md' % DIR, str(cm.exception))

    def publish_fails(self, writes):
        """The Failure of publishing `writes` ({stem: text or None}) on a fresh snapshot."""
        snap = self.backend.load(rmod.Rules())
        with self.assertRaises(Failure) as cm:
            self.backend.publish(snap, writes, 'w\n')
        return str(cm.exception)

    def test_a_delete_that_fails_leaves_the_record_and_names_it(self):
        before = self.backend._read()
        real = os.remove

        def remove(path):
            if os.path.basename(path) == 'BAR.md':
                raise PermissionError(errno.EACCES, 'in use')
            return real(path)
        with mock.patch.object(smod.os, 'remove', remove):
            msg = self.publish_fails({'BAR': None})
        self.assertIn('cannot write %s/BAR.md (it is left as it was)' % DIR, msg)
        self.assertEqual(self.backend._read(), before)

    def test_a_folder_that_cannot_be_made_is_named(self):
        real = os.makedirs

        def makedirs(path, *a, **k):              # git's own folders (the fake git) still made
            if os.path.normcase(path) == os.path.normcase(self.backend.dir):
                raise PermissionError(errno.EACCES, 'denied')
            return real(path, *a, **k)
        with mock.patch.object(smod.os, 'makedirs', makedirs):
            msg = self.publish_fails({'BAR': 'x\n'})
        self.assertIn('cannot write %s' % self.backend.dir, msg)

    def test_a_temp_file_that_cannot_be_made_leaves_the_record_whole(self):
        before = self.backend._read()
        with mock.patch.object(smod.tempfile, 'mkstemp', side_effect=OSError(errno.ENOSPC, 'full')):
            msg = self.publish_fails({'BAR': 'x\n'})
        self.assertIn('cannot write %s/BAR.md (it is left as it was)' % DIR, msg)
        self.assertEqual(self.backend._read(), before)
        self.assertEqual(self.leftovers(), [])

    def test_a_new_file_that_cannot_be_deleted_again_is_named(self):
        """NEW is written, ZED's write fails, and removing NEW (it was not there before) fails:
        NEW is named as not put back."""
        real = os.remove

        def remove(path):
            if os.path.basename(path) == 'NEW.md':
                raise PermissionError(errno.EACCES, 'in use')
            return real(path)
        with mock.patch.object(smod, '_replace_file', side_effect=self.fail_from_second()), \
                mock.patch.object(smod.os, 'remove', remove):
            msg = self.publish_fails({'NEW': 'new\n', 'ZED': 'zed\n'})
        self.assertIn('cannot write %s/ZED.md' % DIR, msg)
        self.assertIn('could not be put back: %s/NEW.md' % DIR, msg)
        self.assertEqual(self.backend._read().get('NEW'), b'new\n')

    def test_outside_the_pm_data_worktree_the_failure_names_no_catch_up(self):
        before = self.backend._read()
        with mock.patch.object(smod, '_replace_file',
                               side_effect=OSError(errno.ENOSPC, 'full')):
            with self.assertRaises(Failure) as cm:
                self.backend._write_files({'BAR': 'x\n'}, before, [DIR + '/BAR.md'], None)
        self.assertIn('cannot write %s/BAR.md' % DIR, str(cm.exception))
        self.assertNotIn('not committed', str(cm.exception))
        self.assertNotIn('catch-up', str(cm.exception))

    def fail_from_second(self):
        """_replace_file that works once (BAR's write) and then fails, leaving its target alone."""
        real, calls = smod._replace_file, [0]

        def replace(full, data):
            calls[0] += 1
            if calls[0] == 1:
                return real(full, data)
            raise OSError(errno.EIO, 'the disk went away')
        return replace


class ReplaceFile(unittest.TestCase):
    """_replace_file: whole or not at all, a refused move retried, a temp file it cannot remove
    named."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-replace-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.full = os.path.join(self.dir, 'FOO.md')
        with open(self.full, 'wb') as f:
            f.write(b'old\n')

    def content(self):
        with open(self.full, 'rb') as f:
            return f.read()

    def others(self):
        return sorted(n for n in os.listdir(self.dir) if n != 'FOO.md')

    def test_the_temp_file_is_beside_the_target_and_is_no_record(self):
        made = []
        real = tempfile.mkstemp

        def mkstemp(*a, **k):
            fd, path = real(*a, **k)
            made.append(path)
            return fd, path
        with mock.patch.object(smod.tempfile, 'mkstemp', mkstemp):
            smod._replace_file(self.full, b'new\n')
        self.assertEqual(self.content(), b'new\n')
        self.assertEqual(os.path.dirname(made[0]), self.dir)
        self.assertFalse(made[0].endswith('.md'), made[0])
        self.assertEqual(self.others(), [])

    def test_a_failed_write_into_the_temp_file_leaves_the_target_whole(self):
        _, fdopen = nth_write_fails(1)
        with fdopen, self.assertRaises(OSError) as cm:
            smod._replace_file(self.full, b'new\n')
        self.assertEqual(cm.exception.errno, errno.ENOSPC)
        self.assertEqual(self.content(), b'old\n')
        self.assertEqual(self.others(), [])

    def test_a_temp_file_that_cannot_be_removed_is_named(self):
        _, fdopen = nth_write_fails(1)
        real = os.remove

        def remove(path):
            if path.endswith('.put-back'):
                raise PermissionError(errno.EACCES, 'in use')
            return real(path)
        with fdopen, mock.patch.object(smod.os, 'remove', remove):
            with self.assertRaises(OSError) as cm:
                smod._replace_file(self.full, b'new\n')
        left = self.others()
        self.assertEqual(len(left), 1, left)
        self.assertEqual(cm.exception.errno, errno.ENOSPC)
        self.assertIn('No space left on device', str(cm.exception))
        self.assertIn(os.path.join(self.dir, left[0]), str(cm.exception))
        self.assertIn('in use', str(cm.exception))
        self.assertEqual(self.content(), b'old\n')

    def test_a_move_refused_for_a_while_is_retried(self):
        real, calls = os.replace, [0]

        def replace(src, dst):
            calls[0] += 1
            if calls[0] <= 2:
                raise PermissionError(errno.EACCES, 'held open by a reader')
            return real(src, dst)
        with mock.patch.object(smod.os, 'replace', replace), \
                mock.patch.object(smod.time, 'sleep') as sleep:
            smod._replace_file(self.full, b'new\n')
        self.assertEqual(calls[0], 3)
        self.assertEqual(self.content(), b'new\n')
        self.assertEqual(self.others(), [])
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(c.args[0] <= 0.05 for c in sleep.call_args_list))

    def test_a_move_refused_past_the_wait_leaves_the_target_whole(self):
        calls = [0]

        def replace(src, dst):
            calls[0] += 1
            raise PermissionError(errno.EACCES, 'held open by a reader')
        with mock.patch.object(smod.os, 'replace', replace), \
                mock.patch.object(smod, 'REPLACE_WAIT', 0.2):
            with self.assertRaises(PermissionError):
                smod._replace_file(self.full, b'new\n')
        self.assertGreater(calls[0], 1)
        self.assertEqual(self.content(), b'old\n')
        self.assertEqual(self.others(), [])

    def test_an_interrupt_before_the_move_leaves_no_temp_file(self):
        with mock.patch.object(smod.os, 'replace', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                smod._replace_file(self.full, b'new\n')
        self.assertEqual(self.content(), b'old\n')
        self.assertEqual(self.others(), [])

    def test_any_other_refusal_is_not_retried(self):
        calls = [0]

        def replace(src, dst):
            calls[0] += 1
            raise OSError(errno.EIO, 'the disk went away')
        with mock.patch.object(smod.os, 'replace', replace):
            with self.assertRaises(OSError):
                smod._replace_file(self.full, b'new\n')
        self.assertEqual(calls[0], 1)
        self.assertEqual(self.content(), b'old\n')
        self.assertEqual(self.others(), [])


if __name__ == '__main__':
    unittest.main()
