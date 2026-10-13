"""Tests for kitlib.proc.winwatch: the window monitor. The real-process tests start their own
children only, end them by their Popen handle, and wait for events with a deadline."""
import os
import subprocess
import sys
import threading
import time
import unittest

if os.name == "nt":
    from kitlib.proc import win32, winwatch

EVENT_WAIT_S = 15       # a window's events reach the monitor well within this
EXIT_WAIT_S = 30


def python_exe():
    """The console python beside this interpreter (sys.executable may be pythonw)."""
    d = os.path.dirname(sys.executable)
    p = os.path.join(d, "python.exe")
    return p if os.path.exists(p) else sys.executable


def watchers():
    return [t for t in threading.enumerate() if t.name == "kit-winwatch"]


@unittest.skipUnless(os.name == "nt", "Windows only")
class RealWindows(unittest.TestCase):
    """A monitor around real children of this test."""

    def setUp(self):
        self.mine = set()
        self.mon = winwatch.Monitor(ours=lambda pid: pid in self.mine)
        self.addCleanup(self.mon.stop)
        self.mon.start()
        if self.mon.error:
            self.skipTest("the window monitor cannot run here: %s" % self.mon.error)

    def child(self, code, flags, **kw):
        p = subprocess.Popen([python_exe(), "-c", code], creationflags=flags, **kw)
        self.mine.add(p.pid)
        self.addCleanup(self.end, p)
        return p

    def end(self, p):
        if p.poll() is None:
            p.kill()
            p.wait(EXIT_WAIT_S)

    def test_a_new_console_child_is_recorded_with_its_pid(self):
        """CREATE_NEW_CONSOLE opens a console window (or a Windows Terminal window or tab): this
        one test opens a window on purpose, for as long as it takes the monitor to see it. The
        child waits on its stdin and ends with exit 0 when the test closes it (a Terminal window
        stays open after a non-zero exit)."""
        p = self.child("import sys; sys.stdin.read()", win32.CREATE_NEW_CONSOLE,
                       stdin=subprocess.PIPE)
        end, seen = time.monotonic() + EVENT_WAIT_S, []
        while time.monotonic() < end and not seen:
            seen = [w for w in self.mon.snapshot() if w.pid == p.pid]
            time.sleep(0.05)
        p.stdin.close()
        self.assertEqual(p.wait(EXIT_WAIT_S), 0)
        windows = self.mon.stop()
        self.assertTrue(seen, [w.text() for w in windows])
        w = [w for w in windows if w.pid == p.pid][0]
        self.assertIn(w.cls, (winwatch.CONSOLE, winwatch.TERMINAL))
        self.assertEqual(winwatch.base(w.exe), "python.exe")
        self.assertIs(w.ours, True)
        self.assertEqual(winwatch.counted(windows), [x for x in windows if x.ours is not False])
        self.assertTrue(winwatch.report_lines(windows)[0].startswith("windows opened: "))

    def test_a_no_window_child_is_not_recorded(self):
        p = self.child("import time; time.sleep(0.5)", win32.CREATE_NO_WINDOW,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        self.assertEqual(p.wait(EXIT_WAIT_S), 0)
        time.sleep(1.0)                  # any late event of it has arrived
        windows = self.mon.stop()
        self.assertEqual([w.text() for w in windows if w.pid == p.pid], [])
        self.assertEqual([x.pid for x in self.mon.pseudos() if x.pid == p.pid], [])


@unittest.skipUnless(os.name == "nt", "Windows only")
class StartStop(unittest.TestCase):
    def test_start_and_stop_leave_no_thread(self):
        before = len(watchers())
        mon = winwatch.Monitor()
        with mon:
            self.assertIsNone(mon.error)
            self.assertEqual(len(watchers()), before + 1)
        self.assertEqual(len(watchers()), before)
        self.assertIsNotNone(mon.windows)
        self.assertIs(mon.stop(), mon.windows)         # a second stop: the same list, no wait

    def test_stop_without_start(self):
        mon = winwatch.Monitor()
        self.assertEqual(mon.stop(), [])
        self.assertIsNone(mon.error)

    def test_a_raising_ours_is_unknown(self):
        def boom(pid):
            raise RuntimeError("x")
        mon = winwatch.Monitor(ours=boom)
        self.assertIsNone(mon._owner(1234))
        self.assertIsNone(winwatch.Monitor()._owner(1234))
        self.assertIs(winwatch.Monitor(ours=lambda pid: True)._owner(1234), True)
        self.assertIsNone(winwatch.Monitor(ours=lambda pid: "in")._owner(1234))


class LooksLaunched(unittest.TestCase):
    def test_paths_and_names(self):
        for t in (r"C:\Python\python.exe", r'"C:\Program Files\Git\bin\bash.exe" --login',
                  r"C:\Windows\system32\cmd.exe - python x.py", "Administrator: cmd.exe",
                  r"\\server\share\tool.CMD", "python.exe", r"D:\a b\run.bat arg"):
            with self.subTest(t):
                self.assertTrue(winwatch.looks_launched(t))

    def test_not_launched(self):
        for t in ("Windows Terminal", "Terminal", "Windows PowerShell", "", None,
                  "python x.py", r"C:\work\notes.txt", "build: 3 of 9 .exe files"):
            with self.subTest(t):
                self.assertFalse(winwatch.looks_launched(t))

    def test_title_exe(self):
        self.assertEqual(winwatch.title_exe(r'"C:\P F\x.exe" a'), r"C:\P F\x.exe")
        self.assertEqual(winwatch.title_exe(r"C:\W\cmd.exe - git status"), r"C:\W\cmd.exe")
        self.assertEqual(winwatch.title_exe("Administrator: x.com"), "x.com")


def W(cls, how, title, mono, **kw):
    return winwatch.Window(cls, how, title, at=0, mono=mono, **kw)


class Attribute(unittest.TestCase):
    PY = r"C:\Py\python.exe"
    GIT = r"C:\Git\git.exe"

    def test_terminal_window_takes_its_program(self):
        w = W(winwatch.TERMINAL, "window", self.PY, 10.0, host_pid=7)
        out = winwatch.attribute([w], [winwatch.Pseudo(55, self.PY, True, mono=10.2)])
        self.assertEqual((out[0].pid, out[0].exe, out[0].ours), (55, self.PY, True))
        self.assertIsNone(w.pid)                    # the input is not changed

    def test_the_exe_the_title_names_wins_over_time(self):
        w = W(winwatch.TERMINAL, "window", self.GIT, 10.0)
        out = winwatch.attribute([w], [winwatch.Pseudo(1, self.PY, True, mono=10.0),
                                       winwatch.Pseudo(2, self.GIT, False, mono=12.0)])
        self.assertEqual(out[0].pid, 2)

    def test_each_program_is_used_once(self):
        a = W(winwatch.TERMINAL, "window", self.PY, 10.0)
        b = W(winwatch.TERMINAL, "window", self.PY, 11.0)
        out = winwatch.attribute([a, b], [winwatch.Pseudo(1, self.PY, True, mono=10.1)])
        self.assertEqual([w.pid for w in out], [1, None])     # b still counts, unattributed

    def test_too_far_apart_is_not_paired(self):
        w = W(winwatch.TERMINAL, "window", self.PY, 10.0)
        out = winwatch.attribute([w], [winwatch.Pseudo(1, self.PY, True,
                                                       mono=10.0 + winwatch.CORRELATE_S + 1)])
        self.assertIsNone(out[0].pid)

    def test_a_tab_needs_a_program(self):
        t = W(winwatch.TERMINAL, "tab", self.PY, 10.0)
        self.assertEqual(winwatch.attribute([t], []), [])                    # a tab switch
        self.assertEqual(winwatch.attribute([t], [winwatch.Pseudo(1, self.GIT, None,
                                                                  mono=10.0)]), [])
        out = winwatch.attribute([t], [winwatch.Pseudo(1, self.GIT, True, mono=10.0)])
        self.assertEqual(out[0].pid, 1)                                      # the run's own
        out = winwatch.attribute([t], [winwatch.Pseudo(3, self.PY, False, mono=10.0)])
        self.assertEqual((out[0].pid, out[0].ours), (3, False))              # by exe

    def test_a_console_window_keeps_its_pid(self):
        w = W(winwatch.CONSOLE, "window", "x", 10.0, pid=9, ours=True)
        out = winwatch.attribute([w], [winwatch.Pseudo(1, self.PY, True, mono=10.0)])
        self.assertEqual(out[0].pid, 9)


class Report(unittest.TestCase):
    def test_none(self):
        self.assertEqual(winwatch.report_lines([]), ["windows opened: 0"])

    def test_counted_and_others(self):
        ours = W(winwatch.CONSOLE, "window", "t", 1.0, pid=5, exe=r"C:\x\py.exe", ours=True)
        unknown = W(winwatch.CONSOLE, "window", "u", 2.0, pid=6, ours=None)
        other = W(winwatch.TERMINAL, "tab", "o", 3.0, pid=7, ours=False)
        lines = winwatch.report_lines([ours, unknown, other], gone=2)
        self.assertTrue(lines[0].startswith("windows opened: 2 - WARNING"), lines)
        self.assertIn("pid 5 py.exe", lines[1])
        self.assertIn("[this run]", lines[1])
        self.assertIn("[run unknown]", lines[2])
        self.assertEqual(lines[3], "  also seen, another run's: 1")
        self.assertIn("[not this run]", lines[4])
        self.assertIn("2 console host window(s)", lines[5])

    def test_fails_wording(self):
        ours = W(winwatch.CONSOLE, "window", "t", 1.0, pid=5, ours=True)
        self.assertEqual(winwatch.report_lines([ours], fails=True)[0],
                         "windows opened: 1 - FAILED: a console window opened")
        self.assertEqual(winwatch.report_lines([], fails=True), ["windows opened: 0"])

    def test_error(self):
        self.assertEqual(winwatch.report_lines([], error="boom"),
                         ["windows opened: unknown (the window monitor did not run: boom)"])

    def test_as_dict(self):
        d = W(winwatch.CONSOLE, "window", "t", 1.0, pid=5, ours=True).as_dict()
        self.assertEqual((d["cls"], d["how"], d["pid"], d["ours"]),
                         (winwatch.CONSOLE, "window", 5, True))


if __name__ == "__main__":
    unittest.main()
