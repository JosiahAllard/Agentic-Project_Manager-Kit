"""start --session KEY --account current|auto (kitlib/launcher/start.py start_entry,
kitlib/launcher/accounts.py resolve_current): the PM starts a coordinator on the account it is on."""
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from kitlib.launcher import accounts, start
from kitlib.launcher import settings as S
from tests.home import machine_config
from tests.launcher.fakes import NOW, FakeHost, Home
from tools import launch

ENTRY = {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord', 'state': 's.md', 'account': 1,
         'name': 'alpha-coord-a1'}


class Current(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, accounts=('1', '2', '10'), signed_out=('1',), repos=('alpha',))
        self.h.write_state('sessions.json', {'kit': dict(ENTRY)})

    def host(self, folder=None):
        env = {'CLAUDE_CONFIG_DIR': folder} if folder else {}
        return FakeHost(self.h.root, env=env)

    def go(self, host, account='current'):
        return start.start_entry(self.h.settings, host, 'kit', account=account, whatif=True)

    def test_the_account_the_process_runs_on_is_used_not_the_entrys(self):
        host = self.host(self.h.accounts['2'][0])
        out = '\n'.join(self.go(host))
        self.assertIn("-n 'alpha-coord-a2'", out)          # the entry's own account is 1
        self.assertIn(self.h.accounts['2'][0], out)
        self.assertNotIn('not identified', out)

    def test_numbered_accounts_sort_by_number(self):
        shared = self.h.accounts['2'][0]
        accts = dict(self.h.accounts, **{'10': [shared]})
        settings = S.Settings(dict(self.h.obj, accounts=accts), self.h.root)
        label, note = accounts.resolve_current(settings, NOW, {'CLAUDE_CONFIG_DIR': shared})
        self.assertEqual((label, note), ('2', None))        # "10" sorts before "2" as text

    def test_an_unidentified_account_falls_back_to_pick(self):
        out = self.go(self.host(os.path.join(self.h.root, 'elsewhere')))
        self.assertEqual(out[0], 'current account not identified; picked 2')    # 1 is signed out
        self.assertIn("-n 'alpha-coord-a2'", '\n'.join(out))

    def test_nothing_signed_in_is_refused(self):
        h = Home(self, accounts=('1',), signed_out=('1',), repos=('alpha',))
        h.write_state('sessions.json', {'kit': dict(ENTRY)})
        with self.assertRaisesRegex(S.LaunchError, 'No account in launcher.accounts is signed in'):
            start.start_entry(h.settings, FakeHost(h.root), 'kit', account='current', whatif=True)

    def test_auto_picks(self):
        out = self.go(self.host(), 'auto')
        self.assertEqual(out[0], 'auto picked account 2')

    def test_the_cli_takes_current(self):
        machine_config(self.h.root, {'launcher': self.h.obj}, case=self)
        host = self.host(self.h.accounts['10'][0])
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = launch.main(['start', '--session', 'kit', '--account', 'current', '--what-if'], host)
        self.assertEqual(code, 0)
        self.assertIn("-n 'alpha-coord-a10'", out.getvalue())


if __name__ == '__main__':
    unittest.main()
