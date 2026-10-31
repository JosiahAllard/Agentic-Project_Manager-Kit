"""The `launcher` config object: checks, defaults, ~ expansion, and loading from the machine config."""
import os
import shutil
import tempfile
import unittest

from kitlib.config.types import ConfigError
from kitlib.launcher import settings as S
from kitlib.lockmcp import PLUGIN_NAME
from tests.home import machine_config


class Check(unittest.TestCase):
    def test_unset_is_good(self):
        self.assertEqual(S.check(None), [])
        self.assertEqual(S.check({}), [])

    def test_every_bad_key_is_named(self):
        errs = S.check({'state_dir': 'relative', 'repos': {'a': 'rel'}, 'accounts': {'x': []},
                        'orgs': {'1': 'rel'}, 'coordinator_agent': 'a b', 'extra': 1})
        text = '\n'.join(errs)
        for want in ('launcher.extra is not a launcher key', 'launcher.state_dir must be an absolute path',
                     'launcher.repos.a must be an absolute path', "launcher.accounts key 'x'",
                     'launcher.accounts.x must be a non-empty list', 'launcher.orgs.1 must be absolute',
                     'launcher.coordinator_agent must be an agent name'):
            self.assertIn(want, text)

    def test_the_coordinator_agent_may_carry_the_plugin_prefix_and_no_other(self):
        p = PLUGIN_NAME + ':'
        for good in ('coordinator', p + 'coordinator', p + 'lead_2'):
            with self.subTest(good):
                self.assertEqual(S.check({'coordinator_agent': good}), [])
                self.assertEqual(S.Settings({'coordinator_agent': good}, '/h').coordinator_agent, good)
        for bad in ('other:coordinator', 'a:b:c', ':coordinator', p, p + p + 'coordinator', p.upper() + 'x'):
            with self.subTest(bad):
                self.assertIn('launcher.coordinator_agent must be an agent name',
                              '\n'.join(S.check({'coordinator_agent': bad})))

    def test_pm_resume_beside_desktop_is_a_bool_key(self):
        self.assertEqual(S.check({'pm_resume_beside_desktop': True}), [])
        self.assertEqual(S.check({'pm_resume_beside_desktop': False}), [])
        for bad in ('true', 1, 0, []):
            self.assertIn('launcher.pm_resume_beside_desktop must be true or false',
                          '\n'.join(S.check({'pm_resume_beside_desktop': bad})))
        self.assertTrue(S.Settings({'pm_resume_beside_desktop': True}, '/h').pm_resume_beside_desktop)
        self.assertFalse(S.Settings({}, '/h').pm_resume_beside_desktop)

    def test_usage_crossover_takes_its_three_values_and_defaults_to_notify(self):
        for good in ('notify', 'normal', 'full'):
            self.assertEqual(S.check({'usage_crossover': good}), [])
            self.assertEqual(S.Settings({'usage_crossover': good}, '/h').usage_crossover, good)
        for bad in ('on', '', True, 1, []):
            self.assertIn('launcher.usage_crossover must be one of notify, normal, full',
                          '\n'.join(S.check({'usage_crossover': bad})), bad)
        self.assertEqual(S.Settings({}, '/h').usage_crossover, 'notify')

    def test_a_bad_object_raises_a_config_error(self):
        with self.assertRaises(ConfigError):
            S.Settings({'accounts': {}}, '/h')
        with self.assertRaises(ConfigError):
            S.Settings([], '/h')


class Defaults(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-launch-')
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_unset_gives_one_account_in_the_default_folder_and_the_coordinator_agent(self):
        s = S.Settings(None, self.home)
        self.assertEqual(s.account_numbers(), ['1'])
        self.assertEqual(s.account_dirs('1'), [os.path.join(self.home, '.claude')])
        self.assertTrue(s.is_default(s.account_dirs('1')[0]))
        self.assertEqual(s.coordinator_agent, 'coordinator')

    def test_tilde_folders_expand_under_home_and_unknown_names_say_which_key(self):
        s = S.Settings({'accounts': {'2': ['~/acct2', '~\\acct2b'], '10': ['~']}, 'orgs': {'1': '~/org1'}},
                       self.home)
        self.assertEqual(s.account_numbers(), ['2', '10'])
        self.assertEqual(s.account_dirs(2), [os.path.join(self.home, 'acct2'), os.path.join(self.home, 'acct2b')])
        self.assertEqual(s.account_dirs('10'), [self.home])
        self.assertEqual(s.org_dir('1'), os.path.join(self.home, 'org1'))
        with self.assertRaisesRegex(S.LaunchError, 'account 3 is not in launcher.accounts'):
            s.account_dirs('3')
        with self.assertRaisesRegex(S.LaunchError, 'org 2 is not in launcher.orgs'):
            s.org_dir('2')
        with self.assertRaisesRegex(S.LaunchError, 'launcher.state_dir is not set'):
            s.state_dir()
        with self.assertRaisesRegex(S.LaunchError, "repo 'x' is not in launcher.repos"):
            s.repo_path('x')

    def test_load_reads_the_launcher_object_of_the_machine_config(self):
        state = os.path.join(self.home, 'state')
        repo = os.path.join(self.home, 'r')
        machine_config(self.home, {'launcher': {'state_dir': state, 'repos': {'r': repo},
                                                'coordinator_agent': 'conductor'}}, case=self)
        s = S.load(self.home)
        self.assertEqual(s.state_dir(), state)
        self.assertEqual(s.repo_path('r'), repo)
        self.assertEqual(s.coordinator_agent, 'conductor')

    def test_load_without_a_config_uses_the_defaults(self):
        s = S.load(self.home)
        self.assertEqual(s.account_numbers(), ['1'])


if __name__ == '__main__':
    unittest.main()
