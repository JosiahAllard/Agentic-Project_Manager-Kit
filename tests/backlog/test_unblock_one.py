"""`unblock` clears one named blocker (docs/backlog.md, verb table): a record with one blocker
unblocks as before; one with several needs `--on <member>` and `--reason`, and a bare `unblock`
refuses with the blockers listed and the command to run. The `check` hint for a dangling
`item:` blocker names the same `--on` form."""
import unittest

from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal, Usage

from tests.backlog import test_cli
from tests.backlog.helpers import LEDGER_TEXT, StoreCase, parse, rec

REASON = 'the blocker landed on main today'
SEVERAL = dict(state='blocked', blocked_on=['item:BAR', 'live-client'])


class UnblockOne(StoreCase):
    remote = False
    ledger_text = LEDGER_TEXT

    def test_single_blocker_bare_unblock_opens_as_before(self):
        self.seed(rec('FOO', state='blocked', blocked_on='live-client'), rec('BAR'))
        change = self.verb('unblock', 'FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('open', ['-']))
        self.assertEqual(change.subject, 'backlog: unblock FOO')

    def test_several_blockers_bare_unblock_refuses_and_lists_them(self):
        self.seed(rec('FOO', **SEVERAL), rec('BAR'))
        with self.assertRaises(Refusal) as cm:
            self.verb('unblock', 'FOO')
        msg = str(cm.exception)
        for m in ('item:BAR', 'live-client'):
            self.assertIn('python tools/backlog.py unblock FOO --on %s --reason "' % m, msg)
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('blocked', ['item:BAR', 'live-client']))

    def test_on_removes_only_the_named_blocker(self):
        self.seed(rec('FOO', **SEVERAL), rec('BAR'))
        change = self.verb('unblock', 'FOO', on='item:BAR', reason=REASON)
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('blocked', ['live-client']))
        self.assertIn('UNBLOCKED FOO - `item:BAR` removed -> live-client', change.lines)

    def test_on_last_blocker_opens(self):
        self.seed(rec('FOO', state='blocked', blocked_on='live-client'))
        self.verb('unblock', 'FOO', on='live-client')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('open', ['-']))

    def test_several_blockers_need_a_reason(self):
        self.seed(rec('FOO', **SEVERAL), rec('BAR'))
        for reason in (None, 'too short', '      '):
            with self.assertRaises(Usage, msg=reason):
                self.verb('unblock', 'FOO', on='item:BAR', reason=reason)
        self.assertEqual(self.get('FOO').all('blocked_on'), ['item:BAR', 'live-client'])

    def test_reason_lands_in_the_commit_subject(self):
        self.seed(rec('FOO', **SEVERAL), rec('BAR'))
        change = self.verb('unblock', 'FOO', on='live-client', reason=REASON)
        self.assertEqual(change.subject, 'backlog: unblock FOO from live-client: %s' % REASON)

    def test_on_a_member_not_blocking_is_usage(self):
        self.seed(rec('FOO', **SEVERAL), rec('BAR'))
        with self.assertRaises(Usage) as cm:
            self.verb('unblock', 'FOO', on='deliberate', reason=REASON)
        self.assertIn('item:BAR, live-client', str(cm.exception))

    def test_removing_owner_from_several_needs_ruling_and_drops_owner_ask(self):
        self.seed(rec('FOO', state='blocked', blocked_on=['owner', 'live-client'],
                      owner_ask='which one?'))
        with self.assertRaises(Refusal):
            self.verb('unblock', 'FOO', on='owner', reason=REASON)
        self.verb('unblock', 'FOO', on='owner', reason=REASON, ruling='R-9001')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on'), r.get('owner_ask'), r.get('hold_ruling')),
                         ('blocked', ['live-client'], None, 'R-9001'))

    def test_ruling_while_owner_stays_is_usage(self):
        self.seed(rec('FOO', state='blocked', blocked_on=['owner', 'live-client'],
                      owner_ask='which one?'))
        with self.assertRaises(Usage):
            self.verb('unblock', 'FOO', on='live-client', reason=REASON, ruling='R-9001')
        self.verb('unblock', 'FOO', on='live-client', reason=REASON)
        r = self.get('FOO')
        self.assertEqual((r.all('blocked_on'), r.get('owner_ask')), (['owner'], 'which one?'))


class DanglingHint(unittest.TestCase):

    def test_check_hint_names_the_on_form(self):
        r = parse(stem='FOO', id='FOO', state='blocked', blocked_on=['item:GONE', 'live-client'])
        found = smod.dangling_items({'FOO': r})
        self.assertEqual(len(found), 1, found)
        self.assertIn('`unblock FOO --on item:GONE --reason "<why>"`', found[0].message)


class UnblockCli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_cli_exit_codes(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'tools/y.py')
        self.ok('block', 'FOO', '--on', 'item:BAR', '--on', 'live-client')
        rc, out = self.run_tool('unblock', 'FOO')
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^REFUSED: ')
        out = self.ok('unblock', 'FOO', '--on', 'item:BAR', '--reason', REASON)
        self.assertIn('`item:BAR` removed -> live-client', out)


if __name__ == '__main__':
    unittest.main()
