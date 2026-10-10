"""Tests for kitlib.proc.job.no_window_flags and Job.start: a parent with no console (the guard
hook under pythonw.exe) starts console programs with no console window; a parent with a console
keeps its children on that console. Every process here is the test's own."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if os.name == "nt":
    from kitlib.proc import job, win32

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Run as the parent: starts a console python through Job.start and writes what the parent and the
# child saw to argv[2]. Under pythonw there is no stderr, so a failure is written there too.
PARENT = "\n".join([
    "import json, os, subprocess, sys, traceback",
    "out = sys.argv[2]",
    "try:",
    "    sys.path.insert(0, sys.argv[1])",
    "    from kitlib.proc import job",
    "    probe = ('import ctypes, json, os; k = ctypes.WinDLL(\"kernel32\"); '",
    "             'k.GetConsoleWindow.restype = ctypes.c_void_p; a = (ctypes.c_uint * 64)(); '",
    "             'n = k.GetConsoleProcessList(a, 64); '",
    "             'print(json.dumps({\"window\": k.GetConsoleWindow() or 0, \"pid\": os.getpid(), '",
    "             '\"console_pids\": list(a[:min(n, 64)])}))')",
    "    j = job.Job()",
    "    p = j.start([sys.argv[3], '-c', probe], stdout=subprocess.PIPE,",
    "                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
    "    text = p.communicate(timeout=60)[0]",
    "    j.end(kill=True)",
    "    r = {'parent_pid': os.getpid(), 'parent_console': job.has_console(),",
    "         'child': json.loads(text)}",
    "except BaseException:",
    "    r = {'error': traceback.format_exc()}",
    "with open(out, 'w') as f:",
    "    f.write(json.dumps(r))"])


def python_exe():
    """The console python beside this interpreter (sys.executable may be pythonw)."""
    d = os.path.dirname(sys.executable)
    p = os.path.join(d, "python.exe")
    return p if os.path.exists(p) else sys.executable


@unittest.skipUnless(os.name == "nt", "Windows only")
class NoWindowFlagsTest(unittest.TestCase):
    def test_console_parent_keeps_flags(self):
        self.assertEqual(job.no_window_flags(0, console=True), 0)
        self.assertEqual(job.no_window_flags(0x200, console=True), 0x200)

    def test_no_console_adds_no_window(self):
        self.assertEqual(job.no_window_flags(0, console=False), win32.CREATE_NO_WINDOW)
        self.assertEqual(job.no_window_flags(0x200, console=False),
                         0x200 | win32.CREATE_NO_WINDOW)

    def test_caller_console_choice_unchanged(self):
        for flag in (win32.CREATE_NEW_CONSOLE, win32.DETACHED_PROCESS, win32.CREATE_NO_WINDOW):
            self.assertEqual(job.no_window_flags(flag, console=False), flag)
            self.assertEqual(job.no_window_flags(flag | 0x200, console=False), flag | 0x200)

    def test_console_none_asks_has_console(self):
        with mock.patch.object(job, "has_console", return_value=False):
            self.assertEqual(job.no_window_flags(0), win32.CREATE_NO_WINDOW)
        with mock.patch.object(job, "has_console", return_value=True):
            self.assertEqual(job.no_window_flags(0), 0)

    def test_start_passes_flags_through(self):
        """Job.start hands Popen the helper's flags plus CREATE_SUSPENDED."""
        seen = {}
        real = subprocess.Popen

        def spy(*a, **kw):
            seen["flags"] = kw.get("creationflags")
            return real(*a, **kw)
        with job.Job() as j, mock.patch.object(job, "has_console", return_value=False), \
                mock.patch.object(job.subprocess, "Popen", spy):
            p = j.start([python_exe(), "-c", "pass"], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(60)
        self.assertEqual(seen["flags"], win32.CREATE_NO_WINDOW | win32.CREATE_SUSPENDED)


@unittest.skipUnless(os.name == "nt", "Windows only")
class ConsoleWindowTest(unittest.TestCase):
    """Real processes: what a console child of Job.start sees of its console."""

    def run_parent(self, exe, creationflags=0):
        d = tempfile.mkdtemp(prefix="kit-nowin-")
        self.addCleanup(shutil.rmtree, d, True)
        script, out = os.path.join(d, "parent.py"), os.path.join(d, "out.json")
        with open(script, "w") as f:
            f.write(PARENT)
        subprocess.run([exe, script, ROOT, out, python_exe()], timeout=120,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=creationflags)
        self.assertTrue(os.path.exists(out), "the parent wrote nothing")
        with open(out) as f:
            r = json.load(f)
        self.assertNotIn("error", r, r.get("error"))
        return r

    def test_pythonw_parent_child_has_no_window(self):
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            self.skipTest("no pythonw.exe beside %s" % sys.executable)
        r = self.run_parent(pythonw)
        self.assertFalse(r["parent_console"], r)
        self.assertEqual(r["child"]["window"], 0, r)              # no console window
        self.assertIn(r["child"]["pid"], r["child"]["console_pids"], r)   # yet a console

    def test_console_parent_child_shares_its_console(self):
        """A parent with a console (hidden here: CREATE_NO_WINDOW, so no window flashes) keeps
        its child on that console, as before."""
        r = self.run_parent(python_exe(), creationflags=win32.CREATE_NO_WINDOW)
        self.assertTrue(r["parent_console"], r)
        self.assertIn(r["parent_pid"], r["child"]["console_pids"], r)


if __name__ == "__main__":
    unittest.main()
