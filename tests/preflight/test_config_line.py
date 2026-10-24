"""preflight's `config` line (kitlib.preflight.configline; docs/kit-json.md "Moving the machine
config into the PM repo"): which machine config files are read (the PM repository's `.pm-kit`),
a machine config still in the home folder, and why the config cannot be read. It names files and
keys, never values. Every case uses a scratch home folder and the fenced pointer."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.config import migration, user
from kitlib.preflight import configline
from tests.home import PM_REPO_FENCE_ENV, pm_repo

SECRET = 'value-never-printed'


class ConfigLineTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.pm = pm_repo(self)
        self.folder = os.path.join(self.pm, user.DIR_NAME)
        self.shown = self.folder.replace('\\', '/')
        self.policy = '%s/%s' % (self.shown, user.FILE_NAME)
        self.local = '%s/%s' % (self.shown, user.LOCAL_FILE_NAME)
        self.home_name = '~/%s/%s' % (user.DIR_NAME, user.FILE_NAME)
        self.old_name = '~/%s/%s' % (migration.OLD_DIR_NAME, user.FILE_NAME)

    def put(self, data, name=user.FILE_NAME, folder=None):
        folder = folder or self.folder
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def put_home(self, data, name=user.DIR_NAME):
        self.put(data, folder=os.path.join(self.home, name))

    def line(self):
        out = configline.line(self.home)
        self.assertTrue(out.startswith('config    '), out)
        self.assertNotIn(SECRET, out)
        return out

    def no_pointer(self):
        patch = mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''})
        patch.start()
        self.addCleanup(patch.stop)
        user.forget()

    def test_none(self):
        self.no_pointer()
        self.assertEqual(self.line(), 'config    none - no PM-repo pointer and no machine config in the '
                         'home folder: every key has its default')

    def test_a_pointer_with_no_file_yet(self):
        self.assertEqual(self.line(), 'config    %s (no file yet: every key has its default)' % self.shown)

    def test_the_files_read_are_named(self):
        self.put({'pending_path': os.path.join(self.home, SECRET)})
        self.assertEqual(self.line(), 'config    %s (config.json)' % self.shown)
        self.put({'owner_email': SECRET + '@example.com'}, name=user.LOCAL_FILE_NAME)
        self.assertEqual(self.line(), 'config    %s (config.json, local.json)' % self.shown)

    def test_a_home_file_beside_the_pm_repositorys_is_broken_naming_its_removal(self):
        self.put({})
        for name in (migration.OLD_DIR_NAME, user.DIR_NAME):
            with self.subTest(name):
                self.put_home({'pending_path': os.path.join(self.home, SECRET)}, name)
                out = self.line()
                self.assertTrue(out.startswith('config    BROKEN the machine config moved to the PM '
                                               'repository'), out)
                # DIR_NAME's file, once there, is the one named (kitlib.config.user _leftover)
                left = os.path.join(self.home, name, user.FILE_NAME)
                self.assertIn("Remove-Item -LiteralPath '%s'" % left, out)
                self.assertNotIn(SECRET, out)

    def test_a_home_file_that_was_never_moved_is_broken(self):
        self.put_home({'pending_path': os.path.join(self.home, SECRET)})
        out = self.line()
        self.assertTrue(out.startswith('config    BROKEN the machine config is still in the home folder'), out)
        self.assertIn('config_move.py', out)
        self.no_pointer()
        out = self.line()
        self.assertTrue(out.startswith('config    BROKEN the machine config is still in the home folder'), out)
        self.assertIn('set the PM repo pointer', out)

    def test_a_pm_folder_that_is_a_file(self):
        with open(self.folder, 'w', encoding='utf-8') as f:
            f.write('x')
        out = self.line()
        self.assertTrue(out.startswith('config    BROKEN '), out)
        self.assertIn('is not a folder', out)

    def test_a_bad_value_is_named_by_its_key_never_shown(self):
        # the strict reader's own message quotes the value (`not %r`); the line must not
        for body, bad in (({'pending_path': [SECRET]}, 'pending_path'),
                          ({'temp_dir': 'relative-%s' % SECRET}, 'temp_dir'),
                          ({'ledger_repos': [SECRET + ' Upper Case']}, 'ledger_repos')):
            with self.subTest(bad):
                self.put(body)
                self.assertEqual(self.line(), 'config    BROKEN %s: bad key(s): %s' % (self.policy, bad))

    def test_a_relative_ledger_path_is_read_against_the_pm_repository_not_refused(self):
        self.put({'ledger_path': 'rulings/%s.md' % SECRET})
        self.assertEqual(self.line(), 'config    %s (config.json)' % self.shown)

    def test_an_unknown_key_and_a_bad_value_are_both_named(self):
        self.put({'a_key_this_kit_does_not_know': SECRET, 'temp_dir': 'rel-' + SECRET})
        self.assertEqual(self.line(), 'config    BROKEN %s: bad key(s): a_key_this_kit_does_not_know, temp_dir'
                         % self.policy)

    def test_a_key_in_the_wrong_file_or_one_the_move_drops_is_named(self):
        self.put({'owner_email': SECRET + '@example.com', 'pm_data': {'path': SECRET}})
        self.put({'ledger_repos': ['a'], 'scrub_patterns': [SECRET]}, name=user.LOCAL_FILE_NAME)
        self.assertEqual(self.line(), 'config    BROKEN %s: bad key(s): owner_email, pm_data; '
                         '%s: bad key(s): ledger_repos' % (self.policy, self.local))

    def test_a_file_that_is_not_json_or_writes_a_key_twice_shows_no_text(self):
        for body, why in (('{"ledger_path": "%s"' % SECRET, 'not valid JSON'),
                          ('{"ledger_path": "a", "ledger_path": "%s"}' % SECRET, 'a key written twice in one object'),
                          ('["%s"]' % SECRET, 'not a JSON object')):
            for name, shown in ((user.FILE_NAME, self.policy), (user.LOCAL_FILE_NAME, self.local)):
                with self.subTest(why=why, file=name):
                    shutil.rmtree(self.folder, True)
                    self.put(body, name=name)
                    self.assertEqual(self.line(), 'config    BROKEN %s: %s' % (shown, why))


if __name__ == '__main__':
    unittest.main()
