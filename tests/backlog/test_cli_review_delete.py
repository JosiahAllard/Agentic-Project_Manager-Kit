"""tools/backlog.py's `clear-review` and `delete` and how `list`, `next` and `pull` show a record
marked for review, run as a subprocess (tests/backlog/test_cli.py's Cli fixture). The mark is
written through the library, as the Action Hub writes it: the CLI's `add` has no `--review`."""
import os
import unittest
from unittest import mock

from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import store as smod
from tests.backlog import test_cli
from tests.backlog.helpers import ADD_FLAGS, ADD_NONE

MARK = 'owner-hub 2026-10-15'


class Cli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def add_marked(self, rid):
        """`add` with the review mark, through the library against the configured store."""
        with mock.patch.dict(os.environ, self.env, clear=True):
            ctx = opener.open_store(self.repo.root, session='owner-hub')
            ctx.store.write(lambda s: pmod.plan_add(s, ctx, rid, 'a thing the owner added',
                                                    ['tools/x.py'], session=None, review=MARK,
                                                    **ADD_NONE))

    def test_the_cli_has_no_review_flag_on_add(self):
        rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py',
                                '--review', MARK, *ADD_FLAGS)
        self.assertEqual(rc, 2, out)
        self.assertIn('--review', out)

    def test_a_marked_record_through_list_next_pull_and_clear_review(self):
        self.add_marked('FOO')
        self.ok('set', 'FOO', '--priority', '2', '--priority-by', 'agent', '--effort', 's')
        self.assertIn('[review %s]' % MARK, self.ok('list'))
        self.repo.git('checkout', '-b', 'tools/wk')
        out = self.ok('next')
        self.assertIn('1 to review', out)
        rc, out = self.run_tool('pull', 'FOO')
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^REFUSED: ')
        self.assertIn('clear-review FOO', out)
        rc, out = self.run_tool('clear-review', 'FOO')
        self.assertEqual(rc, 2, out)                     # --reason is required
        out = self.ok('clear-review', 'FOO', '--reason', 'the coordinator read it')
        self.assertIn('CLEARED the review mark on FOO (was: %s)' % MARK, out)
        log = self.repo.git('log', '--format=%s', smod.STORE_REF)
        self.assertEqual(log.split('\n')[0], 'backlog: clear review FOO: the coordinator read it')
        self.assertNotIn('review', self.ok('show', 'FOO'))
        self.assertIn('pulled_by:  tools/wk', self.ok('pull', 'FOO'))

    def test_an_unpriced_marked_record_shows_in_needs_priority(self):
        self.add_marked('FOO')
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'docs/y.md', *ADD_FLAGS)
        out = self.ok('list', '--needs-priority')
        self.assertIn('FOO', out)
        self.assertIn('BAR', out)
        self.assertIn('2 record(s)', out)
        self.ok('set', 'FOO', '--priority', '2', '--priority-by', 'agent')
        out = self.ok('list', '--needs-priority')
        self.assertNotIn('FOO', out)
        self.assertIn('1 record(s)', out)

    def test_delete_round_trip_and_its_refusals(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        rc, out = self.run_tool('delete', 'FOO')
        self.assertEqual(rc, 2, out)                     # --why is required
        for flag in ('--owner-said', '--ruling'):
            rc, out = self.run_tool('delete', 'FOO', '--why', 'the owner said', flag, 'x')
            self.assertEqual(rc, 2, out)
            self.assertIn('USAGE: unrecognized arguments: %s' % flag, out)
        out = self.ok('delete', 'FOO', '--why', 'the owner says this should not exist')
        self.assertIn('DELETED FOO', out)
        log = self.repo.git('log', '--format=%s', smod.STORE_REF)
        self.assertEqual(log.split('\n')[0], 'deletes FOO: the owner says this should not exist')
        rc, out = self.run_tool('show', 'FOO')
        self.assertEqual(rc, 0, out)
        self.assertIn('deletes FOO: the owner says this should not exist', out)
        rc, out = self.run_tool('delete', 'FOO', '--why', 'the owner says this should not exist')
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r'^NOT FOUND: ')


if __name__ == '__main__':
    unittest.main()
