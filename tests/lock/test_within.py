"""A run confined to a set of threads (book(within=...), the CLI's --within): it takes only cpu
slots whose threads all lie inside the set, waits for those when others are free, is refused
(exit 2, nothing written) when the set cannot hold it, and plans its holds and queue turn on those
slots alone. Entries and records written before `within` existed load and plan as before."""
import datetime
import os
import unittest
from unittest import mock

from .helpers import OWNER, StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import api
    from kitlib.lock import holds as hl
    from kitlib.lock import layout as lay
    from kitlib.lock import queue as qm
    from kitlib.lock import run as rm
    from kitlib.lock import status as st
    from kitlib.lock.store import LockError, iso, utc_now
    from kitlib.proc import process

NOW = 1_800_000_000.0
RID = "r-20261006T120000Z-abcdef"
R_W = "r-20261006T120000Z-c0c0c0"


def holder(rid, cores, held_at, expect_s, ticket=1):
    return {"id": rid, "ticket": ticket, "state": "held", "need": {"cpu": len(cores), "gpu": 0},
            "cores": cores, "gpus": [], "held_at": held_at, "expect_s": expect_s}


def waiter(rid, cpu, expect_s, ticket, **kw):
    e = {"id": rid, "ticket": ticket, "state": "waiting", "need": {"cpu": cpu, "gpu": 0},
         "cores": [], "gpus": [], "held_at": None, "expect_s": expect_s}
    e.update(kw)
    return e


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class Threads(unittest.TestCase):
    def test_parse_threads_reads_a_linux_cpuset_list(self):
        self.assertEqual(lay.parse_threads("8-15"), list(range(8, 16)))
        self.assertEqual(lay.parse_threads(" 0-1, 4 ,6-7"), [0, 1, 4, 6, 7])
        self.assertEqual(lay.parse_threads("3,3,2"), [2, 3])

    def test_parse_threads_refuses_what_is_not_one(self):
        for bad in ("", "8-", "-3", "a", "5-2", "1,,2", "1-2-3", "0-99999", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    lay.parse_threads(bad)

    def test_within_slots_are_the_slots_wholly_inside(self):
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, l3=2))
        self.assertEqual(lay.within_slots(L, range(8, 16)), [4, 5, 6, 7])
        self.assertEqual(lay.within_slots(L, range(8, 15)), [4, 5, 6])     # 14 without 15
        self.assertEqual(lay.within_slots(L, [9]), [])


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class Plan(unittest.TestCase):
    def setUp(self):
        # 8 threads, 2 to a core: 4 cpu slots; no gpu (one placeholder gpu slot).
        self.L = lay.derive(lay.fake_hardware(threads=8, smt=2, gpus=0))

    def plan(self, entries):
        return qm.plan(self.L, {"ticket": 9, "entries": entries}, now=NOW)[0]

    def test_an_idle_layout_gives_the_slots_inside_within(self):
        got = self.plan([waiter("r-w", 2, 100, 1, within={"cpu": [2, 3]})])
        self.assertEqual(got["r-w"]["cores"], [2, 3])
        self.assertEqual(got["r-w"]["start"], NOW)

    def test_it_waits_for_its_slots_while_others_are_free(self):
        got = self.plan([holder("r-h", [2, 3], NOW - 100, 1000),
                         waiter("r-w", 2, 100, 2, within={"cpu": [2, 3]})])
        self.assertEqual(got["r-w"]["start"], NOW + 900)
        self.assertEqual(got["r-w"]["cores"], [2, 3])
        self.assertFalse(got["r-w"]["now"])

    def test_an_entry_without_within_plans_as_before(self):
        for kw in ({}, {"within": {}}, {"within": None}, {"within": {"cpu": []}}):
            with self.subTest(kw=kw):
                got = self.plan([holder("r-h", [2, 3], NOW - 100, 1000),
                                 waiter("r-w", 2, 100, 2, **kw)])
                self.assertEqual((got["r-w"]["start"], got["r-w"]["cores"]), (NOW, [0, 1]))

    def test_short_pools_and_busy_text_count_only_the_within_slots(self):
        q = {"ticket": 9, "entries": [
            dict(holder("r-h", [2, 3], NOW - 100, 1000), repo="kit", slug="h", pid=1),
            waiter("r-w", 1, 100, 2, within={"cpu": [2, 3]})]}
        e = q["entries"][1]
        self.assertEqual(qm.short_pools(self.L, q, e), ["cpu"])
        text = qm.busy_text(self.L, q, e, qm.plan(self.L, q, now=NOW)[0])
        self.assertIn("cpu slots 2-3 are held by kit/h", text)
        self.assertIn("it runs only within cpu slots 2-3 (--within)", text)
        q["entries"][1].pop("within")
        self.assertEqual(qm.short_pools(self.L, q, q["entries"][1]), [])


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class PlanBehind(unittest.TestCase):
    """A waiting --within run keeps later runs off its own slots only: one that fits outside them
    starts now, one that would need a slot inside them waits for it (the backfill rule)."""
    def setUp(self):
        # The fake machine: 2 threads to a core, 12 cpu slots, no gpu.
        self.L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=0))

    def plan(self, entries):
        return qm.plan(self.L, {"ticket": 9, "entries": entries}, now=NOW)[0]

    def test_a_run_behind_it_that_fits_outside_its_slots_starts_now(self):
        got = self.plan([holder("r-a", [4, 5, 6, 7], NOW - 100, 7300),
                         waiter("r-w", 2, 3600, 2, within={"cpu": [4, 5, 6, 7]}),
                         waiter("r-x", 2, 10800, 3)])
        self.assertEqual((got["r-w"]["start"], got["r-w"]["cores"]), (NOW + 7200, [4, 5]))
        self.assertEqual((got["r-x"]["start"], got["r-x"]["cores"]), (NOW, [0, 1]))
        self.assertTrue(got["r-x"]["backfill"])

    def test_a_run_behind_it_that_needs_a_slot_inside_them_still_waits(self):
        # W will take 4-6 once A leaves; 7 is free now but W's, so Y (7 slots) waits for W.
        got = self.plan([holder("r-a", [4, 5], NOW - 100, 7300),
                         waiter("r-w", 3, 3600, 2, within={"cpu": [4, 5, 6, 7]}),
                         waiter("r-x", 2, 10800, 3),
                         waiter("r-y", 7, 10800, 4)])
        self.assertEqual((got["r-w"]["start"], got["r-w"]["cores"]), (NOW + 7200, [4, 5, 6]))
        self.assertEqual((got["r-x"]["start"], got["r-x"]["cores"]), (NOW, [0, 1]))
        self.assertEqual(got["r-y"]["start"], NOW + 7200)
        self.assertNotIn(7, got["r-x"]["cores"])

    def test_one_that_also_needs_a_gpu_slot_bars_every_slot(self):
        # Its gpu has no within, so a run behind it may not start before it on any slot.
        self.L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1))
        got = self.plan([holder("r-a", [4, 5, 6, 7], NOW - 100, 7300),
                         waiter("r-w", 2, 3600, 2, within={"cpu": [4, 5, 6, 7]},
                                need={"cpu": 2, "gpu": 1}),
                         waiter("r-x", 2, 10800, 3)])
        self.assertEqual(got["r-w"]["start"], NOW + 7200)
        self.assertTrue(got["r-w"]["gpus"])
        self.assertEqual(got["r-x"]["start"], NOW + 7200)

    def test_a_parked_one_bars_every_slot(self):
        got = self.plan([holder("r-a", [4, 5, 6, 7], NOW - 100, 7300),
                         waiter("r-w", 2, 3600, 2, within={"cpu": [4, 5, 6, 7]}, parked=True,
                                clear_at=NOW + 7200),
                         waiter("r-x", 2, 10800, 3)])
        self.assertEqual(got["r-w"]["start"], NOW + 7200)
        self.assertEqual(got["r-x"]["start"], NOW + 7200)

    def test_a_within_run_behind_one_with_the_same_set_does_not_start_first(self):
        # Slot 7 is free now, but W1 may need it once A leaves: W2 waits for W1's start.
        got = self.plan([holder("r-a", [4, 5], NOW - 100, 7300),
                         waiter("r-w1", 3, 3600, 2, within={"cpu": [4, 5, 6, 7]}),
                         waiter("r-w2", 1, 10800, 3, within={"cpu": [4, 5, 6, 7]})])
        self.assertEqual((got["r-w1"]["start"], got["r-w1"]["cores"]), (NOW + 7200, [4, 5, 6]))
        self.assertEqual((got["r-w2"]["start"], got["r-w2"]["cores"]), (NOW + 7200, [7]))

    def test_a_within_run_behind_one_whose_set_overlaps_takes_slots_outside_it_now(self):
        got = self.plan([holder("r-a", [4, 5], NOW - 100, 7300),
                         waiter("r-w1", 3, 3600, 2, within={"cpu": [4, 5, 6, 7]}),
                         waiter("r-w2", 2, 10800, 3, within={"cpu": [6, 7, 8, 9]})])
        self.assertEqual((got["r-w1"]["start"], got["r-w1"]["cores"]), (NOW + 7200, [4, 5, 6]))
        self.assertEqual((got["r-w2"]["start"], got["r-w2"]["cores"]), (NOW, [8, 9]))
        self.assertTrue(got["r-w2"]["backfill"])


class Holds(StoreCase):
    def owner_hold(self, slots):
        now = utc_now()
        h, _ = hl.create(self.store, self.layout(), {
            "owner": "owner", "scope": "slots", "start": now,
            "end": now + datetime.timedelta(hours=1), "slots": {"cpu": slots, "gpu": []}},
            OWNER, None)
        return h

    def test_a_hold_over_the_within_slots_blocks_only_a_run_within_them(self):
        h = self.owner_hold([4, 5, 6, 7])
        L, need = self.layout(), {"cpu": 2, "gpu": 0}
        _, blocking, _ = hl.cover_plan(self.store, L, need, 600)
        self.assertEqual(blocking, [])
        cover, blocking, _ = hl.cover_plan(self.store, L, need, 600, within={"cpu": [4, 5, 6, 7]})
        self.assertEqual([x["id"] for x in blocking], [h["id"]])
        self.assertEqual(sorted(cover["cpu"]), [4, 5, 6, 7])

    def test_a_hold_outside_the_within_slots_does_not_block(self):
        self.owner_hold([0, 1, 2, 3, 8, 9, 10, 11])
        _, blocking, _ = hl.cover_plan(self.store, self.layout(), {"cpu": 4, "gpu": 0}, 600,
                                       within={"cpu": [4, 5, 6, 7]})
        self.assertEqual(blocking, [])


class Book(StoreCase):
    def code(self, **kw):
        kw.setdefault("store", self.store)
        kw.setdefault("env", clean_env())
        with self.assertRaises(LockError) as cm:
            rm.book(kw.pop("kind", "engine-test"), kw.pop("repo", "kit"), kw.pop("slug", "t"), **kw)
        return cm.exception

    def everything_held(self):
        now = utc_now()
        hl.create(self.store, self.layout(), {"owner": "owner", "scope": "whole", "start": now,
                                              "end": now + datetime.timedelta(hours=1)},
                  OWNER, None)

    def ended(self):
        d = self.store.path("ended")
        return [self.store.read("ended/" + n) for n in (os.listdir(d) if os.path.isdir(d) else [])
                if n.startswith("r-")]

    def nothing_written(self):
        self.assertEqual(self.ended(), [])
        self.assertIsNone(self.store.read("queue.json"))

    def test_a_run_takes_its_slot_inside_within_and_its_command_sees_those_threads(self):
        usable = self.confine(2)
        L = self.layout()
        last = usable[-1]
        ts = L["slots_cpu"][last]
        within = "%d-%d" % (ts[0], ts[-1])
        with rm.slot("engine-test", "kit", "t", cores=1, within=within, store=self.store,
                     env=clean_env()) as launch:
            self.assertEqual(launch.cores, [last])
            self.assertEqual(launch.env[rm.CPUSET_ENV], within)
            rec = self.store.read("active/%s.json" % launch.id)
            self.assertEqual(rec["within"], within)
            self.assertEqual(rec["within_slots"], {"cpu": [last]})
            q = self.store.read("queue.json")
            self.assertEqual(q["entries"][0]["within"], {"cpu": [last]})

    def test_a_run_without_within_writes_no_within_key(self):
        self.confine(1)
        with rm.slot("engine-test", "kit", "t", cores=1, store=self.store,
                     env=clean_env()) as launch:
            rec = self.store.read("active/%s.json" % launch.id)
            self.assertNotIn("within", rec)
            self.assertNotIn("within_slots", rec)
            self.assertNotIn("within", self.store.read("queue.json")["entries"][0])

    def test_a_partial_slot_is_left_out(self):
        self.everything_held()
        self.assertEqual(self.code(within="8-14", cores=1, no_wait=True).code, 11)
        rec = self.ended()[0]
        self.assertEqual((rec["within"], rec["within_slots"]), ("8-14", {"cpu": [4, 5, 6]}))

    def test_an_owner_hold_over_the_within_slots_refuses_with_11_while_others_are_free(self):
        now = utc_now()
        hl.create(self.store, self.layout(), {
            "owner": "owner", "scope": "slots", "start": now,
            "end": now + datetime.timedelta(hours=1), "slots": {"cpu": [4, 5, 6, 7], "gpu": []}},
            OWNER, None)
        e = self.code(within="8-15", cores=1, no_wait=True)
        self.assertEqual(e.code, 11, e.message)       # the hold keeps it out, not a busy queue (6)
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])

    def test_threads_may_be_given_as_numbers(self):
        self.everything_held()
        self.assertEqual(self.code(within=[11, 10, 8, 9], no_wait=True).code, 11)
        rec = self.ended()[0]
        self.assertEqual((rec["within"], rec["within_slots"]), ("8-11", {"cpu": [4, 5]}))

    def test_refusals_are_usage_and_write_nothing(self):
        for within, kw, says in (
                ("8-", {}, "--within"),
                ("30-40", {}, "not threads of this machine"),
                ("9", {}, "no whole cpu slot"),
                ("8-9", {}, "--cores 1"),                # engine-test is 2 slots
                ("8-15", {"kind": "cpu-eval"}, "--cores 4"),
                ("8-15", {"hold": "h-20261006T120000Z-abcd"}, "--hold")):
            with self.subTest(within=within, kw=kw):
                e = self.code(within=within, no_wait=True, **kw)
                self.assertEqual(e.code, 2, e.message)
                self.assertIn(says, e.message)
                self.nothing_written()

    def test_threads_the_division_leaves_out_are_not_refused(self):
        # The owner's division keeps the first 6 slots (threads 0-11); 12-15 are on the machine.
        self.store.write("machine.json", {"cpu_slots": 6})
        for within, says in (("12-15", "no whole cpu slot"), ("8-30", "not threads of this machine")):
            with self.subTest(within=within):
                e = self.code(within=within, cores=1, no_wait=True)
                self.assertEqual(e.code, 2, e.message)
                self.assertIn(says, e.message)
                self.nothing_written()
        self.everything_held()
        e = self.code(within="8-15", cores=1, no_wait=True)
        self.assertEqual(e.code, 11, e.message)
        rec = self.ended()[0]
        self.assertEqual((rec["within"], rec["within_slots"]), ("8-15", {"cpu": [4, 5]}))

    def relaid(self, division):
        """Patches book's second look at the layout to find `division` adopted meanwhile."""
        L2 = lay.derive(self.hw, division)
        return mock.patch.object(rm, "_relayout", lambda store, L: L2)

    def test_a_layout_adopted_before_it_waits_works_its_slots_out_again(self):
        self.everything_held()
        with self.relaid({"cpu_slot_threads": 4}):            # slot k is threads 4k..4k+3
            e = self.code(within="8-15", cores=1, no_wait=True)
        self.assertEqual(e.code, 11, e.message)
        rec = self.ended()[0]
        self.assertEqual((rec["within"], rec["within_slots"]), ("8-15", {"cpu": [2, 3]}))

    def test_a_layout_adopted_before_it_waits_can_refuse_it(self):
        self.everything_held()
        with self.relaid({"cpu_slot_threads": 8}):            # 8-14 holds no slot of 8 threads
            e = self.code(within="8-14", cores=1, no_wait=True)
        self.assertEqual(e.code, 2, e.message)
        self.assertIn("no whole cpu slot", e.message)
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])

    def test_a_nested_run_inheriting_cores_outside_within_is_refused(self):
        anc = process.own_ancestors()
        pp = os.getppid()
        ct = process.proc_ctime(pp)
        if ct is None or (pp, ct) not in anc:
            self.skipTest("this process's parent cannot be read as an ancestor")
        self.live_record(RID, pid=pp, ctime=ct, has={"cpu": 2}, cores=[0, 1])
        env = clean_env(**{rm.NEST_ENV: "%s:%d:%d" % (RID, pp, ct)})
        e = self.code(within="8-15", env=env)
        self.assertEqual(e.code, 2)
        self.assertIn("nested", e.message)
        s = rm.book("engine-test", "kit", "t", within="0-3", store=self.store, env=env)
        try:
            self.assertTrue(s.nested)
        finally:
            s.release()


class Snapshot(StoreCase):
    def entry(self, rid, ticket, **kw):
        m = self.store.run_mutex(rid)
        self.addCleanup(m.release)
        e = {"id": rid, "ticket": ticket, "state": "waiting", "pid": os.getpid(),
             "ctime": process.own_ctime(), "repo": "kit", "slug": "w", "kind": "engine-test",
             "need": {"cpu": 2, "gpu": 0}, "expect_s": 600, "since": iso(), "held_at": None,
             "cores": [], "gpus": [], "hold": None, "avoid": {}, "prefer": {}, "parked": False,
             "blocked_by": None, "clear_at": None, "avoid_after": {}, "parent": None}
        e.update(kw)
        return e

    def test_a_record_with_within_is_read_and_one_without_reads_none(self):
        self.live_record(RID, within="8-15", within_slots={"cpu": [4, 5, 6, 7]})
        self.live_record(R_W)
        rows = {r["id"]: r for r in api.snapshot(self.store)["runs"]}
        self.assertEqual(rows[RID]["within"], "8-15")
        self.assertIsNone(rows[R_W]["within"])
        self.assertEqual(api.SCHEMA_VERSION, 1)

    def test_a_waiting_entry_shows_its_within_slots(self):
        self.store.write("queue.json", {"ticket": 2, "entries": [
            self.entry(RID, 1, within={"cpu": [4, 5, 6, 7]}), self.entry(R_W, 2)]})
        snap = api.snapshot(self.store)
        rows = {r["id"]: r for r in snap["queue"]["entries"]}
        self.assertEqual(rows[RID]["within_slots"], [4, 5, 6, 7])
        self.assertIsNone(rows[R_W]["within_slots"])
        text = "\n".join(st.render(snap))
        self.assertIn("within cpu slots 4-7 (--within)", text)
        self.assertEqual(text.count("--within"), 1)

    def test_an_old_queue_entry_plans_as_before(self):
        # Written before `within` existed: no key at all.
        self.store.write("queue.json", {"ticket": 1, "entries": [self.entry(RID, 1)]})
        row = api.snapshot(self.store)["queue"]["entries"][0]
        self.assertIsNone(row["within_slots"])
        self.assertIsNotNone(row["est_start"])
