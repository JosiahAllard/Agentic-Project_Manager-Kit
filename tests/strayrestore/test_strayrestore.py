"""kitlib.strayrestore: a main-checkout stray is discarded only when a branch tip holds its exact
bytes at the same path; everything else is LEAVE and never touched (docs/stray-restore.md)."""
import os
import unittest
from unittest import mock

from kitlib import strayrestore as sr
from tests.realgit import real_git
from tests.strayrestore.helpers import ENV, Fixture

REASON = 'git restore, check-attr and cat-file --filters: the fake git lacks them'


def by_path(run):
    return {i.path: i for i in run.items}


@real_git(REASON)
class Untracked(Fixture):

    def test_a_branch_tip_at_the_same_path_deletes_it_on_apply_only(self):
        commit, _wt = self.branch('kit/a', {'docs/new.md': b'new text\n'})
        self.put('docs/new.md', b'new text\n')
        item = by_path(sr.plan(self.top))['docs/new.md']
        self.assertEqual((item.verdict, item.match.ref, item.match.form),
                         (sr.DELETE, 'refs/heads/kit/a', 'raw'))
        self.assertEqual(self.get('docs/new.md'), b'new text\n')
        self.assertEqual(self.log_lines(), [])
        run = sr.apply(self.top, ENV)
        self.assertIsNone(run.error)
        self.assertEqual(run.left, [])
        self.assertFalse(os.path.exists(os.path.join(self.top, 'docs', 'new.md')))
        [rec] = self.log_lines()
        self.assertEqual((rec['path'], rec['status'], rec['action'], rec['match_ref'],
                          rec['match_commit'], rec['bytes_blob'], rec['match_form']),
                         ('docs/new.md', '??', 'deleted', 'refs/heads/kit/a', commit,
                          self.blob('%s:docs/new.md' % commit), 'raw'))
        self.assertFalse(os.listdir(os.path.join(self.r.common_dir(), sr.QUARANTINE)))

    def test_the_same_bytes_at_another_path_leave_it(self):
        self.branch('kit/a', {'other/new.md': b'new text\n'})
        self.put('docs/new.md', b'new text\n')
        run = sr.apply(self.top, ENV)
        self.assertEqual(by_path(run)['docs/new.md'].verdict, sr.LEAVE)
        self.assertEqual(self.get('docs/new.md'), b'new text\n')
        self.assertEqual(self.log_lines(), [])


@real_git(REASON)
class Modified(Fixture):

    def test_a_branch_blob_unlike_head_restores_heads_bytes_and_logs_both(self):
        commit, _wt = self.branch('kit/a', {'README.md': b'branch text\n'})
        self.put('README.md', b'branch text\n')
        run = sr.apply(self.top, ENV)
        self.assertEqual(by_path(run)['README.md'].verdict, sr.RESTORE)
        self.assertEqual(self.get('README.md'), b'hello\n')
        self.assertEqual(run.left, [])
        [rec] = self.log_lines()
        self.assertEqual(rec['action'], 'restored')
        self.assertEqual(rec['status'], ' M')
        self.assertEqual(rec['match_ref'], 'refs/heads/kit/a')
        self.assertEqual(rec['match_commit'], commit)
        self.assertEqual(rec['bytes_blob'], self.blob('%s:README.md' % commit))
        self.assertEqual(rec['head'], self.r.head())
        self.assertEqual(rec['head_blob'], self.blob('HEAD:README.md'))
        self.assertNotEqual(rec['bytes_blob'], rec['head_blob'])

    def test_bytes_no_tip_holds_are_left_byte_for_byte(self):
        self.branch('kit/a', {'README.md': b'branch text\n'})
        self.put('README.md', b'branch text plus one\r\n')
        run = sr.apply(self.top, ENV)
        item = by_path(run)['README.md']
        self.assertEqual(item.verdict, sr.LEAVE)
        self.assertIn('no branch tip', item.reason)
        self.assertEqual(self.get('README.md'), b'branch text plus one\r\n')
        self.assertEqual(run.left, [(' M', 'README.md')])
        self.assertEqual(self.log_lines(), [])

    def test_a_work_tree_delete_is_undeleted_from_head(self):
        os.remove(os.path.join(self.top, 'README.md'))
        run = sr.apply(self.top, ENV)
        self.assertEqual(by_path(run)['README.md'].verdict, sr.UNDELETE)
        self.assertEqual(self.get('README.md'), b'hello\n')
        [rec] = self.log_lines()
        self.assertEqual((rec['action'], rec['status'], rec['match_ref']),
                         ('undeleted', ' D', None))


@real_git(REASON)
class WhichRefs(Fixture):

    def test_a_remote_tracking_tip_counts(self):
        commit, _wt = self.branch('-', {'n.txt': b'n\n'}, detach=True)
        self.r.git('update-ref', 'refs/remotes/origin/kit/gone', commit)
        self.put('n.txt', b'n\n')
        run = sr.apply(self.top, ENV)
        self.assertEqual(by_path(run)['n.txt'].match.ref, 'refs/remotes/origin/kit/gone')
        self.assertFalse(os.path.exists(os.path.join(self.top, 'n.txt')))

    def test_a_tag_a_stash_or_a_detached_commit_do_not(self):
        commit, wt = self.branch('-', {'n.txt': b'n\n'}, detach=True)
        self.r.git('tag', 'v1', commit)
        self.r.git('tag', '-a', '-m', 'annotated', 'v2', commit)
        self.put('README.md', b'stashed\n', wt)
        self.r.git('stash', 'push', '-q', cwd=wt)
        self.put('n.txt', b'n\n')
        self.put('README.md', b'stashed\n')
        run = sr.apply(self.top, ENV)
        self.assertEqual({p: i.verdict for p, i in by_path(run).items()},
                         {'n.txt': sr.LEAVE, 'README.md': sr.LEAVE})
        self.assertEqual(self.get('n.txt'), b'n\n')
        self.assertEqual(self.get('README.md'), b'stashed\n')


@real_git(REASON)
class Refusals(Fixture):

    def test_a_staged_file_is_left(self):
        self.branch('kit/a', {'s.txt': b's\n'})
        self.put('s.txt', b's\n')
        self.r.git('add', 's.txt')
        run = sr.apply(self.top, ENV)
        self.assertEqual(by_path(run)['s.txt'].verdict, sr.LEAVE)
        self.assertEqual(self.get('s.txt'), b's\n')
        self.assertEqual(self.r.git('status', '--porcelain'), 'A  s.txt')

    def test_a_merge_or_rebase_in_progress_refuses_the_run(self):
        self.branch('kit/a', {'s.txt': b's\n'})
        self.put('s.txt', b's\n')
        for make in (lambda: self.put('.git/MERGE_HEAD', (self.r.head() + '\n').encode()),
                     lambda: os.makedirs(os.path.join(self.top, '.git', 'rebase-merge'))):
            with self.subTest(make=make):
                make()
                before = self.snapshot()
                run = sr.apply(self.top, ENV)
                self.assertIn('in progress', run.refused)
                self.assertEqual(run.items, [])
                self.assertEqual(self.snapshot(), before)
                gone = os.path.join(self.top, '.git', 'MERGE_HEAD')
                if os.path.exists(gone):
                    os.remove(gone)


@real_git(REASON)
class Attributes(Fixture):

    def setUp(self):
        super().setUp()
        self.put('.gitattributes', b'*.txt text eol=crlf\nf.dat filter=x\n')
        self.r.git('add', '.gitattributes')
        self.r.git('commit', '-q', '-m', 'attributes')

    def test_a_crlf_checkout_of_the_tip_blob_matches_in_worktree_form(self):
        commit, _wt = self.branch('kit/a', {'a.txt': b'x\ny\n'})
        self.put('a.txt', b'x\r\ny\r\n')
        item = by_path(sr.plan(self.top))['a.txt']
        self.assertEqual((item.verdict, item.match.form), (sr.DELETE, 'worktree'))
        self.assertEqual(item.match.blob, self.blob('%s:a.txt' % commit))

    def test_a_filter_attribute_is_left(self):
        self.branch('kit/a', {'f.dat': b'raw\n'})
        self.put('f.dat', b'raw\n')
        run = sr.apply(self.top, ENV)
        self.assertEqual(by_path(run)['f.dat'].verdict, sr.LEAVE)
        self.assertIn('filter', by_path(run)['f.dat'].reason)
        self.assertEqual(self.get('f.dat'), b'raw\n')


@real_git(REASON)
class Links(Fixture):

    def test_a_junction_on_the_way_is_left(self):
        try:
            import _winapi
        except ImportError:
            self.skipTest('no _winapi: not Windows')
        self.branch('kit/a', {'j/f.txt': b'f\n'})
        outside = os.path.join(self.r.base, 'outside')
        os.makedirs(outside)
        self.put('f.txt', b'f\n', outside)
        _winapi.CreateJunction(outside, os.path.join(self.top, 'j'))
        run = sr.apply(self.top, ENV)
        self.assertTrue(run.items)
        self.assertEqual({i.verdict for i in run.items}, {sr.LEAVE})
        self.assertTrue(all('junction' in i.reason for i in run.items), run.items)
        self.assertTrue(os.path.exists(os.path.join(outside, 'f.txt')))
        self.assertEqual(self.log_lines(), [])

    def test_a_symlink_is_left(self):
        self.branch('kit/a', {'l.txt': b'f\n'})
        target = self.put('target.bin', b'f\n', self.r.base)
        try:
            os.symlink(target, os.path.join(self.top, 'l.txt'))
        except (OSError, NotImplementedError) as exc:
            self.skipTest('no symlink here: %s' % exc)
        run = sr.apply(self.top, ENV)
        self.assertEqual(by_path(run)['l.txt'].verdict, sr.LEAVE)
        self.assertIn('symlink', by_path(run)['l.txt'].reason)
        self.assertTrue(os.path.lexists(os.path.join(self.top, 'l.txt')))
        self.assertEqual(self.log_lines(), [])


@real_git(REASON)
class Safety(Fixture):

    def test_bytes_changed_between_check_and_move_are_left_and_put_back(self):
        self.branch('kit/a', {'docs/new.md': b'new text\n'})
        self.put('docs/new.md', b'new text\n')

        def racer(full):
            with open(full, 'wb') as fh:
                fh.write(b'written meanwhile\n')
        with mock.patch.object(sr, '_after_check', racer):
            run = sr.apply(self.top, ENV)
        item = by_path(run)['docs/new.md']
        self.assertEqual(item.verdict, sr.LEAVE)
        self.assertIn('changed after the check', item.reason)
        self.assertEqual(self.get('docs/new.md'), b'written meanwhile\n')
        self.assertEqual(self.log_lines(), [])
        kept = [f for _r, _d, files in os.walk(os.path.join(self.r.common_dir(), sr.QUARANTINE))
                for f in files]
        self.assertEqual(kept, [])

    def test_the_dry_run_writes_nothing(self):
        self.branch('kit/a', {'docs/new.md': b'new text\n', 'README.md': b'branch\n'})
        self.put('docs/new.md', b'new text\n')
        self.put('README.md', b'branch\n')
        os.remove(os.path.join(self.top, 'README.md'))
        self.put('README.md', b'branch\n')
        before = self.snapshot()
        run = sr.plan(self.top)
        self.assertEqual({i.verdict for i in run.items}, {sr.DELETE, sr.RESTORE})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(os.path.join(self.r.common_dir(), sr.QUARANTINE)))
        self.assertEqual(self.log_lines(), [])

    def test_a_linked_worktree_path_acts_on_the_main_checkout(self):
        _commit, wt = self.branch('kit/a', {'docs/new.md': b'new text\n'})
        self.put('docs/new.md', b'new text\n')
        run = sr.apply(wt, ENV)
        self.assertEqual(os.path.normcase(run.top), os.path.normcase(os.path.normpath(self.top)))
        self.assertFalse(os.path.exists(os.path.join(self.top, 'docs', 'new.md')))
        self.assertTrue(os.path.exists(os.path.join(wt, 'docs', 'new.md')))


class Roles(unittest.TestCase):

    def test_only_the_owner_pm_coordinator_and_lander_may_run_it(self):
        self.assertTrue(sr.allowed(None))
        for mark in ('pm', 'coordinator', 'lander', 'PM'):
            self.assertTrue(sr.allowed(mark), mark)
        for mark in ('deep-worker', 'strong-worker', 'marked-owner', ''):
            self.assertFalse(sr.allowed(mark), mark)


if __name__ == '__main__':
    unittest.main()
