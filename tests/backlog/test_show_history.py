"""`show` of a missing id names the store commit that removed it (kitlib/backlog/plan.py
`require_shown`, store.py `last_removal`): sha, date, session and subject, never an author name;
and a record deleted on a merged side branch is still found (`--full-history`)."""
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import NotFound, Usage
from tests.backlog.helpers import BRANCH, StoreCase, rec, record_text
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
        self.assertIn('no record with `id: FOO`', said)
        self.assertIn('removed by %s on %s (session kit-c4-test): closes FOO: the work is '
                      'finished' % (short, when), said)
        self.assertNotIn(NAME, said)

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
        for path in ('backlog/FO*.md', 'backlog/F?O.md', 'backlog/[F]OO.md', 'backlog/../FOO.md'):
            with self.assertRaises(Usage, msg=path):
                smod.last_removal(self.repo.root, self.remote_tip(), path)


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
        ctx = pmod.Context(store, self.ledger, r.root, branch=BRANCH)
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(store.load(), ctx, 'FOO')
        said = str(cm.exception)
        self.assertIn('no record with `id: FOO`', said)
        self.assertIn("the store's history could not be read", said)


class MainStoreSideBranch(StoreCase):
    """The TOOLS-BACKLOG-SHOW-REMOVED-BY case: added and deleted on a side branch, then merged."""

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
        ctx = pmod.Context(store, self.ledger, r.root, branch=BRANCH)
        with self.assertRaises(NotFound) as cm:
            pmod.require_shown(store.load(), ctx, 'FOO')
        said = str(cm.exception)
        self.assertIn('removed by %s ' % r.git('rev-parse', '--short', deleted), said)
        self.assertIn('(session side-session): closes FOO: done on the side', said)
        self.assertNotIn(r.git('rev-parse', '--short', merge), said)


class Cli(StoreCase):
    """tools/backlog.py `show` prints the removal and still exits 1."""

    def test_show_of_a_closed_record_prints_the_removal_and_exits_1(self):
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        env = fixture_env({'USERPROFILE': home, 'HOME': home})

        def run(*args):
            p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                               capture_output=True, text=True, encoding='utf-8', env=env,
                               timeout=120)
            return p.returncode, p.stdout + p.stderr
        rc, out = run('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        self.assertEqual(rc, 0, out)
        rc, out = run('--session', 'cli-session', 'close', 'FOO', '--reason', 'it is all done')
        self.assertEqual(rc, 0, out)
        rc, out = run('show', 'FOO')
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r'^NOT FOUND: no record with `id: FOO`')
        self.assertIn('(session cli-session): closes FOO: it is all done', out)
        self.assertNotIn(NAME, out)
