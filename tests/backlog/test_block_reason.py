"""`block --reason` (docs/backlog.md, verb table): the reason goes into the commit subject and onto
one body line, `Blocked on <members> (<date>): <reason>`, so `show` says why; no header key."""
import unittest

from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Usage

from tests.backlog import test_cli
from tests.backlog.helpers import StoreCase, TODAY, rec

WHY = 'waiting for the live client to ship its next build'


class BlockReason(StoreCase):
    remote = False

    def test_the_reason_lands_in_subject_and_body(self):
        self.seed(rec('FOO', body='Existing notes.'))
        change = self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assertEqual(change.subject, 'backlog: block FOO on live-client: %s' % WHY)
        r = self.get('FOO')
        self.assertEqual(r.all('blocked_on'), ['live-client'])
        self.assertIn('Existing notes.', r.body)
        self.assertTrue(r.body.rstrip('\n').endswith(
            'Blocked on live-client (%s): %s' % (TODAY, WHY)), r.body)
        self.assertNotIn('block_reason', dict(r.pairs()))
        self.assertIn('  reason (in the body): %s' % WHY, change.lines)

    def test_a_record_with_no_body_gets_just_the_line(self):
        self.seed(rec('FOO'))
        self.verb('block', 'FOO', ['live-client'], reason='  waiting   on the client build  ')
        self.assertEqual(self.get('FOO').body.strip(),
                         'Blocked on live-client (%s): waiting on the client build' % TODAY)

    def test_a_short_reason_is_usage_and_writes_nothing(self):
        self.seed(rec('FOO'))
        tip = self.remote_tip()
        with self.assertRaises(Usage):
            self.verb('block', 'FOO', ['live-client'], reason='later')
        self.assertEqual(self.remote_tip(), tip)

    def test_no_reason_is_unchanged(self):
        self.seed(rec('FOO', body='Existing notes.'))
        change = self.verb('block', 'FOO', ['live-client'])
        self.assertEqual(change.subject, 'backlog: block FOO on live-client')
        self.assertEqual(self.get('FOO').body.strip(), 'Existing notes.')

    def test_the_header_keys_are_unchanged(self):
        self.assertNotIn('block_reason', rmod.FIELDS)


class BlockReasonCli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_show_prints_the_reason(self):
        self.ok('add', 'FOO', '--summary', 'a thing to hold for now', '--scope', 'a/foo.py')
        self.ok('block', 'FOO', '--on', 'live-client', '--reason', WHY)
        self.assertIn('Blocked on live-client (', self.ok('show', 'FOO'))
        self.assertIn(WHY, self.ok('show', 'FOO'))
        log = self.repo.git('log', '-1', '--format=%s', smod.STORE_REF)
        self.assertEqual(log, 'backlog: block FOO on live-client: %s' % WHY)


if __name__ == '__main__':
    unittest.main()
