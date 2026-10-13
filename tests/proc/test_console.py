"""Tests for kitlib.proc.console: the process-wide CreateProcess hook that gives a process with no
console a console with no window before its first start, so no start in a kit process opens a
console window (docs/daily-full.md "The schedule").

The real-process tests run a pythonw.exe parent (no console, as the guard hook and a scheduled task
have) that imports kitlib and then starts console pythons the bare ways: subprocess.run, Popen,
check_output and a multiprocessing child. Every process here is the test's own."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if os.name == "nt":
    import _winapi
    from kitlib.config import user as kuser
    from kitlib.proc import console

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
BARE = ("run", "popen", "check_output", "multiprocessing")

# Run as a child: writes what it sees of its console to argv[1] (a child of pythonw may have no
# pipe), and prints "said" when argv[2] is "say".
PROBE = "\n".join([
    "import ctypes, json, os, sys",
    "k = ctypes.WinDLL('kernel32')",
    "k.GetConsoleWindow.restype = ctypes.c_void_p",
    "a = (ctypes.c_uint * 64)()",
    "n = k.GetConsoleProcessList(a, 64)",
    "with open(sys.argv[1], 'w') as f:",
    "    json.dump({'window': k.GetConsoleWindow() or 0, 'pid': os.getpid(),",
    "               'console_pids': list(a[:min(n, 64)])}, f)",
    "if sys.argv[2:] == ['say']:",
    "    print('said', flush=True)"])

# Run as the parent under pythonw: argv is <kit root> <folder> <mode> <console python>. It imports
# kitlib, then starts the probe with no creation flags of its own (modes "alloc" and "fallback"),
# or with console choices first (mode "choice"), and writes what it saw to <folder>/parent.json.
PARENT = r'''
import json, multiprocessing, os, subprocess, sys, traceback

DN = subprocess.DEVNULL


def report(path):
    """The multiprocessing child: the probe's report, from inside."""
    import ctypes
    k = ctypes.WinDLL('kernel32')
    k.GetConsoleWindow.restype = ctypes.c_void_p
    a = (ctypes.c_uint * 64)()
    n = k.GetConsoleProcessList(a, 64)
    with open(path, 'w') as f:
        json.dump({'window': k.GetConsoleWindow() or 0, 'pid': os.getpid(),
                   'console_pids': list(a[:min(n, 64)])}, f)


def attached():
    import ctypes
    return ctypes.WinDLL('kernel32').GetConsoleProcessList((ctypes.c_uint * 1)(), 1) != 0


def main(root, folder, mode, py):
    r = {'parent_pid': os.getpid()}
    probe = os.path.join(folder, 'probe.py')
    out = lambda name: os.path.join(folder, name + '.json')
    sys.path.insert(0, root)
    r['before'] = attached()
    import kitlib
    from kitlib.proc import console
    console.LOG_PATH = os.path.join(folder, 'incidents.log')
    console.FORCE_FALLBACK = mode == 'fallback'
    r['installed'] = console.installed()
    if mode == 'choice':
        subprocess.run([py, probe, out('detached')], stdin=DN, stdout=DN, stderr=DN,
                       creationflags=subprocess.DETACHED_PROCESS, timeout=60)
        subprocess.run([py, probe, out('own')], creationflags=subprocess.CREATE_NO_WINDOW,
                       timeout=60)
        r['after_choices'] = attached()
        subprocess.run([py, probe, out('run')], timeout=60)
        subprocess.run([py, probe, out('detached_after')], stdin=DN, stdout=DN, stderr=DN,
                       creationflags=subprocess.DETACHED_PROCESS, timeout=60)
    else:
        subprocess.run([py, probe, out('run')], timeout=60)
        subprocess.Popen([py, probe, out('popen')]).wait(60)
        r['said'] = subprocess.check_output([py, probe, out('check_output'), 'say'],
                                            timeout=60).decode().strip()
        multiprocessing.set_executable(py)
        p = multiprocessing.Process(target=report, args=(out('multiprocessing'),))
        p.start()
        p.join(60)
        r['mp_code'] = p.exitcode
        # no std handles of its own: it writes to this process's stdout (the test's file)
        subprocess.run([py, '-c', 'print("child-out", flush=True)'], timeout=60)
    r['after'] = attached()
    return r


if __name__ == '__main__':
    try:
        r = main(*sys.argv[1:5])
    except BaseException:
        r = {'error': traceback.format_exc()}
    with open(os.path.join(sys.argv[2], 'parent.json'), 'w') as f:
        f.write(json.dumps(r))
'''


def python_exe():
    """The console python beside this interpreter (sys.executable may be pythonw)."""
    p = os.path.join(os.path.dirname(sys.executable), "python.exe")
    return p if os.path.exists(p) else sys.executable


def pythonw_exe():
    p = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return p if os.path.exists(p) else None


def alloc_exported():
    import ctypes
    return hasattr(ctypes.WinDLL("kernel32"), "AllocConsoleWithOptions")


@unittest.skipUnless(os.name == "nt", "Windows only")
class InstalledTest(unittest.TestCase):
    """The hook is in place wherever kitlib is, and the stdlib still starts through it: a Python
    whose subprocess or multiprocessing stops calling _winapi.CreateProcess fails here."""

    def fresh(self, code):
        r = subprocess.run([python_exe(), "-c", code], cwd=ROOT, capture_output=True, text=True,
                           timeout=60, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_import_kitlib_installs_it_without_ctypes(self):
        got = self.fresh("import json, sys, _winapi, kitlib; print(json.dumps("
                         "[getattr(_winapi.CreateProcess, %r, False), 'ctypes' in sys.modules]))"
                         % console.MARK)
        self.assertEqual(got, [True, False])

    def test_import_tests_installs_it(self):
        got = self.fresh("import json, _winapi, tests; print(json.dumps("
                         "getattr(_winapi.CreateProcess, %r, False)))" % console.MARK)
        self.assertIs(got, True)

    def test_installed_here_and_idempotent(self):
        self.assertTrue(console.installed())
        self.assertIs(_winapi.CreateProcess, console._create_process)
        self.assertFalse(console.install())
        self.assertIs(_winapi.CreateProcess, console._create_process)

    def test_subprocess_and_multiprocessing_call_it(self):
        import multiprocessing.popen_spawn_win32 as mpw
        self.assertIs(subprocess._winapi, _winapi)
        self.assertIs(mpw._winapi, _winapi)
        seen = []
        real = console._original

        def spy(*a):
            seen.append(a[5])
            return real(*a)
        with mock.patch.object(console, "_original", spy):
            subprocess.run([python_exe(), "-c", "pass"], timeout=60,
                           creationflags=CREATE_NO_WINDOW)
        self.assertEqual(seen, [CREATE_NO_WINDOW])


@unittest.skipUnless(os.name == "nt", "Windows only")
class WrapperTest(unittest.TestCase):
    """_create_process with the real CreateProcess and ensure faked."""

    def setUp(self):
        self.flags, self.ensured, self.answer = [], [], console.ALLOCATED
        for name, value in (("_original", self.original), ("_mode", console._UNKNOWN),
                            ("ensure_windowless_console", self.ensure)):
            p = mock.patch.object(console, name, value)
            p.start()
            self.addCleanup(p.stop)

    def original(self, *a):
        self.flags.append(a[5])
        return "started"

    def ensure(self, fallback=None, child=None):
        self.ensured.append(child)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def start(self, flags):
        return console._create_process(None, "prog arg", None, None, 0, flags, None, None, None)

    def test_a_console_choice_passes_through_and_ensures_nothing(self):
        for flag in (CREATE_NEW_CONSOLE, DETACHED_PROCESS, CREATE_NO_WINDOW):
            self.assertEqual(self.start(flag | 0x200), "started")
        self.assertEqual(self.flags, [CREATE_NEW_CONSOLE | 0x200, DETACHED_PROCESS | 0x200,
                                      CREATE_NO_WINDOW | 0x200])
        self.assertEqual(self.ensured, [])
        self.assertEqual(console._mode, console._UNKNOWN)

    def test_the_first_bare_start_ensures_once(self):
        for answer in (console.ALLOCATED, console.ATTACHED, console.PRESENT):
            with self.subTest(answer):
                console._mode, self.answer, self.ensured[:], self.flags[:] = \
                    console._UNKNOWN, answer, [], []
                self.start(0)
                self.start(0x200)
                self.start(CREATE_NEW_CONSOLE)
                self.assertEqual(self.ensured, ["prog arg"])
                self.assertEqual(self.flags, [0, 0x200, CREATE_NEW_CONSOLE])

    def test_no_console_to_be_had_gives_every_bare_start_no_window(self):
        self.answer = console.NONE
        self.start(0)
        self.start(0x200)
        self.start(DETACHED_PROCESS)
        self.assertEqual(self.ensured, ["prog arg"])
        self.assertEqual(self.flags, [CREATE_NO_WINDOW, 0x200 | CREATE_NO_WINDOW,
                                      DETACHED_PROCESS])

    def test_an_ensure_that_fails_never_stops_the_start(self):
        self.answer = OSError("refused")
        self.assertEqual(self.start(0), "started")
        self.assertEqual(self.flags, [CREATE_NO_WINDOW])


@unittest.skipUnless(os.name == "nt", "Windows only")
class EnsureTest(unittest.TestCase):
    """ensure_windowless_console's choice of path, with the Windows calls faked."""

    def setUp(self):
        self.calls = []
        for name, value in (("_alloc_no_window", lambda: self.call("alloc", self.alloc)),
                            ("_attach_holder", lambda: self.call("attach", console.ATTACHED)),
                            ("_log_incident", lambda how, child: self.calls.append(("log", how)))):
            p = mock.patch.object(console, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.alloc = console.ALLOCATED

    def call(self, name, answer):
        self.calls.append(name)
        return answer

    def ensure(self, console_now, **kw):
        with mock.patch.object(console, "has_console", return_value=console_now):
            return console.ensure_windowless_console(**kw)

    def test_a_process_with_a_console_is_left_alone(self):
        self.assertEqual(self.ensure(True), console.PRESENT)
        self.assertEqual(self.calls, [])

    def test_alloc_first(self):
        self.assertEqual(self.ensure(False), console.ALLOCATED)
        self.assertEqual(self.calls, ["alloc", ("log", console.ALLOCATED)])

    def test_no_export_or_a_refusal_takes_the_holder(self):
        for alloc in (None, console.NONE):
            with self.subTest(alloc):
                self.alloc, self.calls[:] = alloc, []
                self.assertEqual(self.ensure(False), console.ATTACHED)
                self.assertEqual(self.calls, ["alloc", "attach", ("log", console.ATTACHED)])

    def test_forced_fallback(self):
        self.assertEqual(self.ensure(False, fallback=True), console.ATTACHED)
        with mock.patch.object(console, "FORCE_FALLBACK", True):
            self.assertEqual(self.ensure(False), console.ATTACHED)
        self.assertEqual(self.calls, ["attach", ("log", console.ATTACHED)] * 2)

    def test_has_console_is_true_here_or_false(self):
        self.assertIsInstance(console.has_console(), bool)


@unittest.skipUnless(os.name == "nt", "Windows only")
class LogTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kit-console-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "sub", "incidents.log")

    def lines(self, path=None):
        with open(path or self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_one_line_per_incident(self):
        with mock.patch.object(console, "LOG_PATH", self.path):
            console._log_incident(console.ALLOCATED, "prog arg")
            console._log_incident(console.ATTACHED, b"other")
        got = self.lines()
        self.assertEqual([(g["how"], g["child"]) for g in got],
                         [(console.ALLOCATED, "prog arg"), (console.ATTACHED, "other")])
        self.assertEqual(got[0]["pid"], os.getpid())
        self.assertEqual(got[0]["exe"], sys.executable)
        self.assertEqual(got[0]["parent_pid"], os.getppid())
        self.assertTrue(got[0]["parent_exe"])
        self.assertRegex(got[0]["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_default_place_is_the_kit_temp_folder(self):
        with mock.patch.object(kuser, "work_temp", return_value=self.dir) as wt:
            self.assertEqual(console.log_path(), os.path.join(self.dir, console.LOG_NAME))
        self.assertEqual(os.path.normcase(wt.call_args[0][0]), os.path.normcase(ROOT))

    def test_a_log_that_fails_is_swallowed(self):
        """A fenced test process (HomeFenced, a BaseException), a bad machine config, a path
        that is a folder: nothing raised, nothing written."""
        for fail in (kuser.HomeFenced("fenced"), kuser.ConfigError("bad"), RuntimeError("x")):
            with self.subTest(type(fail).__name__), \
                    mock.patch.object(kuser, "work_temp", side_effect=fail):
                console._log_incident(console.ALLOCATED, "prog")
        os.makedirs(self.path)
        with mock.patch.object(console, "LOG_PATH", self.path):
            console._log_incident(console.ALLOCATED, "prog")
        self.assertTrue(os.path.isdir(self.path))

    def test_the_logged_command_lines_are_redacted(self):
        child = subprocess.list2cmdline(["C:\\Program Files\\x.exe", "--token", "tok one",
                                         "API_KEY=sekrit", "a", "b", "c", "d", "e"])
        argv = ["python.exe", "tool.py", "--password=hunter2"]
        with mock.patch.object(console, "LOG_PATH", self.path), \
                mock.patch.object(sys, "orig_argv", argv, create=True):
            console._log_incident(console.ALLOCATED, child)
        got = self.lines()[0]
        self.assertEqual(got["argv"], ["python.exe", "tool.py", "--password=***"])
        self.assertEqual(got["child"],
                         '"C:\\Program Files\\x.exe" --token *** API_KEY=*** a b c ...(+2)')
        for secret in ("tok one", "sekrit", "hunter2"):
            self.assertNotIn(secret, json.dumps(got))

    def test_a_full_log_moves_aside(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"how": "old"}) + "\n" + "x" * 64 + "\n")
        with mock.patch.object(console, "LOG_PATH", self.path), \
                mock.patch.object(console, "LOG_MAX", 32):
            console._log_incident(console.ALLOCATED, "prog")
        self.assertEqual([g["how"] for g in self.lines()], [console.ALLOCATED])
        with open(self.path + ".1", encoding="utf-8") as f:
            self.assertIn('"old"', f.read())


@unittest.skipUnless(os.name == "nt", "Windows only")
class RedactTest(unittest.TestCase):
    """redact_command: what the incident log keeps of a command line."""

    def test_secret_flags_and_values_are_masked(self):
        cases = [
            (["--token", "abc", "--api-key", "abc", "/Password", "abc", "-k", "kept"],
             ["--token", "***", "--api-key", "***", "/Password", "***", "-k", "kept"]),
            (["--token=abc", "--client-secret:abc", "/pwd:abc", "--name=kept"],
             ["--token=***", "--client-secret:***", "/pwd:***", "--name=kept"]),
            (["GITHUB_TOKEN=abc", "AWS_SECRET_ACCESS_KEY=abc", "MODE=kept"],
             ["GITHUB_TOKEN=***", "AWS_SECRET_ACCESS_KEY=***", "MODE=kept"]),
            (["-H", "Authorization: Bearer abc", "Bearer", "abc"],
             ["-H", "Authorization:***", "Bearer", "***"]),
            (["https://user:pw@host/x", "ghp_" + "a" * 36, "sk-" + "b" * 40, "C:\\kept\\a=b"],
             ["https://***@host/x", "***", "***", "C:\\kept\\a=b"]),
        ]
        for words, want in cases:
            with self.subTest(words[0]), mock.patch.object(console, "KEEP_WORDS", 99):
                self.assertEqual(console.redact_command(["prog"] + words), ["prog"] + want)

    def test_it_keeps_the_program_and_the_first_words_each_cut(self):
        path = "C:\\" + "d" * 200 + "\\probe.py"
        got = console.redact_command(["prog", path] + ["w%d" % i for i in range(10)])
        self.assertEqual(len(got), 1 + console.KEEP_WORDS + 1)
        self.assertEqual(got[-1], "...(+%d)" % (11 - console.KEEP_WORDS))
        self.assertEqual(len(got[1]), console.WORD_MAX)
        self.assertTrue(got[1].endswith("\\probe.py"), got[1])        # a path keeps its name
        self.assertEqual(console.redact_command(["prog", "a"]), ["prog", "a"])
        self.assertIsNone(console.redact_command(None))

    def test_a_command_line_string_is_split_as_windows_does(self):
        words = ["C:\\Program Files\\x.exe", 'say "hi"', "a\\\\", "b\\c", "", "--key", "k v"]
        line = subprocess.list2cmdline(words)
        self.assertEqual(console.split_command_line(line), words)
        self.assertEqual(console.split_command_line('a "b ""c"" d" e'), ["a", 'b "c" d', "e"])
        self.assertEqual(console.split_command_line("  a\t b  "), ["a", "b"])
        got = console.redact_command(line.encode())
        self.assertEqual(console.split_command_line(got), words[:5] + ["--key", "***"])


@unittest.skipUnless(os.name == "nt", "Windows only")
class PythonwParentTest(unittest.TestCase):
    """Real processes: a pythonw parent with kitlib imported, its children started bare."""

    def setUp(self):
        self.pythonw = pythonw_exe()
        if not self.pythonw:
            self.skipTest("no pythonw.exe beside %s" % sys.executable)
        self.dir = tempfile.mkdtemp(prefix="kit-console-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, text in (("probe.py", PROBE), ("parent.py", PARENT)):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(text)

    def read(self, name):
        path = os.path.join(self.dir, name)
        self.assertTrue(os.path.exists(path), "%s was not written" % name)
        with open(path) as f:
            return json.load(f)

    def run_parent(self, mode):
        stdout = os.path.join(self.dir, "stdout.txt")
        with open(stdout, "wb") as fh:
            subprocess.run([self.pythonw, os.path.join(self.dir, "parent.py"), ROOT, self.dir,
                            mode, python_exe()], timeout=180, stdin=subprocess.DEVNULL,
                           stdout=fh, stderr=subprocess.DEVNULL)
        r = self.read("parent.json")
        self.assertNotIn("error", r, r.get("error"))
        with open(stdout, "rb") as fh:
            r["stdout"] = fh.read().decode("utf-8", "replace")
        log = os.path.join(self.dir, "incidents.log")
        r["log"] = []
        if os.path.exists(log):
            with open(log, encoding="utf-8") as fh:
                r["log"] = [json.loads(line) for line in fh]
        return r

    def assert_shares_a_windowless_console(self, r, name):
        child = self.read(name + ".json")
        self.assertEqual(child["window"], 0, (name, child))                      # no window
        self.assertIn(child["pid"], child["console_pids"], (name, child))        # a console,
        self.assertIn(r["parent_pid"], child["console_pids"], (name, child))     # the parent's

    def check_bare(self, mode, how):
        r = self.run_parent(mode)
        self.assertFalse(r["before"], r)            # pythonw: no console of its own
        self.assertTrue(r["installed"], r)
        self.assertTrue(r["after"], r)              # it has one now
        for name in BARE:
            with self.subTest(name):
                self.assert_shares_a_windowless_console(r, name)
        self.assertEqual(r["said"], "said")
        self.assertEqual(r["mp_code"], 0)
        self.assertIn("child-out", r["stdout"])     # the parent's std handles are kept
        self.assertEqual([(g["how"], g["pid"]) for g in r["log"]], [(how, r["parent_pid"])])
        self.assertIn("probe.py", r["log"][0]["child"])
        return r

    def test_bare_starts_open_no_window(self):
        self.check_bare("alloc", console.ALLOCATED if alloc_exported() else console.ATTACHED)

    def test_bare_starts_open_no_window_by_the_fallback(self):
        self.check_bare("fallback", console.ATTACHED)

    def test_console_choices_are_kept(self):
        """DETACHED_PROCESS and CREATE_NO_WINDOW starts are the caller's: they ensure nothing
        (the parent still has no console after them), a detached child has no console at all,
        before the parent gets one and after, and a CREATE_NO_WINDOW child has a console of its
        own. The first bare start then gives the parent one, which that child shares."""
        r = self.run_parent("choice")
        self.assertFalse(r["after_choices"], r)
        for name in ("detached", "detached_after"):
            with self.subTest(name):
                self.assertEqual(self.read(name + ".json")["console_pids"], [])
        own = self.read("own.json")
        self.assertEqual(own["window"], 0, own)
        self.assertIn(own["pid"], own["console_pids"], own)
        self.assertNotIn(r["parent_pid"], own["console_pids"], own)
        self.assert_shares_a_windowless_console(r, "run")
        self.assertTrue(r["after"], r)
        self.assertEqual(len(r["log"]), 1, r["log"])


if __name__ == "__main__":
    unittest.main()
