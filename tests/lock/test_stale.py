"""Stale runs (kitlib.lock.stale): the lock asks a held run to stop once every agent host it was
booked under has exited and it has held far past its guide. Never one whose host lives or cannot
be told, one with no recorded host, a waiting, background or releasing run, one already asked to
stop, or (with stale.INCLUDE_DETACHED off) a detached one. The stop goes through the run's own
stop file, by "lock", so its wrapper ends its own tree; a snapshot only shows the reason.

Scratch store only. The records' wrapper is this test process (it stays live); an "ended host" is
a child process of this test that has exited. Nothing here stops or signals a real lock run."""
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import api, notice, stale
    from kitlib.lock import run as rm
    from kitlib.lock import status as st
    from kitlib.lock import store as store_mod
    from kitlib.lock.store import iso, utc_now
    from kitlib.proc import process

RID = "r-20261031T000000Z-57a1e0"
GUIDE_S = 1800                  # warn_s; expect_s 600 is less, so the guide is warn_s
DUE_S = 3 * GUIDE_S             # max(stale_scale 3 x guide, guide + stale_min_s 3600) = 5400


def ago(s):
    return iso(utc_now() - datetime.timedelta(seconds=s))


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.dir = os.path.join(self.tmp, "notices")
        self.store.site = {"notice": {"dir": self.dir}}

    def exited(self):
        """[pid, creation time] of a child process of this test that has exited."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        ct = process.proc_ctime(p.pid)
        p.kill()
        p.wait()
        return [p.pid, ct]

    def stale_record(self, rid=RID, **fields):
        rec = dict(hosts=[self.exited()], warn_s=GUIDE_S, expect_s=600,
                   held_since=ago(DUE_S + 600), since=ago(DUE_S + 700), cores=[0])
        rec.update(fields)
        return self.live_record(rid, **rec)

    def stop_of(self, rid=RID):
        p = self.store.path("active", rid + ".stop")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def notices(self):
        return sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []

    def reap(self):
        return stale.reap(self.store, rm.request_stop)


class NeverReaped(Base):
    def assert_kept(self, **fields):
        self.stale_record(**fields)
        self.assertIsNone(stale.reason(self.store, self.store.read("active/%s.json" % RID)))
        self.assertEqual(self.reap(), [])
        self.assertIsNone(self.stop_of())
        self.assertEqual(self.notices(), [])

    def test_a_live_host(self):
        self.assert_kept(hosts=[[os.getpid(), process.own_ctime()]])

    def test_one_host_live_and_one_gone(self):
        self.assert_kept(hosts=[self.exited(), [os.getpid(), process.own_ctime()]])

    def test_a_host_that_cannot_be_told(self):
        self.assert_kept(hosts=[[os.getpid(), None]])

    def test_no_recorded_host(self):
        self.assert_kept(hosts=[])

    def test_a_record_from_before_hosts_were_recorded(self):
        self.assert_kept(hosts=None)

    def test_before_the_margin(self):
        self.assert_kept(held_since=ago(DUE_S - 600))

    def test_the_margin_is_at_least_the_guide_plus_stale_min_s(self):
        # guide 600 s: 3 x 600 = 1800 s, but guide + 3600 s = 4200 s is the larger.
        self.assert_kept(warn_s=None, expect_s=600, held_since=ago(3000))

    def test_a_waiting_run(self):
        self.assert_kept(state="waiting")

    def test_a_background_run(self):
        self.assert_kept(background=True)

    def test_a_releasing_run(self):
        self.assert_kept(releasing=True)

    def test_a_detached_run_when_detached_runs_are_left_out(self):
        with mock.patch.object(stale, "INCLUDE_DETACHED", False):
            self.assert_kept(detached=True)

    def test_a_run_already_asked_to_stop_keeps_that_request(self):
        self.stale_record()
        self.assertTrue(rm.request_stop(self.store, RID, "the owner", "sess-o"))
        before = self.stop_of()
        self.assertEqual(self.reap(), [])
        self.assertEqual(self.stop_of(), before)
        self.assertEqual(self.notices(), [])

    def test_a_dead_wrapper_is_left_to_the_dead_run_reap(self):
        pid, ct = self.exited()
        self.stale_record(pid=pid, ctime=ct)
        self.assertEqual(self.reap(), [])
        self.assertIsNone(self.stop_of())


class Reaped(Base):
    def test_an_ended_host_past_the_margin_gets_one_stop_by_the_lock(self):
        self.stale_record()
        why = stale.reason(self.store, self.store.read("active/%s.json" % RID))
        self.assertIn("booking session has ended", why)
        self.assertEqual(self.reap(), [RID])
        stop = self.stop_of()
        self.assertEqual((stop["by"], stop["session"]), (stale.BY, None))
        self.assertIn("booking session has ended", stop["why"])
        self.assertEqual(self.notices(), ["%s-stale.json" % RID])
        with open(os.path.join(self.dir, "%s-stale.json" % RID), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual((doc["id"], doc["phase"], doc["run"], doc["kind"], doc["from"]),
                         ("L-%s-stale" % RID, "stale", RID, notice.KIND, notice.FROM))
        self.assertTrue(any("ends stale run %s" % RID in s for s in self.said), self.said)
        # A second look finds the stop already asked: nothing more.
        self.assertEqual(self.reap(), [])

    def test_expect_s_past_warn_s_is_the_guide(self):
        self.stale_record(expect_s=4000, held_since=ago(DUE_S + 600))
        self.assertEqual(self.reap(), [])           # due at 3 x 4000 = 12000 s
        self.stale_record(expect_s=4000, held_since=ago(12600))
        self.assertEqual(self.reap(), [RID])

    def test_no_notice_folder_still_stops(self):
        self.store.site = {}
        self.stale_record()
        self.assertEqual(self.reap(), [RID])
        self.assertEqual(self.notices(), [])

    def test_a_detached_run_when_detached_runs_are_included(self):
        with mock.patch.object(stale, "INCLUDE_DETACHED", True):
            self.stale_record(detached=True)
            self.assertEqual(self.reap(), [RID])

    def test_finish_names_the_lock_and_why(self):
        self.stale_record()
        self.reap()
        r = rm.Run(self.store, self.layout(), "kit", "t", "engine-test", None, [], "held",
                   rid=RID, env=clean_env())
        ended = r.finish(1, "cancelled")
        self.assertEqual(ended["stopped_by"]["by"], stale.BY)
        self.assertIn("booking session has ended", ended["stopped_by"]["why"])


class Races(Base):
    def test_two_reapers_at_once_give_one_stop_and_one_notice(self):
        self.stale_record()
        go, got = threading.Barrier(2), []
        written = []
        real = notice.stale

        def counted(store, rec, why):
            written.append(rec["id"])
            return real(store, rec, why)

        def one():
            go.wait()
            got.extend(self.reap())

        with mock.patch.object(notice, "stale", counted):
            ts = [threading.Thread(target=one) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
        self.assertEqual(got, [RID])
        self.assertEqual(written, [RID])
        self.assertEqual(self.notices(), ["%s-stale.json" % RID])

    def test_a_record_gone_before_the_stop_writes_nothing(self):
        self.stale_record()
        real = rm.request_stop

        def gone_first(store, rid, by, session=None, why=None):
            os.remove(store.path("active", rid + ".json"))
            return real(store, rid, by, session, why=why)

        self.assertEqual(stale.reap(self.store, gone_first), [])
        self.assertIsNone(self.stop_of())
        self.assertEqual(self.notices(), [])

    def test_a_stray_stop_is_pruned(self):
        with open(self.store.path("active", RID + ".stop"), "w", encoding="utf-8") as f:
            json.dump({"by": stale.BY}, f)
        self.store.live_runs(prune=True)
        self.assertIsNone(self.stop_of())

    def test_a_reaper_that_cannot_have_the_reap_mutex_skips_its_pass(self):
        self.stale_record()
        done = threading.Event()
        release = threading.Event()

        def holder():
            with self.store.lock("reap", 5):
                done.set()
                release.wait(30)

        t = threading.Thread(target=holder)
        t.start()
        try:
            self.assertTrue(done.wait(10))
            self.assertEqual(self.reap(), [])
            self.assertIsNone(self.stop_of())
        finally:
            release.set()
            t.join(30)
        self.assertEqual(self.reap(), [RID])


class Recorded(Base):
    def test_booking_hosts_names_only_live_agent_hosts(self):
        snap = [(10, 1, "cmd.exe"), (20, 10, "Claude.exe"), (30, 20, "claude.exe")]
        ct = {20: 200, 30: 999}.get
        self.assertEqual(store_mod.booking_hosts([(10, 100), (20, 200), (30, 300)], snap,
                                                 frozenset({"claude.exe"}), ct), [[20, 200]])
        self.assertEqual(store_mod.booking_hosts([(10, 100)], snap, frozenset({"claude.exe"}), ct),
                         [])

    def test_booking_hosts_that_cannot_read_the_listing_is_empty(self):
        def broken():
            raise OSError("no listing")
        with mock.patch.object(store_mod.process, "snapshot", broken):
            self.assertEqual(store_mod.booking_hosts([(10, 100)], hosts=frozenset({"x.exe"})), [])

    def test_a_plain_run_records_its_agent_host_ancestors(self):
        parent = sorted(process.own_ancestors())
        names = {p: n.lower() for p, _pp, n in process.snapshot()}
        pick = next((pc for pc in parent if names.get(pc[0])), None)
        if pick is None:
            self.skipTest("no named ancestor")
        with mock.patch.object(store_mod, "agent_hosts", lambda *a: frozenset({names[pick[0]]})):
            r = rm.Run(self.store, self.layout(), "kit", "t", "engine-test", None, [], "waiting",
                       env=clean_env())
        self.assertIn([pick[0], pick[1]], r.rec["hosts"])

    def test_a_detached_run_records_its_launchers_agent_host(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        ct = process.proc_ctime(p.pid)
        name = {q: n.lower() for q, _pp, n in process.snapshot()}[p.pid]
        env = clean_env(**{rm.ORIGIN_ENV: "%d:%d" % (p.pid, ct)})
        with mock.patch.object(store_mod, "agent_hosts", lambda *a: frozenset({name})):
            r = rm.Run(self.store, self.layout(), "kit", "t", "engine-test", None, [], "waiting",
                       detached=True, env=env)
        self.assertIn([p.pid, ct], r.rec["hosts"])

    def test_book_records_the_kinds_guide(self):
        self.confine(1)
        s = rm.book("engine-test", "kit", "t", cores=1, store=self.store, env=clean_env())
        try:
            self.assertEqual(s.run.rec["warn_s"], 30 * 60 * self.store.settings.warn_scale)
            self.assertIn("hosts", self.store.read("active/%s.json" % s.id))
        finally:
            s.release()


class Shown(Base):
    def test_snapshot_shows_the_reason_and_writes_nothing(self):
        self.stale_record()
        self.live_record("r-20261031T000000Z-f7e5a1", hosts=[[os.getpid(), process.own_ctime()]])
        before = sorted(os.listdir(self.store.path("active")))
        snap = api.snapshot(self.store)
        rows = {r["id"]: r for r in snap["runs"]}
        self.assertIn("booking session has ended", rows[RID]["stale"])
        self.assertIsNone(rows["r-20261031T000000Z-f7e5a1"]["stale"])
        self.assertEqual(sorted(os.listdir(self.store.path("active"))), before)
        self.assertIsNone(self.stop_of())
        self.assertEqual(self.notices(), [])
        text = st.render(snap)
        self.assertIn("stale: run %s" % RID, text)


class EndToEnd(Base):
    def test_another_bookings_acquire_ends_a_stale_held_run(self):
        self.confine(1)
        s = rm.book("engine-test", "kit", "stuck", cores=1, store=self.store, env=clean_env())
        p = s.start([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: p.poll() is None and p.kill())
        s.run.save(hosts=[self.exited()], held_since=ago(DUE_S + 600))
        out = {}

        def waiter():
            out["got"] = s.wait(p)
            s.release(*out["got"])

        t = threading.Thread(target=waiter)
        t.start()
        other = rm.book("engine-test", "kit", "next", cores=1, store=self.store, env=clean_env(),
                        wait_s=60)
        other.release()
        t.join(60)
        self.assertFalse(t.is_alive())
        self.assertEqual(out["got"], (1, "cancelled"))
        ended = self.store.read("ended/%s.json" % s.id)
        self.assertEqual((ended["how"], ended["stopped_by"]["by"]), ("cancelled", stale.BY))
        self.assertIn("booking session has ended", ended["stopped_by"]["why"])
        self.assertTrue(any("ended by the lock" in x for x in self.said), self.said)


if __name__ == "__main__":
    unittest.main()
