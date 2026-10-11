"""kitlib.hubsite.settings and the machine config's `hub` key. Every machine config here is a
file in a temp home; the real one is never read."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import config
from kitlib.config import user
from kitlib.hubsite import settings


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-hub-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.data = os.path.join(self.home, 'data')

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            f.write(json.dumps(body))

    def refused(self, hub, why):
        self.write({'hub': hub})
        with self.assertRaises(config.ConfigError, msg=repr(hub)) as cm:
            settings.load(home=self.home)
        self.assertIn(why, str(cm.exception))


class MachineConfigKeyTest(HomeCase):
    """kitlib.config.user knows the key and checks only that it is an object."""

    def test_a_machine_config_holding_a_hub_object_is_accepted(self):
        hub = {'port': 8790, 'access': {'team': 'example-team', 'aud': 'tag', 'emails': []}}
        self.write({'hub': hub, 'pm_data': {'path': self.data}})
        self.assertEqual(user.user_config(self.home)['hub'], hub)

    def test_a_hub_value_that_is_not_an_object_is_refused(self):
        for bad in (['x'], 'x', 8790, True):
            self.write({'hub': bad})
            with self.assertRaises(config.ConfigError, msg=repr(bad)) as cm:
                user.user_config(self.home)
            self.assertIn('hub must be an object', str(cm.exception))

    def test_the_key_is_unset_by_default(self):
        self.assertIsNone(user.user_config(self.home)['hub'])
        self.assertEqual(user.failing_keys({'hub': {}}), [])
        self.assertEqual(user.failing_keys({'hub': []}), ['hub'])


class DefaultsTest(HomeCase):
    def test_no_machine_config_gives_the_defaults(self):
        s = settings.load(home=self.home)
        self.assertEqual((s.port, s.tunnel_port), (8790, 8791))
        self.assertIsNone(s.store)
        self.assertEqual((s.commit_every_s, s.backlog_refresh_s), (60, 300))
        self.assertIs(s.push, False)
        self.assertEqual(s.access, settings.Access('', '', ()))
        self.assertEqual(s.access.allowed_emails, frozenset())
        self.assertIsNone(s.relay)

    def test_a_machine_config_without_the_key_gives_the_defaults(self):
        self.write({'pm_start': {}})
        self.assertEqual(settings.load(home=self.home), settings.settings(None))

    def test_an_empty_hub_object_gives_the_defaults(self):
        self.write({'hub': {'_about': 'a note'}})
        self.assertEqual(settings.load(home=self.home), settings.settings(None))

    def test_the_store_defaults_to_the_hub_folder_of_the_data_worktree_and_push_to_on(self):
        self.write({'pm_data': {'path': self.data}})
        s = settings.load(home=self.home)
        self.assertEqual(s.store, os.path.join(self.data, 'hub'))
        self.assertIs(s.push, True)

    def test_the_home_may_come_from_an_environment(self):
        self.write({'hub': {'port': 9001}})
        self.assertEqual(settings.load({'USERPROFILE': self.home}).port, 9001)
        self.assertEqual(settings.load({'HOME': self.home}).port, 9001)

    def test_loading_writes_nothing(self):
        settings.load(home=self.home)
        self.assertEqual(os.listdir(self.home), [])


class ValuesTest(HomeCase):
    def test_every_field_is_carried(self):
        store = os.path.join(self.home, 'store')
        self.write({'pm_data': {'path': self.data},
                    'hub': {'port': 9001, 'tunnel_port': 9002, 'store': store, 'commit_every_s': 5,
                            'push': False, 'backlog_refresh_s': 600,
                            'access': {'team': 'example-team', 'aud': 'abc123',
                                       'emails': ['a@example.invalid']},
                            'relay': {'source': 'https://example.invalid/a'}}})
        s = settings.load(home=self.home)
        self.assertEqual(s, settings.Settings(
            9001, 9002, store, 5, False, 600,
            settings.Access('example-team', 'abc123', ('a@example.invalid',)),
            'https://example.invalid/a'))

    def test_a_null_is_unset_where_unset_has_a_meaning(self):
        self.write({'hub': {'store': None, 'push': None, 'access': None, 'relay': None}})
        self.assertEqual(settings.load(home=self.home), settings.settings(None))
        self.write({'hub': {'access': {'team': None, 'aud': None, 'emails': None},
                            'relay': {'source': None}}})
        self.assertEqual(settings.load(home=self.home), settings.settings(None))
        self.refused({'port': None}, 'port must be a whole number')

    def test_the_email_allow_list_is_normalised_for_comparison(self):
        a = settings.settings({'access': {'emails': ['  Owner@Example.INVALID ', 'b@example.invalid',
                                                     'owner@example.invalid']}}).access
        self.assertEqual(a.emails, ('owner@example.invalid', 'b@example.invalid'))
        self.assertEqual(a.allowed_emails, frozenset(a.emails))
        self.assertTrue(a.allows('OWNER@example.invalid\n'))
        self.assertFalse(a.allows('c@example.invalid'))

    def test_an_access_made_by_hand_holds_the_normalised_emails_too(self):
        a = settings.Access('t', 'a', [' B@Example.invalid', 'b@example.invalid', '', 7, 'A@example.invalid'])
        self.assertEqual(a.emails, ('b@example.invalid', 'a@example.invalid'))
        self.assertEqual(a._replace(team='u').emails, a.emails)
        for e in a.emails:
            self.assertTrue(a.allows(e))

    def test_push_defaults_on_only_for_the_store_in_the_data_worktree(self):
        store = os.path.join(self.home, 'elsewhere')
        self.write({'pm_data': {'path': self.data}})
        self.assertIs(settings.load(home=self.home).push, True)
        self.write({'pm_data': {'path': self.data}, 'hub': {'store': store}})
        self.assertIs(settings.load(home=self.home).push, False)
        self.write({'pm_data': {'path': self.data}, 'hub': {'store': store, 'push': True}})
        self.assertIs(settings.load(home=self.home).push, True)
        self.write({'pm_data': {'path': self.data}, 'hub': {'push': False}})
        self.assertIs(settings.load(home=self.home).push, False)
        self.write({'hub': {'store': store}})
        self.assertIs(settings.load(home=self.home).push, False)

    def test_the_timer_bounds_are_exact(self):
        for key, lo, hi in (('commit_every_s', 1, 3600), ('backlog_refresh_s', 30, 86400)):
            for ok in (lo, hi):
                self.assertEqual(getattr(settings.settings({key: ok}), key), ok)
            for bad in (lo - 1, hi + 1):
                self.refused({key: bad}, '%s must be a whole number from %d to %d' % (key, lo, hi))

    def test_an_empty_allow_list_allows_nobody(self):
        a = settings.settings(None).access
        for email in ('', ' ', None, 'a@example.invalid'):
            self.assertFalse(a.allows(email), repr(email))


class RefusalTest(HomeCase):
    def test_a_key_that_would_name_an_address_or_an_identity_is_unknown(self):
        for key in ('bind', 'public_url', 'owner_logins', 'host'):
            self.refused({key: '0.0.0.0'}, 'unknown key(s) %s' % key)

    def test_a_bad_port_is_refused(self):
        for key in ('port', 'tunnel_port'):
            for bad in (0, -1, 65536, '8790', 8790.0, True):
                self.refused({key: bad}, '%s must be a whole number' % key)

    def test_the_two_ports_must_differ(self):
        self.refused({'port': 9000, 'tunnel_port': 9000}, 'must differ')
        self.refused({'port': 8791}, 'must differ')
        self.refused({'tunnel_port': 8790}, 'must differ')

    def test_a_relative_or_empty_store_is_refused(self):
        self.refused({'store': 'hub'}, 'store must be an absolute path')
        self.refused({'store': ''}, 'store must be')

    def test_bad_timers_and_push_are_refused(self):
        self.refused({'commit_every_s': 0}, 'commit_every_s must be a whole number')
        self.refused({'backlog_refresh_s': 5}, 'backlog_refresh_s must be a whole number')
        self.refused({'push': 'yes'}, 'push must be true or false')

    def test_a_malformed_email_allow_list_is_refused(self):
        for bad, why in (('a@example.invalid', 'emails must be a list'),
                         (['a@example.invalid', ''], 'emails[1] must be a non-empty string'),
                         ([' '], 'emails[0] must be a non-empty string'),
                         ([7], 'emails[0] must be a string'),
                         ([['a@example.invalid']], 'emails[0] must be a string')):
            self.refused({'access': {'emails': bad}}, why)

    def test_a_team_name_that_is_not_one_dns_label_is_refused(self):
        for bad in ('a.example.invalid', 'team/x', 'team:443', 'a@b', '-team', 'team-', ' team', ''):
            self.refused({'access': {'team': bad}}, 'access.team must be')

    def test_a_bad_audience_access_or_relay_is_refused(self):
        self.refused({'access': {'aud': 'a b'}}, 'access.aud must be')
        self.refused({'access': {'aud': ''}}, 'access.aud must be')
        self.refused({'access': {'token': 'x'}}, 'unknown key(s) token')
        self.refused({'access': []}, 'access must be an object')
        self.refused({'relay': True}, 'relay must be an object')
        self.refused({'relay': {'url': 'x'}}, 'unknown key(s) url')

    def test_every_problem_is_named_at_once(self):
        self.write({'hub': {'port': 0, 'bind': 'x'}})
        with self.assertRaises(config.ConfigError) as cm:
            settings.load(home=self.home)
        self.assertIn('unknown key(s) bind', str(cm.exception))
        self.assertIn('port must be a whole number', str(cm.exception))
        self.assertIn(user.config_file(self.home), str(cm.exception))

    def test_a_malformed_pm_data_is_a_config_error(self):
        self.write({'pm_data': {'path': 'relative'}})
        with self.assertRaises(config.ConfigError) as cm:
            settings.load(home=self.home)
        self.assertIn('path must be an absolute path', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
