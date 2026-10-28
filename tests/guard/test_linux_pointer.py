"""The Linux user pointer file under the write jail (docs/guard.md "Write jail", "No root on a
machine config folder"): <profile>/.config/pm-kit/pointer names the PM repository the kit reads its
machine config from (kitlib.platform.linux.paths.read_pointer), and the user can write it, so an
agent session could repoint the kit. No write root may be on, hold or lie in it (jail.touches_config,
jail.user_pointer_file), so a file tool or shell write to it is refused (write-jail) wherever the
session runs. The profile folder is the password database's (paths._pw_dir, stood in here), never
$HOME. Read through tests/guard/linux.py, so it runs on Windows and on Linux alike."""
import posixpath
from unittest import mock

from kitlib.guard import context, jail
from kitlib.platform.linux import paths as linux_paths

from .claudehost import decide
from .test_linux_paths import LinuxCase
from .world import event


class UserPointer(LinuxCase):

    def setUp(self):
        LinuxCase.setUp(self)
        self.profile = self.w.home
        self.folder = posixpath.join(self.profile, *linux_paths.USER_POINTER[:-1])
        self.pointer = posixpath.join(self.folder, linux_paths.USER_POINTER[-1])
        self.notes = self.folder + '-notes'
        self.dirs = self.dirs + (self.folder, self.notes, posixpath.dirname(self.folder))
        p = mock.patch.object(linux_paths, '_pw_dir', lambda: self.profile)
        p.start()
        self.addCleanup(p.stop)

    def write(self, tool, target, cwd, sub=True, env=None):
        key = {'file_path': target, 'content': '/srv/other\n', 'old_string': 'a', 'new_string': 'b'}
        return decide(event(tool, key, cwd, sub), self.w.ctx(environ=env or {}))

    def jailed(self, d, what):
        self.assertIsNotNone(d, '%s was allowed' % what)
        self.assertEqual((d.verdict, d.rule), ('deny', 'write-jail'), '%s: %s' % (what, d.reason))

    def test_the_pointer_file_is_the_kits(self):
        self.assertEqual(jail.user_pointer_file(), self.pointer)

    def test_a_file_tool_write_from_the_pointers_folder_is_refused(self):
        for tool in ('Write', 'Edit', 'MultiEdit'):
            with self.subTest(tool):
                self.jailed(self.write(tool, self.pointer, self.folder), tool)
                self.jailed(self.write(tool, 'pointer', self.folder), tool + ' relative')

    def test_a_shell_redirect_from_the_pointers_folder_is_refused(self):
        for command in ('echo /srv/other > pointer', 'echo /srv/other > %s' % self.pointer,
                        'printf x | tee pointer', 'cp /srv/x pointer'):
            with self.subTest(command):
                self.jailed(self.sub(command, cwd=self.folder), command)

    def test_a_marked_session_there_is_refused_too(self):
        pm = {context.ROLE_ENV: 'pm'}
        self.jailed(self.write('Write', self.pointer, self.folder, sub=False, env=pm), 'pm Write')

    def test_a_temp_root_over_the_pointer_opens_nothing(self):
        env = {'AGENT_TEMP': posixpath.dirname(self.folder)}
        self.jailed(self.write('Write', self.pointer, self.w.wt, env=env), 'Write under AGENT_TEMP')
        self.assertIsNone(self.write('Write', self.w.wt + '/f', self.w.wt, env=env))

    def test_the_password_databases_profile_decides_not_home(self):
        self.profile = '/srv/profiles/p'
        pointer = '/srv/profiles/p/.config/pm-kit/pointer'
        self.dirs = self.dirs + ('/srv/profiles/p/.config/pm-kit',)
        self.jailed(self.write('Write', pointer, '/srv/profiles/p/.config/pm-kit'), 'Write')

    def test_a_nearby_folder_is_still_a_root(self):
        self.assertIsNone(self.write('Write', self.notes + '/pointer', self.notes))
        self.assertIsNone(self.sub('echo x > pointer', cwd=self.notes))

    def test_with_no_profile_folder_there_is_no_pointer_file(self):
        def none():
            raise KeyError('uid 1000')
        with mock.patch.object(linux_paths, '_pw_dir', none):
            self.assertIsNone(jail.user_pointer_file())
