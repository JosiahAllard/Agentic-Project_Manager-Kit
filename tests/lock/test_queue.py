"""kitlib.lock.queue: holder_end (as the owner ruled it) and plan's order and
backfill, on a layout from fake hardware (no store)."""
import os
import unittest

if os.name == "nt":
    from kitlib.lock import layout as lay
    from kitlib.lock import queue as qm

NOW = 1_800_000_000.0


def holder(rid, cores, held_at, expect_s, ticket=1):
    return {"id": rid, "ticket": ticket, "state": "held", "need": {"cpu": len(cores), "gpu": 0},
            "cores": cores, "gpus": [], "held_at": held_at, "expect_s": expect_s}


def waiter(rid, cpu, expect_s, ticket, **kw):
    e = {"id": rid, "ticket": ticket, "state": "waiting", "need": {"cpu": cpu, "gpu": 0},
         "cores": [], "gpus": [], "held_at": None, "expect_s": expect_s}
    e.update(kw)
    return e


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class HolderEnd(unittest.TestCase):
    def test_before_its_expected_end_it_is_that_end(self):
        self.assertEqual(qm.holder_end(NOW - 100, 1000, NOW), NOW + 900)

    def test_overrun_half_is_now_plus_half_expected(self):
        self.assertEqual(qm.holder_end(NOW - 5000, 1000, NOW), NOW + 500)

    def test_overrun_half_is_rechecked_each_pass(self):
        later = NOW + 300
        self.assertEqual(qm.holder_end(NOW - 5000, 1000, later), later + 500)

    def test_no_held_at_counts_as_overrun(self):
        self.assertEqual(qm.holder_end(None, 1000, NOW), NOW + 500)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class Plan(unittest.TestCase):
    def setUp(self):
        # 8 threads, 2 to a core: 4 cpu slots; no gpu (one placeholder gpu slot).
        self.L = lay.derive(lay.fake_hardware(threads=8, smt=2, gpus=0))
        self.assertEqual(self.L["cpu_slots"], 4)

    def plan(self, entries):
        return qm.plan(self.L, {"ticket": 9, "entries": entries}, now=NOW)[0]

    def test_a_short_run_backfills_before_a_big_one(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         waiter("r-big", 4, 100, 2), waiter("r-small", 2, 100, 3)])
        self.assertEqual(got["r-big"]["start"], NOW + 900)
        self.assertEqual(got["r-small"]["start"], NOW)
        self.assertTrue(got["r-small"]["now"])
        self.assertTrue(got["r-small"]["backfill"])
        self.assertEqual(got["r-small"]["cores"], [2, 3])

    def test_a_long_run_does_not_pass_the_one_ahead(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         waiter("r-big", 4, 100, 2), waiter("r-long", 2, 2000, 3)])
        self.assertEqual(got["r-big"]["start"], NOW + 900)
        self.assertEqual(got["r-long"]["start"], NOW + 1000)
        self.assertFalse(got["r-long"]["backfill"])
        self.assertLess(got["r-big"]["position"], got["r-long"]["position"])

    def test_overrun_half_still_lets_a_short_run_through(self):
        es = [holder("r-h", [0, 1], NOW - 5000, 1000),
              waiter("r-big", 4, 100, 2), waiter("r-small", 2, 100, 3)]
        got = self.plan(es)
        self.assertEqual(got["r-big"]["start"], NOW + 500)
        self.assertEqual(got["r-small"]["start"], NOW)

    def test_own_turn_entries_plan_first(self):
        got = self.plan([waiter("r-a", 4, 100, 2), waiter("r-held", 4, 100, 3, hold="h-x")])
        self.assertEqual(got["r-held"]["position"], 1)
        self.assertEqual(got["r-held"]["start"], NOW)
        self.assertEqual(got["r-a"]["start"], NOW + 100)

    def test_avoid_keeps_a_run_off_held_slots(self):
        got = self.plan([waiter("r-a", 2, 100, 2, avoid={"cpu": [0, 1]})])
        self.assertEqual(got["r-a"]["cores"], [2, 3])

    def test_background_slots_are_taken_last(self):
        bg = {"id": "r-bg", "ticket": 1, "state": "bg", "need": {"cpu": 1, "gpu": 0},
              "cores": [0], "gpus": [], "held_at": NOW, "expect_s": 100}
        got, soft = qm.plan(self.L, {"ticket": 2, "entries": [bg, waiter("r-a", 3, 10, 2)]},
                            now=NOW)
        self.assertEqual(soft["cpu"], {0})
        self.assertEqual(got["r-a"]["cores"], [1, 2, 3])


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class MixedOrder(unittest.TestCase):
    """A waiting gpu run whose gpu slots are free goes ahead of cpu-only runs for the
    cores (queue order only); no run passes one ahead of it unless it ends before that
    one starts."""

    def setUp(self):
        self.L = lay.derive(lay.fake_hardware(threads=8, smt=2, gpus=1))
        self.assertEqual((self.L["cpu_slots"], lay.size(self.L, "gpu")), (4, 2))

    def plan(self, entries):
        return qm.plan(self.L, {"ticket": 9, "entries": entries}, now=NOW)[0]

    def gpu_waiter(self, rid, cpu, gpu, expect_s, ticket):
        return waiter(rid, cpu, expect_s, ticket, need={"cpu": cpu, "gpu": gpu})

    def test_a_gpu_run_with_its_gpu_free_goes_ahead_for_the_cores(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         waiter("r-cpu", 2, 100, 2), self.gpu_waiter("r-gpu", 2, 1, 100, 3)])
        self.assertEqual(got["r-gpu"]["position"], 1)
        self.assertEqual((got["r-gpu"]["start"], got["r-gpu"]["cores"]), (NOW, [2, 3]))
        self.assertEqual(got["r-cpu"]["position"], 2)
        self.assertGreater(got["r-cpu"]["start"], NOW)

    def test_a_gpu_run_whose_gpu_is_held_keeps_its_ticket_order(self):
        gh = {"id": "r-g", "ticket": 1, "state": "held", "need": {"cpu": 0, "gpu": 2},
              "cores": [], "gpus": [0, 1], "held_at": NOW - 100, "expect_s": 1000}
        got = self.plan([gh, waiter("r-cpu", 2, 100, 2), self.gpu_waiter("r-gpu", 2, 1, 100, 3)])
        self.assertEqual(got["r-cpu"]["position"], 1)
        self.assertEqual(got["r-cpu"]["start"], NOW)
        self.assertEqual(got["r-gpu"]["position"], 2)
        self.assertEqual(got["r-gpu"]["start"], NOW + 900)

    def test_nothing_passes_the_gpu_run_ahead_unless_it_ends_before_that_run_starts(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         self.gpu_waiter("r-gpu", 4, 1, 100, 2),
                         waiter("r-long", 2, 2000, 3), waiter("r-short", 2, 100, 4)])
        self.assertEqual(got["r-gpu"]["start"], NOW + 900)
        self.assertEqual(got["r-long"]["start"], NOW + 1000)
        self.assertFalse(got["r-long"]["backfill"])
        self.assertEqual(got["r-short"]["start"], NOW)
        self.assertTrue(got["r-short"]["backfill"])
        self.assertLess(got["r-short"]["start"] + 100, got["r-gpu"]["start"])


def moved(seq):
    return {"by": "owner", "name": "reserve_ui", "at": "2026-01-01T00:00:00Z", "seq": seq}


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class MovedToTop(unittest.TestCase):
    """The owner's move to the top: a moved waiting run plans ahead of every other waiting run
    but own_turn ones, the latest move first; nothing behind it backfills across its start, and
    runs holding slots keep them."""

    def setUp(self):
        self.L = lay.derive(lay.fake_hardware(threads=8, smt=2, gpus=1))
        self.assertEqual((self.L["cpu_slots"], lay.size(self.L, "gpu")), (4, 2))

    def plan(self, entries):
        return qm.plan(self.L, {"ticket": 9, "entries": entries}, now=NOW)[0]

    def test_a_moved_run_plans_first_and_starts_when_the_holder_ends(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         waiter("r-a", 4, 100, 2), waiter("r-b", 4, 100, 3),
                         waiter("r-m", 4, 100, 4, moved=moved(1))])
        self.assertEqual(got["r-m"]["position"], 1)
        self.assertEqual(got["r-m"]["start"], NOW + 900)
        self.assertEqual((got["r-a"]["position"], got["r-a"]["start"]), (2, NOW + 1000))
        self.assertEqual(got["r-b"]["position"], 3)

    def test_the_latest_move_goes_first(self):
        got = self.plan([waiter("r-a", 4, 100, 2, moved=moved(1)),
                         waiter("r-b", 4, 100, 3, moved=moved(2)), waiter("r-c", 4, 100, 4)])
        self.assertEqual([got[r]["position"] for r in ("r-b", "r-a", "r-c")], [1, 2, 3])

    def test_own_turn_entries_still_plan_first(self):
        got = self.plan([waiter("r-m", 4, 100, 2, moved=moved(1)),
                         waiter("r-held", 4, 100, 3, hold="h-x")])
        self.assertEqual(got["r-held"]["position"], 1)
        self.assertEqual(got["r-m"]["position"], 2)

    def test_a_moved_run_goes_ahead_of_a_gpu_run_whose_gpu_is_free(self):
        got = self.plan([waiter("r-gpu", 2, 100, 2, need={"cpu": 2, "gpu": 1}),
                         waiter("r-m", 4, 100, 3, moved=moved(1))])
        self.assertEqual(got["r-m"]["position"], 1)
        self.assertEqual(got["r-m"]["start"], NOW)

    def test_nothing_behind_it_backfills_across_its_start(self):
        # Without the move r-long (ticket 2) would start now and r-m would wait for it.
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         waiter("r-long", 2, 2000, 2), waiter("r-short", 2, 100, 3),
                         waiter("r-m", 4, 100, 4, moved=moved(1))])
        self.assertEqual(got["r-m"]["start"], NOW + 900)
        self.assertEqual(got["r-long"]["start"], NOW + 1000)
        self.assertFalse(got["r-long"]["backfill"])
        self.assertEqual(got["r-short"]["start"], NOW)
        self.assertTrue(got["r-short"]["backfill"])

    def test_it_starts_now_when_what_it_needs_is_free(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         waiter("r-a", 4, 100, 2), waiter("r-m", 2, 2000, 3, moved=moved(1))])
        self.assertTrue(got["r-m"]["now"])
        self.assertFalse(got["r-m"]["backfill"])
        self.assertEqual(got["r-m"]["cores"], [2, 3])
        self.assertEqual(got["r-a"]["start"], NOW + 2000)

    def test_move_to_top_marks_the_entry_with_the_next_seq(self):
        q = {"ticket": 3, "entries": [waiter("r-a", 1, 10, 2, moved=moved(4)),
                                      waiter("r-b", 1, 10, 3)]}
        m = qm.move_to_top(q, q["entries"][1], "owner", "reserve_ui", "2026-01-01T00:00:00Z")
        self.assertEqual(m, {"by": "owner", "name": "reserve_ui", "at": "2026-01-01T00:00:00Z",
                             "seq": 5})
        self.assertIs(q["entries"][1]["moved"], m)
        self.assertEqual(q["entries"][1]["ticket"], 3)          # it keeps its ticket


if __name__ == "__main__":
    unittest.main()
