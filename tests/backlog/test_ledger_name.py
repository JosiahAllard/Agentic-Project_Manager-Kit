"""kitlib.backlog.ledger's name for this repository's rulings-ledger column: `backlog.ledger_repo`,
else `repo.name`, lower-cased; neither set is '' (never the folder name), and a ledger built with
no name reports one config error naming both keys and the file."""
import os
import tempfile
import unittest

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


class LedgerName(unittest.TestCase):

    def test_ledger_repo_wins_over_the_repo_name(self):
        self.assertEqual(lmod.ledger_name(FakeConfig('PM', 'some-folder')), 'pm')

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


if __name__ == '__main__':
    unittest.main()
