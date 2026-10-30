"""`show` of a missing id names the store commit that removed it (kitlib/backlog/plan.py
`require_shown`, store.py `last_removal`): sha, date, session and subject, never an author name;
and a record deleted on a merged side branch is still found (`--full-history`)."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Ended, Failure, NotFound, Usage
from kitlib.git import cmd
from tests.backlog.helpers import ADD_FLAGS, BRANCH, StoreCase, kit_json_ref, rec, record_text
from tests.gitfixture import NAME, TempRepo, fixture_env, rmtree

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')


class RefStore(StoreCase):

    def missing(self, rid):
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(self.store.load(), self.ctx, rid)
        return str(cm.exception)

    def test_a_closed_record_names_the_closing_commit_and_its_session(self):
        self.seed(rec('FOO'))
        self.store.session = 'kit-c4-test'
        self.verb('close', 'FOO', 'the work is finished')
        tip = self.remote_tip()
        short = self.repo.git('rev-parse', '--short', tip)
        when = self.repo.git('log', '-1', '--format=%cI', tip)
        said = self.missing('FOO')
        self.assertIn('FOO is no longer in', said)
        self.assertIn('closed by %s on %s (session kit-c4-test): closes FOO: the work is '
                      'finished' % (short, when), said)
        self.assertNotIn(NAME, said)

    def test_a_closed_record_is_ended_with_its_reason_and_an_unknown_one_is_not(self):
        self.seed(rec('FOO'), rec('BAR'), rec('BAZ'))
        self.verb('close', 'FOO', 'the work is finished')
        self.verb('retire', 'BAR', 'no longer wanted at all')
        self.verb('delete', 'BAZ', 'the owner says delete it')
        for rid, how, why in (('FOO', 'closed', 'the work is finished'),
                              ('BAR', 'retired', 'no longer wanted at all'),
                              ('BAZ', 'deleted', 'the owner says delete it')):
            with self.assertRaises(Ended) as cm:
                pmod.require_shown(self.store.load(), self.ctx, rid)
            self.assertEqual(cm.exception.code, 0)
            self.assertIn('%s is no longer in' % rid, str(cm.exception))
            self.assertIn('%s by ' % how, str(cm.exception))
            self.assertTrue(str(cm.exception).endswith('\n  reason: %s' % why), str(cm.exception))
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(self.store.load(), self.ctx, 'NOPE')
        self.assertNotIsInstance(cm.exception, Ended)
        self.assertEqual(cm.exception.code, 1)

    def test_a_record_that_never_existed_is_unknown(self):
        self.seed(rec('FOO'))
        said = self.missing('NOPE')
        self.assertIn('no record with `id: NOPE`', said)
        self.assertIn('the id is unknown', said)

    def test_a_live_record_is_returned(self):
        self.seed(rec('FOO'))
        self.assertEqual(pmod.require_shown(self.store.load(), self.ctx, 'FOO').id, 'FOO')

    def test_a_store_with_no_tip_has_no_history_and_the_id_is_unknown(self):
        self.assertIsNone(self.store.load().tip)
        self.assertIn('the id is unknown', self.missing('FOO'))

    def test_a_removal_without_a_session_line_prints_a_dash(self):
        self.seed(rec('FOO'))
        self.backend.publish(self.store.load(), {'FOO': None}, 'closes FOO: by hand\n\nno trailer\n')
        self.assertIn('(session -): closes FOO: by hand', self.missing('FOO'))

    def test_a_glob_id_is_not_looked_up_and_matches_no_other_record(self):
        self.seed(rec('FOO'), rec('FOX'))
        self.verb('close', 'FOO', 'the work is finished')
        said = self.missing('FO*')
        self.assertIn('history not searched: `FO*` is not a plain id', said)
        self.assertNotIn('removed by', said)
        for rid in ('FO*', 'F?O', '[F]OO', '../FOO', 'F\\OO', ':FOO'):
            with self.assertRaises(Usage, msg=rid):
                smod.last_removal(self.repo.root, self.remote_tip(), smod.STORE_DIR, rid)


class SessionLine(unittest.TestCase):

    def test_the_first_session_line_wins_and_an_empty_one_is_none(self):
        self.assertIsNone(smod.session_of(''))
        self.assertIsNone(smod.session_of('no trailer here\n'))
        self.assertEqual(smod.session_of('Session: one\nSession: two\n'), 'one')
        self.assertIsNone(smod.session_of('Session:\n'))
        self.assertIsNone(smod.session_of('Session:   \nRuling: PM R-1\n'))
        self.assertEqual(smod.session_of('Ruling: PM R-1\nSession: s-1\n'), 's-1')


class UnreadableHistory(StoreCase):

    def test_an_unborn_head_on_a_main_store_is_still_not_found(self):
        r = TempRepo()
        self.addCleanup(r.cleanup)
        r.git('checkout', '-q', '--orphan', 'fresh')
        store = smod.Store(smod.MainBackend(r.root, 'docs/backlog'), rmod.Rules())
        ctx = pmod.Context(store, r.root, branch=BRANCH)
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(store.load(), ctx, 'FOO')
        said = str(cm.exception)
        self.assertIn('no record with `id: FOO`', said)
        self.assertIn("the store's history could not be read", said)


class MainStoreSideBranch(StoreCase):
    """The removed-by case: added and deleted on a side branch, then merged."""

    def test_a_record_deleted_on_a_merged_side_branch_names_the_deleting_commit(self):
        r = TempRepo()
        self.addCleanup(r.cleanup)
        r.git('checkout', '-q', '-b', 'side')
        r.write('docs/backlog/FOO.md', record_text(id='FOO'))
        r.commit('adds FOO', 'docs/backlog/FOO.md')
        r.git('rm', '-q', '--', 'docs/backlog/FOO.md')
        r.git('commit', '-q', '-m', 'closes FOO: done on the side\n\nSession: side-session')
        deleted = r.head()
        r.git('checkout', '-q', 'main')
        r.write('other.txt', 'other\n')
        r.commit('main moves on', 'other.txt')
        r.git('merge', '-q', '--no-ff', '-m', 'merge side', 'side')
        merge = r.head()
        store = smod.Store(smod.MainBackend(r.root, 'docs/backlog'), rmod.Rules())
        ctx = pmod.Context(store, r.root, branch=BRANCH)
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(store.load(), ctx, 'FOO')
        said = str(cm.exception)
        self.assertIn('closed by %s ' % r.git('rev-parse', '--short', deleted), said)
        self.assertIn('(session side-session): closes FOO: done on the side', said)
        self.assertNotIn(r.git('rev-parse', '--short', merge), said)


class MainStoreUncommittedClose(StoreCase):
    """A main store's `close` deletes the file and commits nothing (store.MainBackend.publish), so
    no commit in HEAD's history removed it yet: `show` says the file is tracked at HEAD and
    missing here, not that the id is unknown; a git that does not answer fails closed."""

    def main_store(self):
        r = TempRepo()
        self.addCleanup(r.cleanup)
        root = r.worktree('w')
        r.write('docs/backlog/FOO.md', record_text(id='FOO'), root=root)
        r.commit('adds FOO', 'docs/backlog/FOO.md', cwd=root)
        store = smod.Store(smod.MainBackend(root, 'docs/backlog'), rmod.Rules())
        return r, root, store, pmod.Context(store, root, branch=BRANCH)

    def shown(self, store, ctx, rid):
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(store.load(), ctx, rid)
        return str(cm.exception)

    def test_a_record_closed_and_not_yet_committed_reports_as_closed(self):
        r, root, store, ctx = self.main_store()
        store.write(lambda s: pmod.plan_close(s, ctx, 'FOO', 'the work is finished'))
        self.assertFalse(os.path.exists(os.path.join(root, 'docs', 'backlog', 'FOO.md')))
        said = self.shown(store, ctx, 'FOO')
        self.assertIn('FOO is no longer in', said)
        self.assertIn('tracked at HEAD but missing from this worktree (closed or retired here, '
                      'or removed by hand); no commit names it yet', said)
        self.assertNotIn('the id is unknown', said)

    def test_a_file_removed_by_hand_reads_the_same_and_claims_no_close(self):
        r, root, store, ctx = self.main_store()
        os.remove(os.path.join(root, 'docs', 'backlog', 'FOO.md'))
        said = self.shown(store, ctx, 'FOO')
        self.assertIn('or removed by hand', said)
        self.assertNotIn('closed or retired in this worktree', said)

    def test_a_git_that_does_not_answer_fails_closed(self):
        r, root, store, ctx = self.main_store()
        os.remove(os.path.join(root, 'docs', 'backlog', 'FOO.md'))
        dead = cmd.Result(None, b'', 'git did not answer within 30 s')
        with mock.patch.object(smod.cmd, 'call', return_value=dead):
            with self.assertRaises(Failure) as cm:
                store.uncommitted_removal('FOO')
            self.assertIn('did not answer', str(cm.exception))
            said = self.shown(store, ctx, 'FOO')
        self.assertIn("the store's history could not be read: git did not answer", said)
        self.assertNotIn('the id is unknown', said)

    def test_once_committed_it_names_the_closing_commit(self):
        r, root, store, ctx = self.main_store()
        store.write(lambda s: pmod.plan_close(s, ctx, 'FOO', 'the work is finished'))
        r.git('add', '-A', '--', 'docs/backlog', cwd=root)
        r.git('commit', '-q', '-m', 'closes FOO: the work is finished\n\nSession: wt-session',
              cwd=root)
        said = self.shown(store, ctx, 'FOO')
        self.assertIn('closed by %s ' % r.git('rev-parse', '--short', 'HEAD', cwd=root), said)
        self.assertNotIn('no commit names it yet', said)

    def test_an_id_never_tracked_is_still_unknown(self):
        r, root, store, ctx = self.main_store()
        self.assertIn('the id is unknown', self.shown(store, ctx, 'NOPE'))


class BackslashFolder(StoreCase):
    """A main store configured as `docs\\items` (a valid RelPath) is looked up as `docs/items`."""

    def test_a_backslash_folder_still_names_the_deleting_commit(self):
        r = TempRepo()
        self.addCleanup(r.cleanup)
        r.write('docs/items/FOO.md', record_text(id='FOO'))
        r.commit('adds FOO', 'docs/items/FOO.md')
        r.git('rm', '-q', '--', 'docs/items/FOO.md')
        r.git('commit', '-q', '-m', 'closes FOO: gone\n\nSession: bs-session')
        deleted = r.head()
        store = smod.Store(smod.MainBackend(r.root, 'docs\\items'), rmod.Rules())
        ctx = pmod.Context(store, r.root, branch=BRANCH)
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(store.load(), ctx, 'FOO')
        said = str(cm.exception)
        self.assertIn('closed by %s ' % r.git('rev-parse', '--short', deleted), said)
        self.assertIn('(session bs-session): closes FOO: gone', said)


class Cli(StoreCase):
    """tools/backlog.py `show` of a closed record prints the removal and exits 0; an unknown id
    is NOT FOUND, exit 1; an empty store's `next` and `list` exit 0."""

    def test_an_empty_store_next_and_list_exit_0(self):
        kit_json_ref(self.repo)
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        env = fixture_env({'USERPROFILE': home, 'HOME': home})
        for args in (('next',), ('list',), ('list', '--needs-priority')):
            p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                               capture_output=True, text=True, encoding='utf-8', env=env,
                               timeout=120)
            self.assertEqual(p.returncode, 0, (args, p.stdout + p.stderr))
            self.assertTrue(p.stdout.startswith('BACKLOG empty'), p.stdout)

    def test_show_of_a_closed_record_prints_the_removal_and_exits_0(self):
        kit_json_ref(self.repo)              # the tool writes the store it is told to
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        env = fixture_env({'USERPROFILE': home, 'HOME': home})

        def run(*args):
            p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                               capture_output=True, text=True, encoding='utf-8', env=env,
                               timeout=120)
            return p.returncode, p.stdout + p.stderr
        rc, out = run('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.assertEqual(rc, 0, out)
        rc, out = run('--session', 'cli-session', 'close', 'FOO', '--reason', 'it is all done')
        self.assertEqual(rc, 0, out)
        rc, out = run('show', 'FOO')
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r'^ENDED: FOO is no longer in ')
        self.assertIn('(session cli-session): closes FOO: it is all done', out)
        self.assertIn('\n  reason: it is all done', out)
        self.assertNotIn(NAME, out)
        rc, out = run('show', 'NOPE')
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r'^NOT FOUND: no record with `id: NOPE`')
