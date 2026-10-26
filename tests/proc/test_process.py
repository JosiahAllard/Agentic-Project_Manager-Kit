"""Tests for kitlib.proc.process: snapshots, creation times, ancestry and verified kills. Every
process ended here is the test's own, named by pid and creation time."""
import ctypes
import gc
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.proc import process, win32

TREE = ("import subprocess,sys,time;"
        "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(g.pid)); time.sleep(60)")
PROTECTED = ("lsass.exe", "services.exe", "wininit.exe", "csrss.exe", "smss.exe", "svchost.exe")


def denied(pid):
    """K32.OpenProcess refused with ERROR_ACCESS_DENIED for pid; every other pid opens as usual."""
    real = win32.K32.OpenProcess

    def fake(access, inherit, p):
        if int(p) == int(pid):
            ctypes.set_last_error(win32.ERROR_ACCESS_DENIED)
            return 0
        return real(access, inherit, p)
    return mock.patch.object(win32.K32, "OpenProcess", fake)


def ntdll(query):
    """win32.NTDLL with NtQuerySystemInformation replaced by query(cls, buf, length, size)."""
    return mock.patch.object(win32, "NTDLL", types.SimpleNamespace(NtQuerySystemInformation=query))


def signed(status):
    """An NTSTATUS as the real call returns it: a signed C int."""
    return ctypes.c_int(status).value


@windows_only("Windows only")
class Process(unittest.TestCase):
    def test_the_snapshot_lists_this_process_and_its_parent(self):
        snap = {pid: ppid for pid, ppid, _n in process.snapshot()}
        self.assertEqual(snap[os.getpid()], os.getppid())

    def test_creation_times_agree(self):
        self.assertEqual(process.proc_ctime(os.getpid()), process.own_ctime())
        self.assertTrue(process.alive(os.getpid(), process.own_ctime()))
        self.assertFalse(process.alive(os.getpid(), process.own_ctime() + 1))

    def test_parent_pid_reads_one_handle_and_agrees_with_the_snapshot(self):
        self.assertEqual(process.parent_pid(process.K32.GetCurrentProcess()), os.getppid())
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            h = process.open_process(p.pid)
            try:
                self.assertEqual(process.parent_pid(h), os.getpid())
                self.assertEqual(process.parent_pid(h), process.process_parents()[p.pid])
            finally:
                process.close(h)
        finally:
            p.kill()
            p.wait()

    def test_parent_pid_of_an_unusable_handle_is_none(self):
        self.assertIsNone(process.parent_pid(None))

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

    def test_the_system_listing_gives_this_process_its_creation_time_and_name(self):
        listed = {pid: (ct, name) for pid, ct, name in process.system_processes()}
        snap = {pid: name for pid, _pp, name in process.snapshot()}
        self.assertEqual(listed[os.getpid()],
                         (process.own_ctime(), snap[os.getpid()]))
        self.assertEqual(process.listed_ctime(os.getpid()), process.own_ctime())

    def test_a_listing_too_big_for_the_buffer_is_asked_again_in_a_bigger_one(self):
        # NtQuerySystemInformation is replaced: STATUS_INFO_LENGTH_MISMATCH twice (the second
        # naming a size past double the buffer), then the real call into the last buffer.
        real, asked = win32.NTDLL.NtQuerySystemInformation, []

        def fake(cls, buf, length, size):
            asked.append(length.value)
            if len(asked) == 1:
                return signed(process.STATUS_INFO_LENGTH_MISMATCH)      # no size: doubled
            if len(asked) == 2:
                size._obj.value = 9 << 20
                return signed(process.STATUS_INFO_LENGTH_MISMATCH)
            return real(cls, buf, length, size)
        with ntdll(fake):
            listed = {pid: ct for pid, ct, _n in process.system_processes()}
        self.assertEqual(asked, [2 << 20, 4 << 20, (9 << 20) + (1 << 16)])
        self.assertEqual(listed[os.getpid()], process.own_ctime())

    def test_a_listing_that_never_fits_ends_after_eight_tries(self):
        # Every call says the buffer is too small. The buffers are recorded, not made: the
        # eighth would be 256 MiB.
        asked, made = [], []
        make = ctypes.create_string_buffer

        def small(n):
            made.append(n)
            return make(16)

        def fake(_cls, _buf, length, _size):
            asked.append(length.value)
            return signed(process.STATUS_INFO_LENGTH_MISMATCH)
        with ntdll(fake), mock.patch.object(process.ctypes, "create_string_buffer", small):
            with self.assertRaisesRegex(OSError, "it kept growing"):
                process.system_processes()
        self.assertEqual(asked, [(2 << 20) << k for k in range(8)])
        self.assertEqual(made, asked)
        with ntdll(fake), mock.patch.object(process.ctypes, "create_string_buffer", small):
            self.assertIsNone(process.listed_ctime(os.getpid()))         # no listing: None

    def test_another_status_ends_the_listing_at_once_and_names_it(self):
        asked = []

        def fake(_cls, _buf, length, _size):
            asked.append(length.value)
            return signed(0xC0000022)                                   # STATUS_ACCESS_DENIED
        with ntdll(fake):
            with self.assertRaisesRegex(OSError, "status 0xC0000022"):
                process.system_processes()
            self.assertIsNone(process.listed_ctimes())
        self.assertEqual(asked, [2 << 20, 2 << 20])

    def test_a_pid_this_user_may_not_open_is_judged_by_its_listed_creation_time(self):
        # The denial is made by replacing K32.OpenProcess for one child (ERROR_ACCESS_DENIED, as
        # for a SYSTEM svchost a reused pid names), so it runs the same on any machine.
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        ct = process.proc_ctime(p.pid)
        with denied(p.pid):
            self.assertIsNone(process.proc_ctime(p.pid))        # the denial holds
            self.assertTrue(process.alive(p.pid, ct))
            self.assertFalse(process.alive(p.pid, ct + 1))
            self.assertFalse(process.alive(p.pid, 1))
            self.assertTrue(process.alive(p.pid, None))         # nothing to compare
            self.assertTrue(process.alive(p.pid, "x"))          # as before: not compared
            with mock.patch.object(process, "system_processes", lambda: []):
                self.assertTrue(process.alive(p.pid, 1))        # not listed: cannot tell

            def fails():
                raise OSError("no listing")
            with mock.patch.object(process, "system_processes", fails):
                self.assertIsNone(process.listed_ctime(p.pid))
                self.assertTrue(process.alive(p.pid, 1))        # no listing: cannot tell

    def test_a_protected_process_with_another_creation_time_is_not_alive(self):
        # The real denial, unpatched: a system process this user may not open (not elevated).
        found = None
        for pid, ct, name in process.system_processes():
            if name.lower() in PROTECTED and pid > 4:
                h = win32.K32.OpenProcess(win32.QUERY_LIMITED | win32.SYNCHRONIZE, False, pid)
                if h:
                    process.close(h)
                    continue
                found = (pid, ct)
                break
        if found is None:
            self.skipTest("no protected system process is listed that this user may not open "
                          "(elevated?)")
        pid, ct = found
        self.assertFalse(process.alive(pid, 1))
        self.assertFalse(process.alive(pid, ct + 1))
        self.assertTrue(process.alive(pid, ct))
        self.assertTrue(process.alive(pid, None))

    def test_a_gone_process_is_not_alive(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        pid, ct = p.pid, process.proc_ctime(p.pid)
        p.wait(30)
        self.assertFalse(process.alive(pid, ct))                 # exited, its handle still open
        del p
        gc.collect()                                            # Popen's handle closed: gone
        self.assertFalse(process.alive(pid, ct))

    def test_memory_mb(self):
        m = process.memory_mb()
        self.assertGreater(m["total_mb"], 0)
        self.assertLessEqual(m["free_mb"], m["total_mb"])


if __name__ == "__main__":
    unittest.main()
