"""machine_lock.py run --detach through the tool: what main() asks WMI to start (the WMI call
faked), and one real detach on a scratch store (DetachEndToEnd) that looks for a console window of
the run's wrapper while it runs. Neither imports kitlib.lock.detach, so each runs against a tool
without it. Unit tests of that module: tests/lock/test_detach.py."""
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
    from kitlib.proc import process

TOOL = os.path.join(REPO, "tools", "machine_lock.py")
CARRIER = os.path.join(REPO, "tools", "no_window.py")
GONE_PID = 0xFFFFFFF0                       # no process has it: proc_ctime gives None


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
