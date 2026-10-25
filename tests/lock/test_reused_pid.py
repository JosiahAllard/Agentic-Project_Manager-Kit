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
        # A creation time no process has: elevated, the open works and the time differs; not
        # elevated, the open is refused and the missing run mutex decides.
        self.live_record(RID, pid=procs[0], ctime=1, cores=[0, 1])
        self.assert_not_live()

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
