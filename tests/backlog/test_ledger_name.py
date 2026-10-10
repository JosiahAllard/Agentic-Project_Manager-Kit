"""kitlib.backlog.ledger's name for this repository's rulings-ledger column: `backlog.ledger_repo`,
else `repo.name`, lower-cased; neither set is '' (never the folder name), and a ledger built with
no name reports one config error naming both keys and the file instead of checking any ruling."""
import os
import tempfile
import unittest

from kitlib.backlog import check as cmod
from kitlib.backlog import ledger as lmod
from tests.gitfixture import rmtree

LEDGER = '# Rulings ledger (test fixture)\n\n## R-9168 | 2026-09-25 08:08Z | pm | typed\n\ntext\nItems: FOO\n'


class FakeConfig(object):
    def __init__(self, ledger_repo=None, name=None, where='Z:/x/.kit.json'):
        self._backlog = {'ledger_repo': ledger_repo}
        self.repo = {} if name is None else {'name': name}
        self.where = where

    def section(self, name):
        assert name == 'backlog', name
        return dict(self._backlog)


class FakeSnap(object):
    readable = ()
    file_count = 3


class LedgerName(unittest.TestCase):

    def test_ledger_repo_wins_over_the_repo_name(self):
        self.assertEqual(lmod.ledger_name(FakeConfig('PM', 'project-manager')), 'pm')

    def test_the_repo_name_when_ledger_repo_is_unset(self):
        self.assertEqual(lmod.ledger_name(FakeConfig(None, 'Kit')), 'kit')

    def test_neither_set_is_empty(self):
        self.assertEqual(lmod.ledger_name(FakeConfig()), '')

    def test_for_repo_carries_the_name_and_where_the_config_came_from(self):
        named = lmod.Ledger.for_repo('p', FakeConfig('pm'))
        self.assertEqual(named.repos, ('pm',))
        self.assertIsNone(named.name_error)
        unnamed = lmod.Ledger.for_repo('p', FakeConfig())
        self.assertEqual(unnamed.repos, ())
        for part in ('`backlog.ledger_repo`', '`repo.name`', 'Z:/x/.kit.json'):
            self.assertIn(part, unnamed.name_error)


class Unnamed(unittest.TestCase):

    def setUp(self):
        d = tempfile.mkdtemp(prefix='kit-ledger-')
        self.addCleanup(rmtree, d)
        self.path = os.path.join(d, 'ledger.md')
        with open(self.path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(LEDGER)
        self.ledger = lmod.Ledger.for_repo(self.path, FakeConfig())

    def test_ruling_refusal_names_the_keys_not_an_empty_column(self):
        entries, _why = self.ledger.entries()
        bad = lmod.ruling_refusal(self.ledger, entries, 'R-9168', lmod.OWNER_KINDS)
        self.assertEqual(bad, self.ledger.name_error)

    def test_the_ruling_checks_are_red_with_no_ruled_record(self):
        # an unconfigured name never passes the ruling checks, even with nothing yet to check
        res = cmod._check_rulings(FakeSnap(), self.ledger, cmod.HOLD_RULINGS, 'hold_ruling',
                                  lmod.HOLD_KINDS)
        self.assertTrue(res.red)
        self.assertEqual(res.findings, [])
        self.assertIn('CONFIG ERROR', res.line())
        self.assertIn('`backlog.ledger_repo`', res.line())

    def test_an_unconfigured_ledger_still_skips_before_the_name_is_read(self):
        ledger = lmod.Ledger.for_repo(None, FakeConfig())
        res = cmod._check_rulings(FakeSnap(), ledger, cmod.HOLD_RULINGS, 'hold_ruling',
                                  lmod.HOLD_KINDS)
        self.assertFalse(res.red)
        self.assertIn('SKIPPED', res.line())


if __name__ == '__main__':
    unittest.main()
