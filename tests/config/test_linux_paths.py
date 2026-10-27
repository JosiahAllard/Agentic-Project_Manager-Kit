"""The configuration lookups on Linux (docs/linux-support-plan.md section 8, slice 9): the profile
folder from the password database, the PM-repo pointer files, the system temp folder, and the
machine config resolved from a pointer file (kitlib.platform.linux.paths, kitlib.config.user and
kitlib.config.pointer on the seam).

Every class runs on every platform: kitlib.platform.NAME is patched to 'linux', the password
database to a temp folder (paths._pw_dir) and the machine pointer file to a temp path
(paths.MACHINE_POINTER), so nothing reads this machine's /etc, password database or registry."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import kitlib.platform
from kitlib.config import pointer, user
from kitlib.platform.linux import paths
from tests.home import pm_repo, temp_home


def on_linux(case, pw_dir):
    """For the rest of `case`: NAME is 'linux', the password database gives `pw_dir` (a value,
    or an exception to raise), and no profile folder is remembered."""
    def pw():
        if isinstance(pw_dir, BaseException):
            raise pw_dir
        return pw_dir
    for patch in (mock.patch.object(kitlib.platform, 'NAME', 'linux'),
                  mock.patch.object(paths, '_pw_dir', pw),
                  mock.patch.object(user, '_PROFILE', None)):
        patch.start()
        case.addCleanup(patch.stop)


def native_paths(case):
    """For the rest of `case`: a path absolute as this host spells one counts as a Linux absolute
    path (paths.is_absolute is os.path.isabs), so a test on any host can name real temp folders.
    On Linux it changes nothing."""
    patch = mock.patch.object(paths, 'is_absolute', os.path.isabs)
    patch.start()
    case.addCleanup(patch.stop)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data if isinstance(data, bytes) else data.encode('utf-8'))


class Profile(unittest.TestCase):
    def test_the_profile_is_the_password_database_s_never_home(self):
        on_linux(self, '/home/u')
        with mock.patch.dict(os.environ, {'HOME': '/elsewhere', 'USERPROFILE': '/elsewhere'}):
            self.assertEqual(paths.profile(), '/home/u')
            self.assertEqual(user.real_profile(), '/home/u')

    def test_no_entry_or_a_relative_folder_is_an_os_error(self):
        for given, said in ((KeyError('getpwuid(): uid not found: 1000'), 'no entry'),
                            ('home/u', 'no absolute home folder'), ('', 'no absolute home folder')):
            with self.subTest(given=given):
                on_linux(self, given)
                with self.assertRaises(OSError) as cm:
                    paths.profile()
                self.assertIn(said, str(cm.exception))

    def test_a_windows_folder_is_no_linux_home(self):
        on_linux(self, r'D:\home\u')
        with self.assertRaises(OSError) as cm:
            paths.profile()
        self.assertIn('no absolute home folder', str(cm.exception))

    def test_the_windows_lookup_is_not_asked(self):
        on_linux(self, '/home/u')
        with mock.patch.object(user, '_windows_profile', side_effect=AssertionError('Windows asked')):
            self.assertEqual(user.real_profile(), '/home/u')


class Fence(unittest.TestCase):
    """The home fence compares against the password database's folder on Linux."""

    def setUp(self):
        self.profile = tempfile.mkdtemp(prefix='kit-linux-profile-')
        self.addCleanup(shutil.rmtree, self.profile, True)
        on_linux(self, self.profile)
        native_paths(self)

    def test_a_home_that_resolves_to_the_profile_is_refused(self):
        with mock.patch.dict(os.environ, {'HOME': self.profile, 'USERPROFILE': self.profile}):
            with self.assertRaises(user.HomeFenced):
                user.home_dir()

    def test_a_temp_home_is_given(self):
        home = temp_home(self)
        self.assertEqual(user.home_dir(), home)

    def test_no_password_entry_refuses_every_home(self):
        on_linux(self, KeyError('uid not found'))
        temp_home(self)
        with self.assertRaises(user.HomeFenced) as cm:
            user.home_dir()
        self.assertIn('no entry', str(cm.exception))


class PointerFiles(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-linux-pointer-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.machine = os.path.join(self.base, 'etc', 'pm-kit', 'pointer')
        self.profile = os.path.join(self.base, 'home')
        self.user_file = os.path.join(self.profile, '.config', 'pm-kit', 'pointer')
        on_linux(self, self.profile)
        native_paths(self)
        patch = mock.patch.object(paths, 'MACHINE_POINTER', self.machine)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_files_are_etc_then_the_profile_s_config(self):
        self.assertEqual(paths.pointer_files(), (self.machine, self.user_file))

    def test_the_xdg_variable_does_not_move_the_user_file(self):
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': os.path.join(self.base, 'xdg')}):
            write(os.path.join(self.base, 'xdg', 'pm-kit', 'pointer'), '/srv/elsewhere\n')
            self.assertEqual(paths.read_pointer(), (None, None))

    def test_no_file_is_no_pointer(self):
        self.assertEqual(paths.read_pointer(), (None, None))

    def test_the_user_file_alone_is_the_pointer(self):
        write(self.user_file, '  /srv/pm  \n\n')
        self.assertEqual(paths.read_pointer(), ('/srv/pm', None))

    def test_the_machine_file_wins(self):
        write(self.user_file, '/srv/user-pm\n')
        write(self.machine, '/srv/pm\n')
        self.assertEqual(paths.read_pointer(), ('/srv/pm', None))

    def test_a_bad_machine_file_is_the_answer_never_the_user_s(self):
        write(self.user_file, '/srv/user-pm\n')
        for data, said in ((' \n', 'is empty'), (b'\xff\xfe/srv', 'is not UTF-8 text'),
                           ('/srv/a\n/srv/b\n', 'holds 2 lines')):
            with self.subTest(data=data):
                write(self.machine, data)
                value, why = paths.read_pointer()
                self.assertIsNone(value)
                self.assertIn(self.machine, why)
                self.assertIn(said, why)

    def test_a_machine_file_that_cannot_be_read_is_bad(self):
        os.makedirs(self.machine)               # a folder where the file goes
        value, why = paths.read_pointer()
        self.assertIsNone(value)
        self.assertIn('cannot read the PM repo pointer %s' % self.machine, why)

    def test_no_profile_and_no_machine_file_is_bad(self):
        on_linux(self, KeyError('uid not found'))
        value, why = paths.read_pointer()
        self.assertIsNone(value)
        self.assertIn('cannot find the user PM repo pointer file', why)
        write(self.machine, '/srv/pm\n')
        self.assertEqual(paths.read_pointer(), ('/srv/pm', None))

    def test_the_registry_reader_reads_the_files_on_linux(self):
        write(self.user_file, '/srv/pm\n')
        with mock.patch.object(pointer, '_winreg', side_effect=AssertionError('registry read')):
            self.assertEqual(pointer.registry(), ('/srv/pm', None))
            with mock.patch.object(user, 'fenced', return_value=False):
                self.assertEqual(pointer.read_raw(), ('/srv/pm', None))

    def test_under_the_fence_the_files_are_not_read(self):
        write(self.user_file, '/srv/pm\n')
        with mock.patch.dict(os.environ, {pointer.FENCE_ENV: '/srv/fenced'}):
            self.assertEqual(pointer.read_raw(), ('/srv/fenced', None))


class PointerCheck(unittest.TestCase):
    def setUp(self):
        on_linux(self, '/home/u')

    def test_a_path_without_a_drive_is_absolute_on_linux(self):
        self.assertEqual(pointer.check('/srv/no-such-pm-repo'), 'no folder at /srv/no-such-pm-repo')

    def test_a_relative_path_is_still_refused(self):
        self.assertEqual(pointer.check('srv/pm'), 'srv/pm is not an absolute path')

    def test_a_windows_path_is_not_absolute_on_linux(self):
        for path in (r'C:\pm', 'C:/pm', r'\\server\share\pm'):
            with self.subTest(path=path):
                self.assertEqual(pointer.check(path), '%s is not an absolute path' % path)

    def test_windows_still_wants_a_drive(self):
        with mock.patch.object(kitlib.platform, 'NAME', 'windows'):
            self.assertEqual(pointer.check('/srv/pm'), '/srv/pm is not an absolute path')


class MachineConfigFromThePointerFile(unittest.TestCase):
    """The slice's proof: the machine config resolves on Linux from the pointer file."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-linux-config-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.profile = os.path.join(self.base, 'home')
        on_linux(self, self.profile)
        native_paths(self)
        patch = mock.patch.object(paths, 'MACHINE_POINTER', os.path.join(self.base, 'no-etc'))
        patch.start()
        self.addCleanup(patch.stop)
        self.home = temp_home(self)
        self.pm = pm_repo(self)

    def test_the_config_is_read_from_the_repository_the_file_names(self):
        write(os.path.join(self.profile, '.config', 'pm-kit', 'pointer'), self.pm + '\n')
        temp_dir = os.path.join(self.base, 'kit-temp')     # absolute as this platform spells one
        write(os.path.join(self.pm, user.DIR_NAME, user.FILE_NAME),
              json.dumps({'temp_dir': temp_dir}))
        p = pointer.read(raw=pointer.registry())
        self.assertEqual(p.state, pointer.OK, p)
        self.assertEqual(p.path, self.pm)
        loc = user.locate(self.home, ptr=p)
        self.assertEqual(loc.state, user.OK, loc)
        self.assertEqual(user.user_config(self.home, location=loc)['temp_dir'], temp_dir)

    def test_a_pointer_file_naming_no_repository_is_bad(self):
        missing = os.path.join(self.base, 'gone')
        write(os.path.join(self.profile, '.config', 'pm-kit', 'pointer'), missing)
        p = pointer.read(raw=pointer.registry())
        self.assertEqual((p.state, p.error), (pointer.BAD, 'no folder at %s' % missing))
        self.assertEqual(user.locate(self.home, ptr=p).state, user.BAD)


class SystemTemp(unittest.TestCase):
    def test_linux_s_system_temp_is_tmp_whatever_tmpdir_says(self):
        with mock.patch.dict(os.environ, {'TMPDIR': '/var/tmp/mine'}), \
                mock.patch.object(tempfile, 'gettempdir', return_value='/var/tmp/mine'):
            self.assertEqual(user.system_temp({}, 'linux'), '/tmp')
            with mock.patch.object(kitlib.platform, 'NAME', 'linux'):
                self.assertEqual(user.system_temp({}), '/tmp')

    def test_an_unsupported_platform_keeps_the_process_temp(self):
        with mock.patch.object(tempfile, 'gettempdir', return_value='/var/tmp/x'):
            self.assertEqual(user.system_temp({}, 'darwin'), '/var/tmp/x')

    def test_the_running_platform_is_the_seam_s(self):
        with mock.patch.object(kitlib.platform, 'NAME', 'windows'):
            self.assertEqual(user.system_temp({'LOCALAPPDATA': r'C:\lad'}), os.path.join(r'C:\lad', 'Temp'))
        with mock.patch.object(kitlib.platform, 'NAME', 'linux'):
            self.assertEqual(user.work_temp('/srv/repo', home=temp_home(self), environ={}), '/tmp')
            self.assertEqual(user.default_work_temp('/srv/repo', environ={}), '/tmp')


if __name__ == '__main__':
    unittest.main()
