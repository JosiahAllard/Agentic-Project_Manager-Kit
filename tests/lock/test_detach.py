"""kitlib.lock.detach, machine_lock.py run --detach's logic: WMI starts pythonw.exe running the
carrier tools/no_window.py, which starts the console python running machine_lock.py, so the run
has no console window; ShowWindow is not asked for. Unit tests of the argv, the WMI call and the
wrapper lookup on fakes. Through the tool, and a real detach: tests/lock/test_detach_cli.py."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import datetime
import importlib.util
import unittest
from unittest import mock

from .helpers import REPO

if os.name == "nt":
    from kitlib.lock import detach as dt
    from kitlib.proc import spawn

TOOL = os.path.join(REPO, "tools", "machine_lock.py")


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.py = os.path.join(self.tmp, "python.exe")
        self.pyw = os.path.join(self.tmp, "pythonw.exe")
        for path in (self.py, self.pyw):
            open(path, "wb").close()

    def test_pythonw_runs_the_carrier_which_runs_the_console_python(self):
        argv = dt.command(self.py, r"C:\kit\tools\machine_lock.py", ["run", "--kind", "k", "--detach"],
                          "r-1", ["--", "cmd", "x y"])
        self.assertEqual(argv, [self.pyw, r"C:\kit\tools\no_window.py",
                                self.py, "-B", r"C:\kit\tools\machine_lock.py",
                                "run", "--kind", "k", "--detach", "--detached-id", "r-1",
                                "--", "cmd", "x y"])

    def test_from_pythonw_the_console_python_is_still_python_exe(self):
        argv = dt.command(self.pyw, r"C:\kit\tools\machine_lock.py", [], "r-1", ["--"])
        self.assertEqual(argv[:3], [self.pyw, r"C:\kit\tools\no_window.py", self.py])

    def test_the_carrier_is_the_kit_one_by_path(self):
        argv = dt.command(sys.executable, TOOL, [], "r-1", ["--"])
        self.assertEqual(os.path.normcase(argv[1]),
                         os.path.normcase(os.path.join(REPO, "tools", spawn.CARRIER)))
        self.assertEqual(spawn.CARRIER, "no_window.py")
        self.assertTrue(os.path.isfile(argv[1]), argv[1])
        self.assertIsNone(dt.missing(argv))

    def test_no_pythonw_beside_python_is_missing_not_a_console_start(self):
        os.remove(self.pyw)
        argv = dt.command(self.py, TOOL, [], "r-1", ["--"])
        self.assertEqual(dt.missing(argv), "pythonw.exe is not a file: %s" % self.pyw)

    def test_missing_names_the_first_file_that_is_not_there(self):
        tmp = os.path.join(REPO, "no-such-folder")
        argv = dt.command(os.path.join(tmp, "python.exe"), TOOL, [], "r-1", ["--"])
        self.assertIn("pythonw.exe is not a file", dt.missing(argv))
        argv = dt.command(self.py, TOOL, [], "r-1", ["--"])
        argv[1] = os.path.join(tmp, "no_window.py")
        self.assertIn("the carrier is not a file", dt.missing(argv))
        argv = dt.command(self.py, TOOL, [], "r-1", ["--"])
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
class CarrierFailureLine(unittest.TestCase):
    """The carrier's line when it cannot start its program: with no stderr under pythonw, the
    incident log is all that is left of a detached run that never started."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.log = os.path.join(d.name, "sub", "incidents.log")

    def lines(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_one_line_with_the_program_file_name_only(self):
        now = datetime.datetime(2026, 10, 13, 9, 30, 5)
        self.assertEqual(dt.note_carrier_failure(r'"C:\Py\python.exe"', "exit 127\n(x)",
                                                 path=self.log, now=now, pid=42), self.log)
        self.assertEqual(self.lines(),
                         ["2026-10-13T09:30:05 pid 42 no_window.py could not start python.exe: exit 127 (x)"])
        dt.note_carrier_failure("C:/a/b.exe", "again", path=self.log)
        self.assertEqual(len(self.lines()), 2)          # appended, never replaced

    def test_a_line_that_cannot_be_written_is_not_an_error(self):
        open(os.path.join(os.path.dirname(os.path.dirname(self.log)), "file"), "w").close()
        blocked = os.path.join(os.path.dirname(os.path.dirname(self.log)), "file", "x.log")
        self.assertIsNone(dt.note_carrier_failure("python", "why", path=blocked))

    def test_the_log_is_the_kits_console_incident_log_in_its_work_temp(self):
        from kitlib.config import user
        self.assertEqual(dt.incident_log(environ={dt.INCIDENT_LOG_ENV: self.log}), self.log)
        with mock.patch.object(user, "work_temp", lambda near: os.path.join("W:\\", "t")):
            self.assertEqual(dt.incident_log(environ={}), os.path.join("W:\\", "t", dt.INCIDENT_LOG))
        with mock.patch.object(user, "work_temp", mock.Mock(side_effect=RuntimeError("no drive"))):
            self.assertEqual(dt.incident_log(environ={}),
                             os.path.join(tempfile.gettempdir(), dt.INCIDENT_LOG))
        self.assertEqual(dt.INCIDENT_LOG, "kit-console-incidents.log")

    def test_the_carrier_logs_an_exception_and_lets_it_out(self):
        """tools/no_window.py's exit path when spawn raises (not an OSError, which carry turns
        into 127: tests/proc/test_no_window.py CarrierTest)."""
        spec = importlib.util.spec_from_file_location(
            "no_window_tool", os.path.join(REPO, "tools", "no_window.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        env = mock.patch.dict(os.environ, {dt.INCIDENT_LOG_ENV: self.log})
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(spawn, "carry", mock.Mock(side_effect=ValueError("bad handle"))):
            with self.assertRaises(ValueError):
                tool.main(["python", "-B", "secret.py"])
        line, = self.lines()
        self.assertIn("no_window.py could not start python: ValueError: bad handle", line)
        self.assertNotIn("secret", line)
        with mock.patch.object(spawn, "carry", lambda argv: 127):
            self.assertEqual(tool.main(["python"]), 127)
        self.assertEqual(len(self.lines()), 2)
        with mock.patch.object(spawn, "carry", lambda argv: 0):
            self.assertEqual(tool.main(["python"]), 0)
        self.assertEqual(len(self.lines()), 2)


if __name__ == "__main__":
    unittest.main()
