"""Job.start and Ctrl+C: a SIGINT between making the suspended child and putting it in the job
must not leave it suspended in no job. The Ctrl+C is simulated with signal.raise_signal from a
wrapped Popen, right after the real one returns, so it lands in that window every time. Every
process here is the test's own, ended by its own handle."""
import os
import signal
import subprocess
import sys
import threading
import unittest
from unittest import mock

if os.name == "nt":
    from kitlib.proc import job

PASS = [sys.executable, "-c", "pass"]
SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


def _sentinel(signum, frame):
    """This test's own SIGINT handler: raises as Python's default does, and is never unittest's
    catch-break handler."""
    raise KeyboardInterrupt


class _Subprocess:
    """job.subprocess with Popen replaced; every other name is the real module's."""

    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


@unittest.skipUnless(os.name == "nt", "Windows only")
class CtrlCWhileStarting(unittest.TestCase):
    def setUp(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal handlers are set on the main thread only")
        before = signal.signal(signal.SIGINT, _sentinel)
        self.addCleanup(signal.signal, signal.SIGINT, before)
        self.started, self.seen = [], []
        self.addCleanup(self.reap)
        self.j = job.Job()
        self.addCleanup(self.j.end, True)

    def reap(self):
        for p in self.started:
            if p.poll() is None:
                p.kill()
            p.wait(30)

    def popen(self, then=None, fail=None):
        """Patch job's Popen: the real one, then `then()` (a Ctrl+C, say) before it returns."""
        real = subprocess.Popen

        def start(*args, **kwargs):
            self.seen.append(("popen", signal.getsignal(signal.SIGINT)))
            if fail is not None:
                if then:
                    then()
                raise fail
            p = real(*args, **kwargs)
            self.started.append(p)
            if then:
                then()
            return p
        return mock.patch.object(job, "subprocess", _Subprocess(start))

    def ctrl_c(self):
        signal.raise_signal(signal.SIGINT)

    def where(self):
        """The started child: (in the job?, ended?)."""
        p = self.started[0]
        return self.j.contains(p.pid), p.poll() is not None

    def test_a_ctrl_c_right_after_the_child_is_made_still_raises_and_ends_it(self):
        with self.popen(then=self.ctrl_c):
            with self.assertRaises(KeyboardInterrupt):
                self.j.start(SLEEP)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.where(), ("in", True),
                         "the child was left (in the job?, ended?) after Ctrl+C")
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_handler_that_does_not_raise_sees_the_ctrl_c_once_the_child_is_in_the_job(self):
        calls = []

        def note(signum, frame):
            calls.append(self.j.contains(self.started[0].pid))
        signal.signal(signal.SIGINT, note)
        with self.popen(then=self.ctrl_c):
            p = self.j.start(PASS)
        self.assertEqual(calls, ["in"])
        self.assertEqual(p.wait(60), 0)
        self.assertIs(signal.getsignal(signal.SIGINT), note)

    def test_under_sig_ign_a_ctrl_c_in_the_window_stays_ignored(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        with self.popen(then=self.ctrl_c):
            p = self.j.start(PASS)
        self.assertEqual(p.wait(60), 0)
        self.assertEqual(self.j.contains(p.pid), "in")
        self.assertIs(signal.getsignal(signal.SIGINT), signal.SIG_IGN)

    def test_sigint_is_held_from_creation_to_resume_and_put_back_after(self):
        def before_resume(p):
            self.seen.append(("before_resume", signal.getsignal(signal.SIGINT)))
        with self.popen():
            p = self.j.start(PASS, before_resume=before_resume)
        self.assertEqual(p.wait(60), 0)
        self.assertEqual([step for step, _ in self.seen], ["popen", "before_resume"])
        for step, handler in self.seen:
            self.assertIsNot(handler, _sentinel, "SIGINT not held at %s" % step)
            self.assertIsNot(handler, signal.SIG_IGN, "a Ctrl+C at %s would be lost" % step)
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_start_that_cannot_create_puts_the_handler_back(self):
        with self.popen(fail=OSError("no such program")):
            with self.assertRaises(OSError):
                self.j.start(PASS)
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_ctrl_c_during_a_start_that_cannot_create_is_not_lost(self):
        with self.popen(then=self.ctrl_c, fail=OSError("no such program")):
            with self.assertRaises(KeyboardInterrupt):
                self.j.start(PASS)
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_ctrl_c_and_a_child_that_cannot_join_end_it_and_raise_the_ctrl_c(self):
        self.j.close()                          # no job handle: the assign fails
        with self.popen(then=self.ctrl_c):
            with self.assertRaises(KeyboardInterrupt) as got:
                self.j.start(SLEEP)
        self.assertIsInstance(got.exception.__context__, job.JobError)
        self.assertIsNotNone(self.started[0].poll(), "the child outlived a failed start")
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_start_off_the_main_thread_holds_nothing_and_still_runs(self):
        got = []

        def run():
            try:
                got.append(self.j.start(PASS).wait(60))
            except BaseException as e:      # noqa: BLE001 (reported to the main thread)
                got.append(e)
        t = threading.Thread(target=run)
        t.start()
        t.join(90)
        self.assertEqual(got, [0])
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)


if __name__ == "__main__":
    unittest.main()
