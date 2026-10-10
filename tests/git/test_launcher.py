"""A git that is a launcher: C:/Program Files/Git/cmd/git.exe starts the real git as a child, and
the child holds the pipes. Killing only the launcher leaves the child running with the pipes open,
so a timeout must end the whole tree for the call to return.

The fake `git` here is a Python launcher that starts a sleeping child on its own stdin, stdout and
stderr, writes the child's pid to a file, and waits for it.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib.git import batch, cmd
from tests.realgit import real_git

LAUNCHER = (
    'import subprocess, sys\n'
    'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],\n'
    '                     stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)\n'
    'open(sys.argv[1], "w").write(str(c.pid))\n'
    'sys.exit(c.wait())\n')

# The timeout for a call whose launcher must get its child started first: two python startups
# (launcher, then child) can take over a second on a loaded machine, and a timeout that fires before
# the child exists kills the launcher with nothing to check. The launcher never exits on its own,
# so a call always runs the full timeout; the child sleeps 30, so `took` stays below it.
START_TIMEOUT = 5

# A git that never reads its stdin: a caller writing more than a pipe holds must not block.
DEAF = 'import time; time.sleep(30)'

_K32 = ctypes.WinDLL('kernel32', use_last_error=True)
_K32.OpenProcess.restype = ctypes.c_void_p
_K32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
_K32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
_K32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
_K32.CloseHandle.argtypes = (ctypes.c_void_p,)
SYNCHRONIZE, TERMINATE, WAIT_TIMEOUT, ERROR_INVALID_PARAMETER = 0x00100000, 0x0001, 0x102, 87


def _open(pid):
    return _K32.OpenProcess(SYNCHRONIZE | TERMINATE, False, pid)


def _alive(h, wait):
    """True while the process behind handle h (None: it had ended) runs, waiting up to `wait`
    seconds for it to end."""
    return h is not None and _K32.WaitForSingleObject(h, int(wait * 1000)) == WAIT_TIMEOUT


@real_git('the process tree a timed-out call ends (git is stubbed by a python launcher)')
class LauncherGitTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-launcher-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pidfile = os.path.join(self.dir, 'child.pid')
        self.started = []
        self.script = [LAUNCHER, self.pidfile]
        real = subprocess.Popen

        def fake(args, *a, **kw):
            args = list(args)
            if args and args[0] == 'git':
                args = [sys.executable, '-c'] + self.script
                self.started.append(real(args, *a, **kw))
                return self.started[-1]
            return real(args, *a, **kw)
        patcher = mock.patch.object(subprocess, 'Popen', fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def child(self):
        """A SYNCHRONIZE|TERMINATE handle to the launcher's child, or None when it has ended (a
        handle keeps its pid from being reused); the cleanup ends it if the test failed."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with open(self.pidfile) as f:
                    pid = int(f.read())
                break
            except (OSError, ValueError):
                time.sleep(0.05)
        else:
            self.fail('the fake launcher never started its child')
        h = _open(pid)
        if not h:
            self.assertEqual(ctypes.get_last_error(), ERROR_INVALID_PARAMETER,
                             'cannot open the child %d' % pid)
            return None

        def end():
            if _alive(h, 0):
                _K32.TerminateProcess(h, 1)
            _K32.CloseHandle(h)
        self.addCleanup(end)
        return h

    def test_a_timed_out_call_ends_the_launchers_child_and_returns(self):
        t0 = time.monotonic()
        r = cmd.call(['status'], self.dir, timeout=START_TIMEOUT)
        took = time.monotonic() - t0
        self.assertLess(took, 25)                       # the child sleeps 30 holding the pipes
        self.assertIsNone(r.rc)
        self.assertIn('timed out', r.err)
        h = self.child()
        self.assertFalse(_alive(h, 5), "the launcher's child outlived the timeout")

    def test_a_timed_out_catfile_ends_the_launchers_child_and_frees_its_pipes(self):
        cat = batch.CatFile(self.dir, timeout=START_TIMEOUT)
        t0 = time.monotonic()
        self.assertIsNone(cat.read('HEAD', 'x'))
        self.assertLess(time.monotonic() - t0, 25)
        self.assertTrue(cat.dead)
        h = self.child()
        cat.close()
        self.assertFalse(_alive(h, 5), "the launcher's child outlived the timeout")
        p = self.started[0]
        self.assertTrue(p.stdout.closed and p.stdin.closed, 'the pipes were left to a stuck reader')


    def test_a_git_that_never_reads_its_input_still_times_out(self):
        self.script = [DEAF]
        t0 = time.monotonic()
        r = cmd.call(['cat-file', '--batch'], self.dir, stdin=b'x' * (8 << 20), timeout=1)
        self.assertLess(time.monotonic() - t0, 10)     # the input is far more than a pipe holds
        self.assertIsNone(r.rc)
        self.assertIn('timed out', r.err)
        self.assertIsNotNone(self.started[0].poll())


if __name__ == '__main__':
    unittest.main()
