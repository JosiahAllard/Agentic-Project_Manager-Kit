"""Live checks of a Linux session window (docs/linux-support-plan.md slice 11), on Linux only, each
on a private tmux server (TMUX_TMPDIR in a temp folder) that the test ends itself.

ProcessTitle is the first VM check: a real `claude` started in the window still shows its
--remote-control name in /proc/<pid>/cmdline (Claude Code setting process.title would overwrite
it, and lookup and stop could no longer find the session; docs/launcher.md Known limits). It needs
claude and tmux on PATH and PM_KIT_LIVE_CLAUDE=1, since it starts a real Remote Control session
(no prompt is sent).

Window starts a stand-in `claude` (a script of that name that sleeps) through linuxwin, finds it
by name in /proc, stops it with kitlib.launcher.stop, and checks that the claude and its window
shell are gone and that tmux closed the session. It needs tmux."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.platforms import posix_only

NAME = 'kit-live-window-probe'


def wait_for(what, test, timeout=20.0):
    end = time.monotonic() + timeout
    while True:
        got = test()
        if got:
            return got
        if time.monotonic() > end:
            raise AssertionError('not within %g s: %s' % (timeout, what))
        time.sleep(0.1)


@posix_only('a tmux window and /proc are Linux')
class Base(unittest.TestCase):
    def setUp(self):
        if not shutil.which('tmux'):
            self.skipTest('tmux is not installed')
        self.root = tempfile.mkdtemp(prefix='kit-live-')
        self.addCleanup(shutil.rmtree, self.root, True)
        sock = os.path.join(self.root, 'tmux')
        os.makedirs(sock)
        env = mock.patch.dict(os.environ, {'TMUX_TMPDIR': sock})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('TMUX', None)
        self.addCleanup(self.end_server)

    def end_server(self):
        """The test's own private tmux server; never one the user runs."""
        subprocess.run(['tmux', 'kill-server'], capture_output=True, timeout=30)

    def has_session(self, name):
        return subprocess.run(['tmux', 'has-session', '-t', '=' + name], capture_output=True,
                              timeout=30).returncode == 0

    def session(self, name):
        from kitlib.launcher import lookup
        return lookup.find_exact(lookup.claude_sessions(lookup.list_processes()), name)

    def open(self, name, command):
        from kitlib.launcher import linuxwin
        text = linuxwin.script(name, 'worker', self.root, command)
        linuxwin.open_window(text, name)


class ProcessTitle(Base):
    def test_a_real_claude_keeps_its_remote_control_name_in_cmdline(self):
        if not shutil.which('claude') or os.environ.get('PM_KIT_LIVE_CLAUDE') != '1':
            self.skipTest('needs claude on PATH and PM_KIT_LIVE_CLAUDE=1')
        from kitlib.launcher import linuxwin
        q = linuxwin.q(NAME)
        self.open(NAME, 'claude --remote-control %s -n %s' % (q, q))
        found = wait_for('claude listed with --remote-control %s' % NAME, lambda: self.session(NAME), 60)
        self.assertEqual(len(found), 1, found)


class Window(Base):
    def test_open_find_and_stop_a_window_session(self):
        from kitlib.launcher import host as H, linuxwin, stop
        from tests.launcher.fakes import Home
        bin_dir = os.path.join(self.root, 'bin')
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, 'claude')
        with open(fake, 'w', encoding='utf-8', newline='\n') as f:
            f.write('#!%s\nimport time\ntime.sleep(300)\n' % sys.executable)
        os.chmod(fake, 0o755)
        with mock.patch.dict(os.environ, {'PATH': bin_dir + os.pathsep + os.environ.get('PATH', '')}):
            q = linuxwin.q(NAME)
            self.open(NAME, 'claude --remote-control %s -n %s go' % (q, q))
        self.assertTrue(self.has_session(NAME))
        found, = wait_for('the stand-in claude listed', lambda: self.session(NAME))
        shell_pid = int(found['ParentProcessId'])
        home = Home(self)
        said = stop.stop(home.settings, H.Host(home.root, dict(os.environ)), NAME)
        self.assertIn('window PID %d' % shell_pid, said[0])
        wait_for('the claude and its shell gone', lambda: not self.session(NAME)
                 and not os.path.exists('/proc/%d' % shell_pid))
        wait_for('tmux closed the session', lambda: not self.has_session(NAME))

    def test_a_name_tmux_already_has_is_refused_and_the_first_window_stays(self):
        from kitlib.launcher import linuxwin
        from kitlib.launcher.settings import LaunchError
        self.open('kit-live-a.b', 'sleep 300')
        with self.assertRaisesRegex(LaunchError, 'tmux already has a session'):
            self.open('kit-live-a_b', 'sleep 300')
        self.assertTrue(self.has_session(linuxwin.tmux_name('kit-live-a.b')))


if __name__ == '__main__':
    unittest.main()
