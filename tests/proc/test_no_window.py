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

from kitlib.proc import spawn

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
        self.log = os.path.join(self.dir, "incidents.log")       # never the kit's own log
        self.env = dict(os.environ, KIT_CONSOLE_INCIDENT_LOG=self.log)
        self.env.pop(spawn.AGENT_TEMP, None)          # never the machine's own agent temp folder

    def carry(self, *argv):
        return subprocess.run([self.pythonw, self.CARRIER] + list(argv), timeout=120, env=self.env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def carry_stderr(self, *argv):
        """(the carrier's exit code, what it wrote to a stderr it was given)."""
        done = subprocess.run([self.pythonw, self.CARRIER] + list(argv), timeout=120, env=self.env,
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

    def test_the_program_gets_agent_temp_as_temp_and_tmp(self):
        """Under pythonw, as a task runs it: AGENT_TEMP (missing, so the carrier makes it) is the
        carried program's TEMP and TMP, though the carrier's own TEMP is elsewhere."""
        agent_temp = os.path.join(self.dir, "agent-temp")
        self.env.update(AGENT_TEMP=agent_temp, TEMP=self.dir, TMP=self.dir)
        out = os.path.join(self.dir, "temp.json")
        code = ("import json, os, sys, tempfile; json.dump({'TEMP': os.environ.get('TEMP'), "
                "'TMP': os.environ.get('TMP'), 'gettempdir': tempfile.gettempdir()}, "
                "open(sys.argv[1], 'w'))")
        self.assertEqual(self.carry("python", "-c", code, out), 0)
        seen = self.read(out)
        self.assertEqual((seen["TEMP"], seen["TMP"]), (agent_temp, agent_temp), seen)
        self.assertEqual(os.path.normcase(seen["gettempdir"]), os.path.normcase(agent_temp), seen)
        self.assertTrue(os.path.isdir(agent_temp))

    def test_with_no_agent_temp_the_program_keeps_the_carriers_temp(self):
        self.env.update(TEMP=self.dir, TMP=self.dir)
        out = os.path.join(self.dir, "temp.json")
        code = ("import json, os, sys; json.dump([os.environ.get('TEMP'), os.environ.get('TMP')], "
                "open(sys.argv[1], 'w'))")
        self.assertEqual(self.carry("python", "-c", code, out), 0)
        self.assertEqual(self.read(out), [self.dir, self.dir])

    def test_a_program_named_by_path_is_carried_too(self):
        out = os.path.join(self.dir, "carried.json")
        self.assertEqual(self.carry(python_exe(), self.probe, out, "0"), 0)
        self.assertEqual(self.read(out)["window"], 0)

    def test_a_program_that_is_not_there(self):
        self.assertEqual(self.carry(os.path.join(self.dir, "none.exe"), "secret-arg"), 127)
        with open(self.log, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1, lines)            # under pythonw: not silent
        line = json.loads(lines[0])
        self.assertEqual((line["how"], line["exe"]), ("carrier-fail", "none.exe"))
        self.assertTrue(line["why"].startswith("exit 127"), line)
        self.assertNotIn("secret-arg", lines[0])
        self.assertNotIn(self.dir, lines[0])

    def test_a_program_that_runs_leaves_no_line(self):
        self.assertEqual(self.carry("python", "-c", "pass"), 0)
        self.assertFalse(os.path.exists(self.log))

    def test_no_program_is_a_usage_error(self):
        """The usage line is the carrier's own: a pythonw that failed some other way with 2 (a
        script it cannot open) writes nothing of the kind."""
        code, err = self.carry_stderr()
        self.assertEqual(code, 2)
        self.assertIn("USAGE: pythonw no_window.py PROGRAM [ARGS...]", err)
        self.assertEqual(self.carry(), 2)             # and with stderr the null device

    def test_no_program_with_no_standard_handles_at_all_exits_2(self):
        """Started with no handles (no redirection, so pythonw gets none): sys.stderr is None in
        the carrier, and the usage line is skipped, not an AttributeError's exit 1."""
        seen = os.path.join(self.dir, "handles.txt")
        probe = self.write("handles.py", "import sys; open(sys.argv[1], 'w').write("
                                         "repr((sys.stdin, sys.stdout, sys.stderr)))")
        self.assertEqual(subprocess.run([self.pythonw, probe, seen], timeout=120,
                                        env=self.env).returncode, 0)
        with open(seen) as f:
            self.assertEqual(f.read(), "(None, None, None)")   # the start form gives no stderr
        self.assertEqual(subprocess.run([self.pythonw, self.CARRIER], timeout=120,
                                        env=self.env).returncode, 2)

    def test_no_program_with_a_closed_stderr_pipe_exits_2(self):
        """A stderr whose reading end is gone: the usage line cannot be written, and the exit is
        still 2 (unguarded, the failed flush at interpreter exit made it 120)."""
        r, w = os.pipe()
        os.close(r)
        try:
            code = subprocess.run([self.pythonw, self.CARRIER], timeout=120, env=self.env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=w).returncode
        finally:
            os.close(w)
        self.assertEqual(code, 2)

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


class AgentTempEnvTest(_Folder):
    """kitlib.proc.spawn.agent_temp_env and the environment carry hands the program: AGENT_TEMP
    as TEMP and TMP, else the environment unchanged; never a change to the one given, never an
    exception. Every folder is under the test's own temp folder."""

    def test_temp_and_tmp_become_agent_temp_which_is_made(self):
        agent_temp = os.path.join(self.dir, "a", "b")
        given = {"AGENT_TEMP": agent_temp, "TEMP": r"C:\elsewhere", "TMP": r"C:\elsewhere", "X": "1"}
        before = dict(given)
        env = spawn.agent_temp_env(given)
        self.assertEqual(env, dict(before, TEMP=agent_temp, TMP=agent_temp))
        self.assertTrue(os.path.isdir(agent_temp))
        self.assertEqual(given, before)                       # the one given is never changed
        self.assertIsNot(env, given)

    def test_an_existing_folder_is_used_as_it_is(self):
        env = spawn.agent_temp_env({"AGENT_TEMP": self.dir})
        self.assertEqual((env["TEMP"], env["TMP"]), (self.dir, self.dir))

    def test_no_usable_agent_temp_keeps_the_environment(self):
        a_file = self.write("a-file", "x")
        made = []
        for value in (None, "", "   ", "relative\\temp", "\\no-drive\\temp", a_file, 7):
            with self.subTest(value=value):
                given = {"TEMP": r"C:\t"} if value is None else {"AGENT_TEMP": value, "TEMP": r"C:\t"}
                before = dict(given)
                self.assertIsNone(spawn.agent_temp_env(given, makedirs=lambda p, **kw: made.append(p)
                                                       if value != a_file else os.makedirs(p, **kw)))
                self.assertEqual(given, before)
        self.assertEqual(made, [])                            # nothing made for a value refused

    def test_a_folder_that_cannot_be_made_keeps_the_environment(self):
        def refuse(path, exist_ok=False):
            raise PermissionError(5, "Access is denied", path)

        def broken(path, exist_ok=False):
            raise ValueError("embedded null character")
        target = os.path.join(self.dir, "never")
        for makedirs in (refuse, broken):
            with self.subTest(makedirs.__name__):
                self.assertIsNone(spawn.agent_temp_env({"AGENT_TEMP": target}, makedirs=makedirs))
        # made, yet no folder there (a makedirs that does nothing): not a folder, so unchanged
        self.assertIsNone(spawn.agent_temp_env({"AGENT_TEMP": target}, makedirs=lambda p, **kw: None))
        self.assertFalse(os.path.exists(target))

    def test_an_environment_that_cannot_be_read_never_raises(self):
        class Bad(dict):
            def get(self, *a):
                raise RuntimeError("unreadable")
        self.assertIsNone(spawn.agent_temp_env(Bad()))

    def test_carry_hands_the_program_agent_temp_and_leaves_os_environ_alone(self):
        agent_temp = os.path.join(self.dir, "agent-temp")
        seen = []

        def fake_call(argv, **kw):
            seen.append(kw)
            return 0
        with mock.patch.dict(os.environ, {"AGENT_TEMP": agent_temp}), \
                mock.patch.object(spawn, "call", fake_call):
            before = dict(os.environ)
            self.assertEqual(spawn.carry(["prog.exe", "x"]), 0)
            self.assertEqual(dict(os.environ), before)        # the carrier's own environment
        self.assertEqual((seen[0]["env"]["TEMP"], seen[0]["env"]["TMP"]), (agent_temp, agent_temp))

    def test_carry_with_no_usable_agent_temp_passes_no_environment(self):
        seen = []

        def fake_call(argv, **kw):
            seen.append(kw)
            return 0
        for value in ("relative", os.path.join(self.write("f", "x"), "under-a-file")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"AGENT_TEMP": value}), \
                    mock.patch.object(spawn, "call", fake_call):
                before = dict(os.environ)
                self.assertEqual(spawn.carry(["prog.exe"]), 0)
                self.assertEqual(dict(os.environ), before)
                self.assertNotIn("env", seen[-1])               # inherits this one, unchanged


if __name__ == "__main__":
    unittest.main()
