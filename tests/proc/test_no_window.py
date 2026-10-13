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
    "    import ctypes",
    "    attached = ctypes.WinDLL('kernel32').GetConsoleProcessList((ctypes.c_uint * 1)(), 1) != 0",
    "    import _winapi",
    "    r = {'parent_pid': os.getpid(), 'parent_console': attached,",
    "         'hooked': getattr(_winapi.CreateProcess, '__kit_windowless__', False),",
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
        # kitlib.proc.console's hook is in the parent, yet it gains no console: Job.start names a
        # console choice (CREATE_NO_WINDOW), and the hook leaves such a start alone.
        self.assertTrue(r["hooked"], r)
        self.assertFalse(r["parent_console"], r)
        self.assertEqual(r["child"]["window"], 0, r)              # no console window
        self.assertIn(r["child"]["pid"], r["child"]["console_pids"], r)   # yet a console

    def test_console_parent_child_shares_its_console(self):
        """A parent with a console (hidden here: CREATE_NO_WINDOW, so no window flashes) keeps
        its child on that console, as before."""
        r = self.run_parent(python_exe(), creationflags=win32.CREATE_NO_WINDOW)
        self.assertTrue(r["parent_console"], r)
        self.assertIn(r["parent_pid"], r["child"]["console_pids"], r)


# Run as the child: writes what it sees of its console to argv[1] (no pipe: a child of pythonw has
# none) and exits with argv[2].
PROBE = "\n".join([
    "import ctypes, json, os, sys",
    "k = ctypes.WinDLL('kernel32')",
    "k.GetConsoleWindow.restype = ctypes.c_void_p",
    "a = (ctypes.c_uint * 64)()",
    "n = k.GetConsoleProcessList(a, 64)",
    "with open(sys.argv[1], 'w') as f:",
    "    json.dump({'window': k.GetConsoleWindow() or 0, 'pid': os.getpid(),",
    "               'console_pids': list(a[:min(n, 64)]), 'exe': sys.executable}, f)",
    "sys.exit(int(sys.argv[2]))"])

# Run as the parent: argv is <kit root> <out> <mode> <the child's command line...>. Starts the child
# the way kit code does, with no creation flags and no handles of its own choosing.
SPAWN_PARENT = "\n".join([
    "import ctypes, json, os, sys, traceback",
    "out = sys.argv[2]",
    "try:",
    "    sys.path.insert(0, sys.argv[1])",
    "    mode, argv = sys.argv[3], sys.argv[4:]",
    "    if mode == 'gitcmd':",
    "        from kitlib.git import cmd",
    "        code = cmd.subprocess_run(argv, timeout=60).returncode",
    "    else:",
    "        from kitlib.proc import spawn",
    "        if mode == 'popen':",
    "            code = spawn.popen(argv).wait(60)",
    "        elif mode == 'call':",
    "            code = spawn.call(argv, timeout=60)",
    "        else:",
    "            code = spawn.run(argv, timeout=60).returncode",
    "    attached = ctypes.WinDLL('kernel32').GetConsoleProcessList((ctypes.c_uint * 1)(), 1) != 0",
    "    import _winapi",
    "    r = {'parent_pid': os.getpid(), 'parent_console': attached, 'code': code,",
    "         'hooked': getattr(_winapi.CreateProcess, '__kit_windowless__', False)}",
    "except BaseException:",
    "    r = {'error': traceback.format_exc()}",
    "with open(out, 'w') as f:",
    "    f.write(json.dumps(r))"])

MODES = ("run", "call", "popen", "gitcmd")


def pythonw_exe():
    p = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return p if os.path.exists(p) else None


class _Folder(unittest.TestCase):
    """A temporary folder holding the probe."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kit-nowin-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.probe = self.write("probe.py", PROBE)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def read(self, path):
        self.assertTrue(os.path.exists(path), "%s was not written" % os.path.basename(path))
        with open(path) as f:
            return json.load(f)


@unittest.skipUnless(os.name == "nt", "Windows only")
class SpawnWindowTest(_Folder):
    """Real processes: what a console child of kitlib.proc.spawn, and of kitlib.git.cmd's
    subprocess_run, sees of its console. The child is started with no flags of the caller's."""

    def run_parent(self, exe, mode, creationflags=0):
        script = self.write("parent-%s.py" % mode, SPAWN_PARENT)
        out = os.path.join(self.dir, "out-%s.json" % mode)
        child_out = out + ".child"
        subprocess.run([exe, script, ROOT, out, mode, python_exe(), self.probe, child_out, "3"],
                       timeout=120, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=creationflags)
        r = self.read(out)
        self.assertNotIn("error", r, r.get("error"))
        self.assertEqual(r["code"], 3, r)                       # the child ran, and to its end
        r["child"] = self.read(child_out)
        return r

    def test_pythonw_parent_child_has_no_window(self):
        pythonw = pythonw_exe()
        if not pythonw:
            self.skipTest("no pythonw.exe beside %s" % sys.executable)
        for mode in MODES:
            with self.subTest(mode):
                r = self.run_parent(pythonw, mode)
                # the hook is in, but spawn's CREATE_NO_WINDOW is a console choice it leaves alone
                self.assertTrue(r["hooked"], r)
                self.assertFalse(r["parent_console"], r)
                self.assertEqual(r["child"]["window"], 0, r)                  # no console window
                self.assertIn(r["child"]["pid"], r["child"]["console_pids"], r)   # yet a console

    def test_console_parent_child_shares_its_console(self):
        """A parent with a console (hidden here, so no window flashes) keeps its child on it:
        nothing changes for a tool run from a shell."""
        for mode in MODES:
            with self.subTest(mode):
                r = self.run_parent(python_exe(), mode, creationflags=win32.CREATE_NO_WINDOW)
                self.assertTrue(r["parent_console"], r)
                self.assertIn(r["parent_pid"], r["child"]["console_pids"], r)


@unittest.skipUnless(os.name == "nt", "Windows only")
class CarrierTest(_Folder):
    """tools/no_window.py under pythonw, as a scheduled task runs it: the program it carries has
    no console window, runs under the console python, and its exit code comes back."""

    CARRIER = os.path.join(ROOT, "tools", "no_window.py")

    def setUp(self):
        super().setUp()
        self.pythonw = pythonw_exe()
        if not self.pythonw:
            self.skipTest("no pythonw.exe beside %s" % sys.executable)

    def carry(self, *argv):
        return subprocess.run([self.pythonw, self.CARRIER] + list(argv), timeout=120,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def carry_stderr(self, *argv):
        """(the carrier's exit code, what it wrote to a stderr it was given)."""
        done = subprocess.run([self.pythonw, self.CARRIER] + list(argv), timeout=120,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        return done.returncode, done.stderr.decode("utf-8", "replace")

    def test_python_runs_with_no_window_and_its_exit_code_comes_back(self):
        out = os.path.join(self.dir, "carried.json")
        self.assertEqual(self.carry("python", self.probe, out, "3"), 3)
        child = self.read(out)
        self.assertEqual(child["window"], 0, child)                     # no console window
        self.assertIn(child["pid"], child["console_pids"], child)       # yet a console
        self.assertEqual(os.path.normcase(child["exe"]), os.path.normcase(python_exe()), child)

    def test_a_program_named_by_path_is_carried_too(self):
        out = os.path.join(self.dir, "carried.json")
        self.assertEqual(self.carry(python_exe(), self.probe, out, "0"), 0)
        self.assertEqual(self.read(out)["window"], 0)

    def test_a_program_that_is_not_there(self):
        self.assertEqual(self.carry(os.path.join(self.dir, "none.exe")), 127)

    def test_no_program_is_a_usage_error(self):
        """The usage line is the carrier's own: a pythonw that failed some other way with 2 (a
        script it cannot open) writes nothing of the kind."""
        code, err = self.carry_stderr()
        self.assertEqual(code, 2)
        self.assertIn("USAGE: pythonw no_window.py PROGRAM [ARGS...]", err)
        self.assertEqual(self.carry(), 2)             # and with no stderr at all, as a task has

    def test_an_exit_code_above_the_signed_range_comes_back_whole(self):
        """A program that ends as a crash does (0xC0000005) is seen as that by whoever started the
        carrier; the program here ends itself with ExitProcess, the code as it is. A pin: Python
        3.14 passes the unconverted code through sys.exit too, so this held before exit_code."""
        ender = self.write("ender.py", "\n".join([
            "import ctypes, sys",
            "ctypes.WinDLL('kernel32').ExitProcess(ctypes.c_uint(int(sys.argv[1])))"]))
        for code in (0xC0000005, 0xFFFFFFFE, 0x80000000):
            with self.subTest(code=hex(code)):
                direct = subprocess.run([python_exe(), ender, str(code)], timeout=120,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL).returncode
                self.assertEqual(direct, code)        # the program does end with it
                self.assertEqual(self.carry("python", ender, str(code)), code)


if __name__ == "__main__":
    unittest.main()
