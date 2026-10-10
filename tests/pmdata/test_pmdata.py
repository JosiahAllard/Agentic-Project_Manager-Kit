"""kitlib.pmdata: kit-pm-data.json read/write, locate() from the main checkout and a linked
worktree, and the machine config's pm_data key."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import config, pmdata
from kitlib.config import user
from tests import gitfixture

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


class MachineConfigTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.data = os.path.join(self.home, 'pm')

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            f.write(json.dumps(body))

    def test_unset_is_none(self):
        self.assertIsNone(pmdata.machine_settings(self.home))
        self.write({})
        self.assertIsNone(pmdata.machine_settings(self.home))

    def test_the_schema_accepts_pm_data_and_the_settings_default_branch_and_remote(self):
        self.write({'pm_data': {'path': self.data}})
        self.assertIn('pm_data', user.SCHEMA)
        self.assertEqual(pmdata.machine_settings(self.home),
                         {'path': self.data, 'branch': 'pmdata', 'remote': 'private'})
        self.write({'pm_data': {'path': self.data, 'branch': 'd', 'remote': 'vault'}})
        self.assertEqual(pmdata.machine_settings(self.home),
                         {'path': self.data, 'branch': 'd', 'remote': 'vault'})

    def test_a_pm_data_that_is_not_an_object_is_a_config_error(self):
        self.write({'pm_data': 'x'})
        with self.assertRaises(config.ConfigError) as cm:
            user.user_config(self.home)
        self.assertIn('pm_data', str(cm.exception))

    def test_bad_settings_raise(self):
        for body in ({}, {'path': 'rel'}, {'path': self.data, 'branch': 'a b'},
                     {'path': self.data, 'remote': ''}, {'path': self.data, 'x': 1}):
            self.write({'pm_data': body})
            with self.assertRaises(pmdata.PmDataError, msg=repr(body)):
                pmdata.machine_settings(self.home)


if __name__ == '__main__':
    unittest.main()
