"""The machine config's help and warning text names the config as it is: the three RELATIVE_KEYS
paths say they may be relative to the PM repo, and the machine-paths warning names the file as
kitlib.config.user.display shows it."""
import unittest
from unittest import mock

from kitlib import machinepaths
from kitlib.config import user
from kitlib.config.types import ConfigError


class RelativeKeysDescribe(unittest.TestCase):
    def test_the_relative_keys_say_they_take_a_path_relative_to_the_pm_repo(self):
        for key in user.RELATIVE_KEYS:
            self.assertEqual(user.SCHEMA[key].type.describe(), 'absolute path, or relative to the PM repo', key)

    def test_a_key_that_must_be_absolute_still_says_so(self):
        self.assertEqual(user.SCHEMA['temp_dir'].type.describe(), 'absolute path')


class MachinePathsWarning(unittest.TestCase):
    def test_the_warning_names_the_config_as_display_shows_it(self):
        with mock.patch.object(user, 'machine_path_folders', side_effect=ConfigError('bad')), \
                mock.patch.object(user, 'display', return_value='SHOWN/config.json'):
            names, note = machinepaths.folders('H')
        self.assertEqual(names, machinepaths.DEFAULT_FOLDERS)
        self.assertIn('machine config SHOWN/config.json could not be read', note)
        self.assertNotIn('<PM repo>', note)


if __name__ == '__main__':
    unittest.main()
