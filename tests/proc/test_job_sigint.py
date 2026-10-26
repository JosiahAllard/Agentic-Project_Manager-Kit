"""Job.start and Ctrl+C / Ctrl+Break: a SIGINT or SIGBREAK between making the suspended child and
putting it in the job must not leave it suspended in no job. The signal is simulated with
signal.raise_signal from a wrapped Popen, right after the real one returns (or from
before_resume), so it lands in that window every time. Every process here is the test's own,
ended by its own handle."""
import os
import signal
import subprocess
import sys
import tempfile
import shutil
import threading
import time
import unittest
from unittest import mock
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.proc import job, process, spawn, win32

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS = [sys.executable, "-c", "pass"]
SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]
# Writes the marker it is given, then sleeps: the marker says the command ran.
MARK = "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(30)"

# A process of its own whose handler for argv[1] is SIG_DFL: the signal lands right after its
# Job (no kill-on-close: only Job.start can end the child) makes the child, whose pid it writes.
UNDER_DEFAULT = "\n".join([
    "import signal, subprocess, sys",
    "from kitlib.proc import job",
    "sig, pidfile, marker = getattr(signal, sys.argv[1]), sys.argv[2], sys.argv[3]",
    "signal.signal(sig, signal.SIG_DFL)",
    "real = subprocess.Popen",
    "class Sub:",
    "    def __getattr__(self, name):",
    "        return getattr(subprocess, name)",
    "    def Popen(self, *a, **kw):",
    "        p = real(*a, **kw)",
    "        with open(pidfile, 'w') as f:",
    "            f.write(str(p.pid))",
    "        signal.raise_signal(sig)",
    "        return p",
    "job.subprocess = Sub()",
    "j = job.Job(kill_on_close=False)",
    "j.start([sys.executable, '-c', %r, marker])" % MARK,
    "print('start returned', flush=True)"])


def _sentinel(signum, frame):
    """This test's own SIGINT handler: raises as Python's default does, and is never unittest's
    catch-break handler."""
    raise KeyboardInterrupt


def _break(signum, frame):
    """This test's own SIGBREAK handler (the runner's is SIG_DFL, which would end it)."""
    raise KeyboardInterrupt


class _Subprocess:
    """job.subprocess with Popen replaced; every other name is the real module's."""

    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


class _Signal:
    """job.signal whose getsignal says None (a handler not set from Python) for one signal."""

    def __init__(self, none_for):
        self.none_for = none_for

    def getsignal(self, s):
        return None if s == self.none_for else signal.getsignal(s)

    def __getattr__(self, name):
        return getattr(signal, name)


class _SignalHook:
    """job.signal whose signal(s, h) calls hook(s, h) first: CPython's signal.signal runs any
    pending handler before it swaps, so a signal raised from the hook lands where a real one
    would, and a handler that raises there leaves the swap undone."""

    def __init__(self, hook):
        self.hook = hook

    def signal(self, s, h):
        self.hook(s, h)
        return signal.signal(s, h)

    def __getattr__(self, name):
        return getattr(signal, name)


@windows_only("Windows only")
class CtrlCWhileStarting(unittest.TestCase):
    def setUp(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal handlers are set on the main thread only")
        before = signal.signal(signal.SIGINT, _sentinel)
        self.addCleanup(signal.signal, signal.SIGINT, before)
        before_break = signal.signal(signal.SIGBREAK, _break)
        self.addCleanup(signal.signal, signal.SIGBREAK, before_break)
        self.tmp = tempfile.mkdtemp(prefix="kitlib-job-sigint-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
            self.seen.append(("popen", signal.getsignal(signal.SIGINT),
                              signal.getsignal(signal.SIGBREAK)))
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

    def restored(self):
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)
        self.assertIs(signal.getsignal(signal.SIGBREAK), _break)

    def test_a_ctrl_c_right_after_the_child_is_made_still_raises_and_ends_it(self):
        with self.popen(then=self.ctrl_c):
            with self.assertRaises(KeyboardInterrupt):
                self.j.start(SLEEP)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.where(), ("in", True),
                         "the child was left (in the job?, ended?) after Ctrl+C")
        self.restored()

    def test_a_ctrl_break_right_after_the_child_is_made_still_raises_and_ends_it(self):
        with self.popen(then=lambda: signal.raise_signal(signal.SIGBREAK)):
            with self.assertRaises(KeyboardInterrupt):
                self.j.start(SLEEP)
        self.assertEqual(self.where(), ("in", True),
                         "the child was left (in the job?, ended?) after Ctrl+Break")
        self.restored()

    def test_a_ctrl_c_held_until_resume_ends_the_child_before_it_runs(self):
        """The handler waits for the command's marker before it raises: handed the Ctrl+C after
        the resume, it sees the command run; before the resume, the command never runs."""
        marker = os.path.join(self.tmp, "ran")

        def slow(signum, frame):
            end = time.monotonic() + 3
            while time.monotonic() < end and not os.path.exists(marker):
                time.sleep(0.02)
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, slow)
        with self.popen():
            with self.assertRaises(KeyboardInterrupt):
                self.j.start([sys.executable, "-c", MARK, marker],
                             before_resume=lambda p: self.ctrl_c())
        self.assertEqual(self.where(), ("in", True))
        self.assertFalse(os.path.exists(marker), "the command ran before the Ctrl+C ended it")

    def test_a_handler_that_does_not_raise_sees_the_ctrl_c_once_the_child_is_in_the_job(self):
        calls = []

        def note(signum, frame):
            calls.append((self.j.contains(self.started[0].pid),
                          frame.f_code.co_name if frame is not None else None))
        signal.signal(signal.SIGINT, note)
        with self.popen(then=self.ctrl_c):
            p = self.j.start(PASS)
        self.assertEqual(calls, [("in", "start")])      # a real frame: Job.start's
        self.assertEqual(p.wait(60), 0)
        self.assertIs(signal.getsignal(signal.SIGINT), note)

    def test_under_sig_ign_a_ctrl_c_in_the_window_stays_ignored(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        with self.popen(then=self.ctrl_c):
            p = self.j.start(PASS)
        self.assertEqual(p.wait(60), 0)
        self.assertEqual(self.j.contains(p.pid), "in")
        self.assertIs(signal.getsignal(signal.SIGINT), signal.SIG_IGN)

    def test_sigint_and_sigbreak_are_held_from_creation_to_resume_and_put_back_after(self):
        def before_resume(p):
            self.seen.append(("before_resume", signal.getsignal(signal.SIGINT),
                              signal.getsignal(signal.SIGBREAK)))
        with self.popen():
            p = self.j.start(PASS, before_resume=before_resume)
        self.assertEqual(p.wait(60), 0)
        self.assertEqual([step for step, _, _ in self.seen], ["popen", "before_resume"])
        for step, on_int, on_break in self.seen:
            self.assertIsNot(on_int, _sentinel, "SIGINT not held at %s" % step)
            self.assertIsNot(on_break, _break, "SIGBREAK not held at %s" % step)
            self.assertIsNot(on_int, signal.SIG_IGN, "a Ctrl+C at %s would be lost" % step)
        self.restored()

    def test_a_signal_whose_handler_python_cannot_put_back_is_not_held(self):
        with mock.patch.object(job, "signal", _Signal(signal.SIGINT)), self.popen():
            p = self.j.start(PASS)
        self.assertEqual(p.wait(60), 0)
        (_, on_int, on_break), = self.seen
        self.assertIs(on_int, _sentinel, "SIGINT replaced though it could not be put back")
        self.assertIsNot(on_break, _break, "SIGBREAK not held")
        self.restored()

    def test_a_ctrl_c_while_the_handlers_are_put_back_leaves_none_held_and_loses_none(self):
        """A Ctrl+Break comes while SIGINT is set back (SIGBREAK still held: queued), then a
        Ctrl+C while SIGBREAK is set back (SIGINT's own handler raises before the swap)."""
        breaks, fired = [], set()
        signal.signal(signal.SIGBREAK, lambda signum, frame: breaks.append(signum))
        restore_break = signal.getsignal(signal.SIGBREAK)

        def hook(s, h):
            if s == signal.SIGINT and h is _sentinel and "int" not in fired:
                fired.add("int")
                signal.raise_signal(signal.SIGBREAK)
            elif s == signal.SIGBREAK and h is restore_break and "break" not in fired:
                fired.add("break")
                signal.raise_signal(signal.SIGINT)
        with mock.patch.object(job, "signal", _SignalHook(hook)), self.popen():
            with self.assertRaises(KeyboardInterrupt):
                self.j.start(PASS)
        self.assertEqual(fired, {"int", "break"})
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)
        self.assertIs(signal.getsignal(signal.SIGBREAK), restore_break,
                      "SIGBREAK left held after start")
        self.assertEqual(breaks, [signal.SIGBREAK], "the Ctrl+Break held at the end was lost")
        self.assertEqual(self.j.contains(self.started[0].pid), "in")

    def test_a_handler_that_raises_while_a_signal_is_being_held_leaves_none_held(self):
        """A pending Ctrl+Break whose handler raises as SIGBREAK is about to be held: start
        raises it before any child is made, and SIGINT (held a moment before) is put back."""
        def hook(s, h):
            if s == signal.SIGBREAK and h is not _break:
                signal.raise_signal(signal.SIGBREAK)
        with mock.patch.object(job, "signal", _SignalHook(hook)), self.popen():
            with self.assertRaises(KeyboardInterrupt):
                self.j.start(PASS)
        self.assertEqual(self.seen, [], "a child was made")
        self.restored()

    def test_a_start_that_cannot_create_puts_the_handler_back(self):
        with self.popen(fail=OSError("no such program")):
            with self.assertRaises(OSError):
                self.j.start(PASS)
        self.restored()

    def test_a_ctrl_c_during_a_start_that_cannot_create_is_not_lost(self):
        with self.popen(then=self.ctrl_c, fail=OSError("no such program")):
            with self.assertRaises(KeyboardInterrupt):
                self.j.start(PASS)
        self.restored()

    def test_a_ctrl_c_and_a_child_that_cannot_join_end_it_and_raise_the_ctrl_c(self):
        self.j.close()                          # no job handle: the assign fails
        with self.popen(then=self.ctrl_c):
            with self.assertRaises(KeyboardInterrupt) as got:
                self.j.start(SLEEP)
        self.assertIsInstance(got.exception.__context__, job.JobError)
        self.assertIsNotNone(self.started[0].poll(), "the child outlived a failed start")
        self.restored()

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
        self.restored()


@windows_only("Windows only")
class UnderSigDfl(unittest.TestCase):
    """SIG_DFL ends the process at once; in a process of its own, the child it started must be
    ended too, never left suspended in no job (or running in a job nobody ends)."""

    def run_under_default(self, signame):
        tmp = tempfile.mkdtemp(prefix="kitlib-job-sigdfl-")
        self.addCleanup(shutil.rmtree, tmp, True)
        pidfile, marker = os.path.join(tmp, "pid"), os.path.join(tmp, "ran")
        # Output to a file, never a pipe: a child left behind would hold a pipe open and hang the
        # run where this test should fail.
        with open(os.path.join(tmp, "out"), "w+b") as out:
            try:
                r = spawn.run([sys.executable, "-c", UNDER_DEFAULT, signame, pidfile, marker],
                              env=dict(os.environ, PYTHONPATH=ROOT), stdin=subprocess.DEVNULL,
                              stdout=out, stderr=subprocess.STDOUT, timeout=120)
            except subprocess.TimeoutExpired:   # still reap the child below, then fail
                r = subprocess.CompletedProcess([], None)
            out.seek(0)
            r.stdout = out.read()
        if not os.path.exists(pidfile):
            self.fail("no child was made: %r" % r.stdout)
        with open(pidfile) as f:
            pid = int(f.read())
        h = process.open_process(pid, process.SYNCHRONIZE | process.PROCESS_TERMINATE)
        left = False
        if h:
            try:
                left = win32.K32.WaitForSingleObject(h, 10000) != win32.WAIT_OBJECT_0
                if left:
                    win32.K32.TerminateProcess(h, 1)
            finally:
                process.close(h)
        return r, left, os.path.exists(marker)

    def test_sigint_and_sigbreak_under_sig_dfl_end_the_child_and_then_the_process(self):
        for signame in ("SIGINT", "SIGBREAK"):
            with self.subTest(signame):
                r, left, ran = self.run_under_default(signame)
                self.assertNotIn(b"start returned", r.stdout)
                self.assertIsNotNone(r.returncode, "its process did not end within 120 s")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertFalse(left, "the child outlived its parent, which %s ended" % signame)
                self.assertFalse(ran, "the child ran before %s ended it" % signame)


if __name__ == "__main__":
    unittest.main()
