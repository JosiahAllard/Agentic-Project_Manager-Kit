"""A run record whose pid was reused.

Run r-20261011T064159Z-0ae903 showed as live, holding 2 cpu slots, for about 325 h after its
wrapper died without settling: its pid had become a SYSTEM svchost.exe, which this user may not
open, and kitlib.proc.process.alive says alive for a process it cannot open, without its creation
time. A pid without a matching creation time is not proof of life: when the pid and creation time
cannot be checked, the run's mutex (held by the wrapper from before its record is written until
after the record leaves active/) decides (store.wrapper_state, Store.run_live).

The denial is made by replacing K32.OpenProcess for one pid (ERROR_ACCESS_DENIED), so the cases
run the same on any machine; one case also uses a real protected process of this machine.
"""
import ctypes
import os
import subprocess
import sys
import unittest
from unittest import mock

from .helpers import OWNER, StoreCase

if os.name == "nt":
    from kitlib.lock import api
    from kitlib.lock import holds as hl
    from kitlib.lock import release
    from kitlib.lock import run as rm
    from kitlib.lock import store as st
    from kitlib.proc import process, win32
    from kitlib.proc.win32 import K32

RID = "r-20261011T064159Z-0ae903"
PROTECTED = ("wininit.exe", "services.exe", "lsass.exe", "csrss.exe", "smss.exe")


def denied(pid):
    """K32.OpenProcess refused with ERROR_ACCESS_DENIED for pid, as for another user's or a
    protected process; every other pid is opened as usual."""
    real = K32.OpenProcess

    def fake(access, inherit, p):
        if int(p) == int(pid):
            ctypes.set_last_error(win32.ERROR_ACCESS_DENIED)
            return 0
        return real(access, inherit, p)
    return mock.patch.object(K32, "OpenProcess", fake)


def mutex_fails(code):
    """K32.OpenMutexW refused with the Windows error code, for every name."""
    def fake(access, inherit, name):
        ctypes.set_last_error(code)
        return 0
    return mock.patch.object(K32, "OpenMutexW", fake)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class ReusedPid(StoreCase):
    """A record whose pid names a process this user may not open is live only while its run
    mutex is held."""

    def other_process(self):
        """(pid, creation time) of a child process: the process a reused pid names."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p.pid, process.proc_ctime(p.pid)

    def phantom(self, ctime=1):
        """A record for RID whose pid now names an unopenable process created at another time
        than the record's ctime (the reused pid)."""
        pid, _ct = self.other_process()
        self.live_record(RID, pid=pid, ctime=ctime, cores=[0, 1])
        return pid

    def assert_not_live(self):
        self.assertEqual([r["id"] for r in self.store.live_runs()], [])
        self.assertEqual([r["id"] for r in api.snapshot(self.store)["runs"]], [])
        self.assertIsNone(api.run_record(RID, self.store))
        res = api.end_run(OWNER, RID, self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "not_found")
        live, gone, _own = release.live_runs(self.store)
        self.assertEqual((live, gone), ([], ["active/%s.json" % RID]))

    def assert_live(self):
        self.assertEqual([r["id"] for r in self.store.live_runs()], [RID])
        self.assertEqual([r["id"] for r in api.snapshot(self.store)["runs"]], [RID])
        self.assertTrue(api.run_record(RID, self.store)["live"])
        live, gone, _own = release.live_runs(self.store)
        self.assertEqual((live, gone), (["active/%s.json" % RID], []))

    def test_a_reused_pid_this_user_may_not_open_is_not_live_without_the_run_mutex(self):
        pid = self.phantom()
        with denied(pid):
            self.assert_not_live()

    def test_wrapper_state_cannot_tell_for_an_unopenable_pid_or_a_missing_creation_time(self):
        pid, ct = self.other_process()
        self.assertTrue(st.wrapper_state(pid, ct))
        self.assertIsNone(st.wrapper_state(pid, None))
        with denied(pid):
            self.assertIsNone(st.wrapper_state(pid, ct))
            self.assertIsNone(st.wrapper_state(pid, 1))
        for bad in (None, "x", 1.5j):
            self.assertIsNone(st.wrapper_state(bad, ct), bad)
        self.assertFalse(st.wrapper_state(0, ct))

    def test_an_unopenable_wrapper_holding_its_run_mutex_stays_live(self):
        pid, ct = self.other_process()
        self.live_record(RID, pid=pid, ctime=ct)
        mutex = self.store.run_mutex(RID)
        self.addCleanup(mutex.release)
        with denied(pid):
            self.assert_live()
        mutex.release()
        with denied(pid):
            self.assertEqual([r["id"] for r in self.store.live_runs()], [])

    def test_a_record_with_no_creation_time_is_not_live_on_its_pid_alone(self):
        self.live_record(RID, ctime=None)       # this live process's pid, no creation time
        self.assert_not_live()
        mutex = self.store.run_mutex(RID)
        self.addCleanup(mutex.release)
        self.assert_live()

    def test_a_checkable_pid_still_decides_without_the_mutex(self):
        self.live_record(RID)                   # this process, its creation time: live
        self.assert_live()
        self.store.drop("active/%s.json" % RID)
        self.live_record(RID, ctime=process.own_ctime() + 1)     # another creation time: gone
        mutex = self.store.run_mutex(RID)
        self.addCleanup(mutex.release)
        self.assertEqual(self.store.live_runs(), [])

    def test_a_protected_process_of_this_machine_does_not_keep_a_record_live(self):
        procs = [pid for pid, _pp, name in process.snapshot() if name.lower() in PROTECTED]
        if not procs:
            self.skipTest("no protected system process is listed")
        # The real denial, unpatched: only where this user may not open the process (not
        # elevated), so the run mutex decides and not a creation time compared.
        pid = procs[0]
        if st.wrapper_state(pid, 1) is not None:
            self.skipTest("this runner may open a protected process (elevated)")
        self.live_record(RID, pid=pid, ctime=1, cores=[0, 1])
        self.assert_not_live()
        mutex = self.store.run_mutex(RID)
        self.addCleanup(mutex.release)
        self.assert_live()

    def test_a_run_mutex_that_cannot_be_opened_for_another_reason_counts_as_live(self):
        # Only "no such mutex" (ERROR_FILE_NOT_FOUND) means gone. Access denied, or any other
        # failure (6, invalid handle, here), cannot tell, and a run that cannot be told is live.
        pid, ct = self.other_process()
        self.live_record(RID, pid=pid, ctime=ct, cores=[0, 1])
        for code in (win32.ERROR_ACCESS_DENIED, 6):
            with self.subTest(code=code), denied(pid), mutex_fails(code):
                self.assertIsNone(self.store.run_mutex_held(RID))
                self.assertTrue(self.store.run_live(self.store.read("active/%s.json" % RID)))
                self.assertEqual([r["id"] for r in self.store.live_runs()], [RID])
                live, gone, _own = release.live_runs(self.store)
                self.assertEqual((live, gone), (["active/%s.json" % RID], []))
        with denied(pid):                       # no mutex at all: gone
            self.assertIs(self.store.run_mutex_held(RID), False)
            self.assertEqual(self.store.live_runs(), [])

    def test_a_failed_wait_on_the_wrapper_cannot_tell(self):
        pid, ct = self.other_process()
        with mock.patch.object(K32, "WaitForSingleObject", lambda h, ms: 0xFFFFFFFF):
            self.assertIsNone(st.wrapper_state(pid, ct))

    def test_an_unknown_wrapper_without_a_valid_run_id_counts_as_live(self):
        pid, ct = self.other_process()
        with denied(pid):
            self.assertIs(self.store.run_live({"id": RID, "pid": pid, "ctime": ct}), False)
            for d in ({"pid": pid, "ctime": ct}, {"id": "../x", "pid": pid, "ctime": ct},
                      {"id": 5, "pid": pid, "ctime": ct}, {"id": "", "pid": pid, "ctime": ct}):
                self.assertIs(self.store.run_live(d), True, d)

    def test_settle_lets_the_run_mutex_go_only_after_the_record_left_active(self):
        # The mutex fallback rests on this order: while the mutex is held, the record may be in
        # active/; once it is let go, the record is no longer there.
        run = rm.Run(self.store, self.layout(), "kit", "t", "engine-test", None, [], "held",
                     rid=RID)
        run.save()
        active = self.store.path("active", RID + ".json")
        seen = []

        class Mutex:
            def release(self):
                seen.append((os.path.exists(active),
                             os.path.exists(run.store.path("ended", RID + ".json"))))
        rm.settle(self.store, run, 0, "done", Mutex())
        self.assertEqual(seen, [(False, True)])

    def test_settle_lets_the_run_mutex_go_when_the_record_cannot_leave_active(self):
        # store.remove ignores the OSError: the mutex is still let go, after the ended record
        # was written, and the record left in active/ is listed only while its wrapper lives.
        run = rm.Run(self.store, self.layout(), "kit", "t", "engine-test", None, [], "held",
                     rid=RID)
        run.save()
        active = self.store.path("active", RID + ".json")
        ended = self.store.path("ended", RID + ".json")
        real, seen = os.remove, []

        def refuse(path):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(active):
                raise PermissionError(13, "in use", path)
            real(path)

        class Mutex:
            def release(self):
                seen.append((os.path.exists(active), os.path.exists(ended)))
        with mock.patch.object(st.os, "remove", refuse):
            rm.settle(self.store, run, 0, "done", Mutex())
        self.assertEqual(seen, [(True, True)])
        self.assertEqual(self.store.read("ended/%s.json" % RID)["state"], "ended")
        self.assertEqual([r["id"] for r in self.store.live_runs()], [RID])   # this process
        with mock.patch.object(st, "wrapper_state", lambda pid, ctime: False):
            self.assertEqual(self.store.live_runs(), [])
            live, gone, _own = release.live_runs(self.store, alive=lambda p, c: False)
            self.assertEqual((live, gone), ([], ["active/%s.json" % RID]))

    def test_a_hold_claimed_by_an_unopenable_run_is_claimed_only_while_its_mutex_is_held(self):
        pid, ct = self.other_process()
        h = {"claim": {"id": RID, "pid": pid, "ctime": ct}}
        with denied(pid):
            self.assertFalse(hl.claim_alive(self.store, h))
            mutex = self.store.run_mutex(RID)
            self.addCleanup(mutex.release)
            self.assertTrue(hl.claim_alive(self.store, h))


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class OwnerLiveUnknown(unittest.TestCase):
    """release._owner_live: when alive cannot tell (None, or True for a record with no creation
    time), the run is gone only when its run mutex is surely gone."""

    def test_unknown_with_the_mutex_gone_is_gone(self):
        unknown = lambda p, c: None             # noqa: E731
        d = {"id": RID, "pid": 1234, "ctime": 5}
        self.assertFalse(release._owner_live(d, unknown, lambda rid: True))
        self.assertTrue(release._owner_live(d, unknown, lambda rid: False))

    def test_a_live_pid_with_no_creation_time_asks_the_mutex(self):
        yes = lambda p, c: True                 # noqa: E731
        d = {"id": RID, "pid": 1234}
        self.assertFalse(release._owner_live(d, yes, lambda rid: True))
        self.assertTrue(release._owner_live(d, yes, lambda rid: False))
        self.assertTrue(release._owner_live(dict(d, ctime=5), yes, lambda rid: True))

    def test_unknown_without_a_run_id_or_a_mutex_answer_is_live(self):
        unknown = lambda p, c: None             # noqa: E731
        for d in ({"pid": 1234, "ctime": 5}, {"id": "../x", "pid": 1234, "ctime": 5}):
            self.assertTrue(release._owner_live(d, unknown, lambda rid: True), d)
        self.assertTrue(release._owner_live({"id": RID, "pid": 1234, "ctime": 5}, unknown))

        def boom(rid):
            raise OSError(5, "denied")
        self.assertTrue(release._owner_live({"id": RID, "pid": 1234, "ctime": 5}, unknown, boom))


if __name__ == "__main__":
    unittest.main()
