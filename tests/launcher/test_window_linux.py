"""The launched session's window on Linux (docs/launcher.md "The window"; docs/linux-support-plan.md
slice 11): bash in a detached tmux session, or in launcher.terminal's command. kitlib.platform.NAME
is patched to 'linux' and tmux and the terminal are fakes, so this runs on Windows and starts
nothing. A tmux name tmux already has, and two names made the same tmux name, are refused with
nothing started or ended; Windows's window text is the same as before."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from kitlib.launcher import host as H
from kitlib.launcher import linuxwin, reboot, start, window
from kitlib.launcher import settings as S
from tests.launcher.fakes import FakeHost, Home, role_of
from tests.launcher.test_reboot import PM
from tests.launcher.test_start import Base


class FakeTmux(object):
    """tmux new-session as a list: a name it has is a duplicate (exit 1), as tmux says it."""

    def __init__(self, sessions=()):
        self.sessions = set(sessions)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        name = argv[argv.index('-s') + 1]
        if name in self.sessions:
            return subprocess.CompletedProcess(argv, 1, '', 'duplicate session: %s\n' % name)
        self.sessions.add(name)
        return subprocess.CompletedProcess(argv, 0, '', '')


class Linux(unittest.TestCase):
    def setUp(self):
        patch = mock.patch('kitlib.platform.NAME', 'linux')
        patch.start()
        self.addCleanup(patch.stop)

    def text(self, name='alpha-coordinator-a1', config_dir='/home/u/.claude-a1', temp='/t/agent'):
        cmd = window.claude_command(name, "it's go", 'pm-kit:coordinator')
        return window.script(name, 'coordinator', "/r/it's", cmd, config_dir, temp)

    def test_script_is_bash_one_step_a_line_in_the_windows_order(self):
        lines = self.text().split('\n')
        self.assertEqual(lines[0], "printf '\\033]0;%s\\007' 'alpha-coordinator-a1'")
        self.assertEqual(lines[1:5], ['for v in "${!CLAUDE@}" "${!ANTHROPIC@}"; do unset "$v"; done',
                                      'export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1',
                                      "export PM_KIT_ROLE='coordinator'",
                                      "export PM_KIT_SESSION_NAME='alpha-coordinator-a1'"])
        self.assertEqual(lines[5], "export CLAUDE_CONFIG_DIR='/home/u/.claude-a1'")
        self.assertEqual(lines[6:8], ["mkdir -p -- '/t/agent'", "export AGENT_TEMP='/t/agent' "
                                      "TMPDIR='/t/agent' TEMP='/t/agent' TMP='/t/agent'"])
        self.assertEqual(lines[8], "cd -- '/r/it'\\''s' && claude --agent pm-kit:coordinator "
                         "--permission-mode auto --remote-control 'alpha-coordinator-a1' "
                         "-n 'alpha-coordinator-a1' 'it'\\''s go'")
        self.assertEqual(lines[9], 'exec /bin/bash -i')
        self.assertEqual(len(lines), 10)

    def test_the_default_folder_unsets_the_config_dir_and_no_temp_sets_none(self):
        text = window.script('n', 'worker', '/r', 'claude', None, None)
        self.assertIn('unset CLAUDE_CONFIG_DIR', text.split('\n'))
        self.assertNotIn('AGENT_TEMP', text)

    def test_no_role_is_refused(self):
        with self.assertRaises(ValueError):
            window.script('n', '', '/r', 'claude')

    def test_q_is_bash_single_quoting(self):
        self.assertEqual(window.q("a'b c"), "'a'\\''b c'")
        self.assertEqual(window.q(''), "''")

    def test_the_tmux_name_keeps_only_letters_digits_dash_and_underscore(self):
        self.assertEqual(linuxwin.tmux_name('alpha-coordinator_a1'), 'alpha-coordinator_a1')
        self.assertEqual(linuxwin.tmux_name('a.b:c d'), 'a_b_c_d')

    def test_opens_a_detached_tmux_session_running_the_script_as_a_list(self):
        tmux, text = FakeTmux(), self.text()
        linuxwin.open_window(text, 'alpha.x', run=tmux)
        (argv, kw), = tmux.calls
        self.assertEqual(argv, ['tmux', 'new-session', '-d', '-s', 'alpha_x', '/bin/bash', '-c', text])
        self.assertNotIn('shell', kw)

    def test_a_tmux_name_already_there_is_refused_and_nothing_else_is_run(self):
        tmux = FakeTmux({'alpha-coordinator-a1'})
        with self.assertRaisesRegex(S.LaunchError, "tmux already has a session 'alpha-coordinator-a1'"):
            linuxwin.open_window(self.text(), 'alpha-coordinator-a1', run=tmux)
        self.assertEqual(len(tmux.calls), 1)
        self.assertEqual(tmux.calls[0][0][:2], ['tmux', 'new-session'])

    def test_two_names_made_the_same_tmux_name_collide_the_same_way(self):
        tmux = FakeTmux()
        linuxwin.open_window(self.text('a.b'), 'a.b', run=tmux)
        with self.assertRaisesRegex(S.LaunchError, "tmux already has a session 'a_b'"):
            linuxwin.open_window(self.text('a_b'), 'a_b', run=tmux)
        self.assertEqual([c[0][1] for c in tmux.calls], ['new-session', 'new-session'])

    def test_another_tmux_failure_or_no_tmux_is_a_runtime_error(self):
        def failed(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, '', 'no server running\n')

        def missing(argv, **kw):
            raise FileNotFoundError('tmux')
        with self.assertRaisesRegex(RuntimeError, r'\(tmux exit 1\): no server running'):
            linuxwin.open_window('x', 'n', run=failed)
        with self.assertRaisesRegex(RuntimeError, 'launcher.terminal'):
            linuxwin.open_window('x', 'n', run=missing)

    def test_a_terminal_runs_the_script_in_its_own_session(self):
        got = []
        linuxwin.open_window('TEXT', 'n', terminal=['xterm', '-e'], popen=lambda a, **kw: got.append((a, kw)))
        (argv, kw), = got
        self.assertEqual(argv, ['xterm', '-e', '/bin/bash', '-c', 'TEXT'])
        self.assertTrue(kw['start_new_session'])
        self.assertEqual(kw['stdin'], subprocess.DEVNULL)

    def test_window_open_window_goes_to_linuxwin(self):
        with mock.patch.object(linuxwin, 'open_window') as got:
            window.open_window('TEXT', name='n', terminal=None)
        got.assert_called_once_with('TEXT', 'n', None)

    def test_carries_needs_the_exact_quoted_name_in_both_lines(self):
        cmd = '/bin/bash -c ' + self.text('pm')
        self.assertTrue(linuxwin.carries(cmd, 'pm'))
        self.assertFalse(linuxwin.carries('/bin/bash -c ' + self.text('pm-session'), 'pm'))
        self.assertFalse(linuxwin.carries(cmd, 'p'))
        self.assertFalse(linuxwin.carries(cmd.replace("export PM_KIT_SESSION_NAME='pm'",
                                                      "export PM_KIT_SESSION_NAME='px'"), 'pm'))
        self.assertFalse(linuxwin.carries(cmd.replace("-n 'pm' ", "-n 'px' ")
                                          .replace("--remote-control 'pm' ", ''), 'pm'))
        self.assertFalse(linuxwin.carries(self.text('pm'), 'pm'))     # not bash -c
        self.assertFalse(linuxwin.carries('/bin/bash -i', 'pm'))


class LinuxStart(Base):
    def setUp(self):
        Base.setUp(self)
        patch = mock.patch('kitlib.platform.NAME', 'linux')
        patch.start()
        self.addCleanup(patch.stop)

    def test_start_hands_the_window_its_name_and_says_the_tmux_session(self):
        out = '\n'.join(start.start(self.h.settings, self.host, repo='alpha', account='1',
                                    name='x-coordinator-a1', prompt='go'))
        text, = self.host.opened
        self.assertIn("export PM_KIT_SESSION_NAME='x-coordinator-a1'", text.split('\n'))
        self.assertEqual(self.host.open_kw, [{'name': 'x-coordinator-a1', 'terminal': None}])
        self.assertIn("in tmux session 'x-coordinator-a1' (tmux attach -t 'x-coordinator-a1')", out)

    def test_start_with_a_terminal_hands_it_on(self):
        s = S.Settings(dict(self.h.obj, terminal=['xterm', '-e']), self.h.root)
        out = '\n'.join(start.start(s, self.host, repo='alpha', account='1', name='x-coordinator-a1',
                                    prompt='go'))
        self.assertEqual(self.host.open_kw, [{'name': 'x-coordinator-a1', 'terminal': ['xterm', '-e']}])
        self.assertIn('in a new terminal window (xterm)', out)

    def test_whatif_shows_the_bash_command(self):
        out = '\n'.join(start.start(self.h.settings, self.host, repo='alpha', account='1',
                                    name='x-coordinator-a1', prompt='go', whatif=True))
        self.assertIn("  /bin/bash -c 'printf '\\''\\033]0;%s\\007'\\'' '\\''x-coordinator-a1'\\''", out)
        self.assertEqual(self.host.opened, [])


class LinuxReboot(unittest.TestCase):
    def test_resume_pm_hands_the_window_the_pm_session_name(self):
        h, lines = Home(self), []
        host = FakeHost(h.root, uptime=60)
        with mock.patch('kitlib.platform.NAME', 'linux'):
            reboot.resume_pm(h.settings, host, dict(PM, pm_folder=h.root), lines.append)
        text, = host.opened
        self.assertEqual(role_of(text), 'pm')
        self.assertEqual(host.open_kw, [{'name': 'pm-session', 'terminal': None}])
        self.assertEqual(lines, ['started pm-session'])


class Uptime(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='uptime-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.host = H.Host(self.root, {})

    def test_linux_reads_proc_uptime_in_minutes(self):
        path = os.path.join(self.root, 'uptime')
        with open(path, 'w', encoding='ascii') as f:
            f.write('600.50 1200.00\n')
        with mock.patch('kitlib.platform.NAME', 'linux'), mock.patch.object(H, 'UPTIME', path):
            self.assertAlmostEqual(self.host.uptime_minutes(), 600.5 / 60)

    def test_linux_without_proc_uptime_is_inf(self):
        with mock.patch('kitlib.platform.NAME', 'linux'),                 mock.patch.object(H, 'UPTIME', os.path.join(self.root, 'none')):
            self.assertEqual(self.host.uptime_minutes(), float('inf'))


class Settings(unittest.TestCase):
    def test_terminal_is_a_non_empty_list_of_words(self):
        self.assertEqual(S.check({'terminal': ['xterm', '-e']}), [])
        for bad in ('xterm', [], [''], ['xterm', 3]):
            self.assertTrue(any('launcher.terminal' in e for e in S.check({'terminal': bad})), bad)

    def test_terminal_unset_is_none(self):
        self.assertIsNone(S.Settings({}, '/h').terminal)
        self.assertEqual(S.Settings({'terminal': ['xterm', '-e']}, '/h').terminal, ['xterm', '-e'])


class WindowsUnchanged(Base):
    def test_windows_opens_with_no_keywords_and_says_a_minimized_window(self):
        with mock.patch('kitlib.platform.NAME', 'windows'):
            out = '\n'.join(start.start(self.h.settings, self.host, repo='alpha', account='1',
                                        name='x-coordinator-a1', prompt='go'))
            self.assertEqual(window.q("a'b"), "'a''b'")
            self.assertEqual(window.open_args('n', self.h.settings), {})
        self.assertEqual(self.host.open_kw, [{}])
        self.assertIn('Started x-coordinator-a1 (', out)
        self.assertIn(') in a new minimized window. Remote Control name: x-coordinator-a1', out)
        text, = self.host.opened
        self.assertIn("$env:PM_KIT_ROLE = 'coordinator'", text)


if __name__ == '__main__':
    unittest.main()
