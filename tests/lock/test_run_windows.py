"""kitlib.lock.run and the window monitor (kitlib.proc.winwatch): a run watches for console
windows from its first command's start to its release, and stops watching before its job ends.
The ended record carries what opened; run_command says the report; a gate's slot says nothing,
the gate does (kitlib.gate.runner.gated). Most tests swap the monitor for FakeMonitor, so no
window opens; the one real-monitor test starts a command that opens no window."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import run as rm
    from kitlib.proc import win32, winwatch

TOOL = r"C:\x\tool.exe"
WAIT_IN = "import sys; sys.stdin.read()"


class FakeMonitor:
    """winwatch.Monitor's surface. On stop it reports one window per pid in `pids` (set by the
    test), asking `ours` about each then, as the real monitor asks when the window shows."""
    made = []
    pids = []
    error = None
    start_raises = None             # an exception start() raises
    stop_raises = []                # exceptions the next stop() calls raise, in turn

    def __init__(self, ours=None):
        self.ours, self.gone, self.windows = ours, 0, None
        self.error = FakeMonitor.error
        self.starts, self.stops, self.job_open_at_stop = 0, 0, None
        FakeMonitor.made.append(self)

    def start(self):
        self.starts += 1
        if FakeMonitor.start_raises is not None:
            raise FakeMonitor.start_raises
        return self

    def stop(self):
        self.stops += 1
        if FakeMonitor.stop_raises:
            raise FakeMonitor.stop_raises.pop(0)
        if self.windows is None:
            self.windows = [winwatch.Window(winwatch.CONSOLE, "window", TOOL, pid=pid, exe=TOOL,
                                            ours=self.ours(pid), at=0, mono=0.0)
                            for pid in FakeMonitor.pids]
        return self.windows


@unittest.skipUnless(os.name == "nt", "Windows only")
class Watch(StoreCase):
    def setUp(self):
        super().setUp()
        self.confine(1)
        FakeMonitor.made, FakeMonitor.pids, FakeMonitor.error = [], [], None
        FakeMonitor.start_raises, FakeMonitor.stop_raises = None, []
        patcher = mock.patch.object(rm.winwatch, "Monitor", FakeMonitor)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []
        self.store.say = self.said.append

    def book(self, **kw):
        return rm.book("engine-test", "kit", "t", cores=1, store=self.store, env=clean_env(), **kw)

    def child(self, s):
        p = s.start([sys.executable, "-c", WAIT_IN], stdin=subprocess.PIPE,
                    creationflags=win32.CREATE_NO_WINDOW)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def finish(self, s, p):
        p.stdin.close()
        self.assertEqual(s.wait(p), (0, "done"))

    def test_a_run_watches_from_its_first_start_to_its_release(self):
        s = self.book()
        self.assertEqual(FakeMonitor.made, [])          # booked, nothing started: no monitor
        p = self.child(s)
        FakeMonitor.pids = [p.pid]
        self.assertEqual(len(FakeMonitor.made), 1)
        m = FakeMonitor.made[0]
        self.assertEqual((m.starts, m.stops), (1, 0))
        self.finish(s, p)
        q = self.child(s)                               # a second command: the same monitor
        self.finish(s, q)
        self.assertEqual(len(FakeMonitor.made), 1)
        job_end = s.job.end
        order = []
        s.job.end = lambda **kw: (order.append(("end", m.stops)), job_end(**kw))
        ended = s.release()
        self.assertEqual(order, [("end", 1)])           # stopped before the job ended
        self.assertEqual(m.stops, 1)
        self.assertEqual([(w["pid"], w["ours"], w["exe"]) for w in ended["windows"]],
                         [(p.pid, True, TOOL)])
        self.assertEqual(self.store.read("ended/%s.json" % s.id)["windows"], ended["windows"])
        self.assertEqual(s.window_lines()[0],
                         "windows opened: 1 - WARNING: a console window opened (the verdict "
                         "does not count it)")
        self.assertEqual(self.said, [])                 # a slot itself says nothing

    def test_owner_is_the_runs_job(self):
        s = self.book()
        with s:
            p = self.child(s)
            self.assertIs(s.window_owner(p.pid), True)
            self.assertIs(s.window_owner(os.getpid()), False)
            self.assertIsNone(s.window_owner(0))
            self.finish(s, p)

    def test_no_window_leaves_no_field_and_reports_zero(self):
        s = self.book()
        with s:
            self.finish(s, self.child(s))
        self.assertNotIn("windows", s.ended)
        self.assertEqual(s.window_lines(), ["windows opened: 0"])

    def test_a_run_that_starts_nothing_watches_nothing(self):
        with self.book() as s:
            pass
        self.assertEqual(FakeMonitor.made, [])
        self.assertEqual(s.window_lines(), [])
        self.assertNotIn("windows", s.ended)

    def test_the_switch_turns_it_off(self):
        with mock.patch.object(rm, "WATCH_WINDOWS", False):
            with self.book() as s:
                self.finish(s, self.child(s))
        self.assertEqual(FakeMonitor.made, [])
        self.assertEqual(s.window_lines(), [])

    def test_a_monitor_that_cannot_run_is_reported(self):
        FakeMonitor.error = "no hook"
        with self.book() as s:
            self.finish(s, self.child(s))
        self.assertEqual(s.ended["windows_error"], "no hook")
        self.assertEqual(s.window_lines(),
                         ["windows opened: unknown (the window monitor did not run: no hook)"])

    def test_a_monitor_that_raises_on_start_never_fails_the_run(self):
        FakeMonitor.start_raises = RuntimeError("no thread")
        with self.book() as s:
            self.finish(s, self.child(s))           # the command ran
            self.finish(s, self.child(s))           # a second start does not try again
            self.assertIsNone(s.watch)
        self.assertEqual(len(FakeMonitor.made), 1)
        self.assertEqual(FakeMonitor.made[0].stops, 1)  # the half-started monitor was stopped
        self.assertEqual(s.ended["windows_error"], "RuntimeError: no thread")
        self.assertEqual(s.ended["code"], 0)
        self.assertEqual(s.window_lines(), ["windows opened: unknown (the window monitor did not "
                                            "run: RuntimeError: no thread)"])

    def test_a_ctrl_c_inside_start_leaves_the_monitor_for_release_to_stop(self):
        FakeMonitor.start_raises = KeyboardInterrupt()
        s = self.book()
        with self.assertRaises(KeyboardInterrupt):
            with s:
                self.child(s)
        self.assertEqual(len(FakeMonitor.made), 1)
        m = FakeMonitor.made[0]
        self.assertEqual((m.starts, m.stops), (1, 1))  # release stopped it: no thread left
        self.assertIsNone(s.watch)
        self.assertEqual(s.ended["how"], "interrupted")

    def test_a_ctrl_c_inside_stop_leaves_the_monitor_to_stop_again(self):
        s = self.book()
        with s:
            p = self.child(s)
            m = FakeMonitor.made[0]
            FakeMonitor.stop_raises = [KeyboardInterrupt()]
            with self.assertRaises(KeyboardInterrupt):
                s.watch_stop()
            self.assertIs(s.watch, m)               # still there: the next call stops it
            self.assertEqual(s.watch_stop(), [])
            self.assertIsNone(s.watch)
            self.assertEqual(m.stops, 2)
            self.finish(s, p)

    def test_the_record_keeps_at_most_the_cap(self):
        s = self.book()
        with s:
            p = self.child(s)
            FakeMonitor.pids = [p.pid] * (winwatch.RECORD_CAP + 3)
            self.finish(s, p)
        self.assertEqual(len(s.ended["windows"]), winwatch.RECORD_CAP)
        self.assertEqual(s.ended["windows_truncated"], 3)
        self.assertTrue(s.window_lines()[0].startswith(
            "windows opened: %d - WARNING" % (winwatch.RECORD_CAP + 3)))

    def test_run_command_says_the_report(self):
        FakeMonitor.pids = [os.getpid()]                # another run's window: seen, not counted
        code = rm.run_command([sys.executable, "-c", "import sys; sys.exit(4)"], "engine-test",
                              "kit", "t", cores=1, store=self.store, env=clean_env())
        self.assertEqual(code, 4)
        i = self.said.index("windows opened: 0")
        self.assertEqual(self.said[i + 1], "  also seen, another run's: 1")
        self.assertIn(TOOL, self.said[i + 2])
        self.assertIn("[not this run]", self.said[i + 2])


@unittest.skipUnless(os.name == "nt", "Windows only")
class RealMonitor(StoreCase):
    def test_a_no_window_command_opens_none_of_its_own(self):
        """The real monitor around run_command: the command opens no window (a console-less
        parent's child gets CREATE_NO_WINDOW from Job.start; a console parent's child shares its
        console), so no window is the run's. Another program's window may still be seen; it is
        not counted."""
        self.confine(1)
        said = []
        self.store.say = said.append
        code = rm.run_command([sys.executable, "-c", "pass"], "engine-test", "kit", "t", cores=1,
                              store=self.store, env=clean_env())
        self.assertEqual(code, 0)
        report = [line for line in said if line.startswith("windows opened: ")]
        self.assertEqual(len(report), 1, said)
        rid = [r for r in os.listdir(self.store.path("ended"))][0]
        ended = self.store.read("ended/" + rid)
        self.assertEqual([w for w in ended.get("windows", []) if w["ours"] is True], [])


if __name__ == "__main__":
    unittest.main()
