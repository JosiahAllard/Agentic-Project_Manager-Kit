"""The machine config's `ledger_dir`: one ledger file per name in `ledger_repos`, in that folder.
`ledger_repos` is required with it, and a name whose file would collide is refused."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import config
from kitlib.config import schema, user


class LedgerDirTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dir = os.path.join(self.home, 'rulings')

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            f.write(json.dumps(body))

    def refused(self, body):
        self.write(body)
        with self.assertRaises(config.ConfigError) as cm:
            user.user_config(self.home)
        return str(cm.exception)

    def test_unset_means_no_folder_and_no_files(self):
        self.assertIsNone(user.ledger_dir(self.home))
        self.assertEqual(user.ledger_files(self.home), {})
        self.write({'ledger_path': os.path.join(self.home, 'LEDGER.md'), 'ledger_repos': ['alpha']})
        self.assertIsNone(config.ledger_dir(self.home))
        self.assertEqual(config.ledger_files(self.home), {})

    def test_one_file_per_repo_in_the_listed_order(self):
        self.write({'ledger_dir': self.dir, 'ledger_repos': ['kit', 'beta-2', 'pm']})
        self.assertEqual(user.ledger_dir(self.home), self.dir)
        got = config.ledger_files(self.home)
        self.assertEqual(list(got), ['kit', 'beta-2', 'pm'])
        self.assertEqual(got['beta-2'], os.path.join(self.dir, 'beta-2.md'))

    def test_files_of_reads_a_config_already_taken(self):
        self.write({'ledger_dir': self.dir, 'ledger_repos': ['kit']})
        cfg = user.user_config(self.home)
        self.assertEqual(user.files_of(cfg), {'kit': os.path.join(self.dir, 'kit.md')})
        self.assertEqual(user.files_of({'ledger_dir': None, 'ledger_repos': ['kit']}), {})

    def test_the_frozen_file_stays_readable_beside_the_folder(self):
        frozen = os.path.join(self.dir, 'LEDGER.md')
        self.write({'ledger_dir': self.dir, 'ledger_repos': ['kit'], 'ledger_path': frozen})
        self.assertEqual(user.ledger_path(self.home), frozen)

    def test_ledger_repos_is_required_with_it(self):
        for body in ({'ledger_dir': self.dir}, {'ledger_dir': self.dir, 'ledger_repos': []},
                     {'ledger_dir': self.dir, 'ledger_repos': None}):
            with self.subTest(body=body):
                self.assertIn('ledger_dir needs ledger_repos', self.refused(body))

    def test_a_relative_folder_refuses(self):
        self.assertIn('must be an absolute path', self.refused({'ledger_dir': 'rulings', 'ledger_repos': ['kit']}))

    def test_a_name_whose_file_would_collide_refuses(self):
        frozen = os.path.join(self.dir, 'LEDGER.md')
        for repos, extra, why in ((['kit', 'kit'], {}, "'kit' twice"),
                                  (['all'], {}, "'all'"),
                                  (['standing'], {}, "'standing'"),
                                  (['ledger'], {'ledger_path': frozen}, "'ledger'")):
            with self.subTest(repos=repos):
                got = self.refused(dict({'ledger_dir': self.dir, 'ledger_repos': repos}, **extra))
                self.assertIn('ledger_repos', got)
                self.assertIn(why, got)

    def test_without_the_folder_the_names_are_checked_as_before(self):
        # the one-file ledger: `all` and a repeated name were never refused there
        self.write({'ledger_repos': ['kit', 'kit', 'all', 'standing']})
        self.assertEqual(user.ledger_repos(self.home), ['kit', 'kit', 'all', 'standing'])

    def test_failing_keys_names_ledger_repos_and_never_a_value(self):
        self.assertEqual(user.failing_keys({'ledger_dir': self.dir}), ['ledger_repos'])
        self.assertEqual(user.failing_keys({'ledger_dir': self.dir, 'ledger_repos': ['all']}), ['ledger_repos'])
        self.assertEqual(user.failing_keys({'ledger_dir': self.dir, 'ledger_repos': ['kit']}), [])
        self.assertEqual(user.failing_keys({'ledger_dir': 'rel', 'ledger_repos': ['kit']}), ['ledger_dir'])

    def test_it_is_a_machine_key_only(self):
        self.assertIn('ledger_dir', user.SCHEMA)
        self.assertFalse(any('ledger_dir' in fields for fields in schema.SECTIONS.values()))
        self.assertTrue([line for line in user.doc_lines() if line.startswith('- `ledger_dir` ')])


if __name__ == '__main__':
    unittest.main()
