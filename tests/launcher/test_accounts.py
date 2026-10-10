"""Accounts: the launch folder, the headroom refusal, pick-account, and the usage report pm_watch parses."""
import datetime
import os
import unittest

from kitlib import pmwatch
from kitlib.launcher import accounts as A
from kitlib.launcher import usage
from tests.launcher.fakes import NOW, Home

DAY = datetime.timedelta(days=1)


class LaunchDir(unittest.TestCase):
    def test_the_first_signed_in_folder(self):
        h = Home(self, accounts=('1',))
        second = os.path.join(h.root, 'acct1b')
        os.makedirs(second)
        h.obj['accounts'] = {'1': [os.path.join(h.root, 'nope'), h.accounts['1'][0], second]}
        s = type(h.settings)(h.obj, h.root)
        self.assertEqual(A.launch_dir(s, '1'), h.accounts['1'][0])

    def test_an_account_not_signed_in_is_refused(self):
        h = Home(self, accounts=('1', '2'), signed_out=('2',))
        with self.assertRaisesRegex(A.LaunchError, 'Account 2 is not signed in'):
            A.launch_dir(h.settings, '2')

    def test_an_org_needs_an_oauth_account_in_its_claude_json(self):
        h = Home(self)
        org = os.path.join(h.root, 'org1')
        os.makedirs(org)
        h.obj['orgs'] = {'1': org}
        s = type(h.settings)(h.obj, h.root)
        with self.assertRaisesRegex(A.LaunchError, 'Org 1 is not signed in'):
            A.org_launch_dir(s, '1')
        with open(os.path.join(org, '.claude.json'), 'w') as f:
            f.write('{"oauthAccount": {"x": 1}}')
        self.assertEqual(A.org_launch_dir(s, '1'), org)


class Headroom(unittest.TestCase):
    def test_refused_at_the_limit_unless_forced_and_a_reset_window_is_free(self):
        h = Home(self)
        h.usage('1', five=(90, NOW + DAY / 24), week=(20, NOW + DAY))
        out = []
        with self.assertRaisesRegex(A.LaunchError, "Account 1 is at 90% of its five_hour window.*PM's headroom"):
            A.check_headroom(h.settings, '1', NOW, False, out)
        A.check_headroom(h.settings, '1', NOW, True, out)
        self.assertIn('--force: starting on account 1 anyway', out[-1])
        h.usage('2', five=(99, NOW - DAY), week=(10, NOW + DAY))
        A.check_headroom(h.settings, '2', NOW, False, out)       # its five-hour window has reset

    def test_no_usage_file_is_said_not_refused(self):
        h = Home(self)
        out = []
        A.check_headroom(h.settings, '1', NOW, False, out)
        self.assertIn('no usage-status.json', out[0])


class Pick(unittest.TestCase):
    def test_the_soonest_week_reset_under_the_limit(self):
        h = Home(self, accounts=('1', '2', '3'))
        h.usage('1', week=(50, NOW + 3 * DAY))
        h.usage('2', week=(96, NOW + DAY))           # over 95: passed over
        h.usage('3', week=(10, NOW + 2 * DAY))
        n, rows = A.pick(h.settings, NOW)
        self.assertEqual(n, '3')
        self.assertEqual([r[:2] for r in rows], [('1', 50), ('2', 96), ('3', 10)])
        self.assertEqual(len(A.explain(rows)), 3)
        self.assertTrue(A.explain(rows)[0].startswith('account 1: 50% of week, resets '))

    def test_a_passed_reset_is_fresh_and_moves_a_week_on(self):
        h = Home(self, accounts=('1',))
        h.usage('1', week=(80, NOW - DAY))
        (n, pct, reset), = A.week_rows(h.settings, NOW)
        self.assertEqual((pct, reset), (0, NOW + 6 * DAY))

    def test_every_account_over_still_picks_the_soonest_and_signed_out_ones_are_skipped(self):
        h = Home(self, accounts=('1', '2', '3'), signed_out=('3',))
        h.usage('1', week=(99, NOW + 2 * DAY))
        h.usage('2', week=(97, NOW + DAY))
        self.assertEqual(A.pick(h.settings, NOW)[0], '2')
        self.assertEqual([r[0] for r in A.pick(h.settings, NOW)[1]], ['1', '2'])

    def test_no_signed_in_account_is_an_error(self):
        h = Home(self, accounts=('1',), signed_out=('1',))
        with self.assertRaisesRegex(A.LaunchError, 'No account in launcher.accounts is signed in'):
            A.pick(h.settings, NOW)


class Usage(unittest.TestCase):
    def test_the_lines_pm_watch_parses(self):
        h = Home(self, accounts=('1', '2', '3'))
        h.usage('1', five=(12.0, NOW + DAY / 24), week=(51, NOW + DAY), updated=NOW - datetime.timedelta(minutes=3))
        h.usage('2', five=(70, NOW - DAY), week=(30, NOW + DAY), updated=NOW - DAY)
        lines = usage.report(h.settings, NOW)
        self.assertTrue(lines[0].startswith('account 1: five_hour=12% (resets '))
        self.assertTrue(lines[0].endswith('[updated 3 min ago]'))
        self.assertIn('five_hour=reset', lines[1])
        self.assertEqual(lines[2], 'account 3: no usage file')
        self.assertEqual(lines[3], 'current account: 1')
        self.assertEqual(lines[4], 'account codex: no usage data')
        self.assertEqual(pmwatch.parse_usage('\n'.join(lines)),
                         {'1': {'five': 12, 'week': 51}, '2': {'five': 0, 'week': 30}})


class ParseTime(unittest.TestCase):
    def test_powershell_round_trip_times_with_seven_fraction_digits(self):
        want = datetime.datetime(2026, 10, 10, 14, 21, 2, 123456, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
        self.assertEqual(A.parse_time('2026-10-10T14:21:02.1234567+02:00'), want)
        self.assertEqual(A.parse_time('2026-10-10T12:21:02.1234567Z'), want)
        self.assertEqual(A.parse_time('2026-10-10T12:21:02.123Z'), want.replace(microsecond=123000))
        for bad in ('', None, 'soon', 5):
            self.assertIsNone(A.parse_time(bad))


if __name__ == '__main__':
    unittest.main()
