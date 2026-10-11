"""kitlib.launcher.accounts.current_account: which launcher account this session runs on."""
import os
import unittest

from kitlib.launcher import accounts

HOME = os.path.join('C:' + os.sep, 'Users', 'someone')


def machine(accts):
    return {'launcher': {'accounts': accts}}


class CurrentAccountTest(unittest.TestCase):

    def label(self, cfg, env):
        return accounts.current_account(cfg, environ=env, home=HOME)

    def test_the_account_listing_the_config_folder_is_current(self):
        cfg = machine({'1': ['~/.claude-one', '~/.claude'], '2': ['~/.claude-two']})
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': os.path.join(HOME, '.claude-two')}), '2')
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': os.path.join(HOME, '.claude-one')}), '1')

    def test_unset_or_empty_config_dir_is_the_account_listing_the_default_folder(self):
        cfg = machine({'1': ['~/.claude-one'], '2': ['~/.claude-two', '~/.claude']})
        self.assertEqual(self.label(cfg, {}), '2')
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': ''}), '2')
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': '  '}), '2')

    @unittest.skipUnless(os.name == 'nt', 'case and slash folding is Windows path behaviour')
    def test_tilde_case_and_slashes_fold_on_windows(self):
        cfg = machine({'3': ['~\\.Claude-Three\\'], '4': [HOME.upper() + '/.claude-four']})
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': HOME.lower() + '/.claude-three'}), '3')
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': HOME + '\\.CLAUDE-FOUR\\'}), '4')
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': '~/.claude-three'}), '3')

    def test_no_match_is_default(self):
        cfg = machine({'1': ['~/.claude-one']})
        self.assertEqual(self.label(cfg, {'CLAUDE_CONFIG_DIR': os.path.join(HOME, 'elsewhere')}), 'default')
        self.assertEqual(self.label(cfg, {}), 'default')    # ~/.claude listed by no account

    def test_no_launcher_accounts_is_default_a_fresh_user(self):
        for cfg in ({}, None, {'launcher': {}}, {'launcher': {'accounts': {}}}, {'launcher': 'x'},
                    {'launcher': {'accounts': ['~/.claude']}}):
            self.assertEqual(self.label(cfg, {}), accounts.DEFAULT_LABEL, msg=repr(cfg))

    def test_two_accounts_listing_one_folder_the_lower_number_wins(self):
        cfg = machine({'10': ['~/.claude'], 'work': ['~/.claude'], '2': ['~/.claude']})
        self.assertEqual(self.label(cfg, {}), '2')          # not "10": numbers compare as numbers

    def test_a_malformed_folder_list_is_skipped_not_raised(self):
        cfg = machine({'1': '~/.claude', '2': [None, 5, '', '~/.claude']})
        self.assertEqual(self.label(cfg, {}), '2')


if __name__ == '__main__':
    unittest.main()
