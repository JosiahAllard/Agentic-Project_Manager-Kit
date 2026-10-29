"""Hand-off and recycle, after-reboot's skips, the retired install-autostart, and launch.py's exit codes."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kitlib.launcher import autostart, handoff, reboot
from kitlib.launcher import host as hostmod
from tests.home import machine_config
from tests.launcher.fakes import FakeHost, Home, role_of, session

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402

ALPHA = {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord', 'state': 's.md', 'account': 1,
         'name': 'alpha-coord-a1'}
HELPER = {'repo': 'alpha', 'agent': False, 'stem': 'alpha-help', 'state': 's.md', 'brief': 'b.md', 'account': 2,
          'name': 'alpha-help-a2'}


class Handoff(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, accounts=('1', '2', '3'), signed_out=('3',))
        self.h.write_state('sessions.json', {'_n': 'note', 'alpha': dict(ALPHA), 'helper': dict(HELPER)})
        self.h.write_state('coordinators.json', {'alpha': 1})
        self.host = FakeHost(self.h.root)

    def test_a_successor_on_another_account_rewrites_both_files(self):
        out = handoff.handoff(self.h.settings, self.host, 'alpha', '2')
        self.assertIn('== alpha : alpha-coord-a1 (account 1) -> alpha-coord-a2 (account 2)', out)
        self.assertEqual(self.h.read_state('sessions.json')['alpha']['name'], 'alpha-coord-a2')
        self.assertEqual(self.h.read_state('sessions.json')['alpha']['account'], 2)
        self.assertEqual(self.h.read_state('coordinators.json'), {'alpha': 2})
        text, = self.host.opened
        self.assertEqual(role_of(text), 'coordinator')
        self.assertIn('You succeed alpha-coord-a1', text)

    def test_recycle_counts_up_and_prints_the_stop_line(self):
        out = handoff.handoff(self.h.settings, self.host, 'alpha', recycle=True)
        self.assertEqual(self.h.read_state('sessions.json')['alpha']['name'], 'alpha-coord-a1-r2')
        self.assertIn("stop --name 'alpha-coord-a1' --successor 'alpha-coord-a1-r2'", out[-1])
        self.assertEqual(handoff.successor_name(dict(ALPHA, name='alpha-coord-a1-r2'), '1', True), 'alpha-coord-a1-r3')

    def test_refusals_change_nothing(self):
        before = self.h.read_state('sessions.json')
        with self.assertRaisesRegex(handoff.LaunchError, 'already on account 1'):
            handoff.handoff(self.h.settings, self.host, 'alpha', '1')
        with self.assertRaisesRegex(handoff.LaunchError, 'Account 3 is not signed in'):
            handoff.handoff(self.h.settings, self.host, 'alpha', '3')
        with self.assertRaisesRegex(handoff.LaunchError, 'account 4 is not in launcher.accounts'):
            handoff.handoff(self.h.settings, self.host, 'alpha', '4')
        self.assertEqual(self.h.read_state('sessions.json'), before)
        self.assertEqual(self.host.opened, [])

    def test_a_stem_less_entry_is_skipped_by_all_and_refused_alone(self):
        sessions = self.h.read_state('sessions.json')
        sessions['odd'] = {'repo': 'alpha', 'agent': True, 'account': 1, 'name': 'odd-a1'}
        self.h.write_state('sessions.json', sessions)
        out = handoff.handoff(self.h.settings, self.host, all_=True, account='2', whatif=True)
        self.assertIn('SKIP odd : no stem, so no successor name', out)
        self.assertIn('Would set coordinators.json alpha = 2', out)
        with self.assertRaisesRegex(handoff.LaunchError, "entry 'odd' has no stem"):
            handoff.handoff(self.h.settings, self.host, 'odd', '2')

    def test_all_skips_the_account_an_entry_is_on_and_what_if_writes_nothing(self):
        out = handoff.handoff(self.h.settings, self.host, all_=True, account='2', whatif=True)
        self.assertIn('SKIP helper : already on account 2', out)
        self.assertIn('Would set coordinators.json alpha = 2', out)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.h.read_state('sessions.json')['alpha']['account'], 1)


class AfterReboot(unittest.TestCase):
    def test_skips_running_stems_autostart_off_and_a_running_desktop_app(self):
        h = Home(self)
        h.write_state('sessions.json', {'alpha': dict(ALPHA, agent=False), 'helper': dict(HELPER, autostart=False)})
        h.write_state('coordinators.json', {'pm_session_id': 's1', 'pm_folder': h.root, 'pm_account': 1})
        app = {'Name': 'claude.exe', 'ProcessId': 5, 'ParentProcessId': 1,
               'ExecutablePath': 'C:' + chr(92) + 'WindowsApps' + chr(92) + 'claude.exe', 'CommandLine': None}
        host = FakeHost(h.root, [session(9, 'alpha-coord-a2-r2'), app])
        out = reboot.run(h.settings, host)
        self.assertEqual(host.opened, [])
        self.assertIn('alpha already running, skipped', out)
        self.assertIn('helper autostart off, skipped', out)
        self.assertIn('desktop app running, pm-session skipped', out)
        with open(os.path.join(h.state, 'after-reboot.log'), encoding='utf-8') as f:
            self.assertIn('desktop app running', f.read())

    def test_dry_run_starts_and_writes_nothing(self):
        h = Home(self)
        h.write_state('sessions.json', {'alpha': dict(ALPHA, agent=False)})
        host = FakeHost(h.root)
        out = reboot.run(h.settings, host, dry_run=True)
        self.assertTrue(any(line.startswith('would start alpha-coord-a1 (alpha)') for line in out))
        self.assertEqual((host.opened, host.slept), ([], []))
        self.assertFalse(os.path.exists(os.path.join(h.state, 'after-reboot.log')))


class Autostart(unittest.TestCase):
    """install-autostart is retired: the hub restarts agents. It registers nothing and runs nothing."""

    def test_install_refuses_and_names_the_task_to_remove(self):
        for whatif in (False, True):
            with self.assertRaises(autostart.LaunchError) as cm:
                autostart.install('/kit/tools/launch.py', whatif=whatif)
            text = str(cm.exception)
            self.assertIn('install-autostart is retired', text)
            self.assertIn('nothing registered', text)
            self.assertIn("Unregister-ScheduledTask -TaskName 'Claude coordinators after reboot'", text)
        self.assertFalse(hasattr(autostart, 'task_script'))
        self.assertFalse(hasattr(autostart, 'linux_install'))

    def test_the_names_a_leftover_task_is_known_by_stay(self):
        self.assertEqual(autostart.TASK_NAME, 'Claude coordinators after reboot')
        self.assertEqual(autostart.LINUX_JOB, 'coordinators-after-reboot')

    def test_a_worktree_path_is_known(self):
        sep = chr(92)
        for path in ('/kit/.claude/worktrees/x/tools/launch.py',
                     sep.join(['D:', 'kit', '.Claude', 'Worktrees', 'x', 'tools', 'launch.py'])):
            self.assertTrue(autostart.in_worktree(path), path)
        self.assertFalse(autostart.in_worktree('/kit/tools/launch.py'))
        self.assertFalse(autostart.in_worktree('/kit/worktrees/.claude/launch.py'))


class UserEnv(unittest.TestCase):
    def test_an_expand_sz_value_is_expanded_and_others_kept(self):
        def expand(v):
            return v.replace('%USERPROFILE%', '/home/u')
        self.assertEqual(hostmod.expand_value('%USERPROFILE%/t', True, expand), '/home/u/t')
        self.assertEqual(hostmod.expand_value('%USERPROFILE%/t', False, expand), '%USERPROFILE%/t')
        self.assertEqual(hostmod.expand_value(5, True, expand), 5)


class Cli(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        machine_config(self.h.root, {'launcher': self.h.obj}, case=self)

    def main(self, argv, procs=(), host=None):
        host = host or FakeHost(self.h.root, procs)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = launch.main(argv, host)
        return code, out.getvalue(), err.getvalue(), host

    def test_start_what_if_exits_0_with_the_marked_script(self):
        code, out, _, host = self.main(['start', '--repo', 'alpha', '--account', '1', '--what-if'])
        self.assertEqual(code, 0)
        self.assertEqual(role_of(out), 'coordinator')
        self.assertEqual(host.opened, [])

    def test_a_refused_start_exits_1(self):
        code, _, err, _ = self.main(['start', '--repo', 'nope', '--account', '1'])
        self.assertEqual(code, 1)
        self.assertIn("launch.py start: repo 'nope' is not in launcher.repos", err)

    def test_a_stop_whose_log_write_fails_exits_0_and_says_so_on_stderr(self):
        from tests.launcher.test_lookup_stop import windowed
        os.rmdir(self.h.state)
        open(self.h.state, 'w').close()                  # the state folder is a file: the log write fails
        code, out, err, host = self.main(['stop', '--name', 'x-a1'], windowed('x-a1'))
        self.assertEqual(code, 0)
        self.assertEqual(len(host.killed), 3)
        self.assertIn("stopped 'x-a1'", out)
        self.assertNotIn('could not write', out)
        self.assertIn('launch.py stop: could not write stop.log', err)

    def test_a_refused_stop_exits_2(self):
        code, out, _, _ = self.main(['stop', '--name', 'gone'])
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: no claude.exe with --remote-control exactly 'gone'", out)

    def test_pick_account_prints_the_number_last(self):
        code, out, _, _ = self.main(['pick-account', '--explain'])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], '1')
        self.assertEqual(len(out.splitlines()), 3)

    def test_a_bad_config_exits_1(self):
        machine_config(self.h.root, {'launcher': {'state_dir': 'relative'}}, case=self)
        code, _, err, _ = self.main(['usage'])
        self.assertEqual(code, 1)
        self.assertIn('launcher.state_dir must be an absolute path', err)

    def test_a_window_timeout_exits_1(self):
        class Slow(FakeHost):
            def open_window(self, text):
                raise subprocess.TimeoutExpired(['powershell'], 60)
        code, _, err, _ = self.main(['start', '--repo', 'alpha', '--account', '1'], host=Slow(self.h.root))
        self.assertEqual(code, 1)
        self.assertIn('launch.py start: timed out:', err)

    def test_a_process_list_that_is_not_json_exits_1(self):
        class Garbled(FakeHost):
            def processes(self):
                return json.loads('not json')
        code, _, err, _ = self.main(['stop', '--name', 'x'], host=Garbled(self.h.root))
        self.assertEqual(code, 1)
        self.assertIn('launch.py stop: JSONDecodeError:', err)

    def test_start_defaults_to_a_200k_compact_window_and_takes_an_override(self):
        _, out, _, _ = self.main(['start', '--repo', 'alpha', '--account', '1', '--what-if'])
        self.assertIn('--autocompact 200000 ', out)
        _, out, _, _ = self.main(['start', '--repo', 'alpha', '--account', '1', '--no-agent', '--what-if'])
        self.assertIn('--autocompact 200000 ', out)
        _, out, _, _ = self.main(['start', '--repo', 'alpha', '--account', '1', '--autocompact', '250000',
                                  '--what-if'])
        self.assertIn('--autocompact 250000 ', out)
        _, out, _, _ = self.main(['start', '--repo', 'alpha', '--account', '1', '--autocompact', '0', '--what-if'])
        self.assertNotIn('--autocompact', out)
        code, _, err, _ = self.main(['start', '--repo', 'alpha', '--account', '1', '--autocompact', '50000'])
        self.assertEqual(code, 1)
        self.assertIn('100000..1000000', err)

    def test_session_refuses_the_flags_its_entry_sets(self):
        self.h.write_state('sessions.json', {'rev': dict(ALPHA)})
        base = ['start', '--session', 'rev', '--what-if']
        for extra in (['--org', '1'], ['--brief', 'b.md'], ['--name', 'n'], ['--agent', 'reviewer'],
                      ['--no-agent'], ['--model', 'opus'], ['--effort', 'high'], ['--autocompact', '0'],
                      ['--predecessor', 'old']):
            code, out, err, _ = self.main(base + extra)
            self.assertEqual(code, 1, extra)
            self.assertIn('drop %s' % extra[0], err)
            self.assertEqual(out, '')
        code, out, _, _ = self.main(base + ['--account', '2', '--prompt', 'go'])
        self.assertEqual(code, 0)
        self.assertIn("-n 'alpha-coord-a2'", out)

    def test_install_autostart_is_retired_and_exits_1(self):
        def never(*a, **k):
            raise AssertionError('install-autostart ran a command')
        with mock.patch('subprocess.run', never), mock.patch('kitlib.proc.spawn.run', never):
            for argv in (['install-autostart', '--what-if'], ['install-autostart']):
                with self.subTest(argv=argv):
                    code, out, err, _ = self.main(argv)
                    self.assertEqual(code, 1)
                    self.assertIn('launch.py install-autostart: install-autostart is retired', err)
                    self.assertIn('nothing registered', err)
                    self.assertEqual(out, '')

    def test_usage_errors_exit_2(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                launch.main(['start', '--bogus'], FakeHost(self.h.root))
        self.assertEqual(e.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
