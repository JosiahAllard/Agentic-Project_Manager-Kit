"""Tests for kitlib.proc.job: a job pins, measures and ends a tree; it fails closed;
masks past group 0 are set as group affinities (synthetic: one group here). Every
process here is the test's own."""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

if os.name == "nt":
    from kitlib.proc import job, process, win32

TREE = ("import subprocess,sys,time;"
        "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(g.pid)); time.sleep(60)")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Inside an outer job pinned to one thread: an inner job pinned to another thread (outside the
# outer one) and one pinned to the outer thread; each starts a child and says what it read back.
NESTED = "\n".join([
    "import json, sys",
    "from kitlib.proc import job",
    "r = {}",
    "for k, m in (('outside', int(sys.argv[1])), ('inside', int(sys.argv[2]))):",
    "    j = job.Job(m)",
    "    j.start([sys.executable, '-c', 'pass']).wait(30)",
    "    r[k] = [j.pin, j.effective]",
    "    if k == 'outside':",
    "        r['note'] = j.note",
    "    j.end(kill=True)",
    "open(sys.argv[3], 'w').write(json.dumps(r))"])


def wait_file(path, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            with open(path) as f:
                s = f.read().strip()
            if s:
                return int(s)
        time.sleep(0.05)
    raise AssertionError("%s never written" % path)


def affinity(h):
    pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
    if not win32.K32.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pm.value, sm.value


def own_mask():
    """This process's affinity: under the machine lock (the gate runs the suite as a run) it is
    the outer run's threads, and a child of a job nested in that run's job runs on no others."""
    return affinity(win32.K32.GetCurrentProcess())[0]


def first_own_thread():
    m = own_mask()
    return (m & -m).bit_length() - 1


@unittest.skipUnless(os.name == "nt", "Windows only")
class Plans(unittest.TestCase):
    def test_group_zero_is_a_basic_pin(self):
        self.assertEqual(job.affinity_plan(0xF0), ("basic", 0xF0))
        self.assertIsNone(job.affinity_plan(None))
        self.assertIsNone(job.affinity_plan(0))

    def test_a_mask_past_64_is_set_per_group(self):
        self.assertEqual(job.affinity_plan(0b11 | (0b1 << 64)), ("groups", [(0, 0b11), (1, 1)]))
        self.assertEqual(job.affinity_plan(0b1 << 70), ("groups", [(1, 1 << 6)]))

    def test_group_affinities_lay_out_as_windows_reads_them(self):
        raw = bytes(job.group_affinities([(0, 0b11), (1, 1 << 63)]))
        self.assertEqual(len(raw), 32)                       # GROUP_AFFINITY is 16 bytes
        self.assertEqual(int.from_bytes(raw[0:8], "little"), 0b11)
        self.assertEqual(int.from_bytes(raw[8:10], "little"), 0)
        self.assertEqual(int.from_bytes(raw[16:24], "little"), 1 << 63)
        self.assertEqual(int.from_bytes(raw[24:26], "little"), 1)


@unittest.skipUnless(os.name == "nt", "Windows only")
class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kitlib-job-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_pinned_child_runs_on_its_threads_and_is_measured(self):
        first = first_own_thread()
        j = job.Job(1 << first)
        try:
            self.assertIsNone(j.note)
            self.assertEqual(j.mask, 1 << first)
            p = j.start([sys.executable, "-c", "import time; time.sleep(5)"])
            self.addCleanup(p.wait, 30)     # reap it: an unread Popen warns "still running"
            h = process.open_process(p.pid)
            try:
                self.assertEqual(affinity(h)[0], 1 << first)
            finally:
                process.close(h)
            cpu_s, commit, ws = j.usage()
            self.assertGreaterEqual(cpu_s, 0)
            self.assertGreater(commit, 0)
            self.assertGreater(ws, 0)
        finally:
            j.end(kill=True)

    def test_end_kill_ends_the_whole_tree(self):
        pidfile = os.path.join(self.tmp, "pid")
        j = job.Job()
        p = j.start([sys.executable, "-c", TREE, pidfile])
        g = wait_file(pidfile)
        h = process.open_process(g, process.SYNCHRONIZE)
        try:
            j.end(kill=True)
            self.assertEqual(win32.K32.WaitForSingleObject(h, 20000), win32.WAIT_OBJECT_0)
        finally:
            process.close(h)
        p.wait(30)

    def test_end_without_kill_leaves_a_daemon_running_unpinned(self):
        pidfile = os.path.join(self.tmp, "pid")
        first = first_own_thread()
        j = job.Job(1 << first)
        p = j.start([sys.executable, "-c", TREE, pidfile])
        g = wait_file(pidfile)
        h = process.open_process(g, process.SYNCHRONIZE | process.PROCESS_TERMINATE
                                 | process.PROCESS_QUERY_LIMITED_INFORMATION)
        try:
            j.end(kill=False)
            self.assertEqual(win32.K32.WaitForSingleObject(h, 0), win32.WAIT_TIMEOUT)
            self.assertEqual(affinity(h)[0], own_mask())         # every thread it may use back
        finally:
            win32.K32.TerminateProcess(h, 1)
            process.close(h)
            p.kill()
            p.wait(30)

    def test_a_child_that_cannot_join_the_job_is_ended_not_run(self):
        # Nothing runs outside the job. The old machine_lock Job ran it anyway,
        # unpinned ("not in a job"): red there.
        j = job.Job()
        j.close()                                   # no job handle: the assign must fail
        marker = os.path.join(self.tmp, "ran")
        with self.assertRaises(job.JobError):
            j.start([sys.executable, "-c", "open(%r, 'w').close()" % marker])
        time.sleep(1)
        self.assertFalse(os.path.exists(marker))

    def test_before_resume_runs_while_suspended(self):
        seen = []
        j = job.Job()
        try:
            p = j.start([sys.executable, "-c", "pass"],
                        before_resume=lambda q: seen.append(q.poll()))
            self.assertEqual(p.wait(60), 0)
            self.assertEqual(seen, [None])
        finally:
            j.end(kill=True)

    def test_pids_lists_the_tree(self):
        pidfile = os.path.join(self.tmp, "pid")
        j = job.Job()
        try:
            p = j.start([sys.executable, "-c", TREE, pidfile])
            self.addCleanup(p.wait, 30)     # reap it: an unread Popen warns "still running"
            g = wait_file(pidfile)
            self.assertTrue({p.pid, g} <= set(j.pids()))
        finally:
            j.end(kill=True)


    def test_the_pin_reads_back_as_pinned_when_no_outer_job_overrides_it(self):
        first = first_own_thread()
        j = job.Job(1 << first)
        try:
            j.start([sys.executable, "-c", "pass"]).wait(30)
            self.assertEqual((j.pin, j.effective), ("pinned", 1 << first))
        finally:
            j.end(kill=True)

    def test_a_mask_outside_an_outer_job_reads_back_overridden(self):
        own = own_mask()
        if bin(own).count("1") < 2:
            self.skipTest("needs two threads this process may use")
        a = (own & -own).bit_length() - 1
        rest = own & ~(1 << a)
        b = (rest & -rest).bit_length() - 1
        out = os.path.join(self.tmp, "pin.json")
        env = dict(os.environ, PYTHONPATH=ROOT)
        outer = job.Job(1 << a)
        try:
            p = outer.start([sys.executable, "-c", NESTED, str(1 << b), str(1 << a), out], env=env)
            self.assertEqual(p.wait(60), 0)
        finally:
            outer.end(kill=True)
        with open(out) as f:
            got = json.load(f)
        self.assertEqual(got["outside"], ["overridden", 1 << a])
        self.assertIn("outer job", got["note"])
        self.assertEqual(got["inside"], ["pinned", 1 << a])

    def test_init_closes_the_job_when_anything_after_creating_it_raises(self):
        closed, real = [], job.Job.close

        def spy(self):
            closed.append(self.h)
            real(self)

        with mock.patch.object(job.Job, "close", spy):
            with mock.patch.object(job.cpu, "all_mask", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    job.Job(1)
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0])

if __name__ == "__main__":
    unittest.main()
