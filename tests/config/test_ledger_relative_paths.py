"""A relative `ledger_dir` or `ledger_path` in the PM repository's config.json resolves against the
PM repository root and passes the machine config schema; the docs (docs/kit-json.md, docs/ledger.md)
say these keys may be relative there. Every home and PM repository is a temp folder under the
fence (tests.home)."""
import json
import os
import unittest

from kitlib.config import user
from tests.home import pm_repo, temp_home


class LedgerRelativePathsTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        self.pm = pm_repo(self)
        self.policy = os.path.join(self.pm, user.DIR_NAME, user.FILE_NAME)

    def write(self, body):
        os.makedirs(os.path.dirname(self.policy), exist_ok=True)
        with open(self.policy, 'w', encoding='utf-8') as f:
            f.write(json.dumps(body))

    def test_relative_ledger_keys_resolve_under_the_pm_repo(self):
        self.write({'ledger_dir': 'rulings', 'ledger_repos': ['pm'], 'ledger_path': 'rulings/LEDGER.md'})
        cfg = user.user_config(self.home)          # raises ConfigError on any validation error
        self.assertEqual(cfg['ledger_dir'], os.path.normpath(os.path.join(self.pm, 'rulings')))
        self.assertEqual(cfg['ledger_path'], os.path.normpath(os.path.join(self.pm, 'rulings', 'LEDGER.md')))
        for key in ('ledger_dir', 'ledger_path'):
            self.assertTrue(os.path.isabs(cfg[key]), key)

    def test_an_absolute_value_is_kept(self):
        absolute = os.path.join(self.pm, 'elsewhere')
        self.write({'ledger_dir': absolute, 'ledger_repos': ['pm']})
        self.assertEqual(user.user_config(self.home)['ledger_dir'], absolute)


if __name__ == '__main__':
    unittest.main()
