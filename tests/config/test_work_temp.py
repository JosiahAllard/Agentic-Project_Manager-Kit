"""kitlib.config.user.work_temp / make_temp: the kit's temp files (test run folders, gate logs) on
the checkout's own drive, from a rule that reads neither TEMP nor TMP, so every process agrees."""
import json
import os
import shutil
import string
import tempfile
import unittest
from unittest import mock

from kitlib.config import user
from kitlib.config.types import ConfigError

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVE = os.path.splitdrive(HERE)[0]


def other_letter(*avoid):
    """A drive letter none of `avoid` (drives such as 'C:') uses; the drive need not exist."""
    used = {a[0].upper() for a in avoid if a}
    return next(c for c in 'QRSTUVWXYJKLMNOP' if c not in used) + ':'


def missing_drive():
    """A drive letter with no drive behind it, or None."""
    for c in reversed(string.ascii_uppercase):
        if not os.path.isdir(c + ':' + os.sep):
            return c + ':'
    return None


@unittest.skipUnless(os.name == 'nt' and DRIVE, 'drive letters are Windows only')
class WorkTemp(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(body, f)

    def temp(self, near, env):
        return user.work_temp(near, home=self.home, environ=env, platform='win32')

    def test_a_checkout_off_the_shared_temps_drive_uses_its_own_drive_root(self):
        lad = other_letter(DRIVE) + os.sep + 'lad'
        got = self.temp(HERE, {'LOCALAPPDATA': lad})
        self.assertEqual(got, os.path.join(DRIVE + os.sep, user.TEMP_DIR_NAME))

    def test_temp_and_tmp_are_never_read(self):
        lad = other_letter(DRIVE) + os.sep + 'lad'
        moved = other_letter(DRIVE, lad) + os.sep + 'moved'
        for env in ({'LOCALAPPDATA': lad}, {'LOCALAPPDATA': lad, 'TEMP': moved, 'TMP': moved}):
            self.assertEqual(self.temp(HERE, env), os.path.join(DRIVE + os.sep, user.TEMP_DIR_NAME))
        same = {'LOCALAPPDATA': DRIVE + os.sep + 'lad', 'TEMP': moved, 'TMP': moved}
        self.assertEqual(self.temp(HERE, same), os.path.join(DRIVE + os.sep, 'lad', 'Temp'))

    def test_a_checkout_on_the_shared_temps_drive_uses_the_shared_temp(self):
        """No drive-root folder on the drive the user's temp folder is on already."""
        env = {'LOCALAPPDATA': DRIVE.lower() + os.sep + 'lad'}
        self.assertEqual(self.temp(HERE, env), os.path.join(DRIVE.lower() + os.sep, 'lad', 'Temp'))

    def test_a_drive_that_is_not_there_falls_back_to_the_shared_temp(self):
        gone = missing_drive()
        if gone is None:
            self.skipTest('every drive letter is in use')
        env = {'LOCALAPPDATA': other_letter(DRIVE, gone) + os.sep + 'lad'}
        self.assertEqual(self.temp(gone + os.sep + 'repo', env), user.shared_temp(env, 'win32'))

    def test_the_machine_config_temp_dir_wins(self):
        folder = os.path.join(self.home, 'kit-temp')
        self.write({'temp_dir': folder})
        lad = other_letter(DRIVE) + os.sep + 'lad'
        self.assertEqual(self.temp(HERE, {'LOCALAPPDATA': lad}), folder)

    def test_a_relative_temp_dir_refuses(self):
        self.write({'temp_dir': 'kit-temp'})
        with self.assertRaises(ConfigError) as cm:
            self.temp(HERE, {'LOCALAPPDATA': DRIVE + os.sep + 'lad'})
        self.assertIn('temp_dir must be an absolute path', str(cm.exception))

    def test_without_localappdata_the_checkouts_drive_still_serves(self):
        self.assertEqual(self.temp(HERE, {}), os.path.join(DRIVE + os.sep, user.TEMP_DIR_NAME))
        gone = missing_drive()
        if gone is not None:
            with self.assertRaises(RuntimeError):
                self.temp(gone + os.sep + 'repo', {})

    def test_elsewhere_it_is_the_shared_temp(self):
        with mock.patch('tempfile.gettempdir', return_value='/tmp/x'):
            self.assertEqual(user.work_temp('/srv/repo', home=self.home, environ={},
                                            platform='linux'), '/tmp/x')


class MakeTemp(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-mt-')
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_a_new_folder_in_the_work_temp_made_if_missing(self):
        parent = os.path.join(self.base, 'not-yet')
        with mock.patch.object(user, 'work_temp', return_value=parent):
            folder, note = user.make_temp(self.base, 'kit-x-')
        self.assertIsNone(note)
        self.assertEqual(os.path.dirname(folder), parent)
        self.assertTrue(os.path.basename(folder).startswith('kit-x-'))
        self.assertTrue(os.path.isdir(folder))

    def test_a_work_temp_that_cannot_be_used_falls_back_with_a_note(self):
        """To the kit's default temp folder (default_work_temp), and only when that cannot be
        used either to this process's temp folder."""
        fallback = os.path.join(self.base, 'default')
        for boom in (OSError('access denied'), RuntimeError('no LOCALAPPDATA'),
                     ConfigError('bad temp_dir')):
            with mock.patch.object(user, 'work_temp', side_effect=boom), \
                    mock.patch.object(user, 'default_work_temp', return_value=fallback):
                folder, note = user.make_temp(self.base, 'kit-x-')
            self.assertEqual(os.path.dirname(folder), fallback)
            self.assertIn(str(boom), note)
            with mock.patch.object(user, 'work_temp', side_effect=boom), \
                    mock.patch.object(user, 'default_work_temp',
                                      side_effect=RuntimeError('no default')):
                folder, note = user.make_temp(self.base, 'kit-x-')
            self.addCleanup(shutil.rmtree, folder, True)
            self.assertEqual(os.path.normcase(os.path.dirname(folder)),
                             os.path.normcase(tempfile.gettempdir()))
            self.assertIn(str(boom), note)


if __name__ == '__main__':
    unittest.main()
