"""machine_lock.py run --detach (kitlib.lock.detach): WMI starts pythonw.exe running the carrier
tools/no_window.py, which starts the console python running machine_lock.py, so the run has no
console window; ShowWindow is not asked for. Unit tests of the argv, the WMI call and the wrapper
lookup on fakes; one CLI test through main() with the WMI call faked; one real detach on a scratch
store (DetachEndToEnd) that looks for a console window of the run's wrapper while it runs."""
import contextlib
import ctypes
import io
import json
import os
import subprocess
import sys
import time
import unittest
from ctypes import wintypes
from unittest import mock

from .helpers import REPO, StoreCase, clean_env
from .test_cli import load_tool

if os.name == "nt":
    from kitlib.lock import detach as dt
    from kitlib.proc import process, spawn

TOOL = os.path.join(REPO, "tools", "machine_lock.py")
CARRIER = os.path.join(REPO, "tools", "no_window.py")
GONE_PID = 0xFFFFFFF0                       # no process has it: proc_ctime gives None


def folder(p):
    return os.path.normcase(os.path.dirname(p))


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class Command(unittest.TestCase):
    def test_pythonw_runs_the_carrier_which_runs_the_console_python(self):
        py = r"C:\Py\python.exe"
        argv = dt.command(py, r"C:\kit\tools\machine_lock.py", ["run", "--kind", "k", "--detach"],
                          "r-1", ["--", "cmd", "x y"])
        self.assertEqual(argv, [r"C:\Py\pythonw.exe", r"C:\kit\tools\no_window.py",
                                r"C:\Py\python.exe", "-B", r"C:\kit\tools\machine_lock.py",
                                "run", "--kind", "k", "--detach", "--detached-id", "r-1",
                                "--", "cmd", "x y"])

    def test_from_pythonw_the_console_python_is_still_python_exe(self):
        argv = dt.command(r"C:\Py\pythonw.exe", r"C:\kit\tools\machine_lock.py", [], "r-1", ["--"])
        self.assertEqual(argv[:3], [r"C:\Py\pythonw.exe", r"C:\kit\tools\no_window.py",
                                    r"C:\Py\python.exe"])

    def test_the_carrier_is_the_kit_one(self):
        argv = dt.command(sys.executable, TOOL, [], "r-1", ["--"])
        self.assertEqual(os.path.basename(argv[1]), spawn.CARRIER)
        self.assertTrue(os.path.isfile(argv[1]), argv[1])
        self.assertIsNone(dt.missing(argv))

    def test_missing_names_the_first_file_that_is_not_there(self):
        tmp = os.path.join(REPO, "no-such-folder")
        argv = dt.command(os.path.join(tmp, "python.exe"), TOOL, [], "r-1", ["--"])
        self.assertIn("pythonw.exe is not a file", dt.missing(argv))
        argv[0] = sys.executable
        argv[2] = os.path.join(tmp, "python.exe")
        self.assertIn("python.exe is not a file", dt.missing(argv))


class FakeRun:
    def __init__(self, stdout="0 4242\n", code=0, stderr="", raises=None):
        self.stdout, self.code, self.stderr, self.raises = stdout, code, stderr, raises
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(args, self.code, self.stdout, self.stderr)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class Create(unittest.TestCase):
    def test_wmi_gets_the_command_line_cwd_and_environment_and_no_show_window(self):
        run = FakeRun()
        argv = [r"C:\Py\pythonw.exe", r"C:\kit\tools\no_window.py", "a b"]
        self.assertEqual(dt.create(argv, r"C:\w", {"A": "1"}, run=run), (4242, None))
        (args, kw), = run.calls
        self.assertEqual(args[0], "powershell")
        self.assertIn("Win32_Process", args[-1])
        self.assertNotIn("ShowWindow", args[-1])
        data = json.loads(kw["input"])
        self.assertEqual(data, {"cmd": subprocess.list2cmdline(argv), "cwd": r"C:\w",
                                "env": ["A=1"]})

    def test_failures_are_a_reason_and_no_pid(self):
        for run in (FakeRun(stdout="9 0"), FakeRun(code=1, stderr="boom"), FakeRun(stdout=""),
                    FakeRun(raises=OSError("no powershell")),
                    FakeRun(raises=subprocess.TimeoutExpired("powershell", 60))):
            with self.subTest(stdout=run.stdout, code=run.code, raises=run.raises):
                pid, why = dt.create(["x"], "C:\\", {}, run=run)
                self.assertIsNone(pid)
                self.assertTrue(why)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class WrapperPid(unittest.TestCase):
    def lookup(self, snaps, ctimes, wait_s=1.0):
        snaps = list(snaps)
        clock = iter(range(1000))
        return dt.wrapper_pid(100, wait_s=wait_s, poll_s=0,
                              snapshot=lambda: snaps.pop(0) if len(snaps) > 1 else snaps[0],
                              ctime=ctimes.get, sleep=lambda s: None,
                              clock=lambda: next(clock) / 10.0)

    def test_the_carriers_python_child(self):
        snaps = [[(100, 4, "pythonw.exe")],
                 [(100, 4, "pythonw.exe"), (7, 100, "conhost.exe"), (200, 100, "python.exe")]]
        self.assertEqual(self.lookup(snaps, {100: 50, 200: 60, 7: 55}), 200)

    def test_a_reused_parent_pid_is_not_the_carriers_child(self):
        snaps = [[(100, 4, "pythonw.exe"), (300, 100, "python.exe")]]
        self.assertIsNone(self.lookup(snaps, {100: 50, 300: 40}))

    def test_none_when_the_carrier_is_gone(self):
        self.assertIsNone(self.lookup([[]], {}))

    def test_none_when_no_child_is_seen_in_time(self):
        self.assertIsNone(self.lookup([[(100, 4, "pythonw.exe")]], {100: 50}, wait_s=0.5))

    def test_a_failed_snapshot_is_none(self):
        def boom():
            raise OSError("no snapshot")
        self.assertIsNone(dt.wrapper_pid(100, snapshot=boom, ctime={100: 1}.get))


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class DetachCli(StoreCase):
    """main(['run', ..., '--detach']) with the WMI call faked: what it asks WMI to start."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tool = load_tool()

    def call(self, *argv, cmd):
        hw = os.path.join(self.tmp, "hardware.json")
        with open(hw, "w", encoding="utf-8") as f:
            json.dump(self.hw, f)
        args = list(argv) + ["--test-root", self.root, "--test-hardware", hw,
                             "--test-kinds", self.kinds_path, "--"] + list(cmd)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, clean_env(), clear=True),                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_detach_asks_wmi_for_pythonw_and_the_carrier(self):
        run = FakeRun(stdout="0 %d" % GONE_PID)
        with mock.patch("kitlib.proc.spawn.run", run):
            code, out, err = self.call("run", "--kind", "engine-test", "--repo", "kit", "--slug",
                                       "t", "--cores", "1", "--no-wait", "--detach",
                                       cmd=[sys.executable, "-V"])
        self.assertEqual(code, 0, err)
        rid, word, pid = out.split()
        self.assertTrue(rid.startswith("r-"))
        self.assertEqual((word, pid), ("pid", str(GONE_PID)))
        (args, kw), = run.calls
        self.assertNotIn("ShowWindow", args[-1])
        cmdline = json.loads(kw["input"])["cmd"]
        self.assertTrue(cmdline.startswith(subprocess.list2cmdline(
            [os.path.join(os.path.dirname(sys.executable), "pythonw.exe"), CARRIER,
             os.path.join(os.path.dirname(sys.executable), "python.exe"), "-B", TOOL])),
            cmdline)
        self.assertIn("--detached-id %s" % rid, cmdline)
        self.assertFalse(os.path.isdir(self.store.path("ended"))
                         and os.listdir(self.store.path("ended")), "the caller booked a run")


U = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")


def console_windows_of(pids):
    """[(class, visible)] of top-level console-host windows whose process is one of `pids` (a
    conhost console window reports its client's pid)."""
    out = []
    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(h, _):
        pid = wintypes.DWORD()
        U.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(256)
            U.GetClassNameW(h, buf, 256)
            if buf.value in CONSOLE_CLASSES:
                out.append((buf.value, bool(U.IsWindowVisible(h))))
        return True
    U.EnumWindows(cb(each), 0)
    return out


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class DetachEndToEnd(StoreCase):
    """A real detach on a scratch store: WMI starts the carrier, the run's wrapper books the
    scratch store, runs a 2 s sleep and ends. While it runs, neither the wrapper nor its command
    owns a console window, hidden or shown (before the carrier, WMI's console python had a hidden
    ConsoleWindowClass window). The printed pid is the run record's pid."""

    def test_a_detached_run_has_no_console_window_and_ends(self):
        hw = os.path.join(self.tmp, "hardware.json")
        with open(hw, "w", encoding="utf-8") as f:
            json.dump(self.hw, f)
        argv = [sys.executable, TOOL, "run", "--kind", "engine-test", "--repo", "kit", "--slug",
                "detach-e2e", "--cores", "1", "--no-wait", "--detach", "--test-root", self.root,
                "--test-hardware", hw, "--test-kinds", self.kinds_path, "--",
                sys.executable, "-c", "import time, sys; time.sleep(2); sys.exit(3)"]
        r = subprocess.run(argv, capture_output=True, text=True, env=clean_env(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        rid, _, pid = r.stdout.split()
        pid = int(pid)
        ended = self.store.path("ended", rid + ".json")
        seen, limit = [], time.monotonic() + 60
        while not os.path.exists(ended) and time.monotonic() < limit:
            tree = {pid} | {p for p, _c in process.descendants(pid, process.proc_ctime(pid))}
            seen += console_windows_of(tree)
            time.sleep(0.05)
        self.assertTrue(os.path.exists(ended), "the detached run did not end within 60 s")
        with open(ended, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual((rec["pid"], rec["code"], rec["detached"]), (pid, 3, True))
        self.assertEqual(seen, [], "a console window belonged to the detached run")


if __name__ == "__main__":
    unittest.main()
