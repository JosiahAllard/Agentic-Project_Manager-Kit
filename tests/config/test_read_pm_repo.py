"""kitlib.config.user reading the machine config from the PM repository the PM-repo pointer names
(locate OK): config.json holds POLICY_KEYS and local.json LOCAL_KEYS, a key in the other file or
one the move drops is a ConfigError, RELATIVE_KEYS read against the PM repository, and pm_data is
derived from the pointer, never read. Every home and PM repository is a temp folder under the
fence (tests.home); the real registry and home are never read."""
import json
import os
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.config import ConfigError, move, user
from tests.home import RECORD_ROOT, PM_REPO_FENCE_ENV, pm_repo, temp_home
RID = 'R-' + '9001'     # built, so tests/scrub sees no ruling id in this file


class _PmRepo(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        self.pm = pm_repo(self)
        self.folder = os.path.join(self.pm, user.DIR_NAME)
        self.policy = os.path.join(self.folder, user.FILE_NAME)
        self.local = os.path.join(self.folder, user.LOCAL_FILE_NAME)

    def write(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def refused(self, *whys):
        with self.assertRaises(ConfigError) as cm:
            user.user_config(self.home)
        for why in whys:
            self.assertIn(why, str(cm.exception))
        return str(cm.exception)


class KeySetsTest(unittest.TestCase):
    def test_the_key_sets_are_the_moves(self):
        self.assertEqual(user.POLICY_KEYS, move.POLICY)
        self.assertEqual(user.LOCAL_KEYS, move.LOCAL)
        self.assertEqual(user.DROPPED_KEYS, move.DROPPED)
        self.assertEqual(user.RELATIVE_KEYS, move.RELATIVE_KEYS)
        self.assertEqual(user.LOCAL_FILE_NAME, move.LOCAL_FILE)

    def test_every_schema_key_is_in_exactly_one_file_or_dropped(self):
        sets = (user.POLICY_KEYS, user.LOCAL_KEYS, user.DROPPED_KEYS)
        for key in set(user.SCHEMA) | set(user.RETIRED):
            self.assertEqual(sum(key in s for s in sets), 1, key)
        self.assertTrue(set(user.RELATIVE_KEYS) <= set(user.POLICY_KEYS))


class TwoFilesTest(_PmRepo):
    def test_each_file_gives_its_own_keys(self):
        self.write(self.policy, {'standing_delegation': RID, 'temp_dir': 'Q:\\t'})
        self.write(self.local, {'owner_email': 'owner@example.invalid', 'hub': {'port': 9001}})
        cfg = user.user_config(self.home)
        self.assertEqual((cfg['standing_delegation'], cfg['temp_dir']), (RID, 'Q:\\t'))
        self.assertEqual((cfg['owner_email'], cfg['hub']), ('owner@example.invalid', {'port': 9001}))
        self.assertIsNone(cfg['ledger_path'])

    def test_either_file_absent_is_empty(self):
        self.write(self.local, {'owner_email': 'owner@example.invalid'})
        self.assertEqual(user.user_config(self.home)['owner_email'], 'owner@example.invalid')
        os.remove(self.local)
        self.write(self.policy, {'standing_delegation': RID})
        cfg = user.user_config(self.home)
        self.assertEqual((cfg['standing_delegation'], cfg['owner_email']), (RID, None))

    def test_a_local_key_in_config_json_is_refused_naming_its_file(self):
        self.write(self.policy, {'owner_email': 'owner@example.invalid'})
        self.refused('%s: owner_email belongs in %s, not here' % (self.policy, self.local))

    def test_a_policy_key_in_local_json_is_refused_naming_its_file(self):
        self.write(self.local, {'standing_delegation': RID})
        self.refused('%s: standing_delegation belongs in %s, not here' % (self.local, self.policy))

    def test_a_dropped_key_in_either_file_is_refused(self):
        for path in (self.policy, self.local):
            for key in ('ledger_refs', 'conductor_start'):
                self.write(path, {key: {}})
                self.refused('%s: %s is a key the move drops' % (path, key))
            os.remove(path)

    def test_pm_data_in_either_file_is_derived_never_read(self):
        for path in (self.policy, self.local):
            self.write(path, {'pm_data': {'path': self.pm}})
            self.refused('%s: pm_data is derived' % path)
            os.remove(path)

    def test_an_unknown_key_is_refused(self):
        self.write(self.policy, {'no_such_key': 1})
        self.refused('no_such_key')

    def test_notes_are_skipped_in_both_files(self):
        self.write(self.policy, {'_about': 'policy', 'standing_delegation': RID})
        self.write(self.local, {'_about': 'local'})
        self.assertEqual(user.user_config(self.home)['standing_delegation'], RID)

    def test_every_problem_in_both_files_is_named_at_once(self):
        self.write(self.policy, {'owner_email': 'owner@example.invalid'})
        self.write(self.local, {'hub': 5})
        self.refused('owner_email belongs in', 'hub must be an object')

    def test_a_file_that_cannot_be_parsed_or_is_no_object_is_refused(self):
        self.write(self.local, '{')
        self.refused(self.local)
        self.write(self.local, '[]')
        self.refused('%s must be an object' % self.local)


class RelativeKeysTest(_PmRepo):
    def test_relative_keys_are_read_against_the_pm_repository(self):
        self.write(self.policy, {'ledger_dir': 'rulings', 'ledger_repos': ['pm'],
                                 'pending_path': 'docs\\pending.md'})
        cfg = user.user_config(self.home)
        self.assertEqual(cfg['ledger_dir'], os.path.join(self.pm, 'rulings'))
        self.assertEqual(cfg['pending_path'], os.path.join(self.pm, 'docs', 'pending.md'))

    def test_an_absolute_value_is_kept(self):
        self.write(self.policy, {'ledger_path': 'Q:\\ledger.md'})
        self.assertEqual(user.user_config(self.home)['ledger_path'], 'Q:\\ledger.md')

    def test_another_path_key_written_relative_is_still_refused(self):
        self.write(self.policy, {'temp_dir': 'temp'})
        self.refused('temp_dir must be an absolute path')

    def test_resolved_leaves_other_keys_and_empty_values_alone(self):
        got = user.resolved({'ledger_path': 'a.md', 'temp_dir': 'b', 'pending_path': ''}, 'Q:\\pm')
        self.assertEqual(got, {'ledger_path': 'Q:\\pm\\a.md', 'temp_dir': 'b', 'pending_path': ''})

    def test_a_home_file_keeps_relative_paths_refused(self):
        h = os.path.join(self.home, user.DIR_NAME, user.FILE_NAME)
        self.write(h, {'ledger_path': 'ledger.md'})
        with self.assertRaises(ConfigError) as cm:
            user.user_config(location=user.home_location(self.home))
        self.assertIn('ledger_path must be an absolute path', str(cm.exception))


class DerivedPmDataTest(_PmRepo):
    def test_pm_data_is_the_pointer_repository_and_its_record(self):
        want = {'path': os.path.normpath(self.pm), 'branch': 'main', 'remote': 'origin'}
        self.assertEqual(user.locate(self.home).pm_data, want)
        self.assertEqual(user.user_config(self.home)['pm_data'], want)
        self.assertEqual(pmdata.machine_settings(self.home), want)

    def test_no_record_is_no_pm_data(self):
        pm_repo(self, record=False)
        self.assertEqual(user.locate(self.home).state, user.OK)
        self.assertIsNone(user.user_config(self.home)['pm_data'])

    def test_no_pointer_is_no_pm_data(self):
        with mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''}):
            user.forget()
            loc = user.locate(self.home)
            self.assertEqual((loc.state, loc.pm_data), (user.NONE, None))
            self.assertIsNone(user.user_config(self.home)['pm_data'])
        user.forget()


class FileByKeyTest(_PmRepo):
    def test_ok_names_each_keys_own_file(self):
        for key in user.LOCAL_KEYS:
            self.assertEqual(user.key_file(self.home, key), self.local, key)
            self.assertEqual(user.writable_file(self.home, key), self.local, key)
        for key in user.POLICY_KEYS + (None,):
            self.assertEqual(user.key_file(self.home, key), self.policy, key)
            self.assertEqual(user.writable_file(self.home, key), self.policy, key)

    def test_ok_refuses_a_writer_of_a_dropped_key(self):
        for key in user.DROPPED_KEYS:
            with self.assertRaises(ConfigError, msg=key) as cm:
                user.writable_file(self.home, key)
            self.assertIn(self.folder, str(cm.exception))
        self.assertIn('pm_data is derived', str(cm.exception))
        self.assertEqual(user.key_file(self.home, 'pm_data'), self.policy)

    def test_none_names_the_home_file_to_readers_and_refuses_every_writer(self):
        with mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''}):
            user.forget()
            home_file = os.path.join(self.home, user.DIR_NAME, user.FILE_NAME)
            for key in ('owner_email', 'standing_delegation', None):
                self.assertEqual(user.key_file(self.home, key), home_file)
                with self.assertRaises(ConfigError, msg=key) as cm:
                    user.writable_file(self.home, key)
                self.assertIn('no PM repo pointer is set', str(cm.exception))
                self.assertIn('tools/machine_setup.py --pm-repo', str(cm.exception))
                self.assertIn(user.POINTER_DOC, str(cm.exception))
            self.assertEqual(user.readable_file(self.home), home_file)
            self.assertFalse(os.path.exists(home_file))
        user.forget()

    def test_readable_file_is_the_pm_repositorys_policy_file(self):
        self.assertEqual(user.readable_file(self.home), self.policy)
        self.assertEqual(user.config_dir(self.home), self.folder)


class LocationTest(_PmRepo):
    def test_ok_names_both_files_and_the_repository(self):
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.path, loc.local, loc.folder, loc.pm),
                         (user.OK, self.policy, self.local, self.folder, os.path.normpath(self.pm)))
        self.assertEqual(loc.new, self.policy)

    def test_new_is_the_home_file_without_a_pointer(self):
        with mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''}):
            user.forget()
            loc = user.locate(self.home)
            self.assertEqual(loc.new, os.path.join(self.home, user.DIR_NAME, user.FILE_NAME))
            self.assertIsNone(loc.local)
        user.forget()

    def test_a_given_pointer_is_used_and_the_registry_is_not_read(self):
        from kitlib.config import pointer
        other = pm_repo(self, record=False)
        given = pointer.Pointer(pointer.OK, self.pm, None, None)
        with mock.patch.object(pointer, 'read_raw', side_effect=AssertionError('read')):
            loc = user.locate(self.home, ptr=given)
        self.assertEqual(loc.pm, os.path.normpath(self.pm))
        self.assertNotEqual(os.path.normpath(other), loc.pm)


class MemoTest(_PmRepo):
    def test_the_pointer_is_checked_once_per_registry_answer_until_forget(self):
        from kitlib.config import pointer
        real = pointer.read
        with mock.patch.object(pointer, 'read', side_effect=real) as read:
            user.forget()
            for _ in range(3):
                user.user_config(self.home)
            self.assertEqual(read.call_count, 1)
            user.forget()
            user.locate(self.home)
            self.assertEqual(read.call_count, 2)

    def test_a_record_written_later_is_seen_only_after_forget(self):
        pm = pm_repo(self, record=False)
        self.assertIsNone(user.locate(self.home).pm_data)
        pmdata.write(os.path.join(pm, '.git'), 'main', 'origin', RECORD_ROOT)
        self.assertIsNone(user.locate(self.home).pm_data)
        user.forget()
        self.assertEqual(user.locate(self.home).pm_data['path'], os.path.normpath(pm))

    def test_a_new_pointer_value_is_a_new_answer_without_forget(self):
        other = pm_repo(self)
        self.assertEqual(user.locate(self.home).pm, os.path.normpath(other))
        with mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: self.pm}):
            self.assertEqual(user.locate(self.home).pm, os.path.normpath(self.pm))


if __name__ == '__main__':
    unittest.main()
