"""Tests for kitlib.proc.console: the process-wide CreateProcess hook that gives a process with no
console a console with no window before its first start, so no start in a kit process opens a
console window (docs/daily-full.md "The schedule").

The real-process tests run a pythonw.exe parent (no console, as the guard hook and a scheduled task
have) that imports kitlib and then starts console pythons the bare ways: subprocess.run, Popen,
check_output and a multiprocessing child; from THREADS threads at once; when the parent was started
with no std handles; and with a child sending a console Ctrl+C or Ctrl+Break, which only ever
reaches the parent's own new console (the sender checks who is on it first). Every process here
is the test's own."""
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
STD_NAMES = ("in", "out", "err")
FILE_TYPE_CHAR, FILE_TYPE_PIPE = 2, 3
STATUS_CONTROL_C_EXIT = 0xC000013A

# The std handles a process has, as {name: [handle, GetFileType]}: 0 none, -1 invalid (no type),
# file type 1 disk, 2 character (a console), 3 pipe.
STD = "\n".join([
    "def std_handles():",
    "    import ctypes",
    "    k = ctypes.WinDLL('kernel32')",
    "    k.GetStdHandle.restype = ctypes.c_void_p",
    "    k.GetFileType.argtypes = (ctypes.c_void_p,)",
    "    got = {}",
    "    for name, n in (('in', -10), ('out', -11), ('err', -12)):",
    "        h = k.GetStdHandle(n) or 0",
    "        bad = h in (0, 2 ** 64 - 1, 2 ** 32 - 1)",
    "        got[name] = [-1 if h == 2 ** 64 - 1 else h, None if bad else k.GetFileType(h)]",
    "    return got"])

# Run as a child: writes what it sees of its console and std handles to argv[1] (a child of
# pythonw may have no pipe), and prints "said" when argv[2] is "say".
PROBE = "\n".join([
    "import ctypes, json, os, sys",
    STD,
    "k = ctypes.WinDLL('kernel32')",
    "k.GetConsoleWindow.restype = ctypes.c_void_p",
    "a = (ctypes.c_uint * 64)()",
    "n = k.GetConsoleProcessList(a, 64)",
    "with open(sys.argv[1], 'w') as f:",
    "    json.dump({'window': k.GetConsoleWindow() or 0, 'pid': os.getpid(),",
    "               'console_pids': list(a[:min(n, 64)]), 'std': std_handles()}, f)",
    "if sys.argv[2:] == ['say']:",
    "    print('said', flush=True)"])

# Run as a child on the parent's console: says it is ready (argv[1] + ".ready"), sleeps, and
# writes to argv[1] whether a Ctrl+C ended the sleep.
WAITER = "\n".join([
    "import ctypes, sys, time",
    "ctypes.WinDLL('kernel32').SetConsoleCtrlHandler(None, False)",
    "open(sys.argv[1] + '.ready', 'w').close()",
    "try:",
    "    time.sleep(30)",
    "    end = 'timeout'",
    "except KeyboardInterrupt:",
    "    end = 'interrupted'",
    "with open(sys.argv[1], 'w') as f:",
    "    f.write(end)"])

# Run as a child on the parent's console: sends the console control event argv[2] (0 Ctrl+C,
# 1 Ctrl+Break) to the whole console, but only when every process on it is itself or a pid in
# argv[3:] (never a console the test shares).
SENDER = "\n".join([
    "import ctypes, json, os, sys",
    "k = ctypes.WinDLL('kernel32')",
    "keep = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda e: 1)   # it survives the event",
    "k.SetConsoleCtrlHandler(keep, 1)",
    "a = (ctypes.c_uint * 64)()",
    "n = k.GetConsoleProcessList(a, 64)",
    "pids = sorted(a[:min(n, 64)])",
    "ok = 0 < n <= 64 and set(pids) <= {os.getpid()} | {int(p) for p in sys.argv[3:]}",
    "sent = bool(k.GenerateConsoleCtrlEvent(int(sys.argv[2]), 0)) if ok else False",
    "with open(sys.argv[1], 'w') as f:",
    "    json.dump({'console_pids': pids, 'sent': sent, 'refused': not ok}, f)"])
THREADS = 8

# Run as the parent under pythonw: argv is <kit root> <folder> <mode> <console python>. It imports
# kitlib, then starts the probe with no creation flags of its own (modes "alloc" and "fallback"),
# or with console choices first (mode "choice"), or from THREADS threads at once ("threads" and
# "threads-fallback"), or with no std handles of its own ("std" and "std-fallback"), or has a
# child send a Ctrl+C or a Ctrl+Break to the console it shares ("ctrlc", "ctrlbreak"), and writes
# what it saw to <folder>/parent.json.
PARENT = STD + "\nTHREADS = %d\n" % THREADS + r'''
import json, multiprocessing, os, subprocess, sys, threading, time, traceback

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
    console.FORCE_FALLBACK = mode.endswith('fallback')
    r['installed'] = console.installed()
    if mode.startswith('threads'):
        r['n'], r['thread_errors'] = THREADS, []
        barrier = threading.Barrier(THREADS)

        def start(i):
            try:
                barrier.wait(60)
                subprocess.run([py, probe, out('t%d' % i)], timeout=60)
            except BaseException:
                r['thread_errors'].append(traceback.format_exc())
        threads = [threading.Thread(target=start, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
    elif mode.startswith('std'):
        r['std_before'] = std_handles()
        subprocess.run([py, probe, out('run')], timeout=60)
        r['std_after'] = std_handles()
        subprocess.Popen([py, probe, out('popen')]).wait(60)
        r['said'] = subprocess.check_output([py, probe, out('check_output'), 'say'],
                                            timeout=60).decode().strip()
    elif mode in ('ctrlc', 'ctrlbreak'):
        import ctypes
        subprocess.run([py, probe, out('run')], timeout=60)     # this process gets its console
        ctypes.WinDLL('kernel32').SetConsoleCtrlHandler(None, False)    # no inherited ignore
        waiter = subprocess.Popen([py, os.path.join(folder, 'waiter.py'), out('waiter')])
        end = time.monotonic() + 60
        while not os.path.exists(out('waiter') + '.ready') and time.monotonic() < end:
            time.sleep(0.02)
        r['parent_interrupted'] = False
        sender = subprocess.Popen([py, os.path.join(folder, 'sender.py'), out('sender'),
                                   '0' if mode == 'ctrlc' else '1', str(os.getpid()),
                                   str(waiter.pid)])
        try:
            while waiter.poll() is None and time.monotonic() < end:
                time.sleep(0.02)
            time.sleep(0.5)
        except KeyboardInterrupt:
            r['parent_interrupted'] = True
        r['sender_code'], r['waiter_code'] = sender.wait(60), waiter.wait(60)
    elif mode == 'choice':
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


def alloc_how():
    """What ensure answers by default here: ALLOCATED where kernel32 exports
    AllocConsoleWithOptions, else the fallback's ATTACHED."""
    return console.ALLOCATED if alloc_exported() else console.ATTACHED


def file_types(std):
    """{name: GetFileType} of an STD report."""
    return {name: v[1] for name, v in std.items()}


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
                            ("_take_ctrl", lambda: self.calls.append("take")),
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
        self.assertEqual(self.calls, ["alloc", "take", ("log", console.ALLOCATED)])

    def test_no_export_or_a_refusal_takes_the_holder(self):
        for alloc in (None, console.NONE):
            with self.subTest(alloc):
                self.alloc, self.calls[:] = alloc, []
                self.assertEqual(self.ensure(False), console.ATTACHED)
                self.assertEqual(self.calls, ["alloc", "attach", "take", ("log", console.ATTACHED)])

    def test_forced_fallback(self):
        self.assertEqual(self.ensure(False, fallback=True), console.ATTACHED)
        with mock.patch.object(console, "FORCE_FALLBACK", True):
            self.assertEqual(self.ensure(False), console.ATTACHED)
        self.assertEqual(self.calls, ["attach", "take", ("log", console.ATTACHED)] * 2)

    def test_the_ctrl_handler_takes_ctrl_c_and_break_only(self):
        self.assertEqual([console.ctrl_taken(e) for e in range(7)],
                         [True, True, False, False, False, False, False])

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
        for name, text in (("probe.py", PROBE), ("parent.py", PARENT), ("waiter.py", WAITER),
                           ("sender.py", SENDER)):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(text)

    def read(self, name):
        path = os.path.join(self.dir, name)
        self.assertTrue(os.path.exists(path), "%s was not written" % name)
        with open(path) as f:
            return json.load(f)

    def run_parent(self, mode, std="file"):
        """Run the pythonw parent. `std` "file": its stdout is a file the test reads, its stdin
        and stderr NUL. "unset": started with no std handles named (no STARTF_USESTDHANDLES) and
        none inherited, as a GUI program or a scheduled task starts one. "null": std handles
        named, all NULL."""
        argv = [self.pythonw, os.path.join(self.dir, "parent.py"), ROOT, self.dir, mode,
                python_exe()]
        stdout = os.path.join(self.dir, "stdout.txt")
        if std == "file":
            with open(stdout, "wb") as fh:
                subprocess.run(argv, timeout=180, stdin=subprocess.DEVNULL, stdout=fh,
                               stderr=subprocess.DEVNULL)
        else:
            si = subprocess.STARTUPINFO()
            if std == "null":
                si.dwFlags = subprocess.STARTF_USESTDHANDLES
                si.hStdInput = si.hStdOutput = si.hStdError = 0
            subprocess.run(argv, timeout=180, close_fds=True, startupinfo=si)
        r = self.read("parent.json")
        self.assertNotIn("error", r, r.get("error"))
        r["stdout"] = ""
        if os.path.exists(stdout):
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
        self.check_bare("alloc", alloc_how())

    def test_bare_starts_open_no_window_by_the_fallback(self):
        self.check_bare("fallback", console.ATTACHED)

    def check_threads(self, mode, how):
        r = self.run_parent(mode)
        self.assertEqual(r["thread_errors"], [])
        self.assertFalse(r["before"], r)
        pids = set()
        for i in range(r["n"]):
            with self.subTest(i):
                self.assert_shares_a_windowless_console(r, "t%d" % i)
                pids.add(self.read("t%d.json" % i)["pid"])
        self.assertEqual(len(pids), THREADS)
        self.assertEqual([(g["how"], g["pid"]) for g in r["log"]], [(how, r["parent_pid"])])

    def test_bare_starts_from_threads_at_once_make_one_console(self):
        """THREADS threads leave a barrier together and each starts a child bare: one ensure
        (one log line), and every child is on the one console, the parent's."""
        self.check_threads("threads", alloc_how())

    def test_bare_starts_from_threads_at_once_make_one_console_by_the_fallback(self):
        self.check_threads("threads-fallback", console.ATTACHED)

    def check_no_std(self, mode, std, how):
        """A parent started with no std handles (a GUI program or a scheduled task starting
        pythonw): all three NULL. The console ensure gives it fills them with console handles,
        so its bare children get console handles (check_output's stdout is its pipe), and none
        has a window."""
        r = self.run_parent(mode, std)
        self.assertEqual(r["std_before"], {n: [0, None] for n in STD_NAMES})
        self.assertEqual(file_types(r["std_after"]), dict.fromkeys(STD_NAMES, FILE_TYPE_CHAR))
        for name in ("run", "popen", "check_output"):
            with self.subTest(name):
                self.assert_shares_a_windowless_console(r, name)
        for name in ("run", "popen"):
            self.assertEqual(file_types(self.read(name + ".json")["std"]),
                             dict.fromkeys(STD_NAMES, FILE_TYPE_CHAR), name)
        self.assertEqual(file_types(self.read("check_output.json")["std"]),
                         {"in": FILE_TYPE_CHAR, "out": FILE_TYPE_PIPE, "err": FILE_TYPE_CHAR})
        self.assertEqual(r["said"], "said")
        self.assertEqual([(g["how"], g["pid"]) for g in r["log"]], [(how, r["parent_pid"])])

    def test_a_parent_with_no_std_handles_named(self):
        self.check_no_std("std", "unset", alloc_how())

    def test_a_parent_with_null_std_handles(self):
        self.check_no_std("std", "null", alloc_how())

    def test_a_parent_with_no_std_handles_named_by_the_fallback(self):
        self.check_no_std("std-fallback", "unset", console.ATTACHED)

    def check_ctrl(self, mode):
        """A child on the console the parent got sends it a console control event; the sender
        sends only when the console holds just itself, the parent and the waiter. The parent is
        not ended by it (its report is written: run_parent) and is not interrupted."""
        r = self.run_parent(mode)
        sent = self.read("sender.json")
        self.assertEqual((sent["refused"], sent["sent"]), (False, True), sent)
        self.assertIn(r["parent_pid"], sent["console_pids"], sent)
        self.assertFalse(r["parent_interrupted"], r)
        return r

    def test_a_console_ctrl_c_leaves_the_parent_and_reaches_its_children(self):
        r = self.check_ctrl("ctrlc")
        with open(os.path.join(self.dir, "waiter.json")) as f:
            self.assertEqual(f.read(), "interrupted")       # KeyboardInterrupt, as before
        self.assertEqual(r["waiter_code"], 0)

    def test_a_console_ctrl_break_leaves_the_parent_and_ends_its_children(self):
        r = self.check_ctrl("ctrlbreak")
        self.assertEqual(r["waiter_code"], STATUS_CONTROL_C_EXIT)

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
