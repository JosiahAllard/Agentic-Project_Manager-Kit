"""kitlib.config.user's `machine_path_folders` key: the drive-rooted folder names the machine-path
check refuses besides a user profile (kitlib.machinepaths), read from the machine config. Unset, it
names none: the check then refuses only user-profile paths. Fixture folder names are invented; no
test here names a folder of this machine."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib.config import user
from kitlib.config.types import ConfigError


class MachinePathFolders(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(body, f)

    def test_the_kit_codes_no_default_folder_list(self):
        self.assertFalse(hasattr(user, 'DEFAULT_MACHINE_FOLDERS'))

    def test_no_config_file_names_no_folder(self):
        self.assertEqual(user.machine_path_folders(self.home), ())

    def test_unset_key_names_no_folder(self):
        self.write({'owner_email': 'someone@example.com'})
        self.assertEqual(user.machine_path_folders(self.home), ())

    def test_a_set_list_is_read(self):
        self.write({'machine_path_folders': ['Src', 'Scratch']})
        self.assertEqual(user.machine_path_folders(self.home), ('Src', 'Scratch'))

    def test_an_empty_list_is_kept_empty(self):
        self.write({'machine_path_folders': []})
        self.assertEqual(user.machine_path_folders(self.home), ())

    def test_a_bad_value_refuses_naming_the_key(self):
        for bad in (['a/b'], ['a\\b'], [''], ['two words'], 'Src', [3]):
            self.write({'machine_path_folders': bad})
            with self.assertRaises(ConfigError) as caught:
                user.machine_path_folders(self.home)
            self.assertIn('machine_path_folders', str(caught.exception), bad)

    def test_the_old_key_name_is_refused(self):
        self.write({'machine_folders': ['Src']})
        with self.assertRaises(ConfigError) as caught:
            user.machine_path_folders(self.home)
        self.assertIn('machine_folders', str(caught.exception))


class TempDirName(unittest.TestCase):

    def test_the_default_temp_folder_name_is_kit_temp(self):
        self.assertEqual(user.TEMP_DIR_NAME, 'kit-temp')


if __name__ == '__main__':
    unittest.main()
