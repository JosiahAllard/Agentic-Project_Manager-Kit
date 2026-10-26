"""Tests for kitlib.proc.console: the process-wide CreateProcess hook that gives a process with no
console a console with no window before its first start, so no start in a kit process opens a
console window (docs/daily-full.md "The schedule").

The real-process tests run a pythonw.exe parent (no console, as the guard hook and a scheduled task
have) that imports kitlib and then starts console pythons the bare ways: subprocess.run, Popen,
check_output and a multiprocessing child; from THREADS threads at once; when the parent was started
with no std handles; and with a child sending a console Ctrl+C or Ctrl+Break, which only ever
reaches the parent's own new console (the sender checks who is on it first). Every process here
is the test's own."""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from tests.platforms import windows_only

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

# The sender's guard: every process on the console is the sender, a root (the parent, the waiter)
# or a descendant of one (a venv's python.exe redirector starts the real interpreter as its child,
# on the same console). `parent_of` is {pid: parent pid}; a loop in it (a reused pid) ends the
# walk, refused.
SENDER_GUARD = "\n".join([
    "def on_own_console(pids, me, roots, parent_of):",
    "    allowed = {me} | set(roots)",
    "    for pid in pids:",
    "        seen, p = set(), pid",
    "        while p not in allowed and p in parent_of and p not in seen:",
    "            seen.add(p)",
    "            p = parent_of[p]",
    "        if p not in allowed:",
    "            return False",
    "    return len(pids) > 0"])

# Run as a child on the parent's console: argv is <out> <event> <kit root> <send|dry> <root pid>...
# It sends the console control event (0 Ctrl+C, 1 Ctrl+Break) to the whole console, but only when
# SENDER_GUARD passes (never a console the test shares), and only in "send" mode.
SENDER = "\n".join([
    "import ctypes, json, os, sys",
    "sys.path.insert(0, sys.argv[3])",
    "from kitlib.proc import process",
    SENDER_GUARD,
    "k = ctypes.WinDLL('kernel32')",
    "keep = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda e: 1)   # it survives the event",
    "k.SetConsoleCtrlHandler(keep, 1)",
    "a = (ctypes.c_uint * 64)()",
    "n = k.GetConsoleProcessList(a, 64)",
    "pids = sorted(a[:min(n, 64)])",
    "parent_of = {pid: pp for pid, pp, _name in process.snapshot()}",
    "roots = [int(p) for p in sys.argv[5:]]",
    "ok = n <= 64 and on_own_console(pids, os.getpid(), roots, parent_of)",
    "send = ok and sys.argv[4] == 'send'",
    "sent = bool(k.GenerateConsoleCtrlEvent(int(sys.argv[2]), 0)) if send else False",
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
                                   '0' if mode == 'ctrlc' else '1', root, 'send',
                                   str(os.getpid()), str(waiter.pid)])
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


@windows_only("Windows only")
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


@windows_only("Windows only")
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


@windows_only("Windows only")
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


@windows_only("Windows only")
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


@windows_only("Windows only")
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
            (["--pass", "abc", "--passphrase", "abc", "--pw", "abc", "-p", "kept"],
             ["--pass", "***", "--passphrase", "***", "--pw", "***", "-p", "kept"]),
            (["-u", "alice:abc", "--user", "alice:abc", "--user=alice:abc", "-U", "bob:abc"],
             ["-u", "alice:***", "--user", "alice:***", "--user=alice:***", "-U", "bob:***"]),
            # a user flag's value: all after its first : (a drive path aside), or whole if it
            # ends at that :
            (["-u", "C:\\kept\\x.py", "-u", "kept", "--user", "git+https://h/x:1"],
             ["-u", "C:\\kept\\x.py", "-u", "kept", "--user", "git+https:***"]),
            (["-u", ":PAT", "-u", "sk_test_abc:", "-u", "a/b:pw", "-u", "user:pa://x"],
             ["-u", ":***", "-u", "***:", "-u", "a/b:***", "-u", "user:***"]),
            (["https://h/x?a=1&access_token=abc&b=2", "--data=a=1&password=abc", "--data=pw=abc"],
             ["https://h/x?a=1&access_token=***&b=2", "--data=a=1&password=***", "--data=pw=***"]),
            (['{"user":"kept","password":"abc","n":1}', '{"apiKey": 12345, "a": "pw=abc"}'],
             ['{"user":"kept","password":"***","n":1}', '{"apiKey": ***, "a": "pw=***"}']),
            (['{"token":"abc', '{"token":"a\\"bc"}', '{"token":"abc\\'],
             ['{"token":"***"', '{"token":"***"}', '{"token":"***"']),
            (["git://tok@h/r", "https://u:p%2Fw@h/x", "ftp://h/kept@x"],
             ["git://***@h/r", "https://***@h/x", "ftp://h/kept@x"]),
            # URL user info: with a : after scheme://, all up to the last @ (a password may hold
            # / # or @; a port before /@ is masked too); else the authority up to its last @
            (["https://user:pa#ss@host/", "https://u:pa/ss@h/x", "https://u:p/w@h/x",
              "https://u:p@ss@h/x", "https://host:8080/@scope/pkg"],
             ["https://***@host/", "https://***@h/x", "https://***@h/x", "https://***@h/x",
              "https://***@scope/pkg"]),
            (["https://h?q=@kept", "https://h/x?r=git://tok@h2/y", "-u", "https://u:pa/ss@h"],
             ["https://h?q=@kept", "https://***@h2/y", "-u", "https:***"]),
            # the value of -u/--user gets every other mask too, and a flag is not its value
            (["-u", "https://user:" + "ghp_" + "a" * 36 + "@github.com/o/r.git", "main"],
             ["-u", "https:***", "main"]),
            (["-u", "ghp_" + "a" * 36, "-u", "GITHUB_TOKEN=abc", "--user=https://u:p@h/"],
             ["-u", "***", "-u", "GITHUB_TOKEN=***", "--user=https:***"]),
            (["-u", "--token", "abc", "--token", "--password", "abc", "-u", "-v", "kept"],
             ["-u", "--token", "***", "--token", "--password", "***", "-u", "-v", "kept"]),
            # user flags: a cluster ending in u, glued, long forms, /u, DOMAIN\user, any case
            (["-sSu", "a:abc", "-uuser:abc", "--proxy-user", "b:abc", "--proxy-user=c:abc",
              "/u", "d:abc", "-u", "DOMAIN\\e:abc", "-U", "f:abc", "--USER", "g:abc"],
             ["-sSu", "a:***", "-uuser:***", "--proxy-user", "b:***", "--proxy-user=c:***",
              "/u", "d:***", "-u", "DOMAIN\\e:***", "-U", "f:***", "--USER", "g:***"]),
            (["\\\\h\\s", "/user:DOMAIN\\e", "abc", "/persistent:no"],      # net use
             ["\\\\h\\s", "/user:DOMAIN\\e", "***", "/persistent:no"]),
            # -p/-P/ /p go with a word starting -u or --user seen before them (or sshpass);
            # /RP and a glued -pVALUE always
            (["-u", "root", "-h", "db", "-p", "abc", "-P", "abc", "-pabc", "/p:abc"],
             ["-u", "root", "-h", "db", "-p", "***", "-P", "***", "-p***", "/p:***"]),
            (["sshpass", "-p", "abc", "ssh", "h"], ["sshpass", "-p", "***", "ssh", "h"]),
            (["/create", "/u", "e", "/p", "abc", "/RP", "abc", "/rp:abc"],
             ["/create", "/u", "e", "/p", "***", "/RP", "***", "/rp:***"]),
            (["-uroot", "-P", "abc", "-Pabc"], ["-uroot", "-P", "***", "-P***"]),
            (["--user=root", "-p", "abc"], ["--user=root", "-p", "***"]),
            (["-h", "h", "-pabc", "-psecret", "x", "-P3306"],
             ["-h", "h", "-p***", "-p***", "***", "-P3306"]),
            (["-p", "kept", "-P", "8080", "-P8080"], ["-p", "kept", "-P", "8080", "-P8080"]),
            # a quoted value is masked to its closing quote (or the end), keeping its quotes
            (["f(password='hunter2')", "$env:TOKEN='abc'", "{'token': 'abc'}", 'f(pw="a b")'],
             ["f(password='***')", "$env:TOKEN='***'", "{'token': '***'}", 'f(pw="***")']),
            (["x=1;secret='a\\'b';y=kept", "f(password='abc def", '["token": "a,b", 2]'],
             ["x=1;secret='***';y=kept", "f(password='***'", '["token": "***", 2]']),
            # name = value with spaces, in one word or across words; bracketed and bare keys
            (["password = 'hunter2'", "$env:GITHUB_TOKEN = 'abc'", "os.environ['TOKEN']='abc'",
              "{password:abc}", "{a:1, secret : abc}", "mode = kept"],
             ["password = '***'", "$env:GITHUB_TOKEN = '***'", "os.environ['TOKEN']='***'",
              "{password:***}", "{a:1, secret : ***}", "mode = kept"]),
            (["$env:GITHUB_TOKEN", "=", "'abc'", "password", "=", "abc", "mode", "=", "kept",
              '{"password":', '"abc"}'],
             ["$env:GITHUB_TOKEN", "=", "***", "password", "=", "***", "mode", "=", "kept",
              '{"password":', "***"]),
            # Bearer and Basic hide the next word under any header name
            (["-H", "X-Custom: Bearer abc", "X-Api: basic Zm9vOmJhcg==", "-H",
              "Authorization:", "Bearer", "abc"],
             ["-H", "X-Custom: Bearer ***", "X-Api: basic ***", "-H",
              "Authorization:***", "Bearer", "***"]),
            # pass and pw are secret anywhere in a name, digits aside, but for a few words
            (["DBPASS=a", "PGPASS=a", "ROOTPASS=a", "userpass=a", "adminpw=a", "SMTPPW=a",
              "PASSCODE=a", "PASS_PHRASE=a", "PASS1=a", "DB_PASS_2=a", "dbPass=a"],
             ["DBPASS=***", "PGPASS=***", "ROOTPASS=***", "userpass=***", "adminpw=***",
              "SMTPPW=***", "PASSCODE=***", "PASS_PHRASE=***", "PASS1=***", "DB_PASS_2=***",
              "dbPass=***"]),
            (["--passcode", "a", "--pass-phrase", "a", "--pass-file", "a", "--smtp-pw", "a",
              "--passwd", "a", "-PassThruPassword", "a"],
             ["--passcode", "***", "--pass-phrase", "***", "--pass-file", "***", "--smtp-pw",
              "***", "--passwd", "***", "-PassThruPassword", "***"]),
            (["-PassThru", "kept", "/passive", "kept", "--bypass-list", "kept", "--stopwatch",
              "kept", "--passthrough", "kept"],
             ["-PassThru", "kept", "/passive", "kept", "--bypass-list", "kept", "--stopwatch",
              "kept", "--passthrough", "kept"]),
            # -PassThru ends in u, so it counts as a user flag: a glued -P after it is masked
            (["-PassThru:$true", "kept"], ["-PassThru:$true", "kept"]),
            (["-PassThru", "x", "-PassThru:$true"], ["-PassThru", "x", "-P***"]),
        ]
        for words, want in cases:
            with self.subTest(words[0]), mock.patch.object(console, "KEEP_WORDS", 99):
                self.assertEqual(console.redact_command(["prog"] + words), ["prog"] + want)

    def test_odd_input_never_raises(self):
        cases = [
            ('prog --token "abc def', 'prog --token ***'),           # an unterminated quote
            ('prog --token abc\\', 'prog --token ***'),              # a trailing backslash
            ('prog C:\\kept\\', 'prog C:\\kept\\'),
            ('prog "', 'prog ""'),
            ('', ''),
        ]
        for line, want in cases:
            with self.subTest(line):
                self.assertEqual(console.redact_command(line), want)
        self.assertEqual(console.redact_command(b"prog \xff\xfe --pw x"),
                         "prog \\xff\\xfe --pw ***")             # a bad byte is not a lost line
        self.assertEqual(console.redact_command([b"prog", b"\xff", b"--pw", b"x"]),
                         ["prog", "\\xff", "--pw", "***"])
        for odd in (12, object(), [None, 3, b"\xff"], ["prog", "=" * 5000 + "token=x"]):
            with self.subTest(type(odd).__name__):
                console.redact_command(odd)          # no exception
        with mock.patch.object(console, "_mask", side_effect=ValueError("boom")):
            self.assertEqual(console.redact_command("prog --x"), console.MASK)
            self.assertEqual(console.redact_command(["prog"]), [console.MASK])

    def test_long_hostile_words_are_masked_in_linear_time(self):
        # each about 90k characters; a pattern that restarts inside a run (the JWT form on
        # `eyJ` repeated took 8 s here) is quadratic, the masks all take well under 1 s
        import time
        words = ["eyJ" * 30000, 'x="' * 30000, "{a:" * 30000, "'a':" * 22000,
                 "-" + "u" * 90000 + ":", "https://" + ":@" * 45000, "://" * 30000,
                 "aB" * 45000 + "=x", "a!" * 45000, "$env:TOKEN = " * 7000,
                 "a" + " " * 90000 + "b", "a" + "	" * 90000 + "b"]
        for w in words:
            with self.subTest(w[:12]):
                start = time.perf_counter()
                console._mask(["-u", w, "--token", w, w])
                self.assertLess(time.perf_counter() - start, 3.0)

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


class SenderGuardTest(unittest.TestCase):
    """The Ctrl+C sender's guard (SENDER_GUARD): it sends only on a console of the test's own."""

    def setUp(self):
        ns = {}
        exec(SENDER_GUARD, ns)                  # noqa: S102 - the test's own source
        self.guard = ns["on_own_console"]

    def test_the_roots_and_their_descendants_pass(self):
        # 1 parent, 2 waiter (a venv redirector), 3 its real interpreter, 4 that one's child
        parent_of = {1: 100, 2: 1, 3: 2, 4: 3, 10: 1}
        self.assertTrue(self.guard([1, 2, 3, 4, 10], 10, [1, 2], parent_of))
        self.assertTrue(self.guard([10], 10, [], parent_of))

    def test_any_other_process_is_refused(self):
        parent_of = {1: 100, 2: 1, 10: 1, 100: 4, 7: 100, 8: 9, 9: 8}
        for pid, why in ((100, "the parent's parent"), (7, "a sibling of the parent"),
                         (8, "a pid in a parent loop"), (55, "not in the snapshot")):
            with self.subTest(why):
                self.assertFalse(self.guard([1, 2, 10, pid], 10, [1, 2], parent_of))
        self.assertFalse(self.guard([], 10, [1, 2], parent_of))

    @windows_only("Windows only")
    def test_the_real_sender_refuses_the_test_s_console(self):
        """The sender itself, run dry from this process, sees this process on its console (not
        a root, not its descendant) and refuses."""
        folder = tempfile.mkdtemp(prefix="kit-console-")
        self.addCleanup(shutil.rmtree, folder, True)
        script, out = os.path.join(folder, "sender.py"), os.path.join(folder, "sender.json")
        with open(script, "w") as f:
            f.write(SENDER)
        subprocess.run([sys.executable, script, out, "0", ROOT, "dry"], timeout=60, check=True)
        with open(out) as f:
            got = json.load(f)
        self.assertIn(os.getpid(), got["console_pids"], got)
        self.assertEqual((got["refused"], got["sent"]), (True, False), got)


@windows_only("Windows only")
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
        sends only when the console holds just itself, the parent, the waiter and their
        descendants (SENDER_GUARD). The parent is
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


@windows_only("Windows only")
class CarrierFailureLine(unittest.TestCase):
    """The carrier's line when it cannot start its program: with no stderr under pythonw, the
    incident log is all that is left of a detached run that never started. console.py is the one
    writer of that log."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.log = os.path.join(d.name, "sub", "incidents.log")

    def lines(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def tool(self):
        spec = importlib.util.spec_from_file_location(
            "no_window_tool", os.path.join(ROOT, "tools", "no_window.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        return tool

    def test_one_json_line_with_the_program_file_name_only(self):
        now = datetime.datetime(2026, 10, 13, 9, 30, 5).timetuple()
        self.assertEqual(console.note_carrier_failure(r'"C:\Py\python.exe"', "exit 127\n(x)",
                                                      path=self.log, now=now, pid=42), self.log)
        self.assertEqual(self.lines(), [{"time": "2026-10-13T09:30:05Z", "how": "carrier-fail",
                                         "pid": 42, "exe": "python.exe", "why": "exit 127 (x)"}])
        console.note_carrier_failure("C:/a/b.exe", "again", path=self.log)
        self.assertEqual([x["exe"] for x in self.lines()], ["python.exe", "b.exe"])  # appended

    def test_a_line_that_cannot_be_written_is_not_an_error(self):
        open(os.path.join(os.path.dirname(os.path.dirname(self.log)), "file"), "w").close()
        blocked = os.path.join(os.path.dirname(os.path.dirname(self.log)), "file", "x.log")
        self.assertIsNone(console.note_carrier_failure("python", "why", path=blocked))

    def test_the_log_is_the_kits_console_incident_log_in_its_work_temp(self):
        self.assertEqual(console.carrier_log_path(environ={console.CARRIER_LOG_ENV: self.log}), self.log)
        with mock.patch.object(kuser, "work_temp", lambda near: os.path.join("W:\\", "t")), \
                mock.patch.object(console, "LOG_PATH", None):
            self.assertEqual(console.carrier_log_path(environ={}),
                             os.path.join("W:\\", "t", console.LOG_NAME))
        self.assertEqual(console.LOG_NAME, "kit-console-incidents.log")

    def test_no_work_temp_folder_skips_the_line_and_never_raises(self):
        """A fenced test process's HomeFenced is a BaseException and is caught as well."""
        for err in (RuntimeError("no drive"), kuser.HomeFenced("fenced")):
            with self.subTest(err=type(err).__name__), \
                    mock.patch.dict(os.environ, {console.CARRIER_LOG_ENV: ""}), \
                    mock.patch.object(console, "LOG_PATH", None), \
                    mock.patch.object(kuser, "work_temp", mock.Mock(side_effect=err)):
                self.assertIsNone(console.carrier_log_path(environ={}))
                self.assertIsNone(console.note_carrier_failure("python", "why"))
        with mock.patch.object(console.os, "makedirs", mock.Mock(side_effect=kuser.HomeFenced("x"))):
            self.assertIsNone(console.note_carrier_failure("python", "why", path=self.log))

    def test_the_carrier_logs_an_exception_through_console_and_lets_it_out(self):
        """tools/no_window.py's exit path when spawn raises (not an OSError, which carry turns
        into 127: tests/proc/test_no_window.py CarrierTest)."""
        from kitlib.proc import spawn
        tool = self.tool()
        env = mock.patch.dict(os.environ, {console.CARRIER_LOG_ENV: self.log})
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(spawn, "carry", mock.Mock(side_effect=ValueError("bad handle"))):
            with self.assertRaises(ValueError):
                tool.main(["python", "-B", "secret.py"])
        line, = self.lines()
        self.assertEqual((line["how"], line["exe"], line["why"]), ("carrier-fail", "python", "ValueError"))
        self.assertNotIn("secret", json.dumps(line))
        self.assertNotIn("bad handle", json.dumps(line))   # the type only: a message can echo argv
        with mock.patch.object(spawn, "carry", lambda argv: 127):
            self.assertEqual(tool.main(["python"]), 127)
        self.assertEqual(len(self.lines()), 2)
        with mock.patch.object(spawn, "carry", lambda argv: 0):
            self.assertEqual(tool.main(["python"]), 0)
        self.assertEqual(len(self.lines()), 2)

    def test_a_failing_log_changes_neither_the_exit_code_nor_the_exception(self):
        from kitlib.proc import spawn
        tool = self.tool()
        broken = mock.Mock(side_effect=RuntimeError("log broke"))
        with mock.patch.object(console, "note_carrier_failure", broken):
            with mock.patch.object(spawn, "carry", lambda argv: 127):
                self.assertEqual(tool.main(["python"]), 127)
            with mock.patch.object(spawn, "carry", mock.Mock(side_effect=ValueError("x"))):
                with self.assertRaises(ValueError):
                    tool.main(["python"])
        self.assertEqual(broken.call_count, 2)


if __name__ == "__main__":
    unittest.main()
