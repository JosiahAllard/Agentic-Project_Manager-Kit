"""The default mode's Linux forms of its Windows-only steps (docs/machine-setup.md "Linux"): the kit
command ~/.local/bin/kit, a leftover link's `rm` line, the user environment in environment.d, the
agents' temp folder's mode 0700 and its no-default note, the desktop entry, the pointer file and the
declined-sudo note; and machine_setup.py --delegation's arguments. The platform is patched and every
write goes to a test folder, so each case runs on any host."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib import machinesetup
from kitlib.config import pointer as cpointer
from kitlib.machinesetup import agenttemp, common, extras, kitcommand, links, settings
from kitlib.machinesetup import pointer as mpointer
from kitlib.machinesetup.common import Plan

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import machine_setup as setup_cli                                   # noqa: E402

LINUX = mock.patch('kitlib.platform.NAME', 'linux')


def notes(plan, section='extras'):
    return plan.notes[section]


class Temp(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)


class KitCommand(Temp):

    def plan(self, path, environ):
        plan = Plan()
        with LINUX:
            kitcommand.linux_steps(plan, '/home/<user>/pm kit', path, environ=environ)
        return plan

    def test_launcher_text(self):
        self.assertEqual(kitcommand.launcher_sh('/home/<user>/pm kit').splitlines(), [
            '#!/bin/sh',
            '# pm-kit: the kit command (kit TOOL [ARGUMENTS...]); written by tools/machine_setup.py '
            '--apply, which rewrites it',
            "exec python3 '/home/<user>/pm kit/tools/kit.py' \"$@\""])

    def test_written_executable_then_nothing_to_do(self):
        path = os.path.join(self.dir, '.local', 'bin', 'kit')
        bin_on_path = {'PATH': os.pathsep.join(['/usr/bin', os.path.dirname(path) + '/'])}
        plan = self.plan(path, bin_on_path)
        self.assertEqual(len(plan.changes), 1)
        self.assertIn('write the kit command %s (mode 755)' % path, plan.changes[0].text)
        self.assertEqual(notes(plan), [])
        with mock.patch.object(kitcommand.os, 'chmod') as chmod:
            plan.changes[0].do()
        chmod.assert_called_once_with(path + '.tmp', 0o755)
        with open(path, newline='') as f:
            self.assertEqual(f.read(), kitcommand.launcher_sh('/home/<user>/pm kit'))
        self.assertEqual(self.plan(path, bin_on_path).changes, [])

    def test_a_file_not_written_by_setup_is_left(self):
        path = os.path.join(self.dir, 'kit')
        with open(path, 'w') as f:
            f.write('#!/bin/sh\necho mine\n')
        plan = self.plan(path, {'PATH': self.dir})
        self.assertEqual(plan.changes, [])
        self.assertIn('was not written by machine setup; left as is', notes(plan)[0])

    def test_a_folder_off_path_is_a_note(self):
        plan = self.plan(os.path.join(self.dir, 'bin', 'kit'), {'PATH': os.pathsep.join(['/usr/bin', '/bin'])})
        self.assertEqual(len(plan.changes), 1)
        self.assertIn('is not on this PATH', notes(plan)[0])

    def test_survey_takes_the_linux_branch_without_the_export(self):
        ctx = SimpleNamespace(kit=ROOT, home=self.dir, host=None)
        place = SimpleNamespace(kind=kitcommand.gitrepo.MAIN, detail='')
        with LINUX, mock.patch.object(kitcommand.worktreesweep, 'tool_root', return_value=ROOT), \
                mock.patch.object(kitcommand.worktreesweep, 'is_export', return_value=False), \
                mock.patch.object(kitcommand.gitrepo, 'classify', return_value=place), \
                mock.patch.object(kitcommand.pluginsource, 'load') as load, \
                mock.patch.object(kitcommand, 'linux_steps') as steps:
            kitcommand.survey(Plan(), ctx)
        load.assert_not_called()
        self.assertEqual(steps.call_args[0][2], os.path.join(self.dir, '.local', 'bin', 'kit'))


class Links(unittest.TestCase):

    def test_remove_cmd_is_rm_on_linux(self):
        with LINUX:
            self.assertEqual(links.remove_cmd("/home/<user>/.claude/skills/it's"),
                             "rm -- '/home/<user>/.claude/skills/it'\"'\"'s'")


class SettingsHooks(unittest.TestCase):
    """The settings step reads a Linux hook's absolute paths and python3: an account settings file
    that already has the guard's entry gets no second one, and the PM SessionStart hook it wrote is
    its own on the next run (not a foreign checklist to refuse)."""

    KIT, HOME = '/opt/pm kit', '/home/<user>'
    SCRIPT = '/opt/pm kit/boot/hook.py'

    def hook(self, *words, interp='/usr/bin/python3'):
        return {'type': 'command', 'command': interp, 'args': [self.SCRIPT, 'claude'] + list(words)}

    def test_a_settings_file_with_the_guard_gets_no_second_one(self):
        data = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [self.hook('PreToolUse')]}]}}
        ctx = SimpleNamespace(kit=self.KIT, home=self.HOME, managed_data=None)
        with LINUX:
            self.assertFalse(settings._wants_guard(Plan(), self.HOME + '/.claude/settings.json', data, ctx))

    def test_the_pm_hook_reads_as_the_kit_entry_and_its_words(self):
        want = (common.norm(self.SCRIPT), ('claude', 'SessionStart', 'pm'))
        with LINUX:
            for interp in ('/usr/bin/python3', 'python3', '/usr/bin/python3.12', '/usr/bin/python'):
                with self.subTest(interp=interp):
                    self.assertEqual(settings._signature(self.hook('SessionStart', 'pm', interp=interp), self.HOME),
                                     want)
            self.assertEqual(settings.expand('~/.claude/x.py', self.HOME),
                             os.path.normpath(self.HOME + '/.claude/x.py'))

    def test_a_guard_entry_written_without_on_failure_gets_it(self):
        """A Linux account whose guard entry predates onFailure (matched by path alone, so no second
        entry is added) gets it on a rerun; another hook and an onFailure already set are kept."""
        other = {'type': 'command', 'command': '/usr/bin/python3', 'args': ['/opt/other/x.py']}
        kept = dict(self.hook('PreToolUse'), onFailure='allow')
        data = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [self.hook('PreToolUse'), other]},
                                         {'matcher': 'Read', 'hooks': [kept]}]}}
        ctx = SimpleNamespace(kit=self.KIT, home=self.HOME, managed_data=None)
        did = []
        with LINUX:
            settings._guard_on_failure(data, ctx, did)
        first, second = data['hooks']['PreToolUse']
        self.assertEqual(first['hooks'][0]['onFailure'], 'block')
        self.assertNotIn('onFailure', first['hooks'][1])
        self.assertEqual(second['hooks'][0]['onFailure'], 'allow')
        self.assertEqual(did, ['give the guard\'s PreToolUse entry onFailure "block"'])
        did = []
        with LINUX:
            settings._guard_on_failure(data, ctx, did)
        self.assertEqual(did, [])

    def test_windows_gives_the_guard_entry_no_on_failure(self):
        data = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': 'C:/Py/pythonw.exe', 'args': ['C:/kit/boot/hook.py', 'claude', 'PreToolUse']}]}]}}
        ctx = SimpleNamespace(kit='C:\\kit', home='C:\\home\\kit', managed_data=None)
        did = []
        with mock.patch('kitlib.platform.NAME', 'windows'):
            settings._guard_on_failure(data, ctx, did)
        self.assertEqual(did, [])
        self.assertNotIn('onFailure', data['hooks']['PreToolUse'][0]['hooks'][0])

    def test_a_posix_path_is_no_path_on_windows(self):
        with mock.patch('kitlib.platform.NAME', 'windows'):
            self.assertEqual(settings.expand(self.SCRIPT, self.HOME), '')


class Environment(Temp):

    def test_round_trip_keeps_other_lines(self):
        path = os.path.join(self.dir, 'environment.d', 'pm-kit.conf')
        self.assertIsNone(extras.env_file_get(path, 'AGENT_TEMP'))
        extras.env_file_set(path, 'AGENT_TEMP', '/srv/agent-temp')
        with open(path, 'a') as f:
            f.write('# a comment\nOTHER=1\n')
        extras.env_file_set(path, 'AGENT_TEMP', '/srv/t2')
        extras.env_file_set(path, 'KIT_MACHINE_LOCK', '/k/tools/machine_lock.py')
        self.assertEqual(extras.env_file_get(path, 'AGENT_TEMP'), '/srv/t2')
        with open(path, newline='') as f:
            self.assertEqual(f.read(), 'AGENT_TEMP=/srv/t2\n# a comment\nOTHER=1\n'
                                       'KIT_MACHINE_LOCK=/k/tools/machine_lock.py\n')
        with self.assertRaises(ValueError):
            extras.env_file_set(path, 'AGENT_TEMP', 'a\nEVIL=1')

    def test_host_reads_and_writes_the_file_on_linux(self):
        path = os.path.join(self.dir, 'pm-kit.conf')
        with LINUX, mock.patch.object(extras, 'env_file', return_value=path):
            extras.Host().env_set('AGENT_TEMP', '/srv/t')
            self.assertEqual(extras.Host().env_get('AGENT_TEMP'), '/srv/t')

    def test_undo_text_per_platform(self):
        with LINUX:
            self.assertEqual(extras.env_undo('AGENT_TEMP', None),
                             'delete the AGENT_TEMP line in ~/.config/environment.d/pm-kit.conf')
            self.assertEqual(extras.env_undo('AGENT_TEMP', '/old'),
                             'set AGENT_TEMP=/old in ~/.config/environment.d/pm-kit.conf')
        with mock.patch('kitlib.platform.NAME', 'windows'):
            self.assertEqual(extras.env_undo('AGENT_TEMP', None),
                             '[Environment]::SetEnvironmentVariable("AGENT_TEMP", $null, "User")')


class AgentTemp(Temp):

    def test_no_default_folder_on_linux_is_a_note(self):
        plan = Plan()
        ctx = SimpleNamespace(kit='/home/<user>/pm-kit', home=self.dir, agent_temp=None)
        with LINUX, mock.patch.object(agenttemp, 'choose', return_value=(None, None)):
            self.assertEqual(agenttemp.survey(plan, ctx), (None, False))
        self.assertIn('on Linux there is no default folder', notes(plan)[0])

    def test_folder_is_made_private(self):
        plan = Plan()
        with LINUX:
            extras._acl(plan, SimpleNamespace(host=None), self.dir)
        self.assertEqual(len(plan.changes), 1)
        self.assertIn('mode 700, yours only: chmod 700', plan.changes[0].text)
        self.assertIn('chmod ', plan.changes[0].undo)
        with mock.patch.object(extras.os, 'chmod') as chmod:
            plan.changes[0].do()
        chmod.assert_called_once_with(self.dir, 0o700)

    def test_a_private_folder_has_nothing_to_do(self):
        plan = Plan()
        with LINUX, mock.patch.object(extras.os, 'stat', return_value=SimpleNamespace(st_mode=0o40700)):
            extras._acl(plan, SimpleNamespace(host=None), self.dir)
        self.assertEqual(plan.changes, [])

    def test_a_folder_this_run_makes_has_no_restore(self):
        plan = Plan()
        with LINUX:
            extras._acl(plan, SimpleNamespace(host=None), os.path.join(self.dir, 'new'), made=True)
        self.assertIn('(made by this run)', plan.changes[0].text)
        self.assertIsNone(plan.changes[0].undo)


class DesktopEntry(Temp):

    def test_written_then_nothing_to_do(self):
        ctx = SimpleNamespace(home=self.dir, kit='/home/<user>/pm-kit', shortcut=True)
        plan = Plan()
        with LINUX:
            extras.desktop_entry(plan, ctx)
        self.assertEqual(len(plan.changes), 1)
        path = os.path.join(self.dir, *extras.DESKTOP_FILE)
        self.assertIn('write the desktop entry %s' % path, plan.changes[0].text)
        plan.changes[0].do()
        with open(path) as f:
            text = f.read()
        self.assertTrue(text.startswith('[Desktop Entry]\nType=Application\n'), text)
        self.assertIn('reserve_ui.pyw"', text)
        again = Plan()
        extras.desktop_entry(again, ctx)
        self.assertEqual(again.changes, [])


class PointerFile(Temp):

    def test_write_file(self):
        path = os.path.join(self.dir, 'etc', 'pm-kit', 'pointer')
        mpointer.write_file(path, '/srv/pm')
        with open(path, newline='') as f:
            self.assertEqual(f.read(), '/srv/pm\n')

    def test_made_folders_are_0755_whatever_the_umask(self):
        # Root's umask (027, 077) would leave /etc/pm-kit unreadable to the kit user.
        path = os.path.join(self.dir, 'etc', 'pm-kit', 'pointer')
        real = os.chmod
        with mock.patch.object(mpointer.os, 'chmod', side_effect=real) as chmod:
            mpointer.write_file(path, '/srv/pm')
        folders = [c[0] for c in chmod.call_args_list if c[0][0] != path + '.tmp']
        self.assertEqual(folders, [(os.path.join(self.dir, 'etc', 'pm-kit'), 0o755),
                                   (os.path.join(self.dir, 'etc'), 0o755)])
        with mock.patch.object(mpointer.os, 'chmod', side_effect=real) as chmod:
            mpointer.write_file(path, '/srv/pm2')
        self.assertEqual([c[0] for c in chmod.call_args_list], [(path + '.tmp', 0o644)])

    def test_the_registry_writer_writes_the_file_on_linux(self):
        with LINUX, mock.patch.object(mpointer.user, 'fenced', return_value=False), \
                mock.patch.object(mpointer, 'write_file') as write:
            mpointer.Registry().write('/srv/pm')
        write.assert_called_once_with('/etc/pm-kit/pointer', '/srv/pm')

    def test_declining_sudo_names_the_user_pointer_line(self):
        class Host(object):
            def is_admin(self):
                return False

        class Reg(object):
            def read(self):
                return None, None

        with LINUX, mock.patch.object(cpointer, 'check', return_value=None):
            plan = mpointer.survey(mpointer.PointerContext(self.dir, ROOT, host=Host(), registry=Reg()))
        got = ' | '.join(notes(plan, mpointer.SECTION))
        self.assertIn('declining sudo: with no /etc/pm-kit/pointer', got)
        self.assertIn("mkdir -p ~/.config/pm-kit && printf '%s\\n' ", got)
        self.assertIn('> ~/.config/pm-kit/pointer', got)


class DelegationArguments(unittest.TestCase):

    def main(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(machinesetup, 'run_delegation', return_value=(0, [])) as run:
            code = setup_cli.main(argv)
        return code, err.getvalue(), run

    def test_delegation_runs_its_mode(self):
        code, _err, run = self.main(['--delegation', '--kit', ROOT, '--apply'])
        self.assertEqual(code, 0)
        ctx = run.call_args[0][0]
        self.assertEqual(ctx.kit, os.path.abspath(ROOT))
        self.assertTrue(run.call_args[1]['apply'])

    def test_delegation_takes_no_other_option(self):
        code, err, run = self.main(['--delegation', '--kit', ROOT, '--agent-temp', '/srv/t'])
        self.assertEqual(code, machinesetup.EXIT_USAGE)
        self.assertIn('--agent-temp does not go with --delegation', err)
        run.assert_not_called()

    def test_delegation_is_a_mode_of_its_own(self):
        code, _err, run = self.main(['--delegation', '--pm-repo', '/srv/pm', '--kit', ROOT])
        self.assertEqual(code, machinesetup.EXIT_USAGE)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
