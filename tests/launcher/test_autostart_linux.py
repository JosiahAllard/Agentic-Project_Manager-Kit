"""kitlib.launcher.autostart on Linux (docs/linux-support-plan.md slice 10): install-autostart
writes the pm-kit job coordinators-after-reboot, a systemd user service started with the user's
session, else a crontab @reboot line. kitlib.platform.NAME is patched to 'linux' and the job goes
to a temporary home with a fake systemctl and crontab, so this runs on Windows too and installs
nothing real."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.host import registry
from kitlib.launcher import autostart
from kitlib.launcher.settings import LaunchError
from kitlib.platform.linux import schedule
from tests.platform.test_linux_schedule import FakeSystem, which_cron, which_none


def present(path):
    return True


def absent(path):
    return False


class Linux(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='autostart-linux-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.kit = os.path.join(self.home, 'kit')
        self.launch = os.path.join(self.kit, 'tools', 'launch.py')
        self.py = os.path.abspath(sys.executable)
        patch = mock.patch('kitlib.platform.NAME', 'linux')
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, run, whatif=False, which=which_cron, isfile=present):
        return autostart.linux_install(self.launch, self.py, whatif, run=run, isfile=isfile,
                                       home=self.home, which=which)

    def test_install_goes_to_the_linux_job(self):
        with mock.patch.object(autostart, 'linux_install', return_value=['done']) as got:
            self.assertEqual(autostart.install(self.launch, self.py, run='RUN', isfile=present), ['done'])
        got.assert_called_once_with(self.launch, self.py, False, run='RUN', isfile=present)

    def test_install_with_no_run_hands_the_linux_job_none(self):
        """None, not spawn.run: the Linux job then runs its commands without spawn's Windows
        console check."""
        with mock.patch.object(autostart, 'linux_install', return_value=['done']) as got:
            autostart.install(self.launch, self.py, isfile=present)
        self.assertIsNone(got.call_args[1]['run'])

    def test_a_worktree_is_still_refused_first(self):
        folder = sorted(registry.union('worktrees_directory'))[0]
        wt = os.path.join(self.kit, *(folder.split('/') + ['x', 'tools', 'launch.py']))
        with mock.patch.object(autostart, 'linux_install') as got:
            with self.assertRaises(LaunchError):
                autostart.install(wt, self.py)
        got.assert_not_called()

    def test_with_systemd_it_is_a_session_service_enabled_not_started(self):
        run = FakeSystem(linger='yes')
        lines = self.install(run)
        task = schedule.find(autostart.LINUX_JOB, schedule.written(self.home))
        self.assertEqual(task['backend'], schedule.SYSTEMD)
        self.assertEqual(task['argv'], [self.py, os.path.abspath(self.launch), 'after-reboot'])
        self.assertEqual(task['workdir'], self.kit)
        self.assertEqual(task['when'], ('boot',))
        self.assertEqual(run.ran('systemctl', '--user', 'enable'),
                         [['systemctl', '--user', 'enable', 'pm-kit-coordinators-after-reboot.service']])
        text = '\n'.join(lines)
        self.assertIn('after-reboot --test', text)
        self.assertIn('Remove it with: systemctl --user disable', text)
        self.assertNotIn('linger', text)

    def test_linger_off_is_a_note_and_is_never_turned_on(self):
        run = FakeSystem(linger='no')
        with mock.patch.object(schedule, 'linger', lambda run=None, uid=None: False):
            text = '\n'.join(self.install(run))
        self.assertIn(schedule.LINGER_COMMAND, text)
        self.assertFalse([a for a, _ in run.calls if a[0] == 'loginctl' and 'enable-linger' in a])

    def test_without_systemd_it_is_a_crontab_reboot_line(self):
        run = FakeSystem(systemd=False, crontab=None)
        self.install(run)
        self.assertTrue(run.crontab.startswith('@reboot cd '), run.crontab)
        self.assertIn('# pm-kit job: coordinators-after-reboot', run.crontab)

    def test_whatif_writes_nothing(self):
        run = FakeSystem()
        lines = self.install(run, whatif=True)
        self.assertEqual(lines[0], 'Would write:')
        self.assertEqual(os.listdir(self.home), [])
        self.assertEqual(run.ran('systemctl', '--user', 'enable'), [])

    def test_neither_backend_is_refused_naming_both(self):
        with self.assertRaises(LaunchError) as e:
            self.install(FakeSystem(systemd=False), which=which_none)
        self.assertIn('no user systemd', str(e.exception))
        self.assertIn('no crontab', str(e.exception))
        self.assertEqual(os.listdir(self.home), [])

    def test_a_missing_python_is_refused(self):
        with self.assertRaises(LaunchError) as e:
            self.install(FakeSystem(), isfile=absent)
        self.assertIn('no python at', str(e.exception))
        self.assertEqual(os.listdir(self.home), [])


if __name__ == '__main__':
    unittest.main()
