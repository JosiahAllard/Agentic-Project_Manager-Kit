"""kitlib.pmdata: kit-pm-data.json read/write, locate() from the main checkout and a linked
worktree, and the machine config's pm_data key."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import config, pmdata
from kitlib.config import user
from tests import gitfixture
from tests.home import PM_REPO_FENCE_ENV, machine_config, pm_repo

ROOT = 'a' * 40


class RecordTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-pmdata-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def put(self, body):
        with open(os.path.join(self.dir, pmdata.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def test_absent_is_none(self):
        self.assertIsNone(pmdata.read(self.dir))

    def test_write_then_read_round_trips(self):
        rec = pmdata.write(self.dir, 'pmdata', 'private', ROOT)
        self.assertEqual(rec.as_dict(), {'branch': 'pmdata', 'remote': 'private', 'root': ROOT})
        got = pmdata.read(self.dir)
        self.assertEqual(got, rec)
        self.assertEqual(got.common_dir, self.dir)
        with open(pmdata.record_path(self.dir), encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'branch': 'pmdata', 'remote': 'private', 'root': ROOT})
        self.assertEqual(os.listdir(self.dir), [pmdata.FILE_NAME])      # no temp file left

    def test_write_replaces_an_earlier_record(self):
        pmdata.write(self.dir, 'pmdata', 'private', ROOT)
        pmdata.write(self.dir, 'data2', 'vault', 'b' * 64)
        self.assertEqual(pmdata.read(self.dir), pmdata.Record('data2', 'vault', 'b' * 64))

    def test_write_refuses_an_invalid_field_and_writes_nothing(self):
        for args in (('', 'private', ROOT), ('pmdata', 'a b', ROOT), ('pmdata', 'private', 'abc'),
                     ('pmdata', 'private', 'A' * 40), ('-x', 'private', ROOT), ('a..b', 'r', ROOT),
                     (None, 'private', ROOT)):
            with self.assertRaises(pmdata.PmDataError, msg=repr(args)):
                pmdata.write(self.dir, *args)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_malformed_record_raises_and_names_the_file(self):
        for body in ('{', '[]', '"x"', {}, {'branch': 'pmdata', 'remote': 'private'},
                     {'branch': 'pmdata', 'remote': 'private', 'root': ROOT, 'more': 1},
                     {'branch': 'pmdata', 'remote': 'private', 'root': 'xyz'},
                     {'branch': 5, 'remote': 'private', 'root': ROOT}):
            self.put(body)
            with self.assertRaises(pmdata.PmDataError, msg=repr(body)) as cm:
                pmdata.read(self.dir)
            self.assertIn(pmdata.FILE_NAME, str(cm.exception))

    def test_a_record_that_is_not_utf8_or_is_a_folder_raises(self):
        with open(pmdata.record_path(self.dir), 'wb') as f:
            f.write(b'{"branch": "' + bytes([0xff, 0xfe]) + b'"}')
        with self.assertRaises(pmdata.PmDataError):
            pmdata.read(self.dir)
        os.remove(pmdata.record_path(self.dir))
        os.mkdir(pmdata.record_path(self.dir))
        with self.assertRaises(pmdata.PmDataError):
            pmdata.read(self.dir)


    def test_a_deeply_nested_record_raises_pmdata_error(self):
        self.put('[' * 100000)
        with self.assertRaises(pmdata.PmDataError):
            pmdata.read(self.dir)

    def test_a_failed_write_leaves_no_temp_file_and_raises_pmdata_error(self):
        os.mkdir(pmdata.record_path(self.dir))          # os.replace onto a folder fails
        with self.assertRaises(pmdata.PmDataError):
            pmdata.write(self.dir, 'pmdata', 'private', ROOT)
        self.assertEqual(os.listdir(self.dir), [pmdata.FILE_NAME])
        with self.assertRaises(pmdata.PmDataError):
            pmdata.write(os.path.join(self.dir, 'missing'), 'pmdata', 'private', ROOT)


class LocateTest(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)

    def test_not_found_without_a_record(self):
        self.assertIsNone(pmdata.locate(self.repo.root))

    def test_found_from_the_main_checkout_and_a_linked_worktree(self):
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', ROOT)
        linked = self.repo.worktree('other')
        for place in (self.repo.root, linked):
            rec = pmdata.locate(place)
            self.assertEqual(rec, pmdata.Record('pmdata', 'private', ROOT), place)
            self.assertTrue(os.path.samefile(rec.common_dir, self.repo.common_dir()))

    def test_malformed_record_raises(self):
        with open(os.path.join(self.repo.common_dir(), pmdata.FILE_NAME), 'w') as f:
            f.write('{')
        with self.assertRaises(pmdata.PmDataError):
            pmdata.locate(self.repo.root)

    def test_not_a_repository_is_none(self):
        folder = tempfile.mkdtemp(prefix='kit-nogit-')
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertIsNone(pmdata.locate(folder))


    def test_a_git_failure_other_than_not_a_repository_raises(self):
        for why in ('git rev-parse timed out after 30 seconds', 'could not run git in x: nope',
                    "fatal: detected dubious ownership in repository at 'x'", None):
            with mock.patch.object(pmdata.gitrepo, 'common_dir', return_value=(None, why)):
                with self.assertRaises(pmdata.PmDataError, msg=why):
                    pmdata.locate(self.repo.root)

    def test_git_saying_not_a_repository_is_none(self):
        why = 'fatal: not a git repository (or any of the parent directories): .git'
        with mock.patch.object(pmdata.gitrepo, 'common_dir', return_value=(None, why)):
            self.assertIsNone(pmdata.locate(self.repo.root))


class MachineConfigTest(unittest.TestCase):
    """machine_settings: pm_data is derived from the PM-repo pointer's repository and its record
    (kitlib.config.user.locate), never read from a file."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.data = os.path.join(self.home, 'pm')

    def no_pointer(self):
        patch = mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''})
        patch.start()
        self.addCleanup(patch.stop)
        user.forget()
        self.addCleanup(user.forget)

    def test_no_pointer_or_no_record_is_none(self):
        self.no_pointer()
        self.assertIsNone(pmdata.machine_settings(self.home))
        pm_repo(self, record=False)
        self.assertIsNone(pmdata.machine_settings(self.home))
        machine_config(self.home, {}, case=self)
        self.assertIsNone(pmdata.machine_settings(self.home))

    def test_it_is_the_pointers_repository_and_its_record(self):
        pm = pm_repo(self)
        self.assertIn('pm_data', user.SCHEMA)
        self.assertEqual(pmdata.machine_settings(self.home),
                         {'path': os.path.normpath(pm), 'branch': 'main', 'remote': 'origin'})
        pmdata.write(os.path.join(pm, '.git'), 'main', 'vault', ROOT)   # the pointer needs its branch
        user.forget()
        self.assertEqual(pmdata.machine_settings(self.home),
                         {'path': os.path.normpath(pm), 'branch': 'main', 'remote': 'vault'})

    def test_a_pm_data_key_in_a_file_is_a_config_error(self):
        pm_repo(self)
        machine_config(self.home, '{"pm_data": {"path": "%s"}}' % self.data.replace('\\', '/'), case=self)
        with self.assertRaises(config.ConfigError) as cm:
            user.user_config(self.home)
        self.assertIn('pm_data is derived', str(cm.exception))

    def test_a_home_folder_file_with_no_pointer_is_a_config_error(self):
        self.no_pointer()
        os.makedirs(os.path.join(self.home, user.DIR_NAME))
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump({'pm_data': {'path': self.data}}, f)
        with self.assertRaises(config.ConfigError) as cm:
            pmdata.machine_settings(self.home)
        self.assertIn('still in the home folder', str(cm.exception))

    def test_the_home_folder_file_read_as_the_moves_source_checks_pm_data(self):
        os.makedirs(os.path.join(self.home, user.DIR_NAME))
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump({'pm_data': 'x'}, f)
        with self.assertRaises(config.ConfigError) as cm:
            user.user_config(location=user.home_location(self.home))
        self.assertIn('pm_data', str(cm.exception))

    def test_check_settings_defaults_and_normalises(self):
        self.assertIsNone(pmdata.check_settings(None, 'w'))
        self.assertEqual(pmdata.check_settings({'path': self.data + os.sep + 'a' + os.sep + '..'}, 'w'),
                         {'path': self.data, 'branch': 'pmdata', 'remote': 'private'})

    def test_bad_settings_raise(self):
        for body in ('x', {}, {'path': 'rel'}, {'path': self.data, 'branch': 'a b'},
                     {'path': self.data, 'remote': ''}, {'path': self.data, 'x': 1}):
            with self.assertRaises(pmdata.PmDataError, msg=repr(body)):
                pmdata.check_settings(body, 'w')


if __name__ == '__main__':
    unittest.main()
