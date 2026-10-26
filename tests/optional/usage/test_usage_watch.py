"""Tests for usage_watch.py, and round trips through write-usage-status.ps1 and statusline-usage.ps1
into its reader.

    python -m unittest test_usage_watch -v
"""
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from tests.platforms import windows_only

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'usage')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)            # a run from this folder: tests.home and kitlib
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)
from kitlib.config import user as kuser  # noqa: E402
from tests import home as thome  # noqa: E402
import usage_watch as W  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def reading(five, week=10, five_reset=None, week_reset=None):
    return dict(updated=NOW, five=five, five_reset=five_reset, week=week,
                week_reset=week_reset or NOW + dt.timedelta(days=3))


def status(path, five, week, updated, five_reset="2026-10-04T15:00:00.0000000+00:00"):
    d = {"updated_at": updated, "session_id": "t",
         "five_hour": {"used_percentage": five, "resets_at": five_reset} if five_reset else None,
         "seven_day": {"used_percentage": week, "resets_at": "2026-10-08T00:00:00.0000000+00:00"}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f)


class Bands(unittest.TestCase):
    def test_band_edges(self):
        self.assertEqual([W.band(p) for p in (0, 59, 60, 79, 80, 89, 90, 92, 93, 100)],
                         [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])


class Events(unittest.TestCase):
    def test_quiet_when_nothing_matters(self):
        self.assertIsNone(W.event(reading(20), reading(40), NOW))
        self.assertIsNone(W.event(reading(61), reading(64), NOW))     # same 5-point step

    def test_band_change(self):
        self.assertTrue(W.event(reading(55), reading(61), NOW).startswith("BAND five_hour 55% -> 61%"))

    def test_step_above_sixty(self):
        self.assertTrue(W.event(reading(61), reading(66), NOW).startswith("STEP five_hour 61% -> 66%"))

    def test_week_crossing_either_way(self):
        self.assertTrue(W.event(reading(20, 79), reading(20, 81), NOW).startswith("WEEK"))
        self.assertTrue(W.event(reading(20, 81), reading(20, 79), NOW).startswith("WEEK"))

    def test_reset_when_the_old_reset_time_passed(self):
        old = reading(70, five_reset=NOW - dt.timedelta(minutes=1))
        new = reading(5, five_reset=NOW + dt.timedelta(hours=5))
        self.assertTrue(W.event(old, new, NOW).startswith("RESET five_hour window reset"))


class ReadAndOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "usage-status.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_status_file(self):
        status(self.path, 64, 81, "2026-10-04T12:00:00.1234567+00:00")
        r = W.read(self.path)
        self.assertEqual((r["five"], r["week"]), (64, 81))
        self.assertEqual(r["updated"], dt.datetime(2026, 10, 4, 12, 0, 0, 123456, tzinfo=UTC))
        self.assertIn("seven_day over 80", W.line(r))

    def test_no_window_open_reads_as_zero(self):
        status(self.path, 0, 10, "2026-10-04T12:00:00.0000000+00:00", five_reset=None)
        r = W.read(self.path)
        self.assertEqual((r["five"], r["five_reset"]), (0, None))
        self.assertIn("resets none open", W.line(r))

    def test_unreadable_is_none(self):
        self.assertIsNone(W.read(self.path))
        with open(self.path, "w") as f:
            f.write("{")
        self.assertIsNone(W.read(self.path))

    def test_once_prints_reading_or_stale(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            W.main(["--config-dir", self.tmp.name, "--once"])
        self.assertTrue(out.getvalue().startswith("STALE usage file missing"))
        status(self.path, 30, 10, dt.datetime.now(UTC).isoformat())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            W.main(["--config-dir", self.tmp.name, "--once"])
        self.assertTrue(out.getvalue().startswith("five_hour 30%"))


class ConfigDir(unittest.TestCase):
    """Which folder main() reads, and that the home is looked up only when nothing names one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kit-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def once(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            W.main([*args, "--once"])
        return out.getvalue()

    # Guards (green before they were written): a given folder, the env folder and the default
    # folder's name.

    def test_a_given_config_dir_looks_up_no_home(self):
        thome.fence_decoy(self)
        self.assertIn(os.path.join(self.tmp, "usage-status.json"), self.once("--config-dir", self.tmp))

    def test_claude_config_dir_looks_up_no_home(self):
        thome.fence_decoy(self)
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp
        self.assertIn(os.path.join(self.tmp, "usage-status.json"), self.once())

    def test_an_empty_config_dir_is_the_current_folder(self):
        thome.fence_decoy(self)
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        self.assertIn(": usage-status.json (", self.once("--config-dir", ""))

    def test_the_default_is_the_homes_claude_folder(self):
        # Spelled as os.path.expanduser("~/.claude") spells it.
        home = thome.temp_home(self)
        self.assertIn(os.path.join(home + "/.claude", "usage-status.json"), self.once())
        self.assertIn(os.path.join(os.path.expanduser("~/.claude"), "usage-status.json"), self.once())

    def test_the_default_is_fenced(self):
        thome.fence_decoy(self)
        with self.assertRaises(kuser.HomeFenced):
            self.once()


@windows_only("needs Windows PowerShell")
@unittest.skipUnless(shutil.which("powershell"), "needs Windows PowerShell")
class WriterRoundTrip(unittest.TestCase):
    def run_writer(self, *args):
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               os.path.join(CODE, "write-usage-status.ps1"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_writer_output_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.run_writer("42", "2026-10-04T15:00:00Z", "77", "2026-10-08T00:00:00Z", "-ConfigDir", d)
            self.assertEqual(p.returncode, 0, p.stderr)
            r = W.read(os.path.join(d, "usage-status.json"))
            self.assertEqual((r["five"], r["week"]), (42, 77))
            self.assertEqual(r["five_reset"], dt.datetime(2026, 10, 4, 15, 0, tzinfo=UTC))
            self.assertFalse(os.path.exists(os.path.join(d, "usage-status.json.tmp")))

    def test_no_five_hour_window(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.run_writer("0", "", "5", "2026-10-08T00:00:00Z", "-ConfigDir", d)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIsNone(W.read(os.path.join(d, "usage-status.json"))["five_reset"])

    def test_missing_config_dir_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.run_writer("1", "", "1", "2026-10-08T00:00:00Z", "-ConfigDir", os.path.join(d, "nope"))
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("no such config folder", p.stderr + p.stdout)


@windows_only("needs Windows PowerShell")
@unittest.skipUnless(shutil.which("powershell"), "needs Windows PowerShell")
class StatuslineRoundTrip(unittest.TestCase):
    """statusline-usage.ps1 (the settings' statusLine command) writes the file usage_watch reads."""

    def run_statusline(self, session, config_dir):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               os.path.join(CODE, "statusline-usage.ps1")],
                              input=json.dumps(session), capture_output=True, text=True, timeout=60, env=env)

    def test_rate_limits_written_and_shown(self):
        five = int(dt.datetime(2026, 10, 4, 15, 0, tzinfo=UTC).timestamp())
        week = int(dt.datetime(2026, 10, 8, 0, 0, tzinfo=UTC).timestamp())
        with tempfile.TemporaryDirectory() as d:
            p = self.run_statusline({"session_id": "s1", "model": {"display_name": "Opus"},
                                     "context_window": {"used_percentage": 12},
                                     "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": five},
                                                     "seven_day": {"used_percentage": 77, "resets_at": week}}}, d)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "[Opus]  5h 42%  7d 77%  ctx 12%")
            r = W.read(os.path.join(d, "usage-status.json"))
            self.assertEqual((r["five"], r["week"]), (42, 77))
            self.assertEqual(r["five_reset"], dt.datetime(2026, 10, 4, 15, 0, tzinfo=UTC))
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])

    def test_failed_move_leaves_no_tmp(self):
        # Hold the target open with no sharing, as a reader would: the move fails.
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "usage-status.json")
            status(target, 1, 1, "2026-10-04T12:00:00.0000000+00:00")
            h = k32.CreateFileW(target, 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, share none
            self.assertNotEqual(h, wintypes.HANDLE(-1).value)
            try:
                five = int(dt.datetime(2026, 10, 4, 15, 0, tzinfo=UTC).timestamp())
                p = self.run_statusline({"session_id": "s1", "model": {"display_name": "Opus"},
                                         "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": five}}}, d)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stdout.strip(), "[Opus]  5h 42%")
                self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])
            finally:
                k32.CloseHandle(h)

    def test_stale_tmp_swept_and_others_kept(self):
        five = int(dt.datetime(2026, 10, 4, 15, 0, tzinfo=UTC).timestamp())
        with tempfile.TemporaryDirectory() as d:
            names = {"old": "usage-status.json.123.tmp", "new": "usage-status.json.456.tmp",
                     "other": "other.json.789.tmp", "odd": "usage-status.json.abc.tmp"}
            for n in names.values():
                with open(os.path.join(d, n), "w") as f:
                    f.write("x")
            old = os.path.getmtime(os.path.join(d, names["old"])) - 3600
            for n in (names["old"], names["other"], names["odd"]):
                os.utime(os.path.join(d, n), (old, old))
            os.mkdir(os.path.join(d, "sub"))
            with open(os.path.join(d, "sub", "usage-status.json.1.tmp"), "w") as f:
                f.write("x")
            os.utime(os.path.join(d, "sub", "usage-status.json.1.tmp"), (old, old))
            p = self.run_statusline({"session_id": "s1", "model": {"display_name": "Opus"},
                                     "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": five}}}, d)
            self.assertEqual(p.returncode, 0, p.stderr)
            left = set(os.listdir(d))
            self.assertNotIn(names["old"], left)
            self.assertLessEqual({names["new"], names["other"], names["odd"], "sub", "usage-status.json"}, left)
            self.assertTrue(os.path.exists(os.path.join(d, "sub", "usage-status.json.1.tmp")))

    def test_no_rate_limits_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.run_statusline({"session_id": "s1", "model": {"display_name": "Opus"}}, d)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "[Opus]  usage n/a")
            self.assertFalse(os.path.exists(os.path.join(d, "usage-status.json")))


if __name__ == "__main__":
    unittest.main()
