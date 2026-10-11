import ctypes
import datetime
import unittest

from kitlib import pmstart
from kitlib.pmstart import fglock
from tests.pmstart import keep_off_launcher_repos

CFG = {}
NOW = datetime.datetime(2026, 10, 6, 9, 0)
FAKES = dict(handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"})


def setUpModule():
    """The other checklist checks read this machine; keep them off it."""
    global _orig_settings
    _orig_settings = pmstart.pm_data_settings
    pmstart.pm_data_settings = lambda: None


def tearDownModule():
    pmstart.pm_data_settings = _orig_settings


class FakeUser32:
    def __init__(self, value, ok=1):
        self.value, self.ok, self.calls = value, ok, []

    def SystemParametersInfoW(self, action, param, ptr, ini):
        self.calls.append((action, param, ini))
        if self.ok:
            ptr._obj.value = self.value
        return self.ok


class FgLockTest(unittest.TestCase):
    def test_zero_gives_no_line(self):
        self.assertEqual(fglock.check(CFG, read=lambda: 0), [])

    def test_armed_gives_one_line(self):
        out = fglock.check(CFG, read=lambda: 2147483647)
        self.assertEqual(len(out), 1)
        for s in ("2147483647", "docs/windows-token-leak.md", "fix.ps1", "fix.log"):
            self.assertIn(s, out[0])

    def test_read_live_with_fake_dll(self):
        dll = FakeUser32(7)
        self.assertEqual(fglock.read_live(dll), 7)
        self.assertEqual(dll.calls, [(0x2000, 0, 0)])    # a GET, nothing written

    def test_read_live_failure_raises_oserror(self):
        with self.assertRaises(OSError):
            fglock.read_live(FakeUser32(0, ok=0))

    def test_in_checklist_last_and_failure_isolated(self):
        txt = pmstart.checklist(dict(CFG, standing=["x"]), NOW, procs=[],
                                foreground_lock={"read": lambda: 5}, **FAKES)
        lines = txt.splitlines()
        self.assertTrue(lines[1].startswith("- standing rule: x"))
        self.assertTrue(lines[-1].startswith("- foreground lock timeout is 5"), lines[-1])

        def boom():
            raise OSError("no user32")
        txt = pmstart.checklist(dict(CFG, standing=["x"]), NOW, procs=[],
                                foreground_lock={"read": boom}, **FAKES)
        self.assertIn("could not check foreground_lock: OSError", txt)
        self.assertIn("standing rule: x", txt)


if __name__ == "__main__":
    unittest.main()
