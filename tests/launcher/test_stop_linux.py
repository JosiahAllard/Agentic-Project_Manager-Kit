"""Stop on Linux (docs/launcher.md "Stop"; docs/linux-support-plan.md slice 11): the claude process,
then its window, the bash that runs the window's script, only when that script carries exactly the
name (the PM_KIT_SESSION_NAME line and the claude line); each by pid and creation time; tmux or a
terminal above the shell is never stopped. kitlib.platform.NAME is patched to 'linux' and the
process list is a fake, so this runs on Windows and stops nothing."""
import unittest
from unittest import mock

from kitlib.launcher import lookup, stop, window
from tests.launcher.fakes import FakeHost, Home

TMUX = {'Name': 'tmux: server', 'ProcessId': 30, 'ParentProcessId': 1,
        'CommandLine': 'tmux new-session -d -s x'}


def claude(pid, name, parent, image='claude'):
    """A claude row as /proc gives it: the arguments joined by spaces, unquoted."""
    return {'Name': image, 'ProcessId': pid, 'ParentProcessId': parent, 'ExecutablePath': '/usr/bin/claude',
            'CommandLine': 'claude --agent pm-kit:coordinator --permission-mode auto '
                           '--remote-control %s -n %s go' % (name, name)}


def shell(pid, name, parent=30, rc=True):
    """The window's bash for `name`: /bin/bash -c and the script, as /proc gives it."""
    text = window.script(name, 'coordinator', '/r', window.claude_command(name, 'go', 'pm-kit:coordinator',
                                                                         remote_control=rc))
    return {'Name': 'bash', 'ProcessId': pid, 'ParentProcessId': parent, 'ExecutablePath': '/usr/bin/bash',
            'CommandLine': '/bin/bash -c ' + text}


class Linux(unittest.TestCase):
    def setUp(self):
        patch = mock.patch('kitlib.platform.NAME', 'linux')
        patch.start()
        self.addCleanup(patch.stop)
        self.h = Home(self)

    def stopped(self, procs, name, **kw):
        host = FakeHost(self.h.root, procs)
        return stop.stop(self.h.settings, host, name, **kw), host

    def test_the_claude_then_its_window_shell_by_pid_and_ctime_and_never_tmux(self):
        said, host = self.stopped([claude(10, 'x-a1', 20), shell(20, 'x-a1'), TMUX], 'x-a1')
        self.assertEqual(host.killed, [(10, 1010), (20, 1020)])
        self.assertIn("claude PID 10, window PID 20, conhost PID -", said[0])

    def test_a_session_started_without_remote_control_has_its_window_found_by_n(self):
        row = claude(10, 'x-a1', 20)
        row['CommandLine'] = 'claude --agent pm-kit:coordinator -n x-a1 go'
        _said, host = self.stopped([row, shell(20, 'x-a1', rc=False), TMUX], 'x-a1')
        self.assertEqual(host.killed, [(10, 1010), (20, 1020)])

    def test_a_node_claude_is_a_session(self):
        _said, host = self.stopped([claude(10, 'x-a1', 20, image='node'), shell(20, 'x-a1'), TMUX], 'x-a1')
        self.assertEqual(host.killed, [(10, 1010), (20, 1020)])

    def test_a_name_that_is_a_prefix_of_the_window_name_takes_no_window(self):
        """pm vs pm-session: the shell carries 'pm-session', the session is 'pm'."""
        procs = [claude(10, 'pm', 20), shell(20, 'pm-session'), TMUX]
        target, win, con, _succ, lines = stop.plan(procs, 'pm')
        self.assertEqual(target['ProcessId'], 10)
        self.assertIsNone(win)
        self.assertIsNone(con)
        self.assertIn('no window (parent PID 20 bash does not carry the name)', lines[1])
        _said, host = self.stopped(procs, 'pm')
        self.assertEqual(host.killed, [(10, 1010)])

    def test_a_bash_parent_without_the_window_script_is_not_the_window(self):
        plain = {'Name': 'bash', 'ProcessId': 20, 'ParentProcessId': 30,
                 'CommandLine': "/bin/bash -c claude --remote-control 'x-a1' -n 'x-a1' go"}
        _said, host = self.stopped([claude(10, 'x-a1', 20), plain, TMUX], 'x-a1')
        self.assertEqual(host.killed, [(10, 1010)])

    def test_a_parent_that_is_not_bash_is_not_the_window(self):
        row = shell(20, 'x-a1')
        row['Name'] = 'sh'
        _said, host = self.stopped([claude(10, 'x-a1', 20), row, TMUX], 'x-a1')
        self.assertEqual(host.killed, [(10, 1010)])

    def test_a_claude_whose_parent_is_tmux_stops_only_the_claude(self):
        _said, host = self.stopped([claude(10, 'x-a1', 30), TMUX], 'x-a1')
        self.assertEqual(host.killed, [(10, 1010)])

    def test_a_shell_that_no_longer_carries_the_name_at_its_ctime_is_left_alone(self):
        procs = [claude(10, 'x-a1', 20), shell(20, 'x-a1'), TMUX]
        host = FakeHost(self.h.root, procs)
        real = host.command_line

        def reread(pid, ctime):
            return '/bin/bash -i' if int(pid) == 20 else real(pid, ctime)
        host.command_line = reread
        said = stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(host.killed, [(10, 1010)])
        self.assertIn('window PID -', said[0])

    def test_the_shell_is_read_again_before_the_claude_is_stopped(self):
        procs = [claude(10, 'x-a1', 20), shell(20, 'x-a1'), TMUX]
        host = FakeHost(self.h.root, procs)
        order, real_read, real_kill = [], host.command_line, host.terminate

        def read(pid, ctime):
            order.append(('read', int(pid)))
            return real_read(pid, ctime)

        def kill(pid, ctime):
            order.append(('kill', int(pid)))
            return real_kill(pid, ctime)
        host.command_line, host.terminate = read, kill
        stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(order, [('read', 10), ('read', 20), ('kill', 10), ('kill', 20)])

    def test_a_claude_that_could_not_be_stopped_stops_no_window(self):
        host = FakeHost(self.h.root, [claude(10, 'x-a1', 20), shell(20, 'x-a1'), TMUX])
        host.terminate = lambda pid, ct: 'denied'
        with self.assertRaisesRegex(stop.LaunchError, 'could not stop PID 10'):
            stop.stop(self.h.settings, host, 'x-a1')

    def test_refusals_name_the_linux_process(self):
        with self.assertRaisesRegex(stop.Refused, "no claude with --remote-control exactly 'x-a1'"):
            stop.plan([shell(20, 'x-a1'), TMUX], 'x-a1')
        two = [claude(10, 'x-a1', 20), claude(11, 'x-a1', 21), TMUX]
        with self.assertRaisesRegex(stop.Refused, "2 claude match 'x-a1'"):
            stop.plan(two, 'x-a1')
        with self.assertRaisesRegex(stop.Refused, 'PM session'):
            stop.plan([claude(10, 'x', 20)], 'pm-session')

    def test_lookup_takes_the_claude_hosts_linux_images_only(self):
        procs = [claude(1, 'a', 0), claude(2, 'b', 0, image='node'), claude(3, 'c', 0, image='bash'),
                 claude(4, 'd', 0, image='claude.exe')]
        self.assertEqual([p['ProcessId'] for p in lookup.claude_sessions(procs)], [1, 2])


class Windows(unittest.TestCase):
    def test_windows_takes_claude_exe_only_and_says_claude_exe(self):
        with mock.patch('kitlib.platform.NAME', 'windows'):
            self.assertEqual(lookup.claude_sessions([claude(1, 'a', 0)]), [])
            with self.assertRaisesRegex(stop.Refused, "no claude.exe with --remote-control exactly 'a'"):
                stop.plan([claude(1, 'a', 0)], 'a')


if __name__ == '__main__':
    unittest.main()
