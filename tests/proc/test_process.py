"""Tests for kitlib.proc.process: snapshots, creation times, ancestry and verified kills. Every
process ended here is the test's own, named by pid and creation time."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

if os.name == "nt":
    from kitlib.proc import process

TREE = ("import subprocess,sys,time;"
        "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(g.pid)); time.sleep(60)")


@unittest.skipUnless(os.name == "nt", "Windows only")
class Process(unittest.TestCase):
    def test_the_snapshot_lists_this_process_and_its_parent(self):
        snap = {pid: ppid for pid, ppid, _n in process.snapshot()}
        self.assertEqual(snap[os.getpid()], os.getppid())

    def test_creation_times_agree(self):
        self.assertEqual(process.proc_ctime(os.getpid()), process.own_ctime())
        self.assertTrue(process.alive(os.getpid(), process.own_ctime()))
        self.assertFalse(process.alive(os.getpid(), process.own_ctime() + 1))

    def test_own_ancestors_hold_the_parent(self):
        self.assertIn(os.getppid(), {p for p, _c in process.own_ancestors()})

    def test_terminate_verified_refuses_a_wrong_creation_time(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            ct = process.proc_ctime(p.pid)
            self.assertEqual(process.terminate_verified(p.pid, ct + 1), "reused")
            self.assertIsNone(p.poll())
            self.assertEqual(process.terminate_verified(p.pid, ct), "killed")
            self.assertEqual(p.wait(30), 1)
        finally:
            p.kill()
            p.wait()

    def test_kill_tree_ends_a_child_and_its_grandchild(self):
        tmp = tempfile.mkdtemp(prefix="kitlib-proc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        pidfile = os.path.join(tmp, "pid")
        p = subprocess.Popen([sys.executable, "-c", TREE, pidfile])
        try:
            end = time.time() + 30
            g = None
            while g is None:
                self.assertLess(time.time(), end)
                try:
                    with open(pidfile) as f:
                        g = int(f.read().strip())
                except (OSError, ValueError):
                    time.sleep(0.05)
            gct = process.proc_ctime(g)
            pct = process.proc_ctime(p.pid)
            self.assertIn((g, gct), process.descendants(p.pid, pct))
            got = process.kill_tree(p.pid, pct)
            self.assertEqual((got[p.pid], got[g]), ("killed", "killed"))
            self.assertLessEqual(set(got.values()), {"killed", "gone"})  # a venv redirector adds one
            p.wait(30)
            end = time.time() + 20
            while process.alive(g, gct) and time.time() < end:
                time.sleep(0.05)
            self.assertFalse(process.alive(g, gct))
        finally:
            p.kill()
            p.wait()

    def test_set_affinity_pins_its_own_child_and_checks_its_creation_time(self):
        import ctypes
        from kitlib.proc import win32
        own, every = ctypes.c_size_t(), ctypes.c_size_t()
        if not win32.K32.GetProcessAffinityMask(win32.K32.GetCurrentProcess(), ctypes.byref(own),
                                                ctypes.byref(every)):
            raise ctypes.WinError(ctypes.get_last_error())
        first = (own.value & -own.value).bit_length() - 1
        # Under the machine lock (the gate runs the suite as a run) this process and its child
        # are in the run's job, whose affinity limit wins over a process's own mask: the call
        # succeeds and the child still runs on the run's threads. set_affinity says so.
        in_job = own.value != every.value
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            ct = process.proc_ctime(p.pid)
            self.assertEqual(process.set_affinity(p.pid, ct + 1, 1 << first), "reused")
            self.assertEqual(process.set_affinity(p.pid, ct, 1 << first),
                             "overridden" if in_job else "pinned")
            h = process.open_process(p.pid)
            try:
                pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
                win32.K32.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm))
                self.assertEqual(pm.value, own.value if in_job else 1 << first)
            finally:
                process.close(h)
        finally:
            p.kill()
            p.wait()

    def test_an_exited_process_is_never_reported_killed(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        ct = process.proc_ctime(p.pid)
        p.wait(30)
        self.assertIn(process.terminate_verified(p.pid, ct), ("gone", "reused", "denied"))
        self.assertNotIn("killed", process.kill_tree(p.pid, ct, snap=[]).values())

    def test_memory_mb(self):
        m = process.memory_mb()
        self.assertGreater(m["total_mb"], 0)
        self.assertLessEqual(m["free_mb"], m["total_mb"])


if __name__ == "__main__":
    unittest.main()
