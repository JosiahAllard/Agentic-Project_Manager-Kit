"""kitlib.config.user's `machine_folders` key: the drive-rooted folder names the machine-path
check refuses (kitlib.machinepaths), read from the machine config, with the kit's default list when
unset. Fixture folder names are invented; no test here names a folder of this machine."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib.config import user
from kitlib.config.types import ConfigError


class MachineFolders(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(body, f)

    def test_the_default_list_is_the_kit_temp_folder_and_two_more(self):
        self.assertEqual(len(user.DEFAULT_MACHINE_FOLDERS), 3)
        self.assertEqual(user.DEFAULT_MACHINE_FOLDERS[-1], user.TEMP_DIR_NAME)
        self.assertNotIn('Users', user.DEFAULT_MACHINE_FOLDERS)

    def test_no_config_file_gives_the_default(self):
        self.assertEqual(user.machine_folders(self.home), user.DEFAULT_MACHINE_FOLDERS)

    def test_unset_key_gives_the_default(self):
        self.write({'owner_email': 'someone@example.com'})
        self.assertEqual(user.machine_folders(self.home), user.DEFAULT_MACHINE_FOLDERS)

    def test_a_set_list_replaces_the_default(self):
        self.write({'machine_folders': ['Src', 'Scratch']})
        self.assertEqual(user.machine_folders(self.home), ('Src', 'Scratch'))

    def test_an_empty_list_is_kept_empty(self):
        self.write({'machine_folders': []})
        self.assertEqual(user.machine_folders(self.home), ())

    def test_a_bad_value_refuses_naming_the_key(self):
        for bad in (['a/b'], ['a\\b'], [''], ['two words'], 'Src', [3]):
            self.write({'machine_folders': bad})
            with self.assertRaises(ConfigError) as caught:
                user.machine_folders(self.home)
            self.assertIn('machine_folders', str(caught.exception), bad)


if __name__ == '__main__':
    unittest.main()
