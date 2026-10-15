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


LAND = "landing:z:\\repo\\.git"


def land(rid, cpu, expect_s, ticket, **kw):
    return waiter(rid, cpu, expect_s, ticket, priority="land", resource=LAND, **kw)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class LandPriority(unittest.TestCase):
    """priority "land": behind own_turn and owner-moved entries, ahead of every other waiting
    entry (gpu-free ones too), ticket order among landings; backfill as for any run ahead."""

    def setUp(self):
        self.L = lay.derive(lay.fake_hardware(threads=8, smt=2, gpus=1))
        self.assertEqual((self.L["cpu_slots"], lay.size(self.L, "gpu")), (4, 2))

    def plan(self, entries):
        return qm.plan(self.L, {"ticket": 9, "entries": entries}, now=NOW)[0]

    def test_the_full_order(self):
        # Each run needs every core, so each starts only after the one ahead of it.
        es = [waiter("r-plain", 4, 100, 2),
              waiter("r-gpu", 4, 100, 3, need={"cpu": 4, "gpu": 1}),
              waiter("r-land2", 4, 100, 7, priority="land"),
              waiter("r-land1", 4, 100, 6, priority="land"),
              waiter("r-moved", 4, 100, 5, moved=moved(1)),
              waiter("r-own", 4, 100, 8, hold="h-x")]
        got = self.plan(es)
        self.assertEqual(sorted(got, key=lambda r: got[r]["position"]),
                         ["r-own", "r-moved", "r-land1", "r-land2", "r-gpu", "r-plain"])
        self.assertEqual([got[r]["start"] for r in ("r-own", "r-moved", "r-land1", "r-land2")],
                         [NOW, NOW + 100, NOW + 200, NOW + 300])

    def test_a_landing_goes_ahead_of_an_earlier_ordinary_run(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         waiter("r-a", 4, 100, 2), waiter("r-l", 4, 100, 3, priority="land")])
        self.assertEqual((got["r-l"]["position"], got["r-l"]["start"]), (1, NOW + 900))
        self.assertEqual((got["r-a"]["position"], got["r-a"]["start"]), (2, NOW + 1000))

    def test_an_entry_without_the_fields_is_ordinary(self):
        # An older queue.json entry: no "priority", no "resource" key at all.
        old = waiter("r-old", 4, 100, 2)
        self.assertNotIn("priority", old)
        self.assertIsNone(qm.entry_priority(old))
        self.assertIsNone(qm.entry_resource(old))
        got = self.plan([old, waiter("r-l", 4, 100, 3, priority="land")])
        self.assertEqual(got["r-l"]["position"], 1)
        self.assertEqual(got["r-old"]["start"], NOW + 100)

    def test_an_unknown_priority_on_disk_plans_as_ordinary(self):
        got = self.plan([waiter("r-a", 4, 100, 2), waiter("r-x", 4, 100, 3, priority="urgent")])
        self.assertEqual(got["r-a"]["position"], 1)

    def test_a_short_run_backfills_ahead_of_a_landing_only_if_it_ends_first(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         land("r-l", 4, 100, 2),
                         waiter("r-long", 2, 2000, 3), waiter("r-short", 2, 900, 4)])
        self.assertEqual(got["r-l"]["start"], NOW + 900)
        self.assertEqual((got["r-short"]["start"], got["r-short"]["backfill"]), (NOW, True))
        self.assertLessEqual(got["r-short"]["start"] + 900, got["r-l"]["start"])
        self.assertEqual((got["r-long"]["start"], got["r-long"]["backfill"]), (NOW + 1000, False))

    def test_one_second_too_long_does_not_backfill(self):
        got = self.plan([holder("r-h", [0, 1], NOW - 100, 1000),
                         land("r-l", 4, 100, 2), waiter("r-over", 2, 901, 3)])
        self.assertEqual(got["r-l"]["start"], NOW + 900)
        self.assertEqual(got["r-over"]["start"], NOW + 1000)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class ResourceExclusion(unittest.TestCase):
    """An entry naming a resource never starts while another live entry holds it, and two
    entries naming it are never planned to overlap, however many slots are free."""

    def setUp(self):
        self.L = lay.derive(lay.fake_hardware(threads=8, smt=2, gpus=0))

    def plan(self, entries):
        return qm.plan(self.L, {"ticket": 9, "entries": entries}, now=NOW)[0]

    def test_a_held_entry_with_the_resource_keeps_a_waiting_one_out(self):
        h = holder("r-h", [0], NOW - 100, 1000)
        h["resource"] = LAND
        got = self.plan([h, land("r-l", 1, 100, 2)])
        self.assertFalse(got["r-l"]["now"])
        self.assertEqual(got["r-l"]["start"], NOW + 900)
        del h["resource"]                                   # slots alone let it start now
        self.assertTrue(self.plan([h, land("r-l", 1, 100, 2)])["r-l"]["now"])

    def test_another_resource_or_none_does_not(self):
        h = holder("r-h", [0], NOW - 100, 1000)
        h["resource"] = "landing:z:\\other\\.git"
        got = self.plan([h, land("r-l", 1, 100, 2), waiter("r-a", 1, 100, 3, resource=None)])
        self.assertTrue(got["r-l"]["now"])
        self.assertTrue(got["r-a"]["now"])

    def test_two_waiting_entries_with_it_never_overlap(self):
        got = self.plan([land("r-1", 1, 300, 2), land("r-2", 1, 100, 3), waiter("r-3", 1, 50, 4,
                                                                                 resource=LAND)])
        self.assertEqual(got["r-1"]["start"], NOW)
        self.assertEqual(got["r-2"]["start"], NOW + 300)
        self.assertEqual(got["r-3"]["start"], NOW + 400)
        spans = sorted((got[r]["start"], got[r]["start"] + e) for r, e in
                       (("r-1", 300), ("r-2", 100), ("r-3", 50)))
        for (a0, a1), (b0, _b1) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0)

    def test_a_landing_waiting_only_for_its_resource_does_not_hold_back_unrelated_runs(self):
        # Sixteen cpu slots (fake hardware). X1 lands repo X on a quarter of them, 30 min left;
        # X2, repo X's next landing, waits for X1 alone (its cores are free now). A landing of
        # repo Y and a 60-min ordinary run start now on idle cores; X3, repo X's third, still
        # waits for X2.
        L = lay.derive(lay.fake_hardware(threads=32, smt=2, gpus=0))
        self.assertEqual(L["cpu_slots"], 16)
        other = "landing:z:\\other\\.git"

        def plan(extra):
            x1 = holder("r-x1", [0, 1, 2, 3], NOW - 600, 2400)
            x1.update(resource=LAND, priority="land")
            es = [x1, land("r-x2", 4, 1200, 2)] + extra
            return qm.plan(L, {"ticket": 9, "entries": es}, now=NOW)[0]
        alone = plan([])["r-x2"]["start"]
        self.assertEqual(alone, NOW + 1800)
        got = plan([waiter("r-y", 4, 2700, 3, priority="land", resource=other),
                    waiter("r-run", 4, 3600, 4), land("r-x3", 4, 600, 5)])
        self.assertEqual(got["r-x2"]["start"], alone)               # no later than it would have
        self.assertTrue(got["r-y"]["now"], got["r-y"])
        self.assertTrue(got["r-run"]["now"], got["r-run"])
        self.assertFalse(got["r-y"]["backfill"] or got["r-run"]["backfill"])
        self.assertEqual(got["r-x3"]["start"], NOW + 1800 + 1200)   # after X2, never beside it
        x2 = set(got["r-x2"]["cores"])
        self.assertFalse(x2 & (set(got["r-y"]["cores"]) | set(got["r-run"]["cores"])))

    def test_a_resource_delayed_landing_still_gets_its_cores_from_a_run_behind_it(self):
        # Every core but X1's is wanted by the run behind X2 for longer than X1 has left: the
        # run may not take X2's cores, so it waits for X2 to end, and X2 starts as X1 ends.
        L = lay.derive(lay.fake_hardware(threads=32, smt=2, gpus=0))
        x1 = holder("r-x1", [0, 1, 2, 3], NOW - 600, 2400)
        x1["resource"] = LAND
        got = qm.plan(L, {"ticket": 9, "entries": [x1, land("r-x2", 4, 1200, 2),
                                                   waiter("r-big", 16, 3600, 3)]}, now=NOW)[0]
        self.assertEqual(got["r-x2"]["start"], NOW + 1800)
        self.assertEqual(got["r-big"]["start"], NOW + 3000)

    def test_a_parked_landing_waiting_for_its_resource_stays_a_barrier(self):
        # A parked entry is never passed by a run behind it, whatever put its start off.
        L = lay.derive(lay.fake_hardware(threads=32, smt=2, gpus=0))
        x1 = holder("r-x1", [0, 1, 2, 3], NOW - 600, 2400)
        x1["resource"] = LAND
        x2 = land("r-x2", 4, 1200, 2, parked=True, clear_at=NOW, avoid_after={})
        got = qm.plan(L, {"ticket": 9, "entries": [x1, x2, waiter("r-run", 4, 3600, 3)]},
                      now=NOW)[0]
        self.assertEqual(got["r-x2"]["start"], NOW + 1800)
        self.assertEqual(got["r-run"]["start"], NOW + 1800)

    def test_a_landing_whose_cores_are_busy_too_still_holds_back_a_long_run(self):
        # X2 needs one core more than X1 leaves free: it waits for X1's cores as well as its
        # resource, so it stays a barrier. The 3-core run behind it would fit beside
        # it, yet it may not start ahead of it and still run when X2 starts.
        L = lay.derive(lay.fake_hardware(threads=32, smt=2, gpus=0))
        x1 = holder("r-x1", [0, 1, 2, 3], NOW - 600, 2400)
        x1["resource"] = LAND
        got = qm.plan(L, {"ticket": 9, "entries": [x1, land("r-x2", 13, 1200, 2),
                                                   waiter("r-run", 3, 3600, 3)]}, now=NOW)[0]
        self.assertEqual(got["r-x2"]["start"], NOW + 1800)
        self.assertEqual(got["r-run"]["start"], NOW + 1800)

    def test_two_landings_of_a_repo_and_a_backfill_candidate(self):
        # A holder leaves two of the four slots busy for 900 s. Landing 1 needs all four, so it
        # waits for the slots (a barrier); landing 2 names the same resource and follows it.
        # Only a run that ends by landing 1's start may go ahead; none passes landing 2 by it.
        def plan(candidate):
            es = [holder("r-h", [0, 1], NOW - 100, 1000), land("r-l1", 4, 100, 2),
                  land("r-l2", 4, 100, 3), candidate]
            return self.plan(es)
        got = plan(waiter("r-fits", 2, 900, 4))
        self.assertEqual((got["r-fits"]["start"], got["r-fits"]["backfill"]), (NOW, True))
        self.assertEqual(got["r-l1"]["start"], NOW + 900)
        self.assertEqual(got["r-l2"]["start"], NOW + 1000)      # never beside landing 1
        got = plan(waiter("r-over", 2, 901, 4))
        self.assertFalse(got["r-over"]["backfill"])
        self.assertGreaterEqual(got["r-over"]["start"], got["r-l1"]["start"])
        self.assertEqual(got["r-l1"]["start"], NOW + 900)       # the candidate cost it nothing
        self.assertEqual(got["r-l2"]["start"], NOW + 1000)
        self.assertLess(got["r-l1"]["position"], got["r-l2"]["position"])

    def test_a_background_run_naming_it_holds_it_but_its_slots_still_yield(self):
        bg = {"id": "r-bg", "ticket": 1, "state": "bg", "need": {"cpu": 1, "gpu": 0},
              "cores": [0], "gpus": [], "held_at": NOW, "expect_s": 600, "resource": LAND}
        got, soft = qm.plan(self.L, {"ticket": 3, "entries": [bg, land("r-l", 1, 100, 2),
                                                             waiter("r-a", 4, 10, 3)]}, now=NOW)
        self.assertEqual(soft["cpu"], {0})
        self.assertEqual(got["r-l"]["start"], NOW + 600)
        self.assertTrue(got["r-a"]["now"])                  # background slots yield as before

    def test_its_parent_holding_it_does_not_keep_it_out(self):
        p = holder("r-p", [0], NOW - 100, 1000)
        p["resource"] = LAND
        got = self.plan([p, land("r-child", 1, 100, 2, parent="r-p"),
                         land("r-other", 1, 100, 3)])
        self.assertTrue(got["r-child"]["now"])
        self.assertFalse(got["r-other"]["now"])

    def test_an_own_turn_entry_waits_for_it_too(self):
        h = holder("r-h", [0], NOW - 100, 1000)
        h["resource"] = LAND
        got = self.plan([h, land("r-l", 1, 100, 2, hold="h-x")])
        self.assertEqual(got["r-l"]["start"], NOW + 900)

    def test_the_resource_holder_is_named_in_why_it_waits(self):
        h = holder("r-h", [0], NOW - 100, 1000)
        h.update(resource=LAND, repo="kit", slug="other")
        e = land("r-l", 1, 100, 2)
        q = {"ticket": 3, "entries": [h, e]}
        why = qm.busy_text(self.L, q, e, qm.plan(self.L, q, now=NOW)[0])
        self.assertIn("resource %s is held by" % LAND, why)
        self.assertIn("r-h", why)

    def test_join_carries_both_and_leaves_them_none_for_an_ordinary_run(self):
        q = {"ticket": 0, "entries": []}
        rec = {"id": "r-x", "repo": "kit", "slug": "s", "kind": "engine-test", "pid": 1,
               "ctime": 1, "expect_s": 60, "resource": LAND, "priority": "land"}
        e = qm.join(q, rec, {"cpu": 1, "gpu": 0})
        self.assertEqual((e["resource"], e["priority"]), (LAND, "land"))
        rec2 = {k: v for k, v in rec.items() if k not in ("resource", "priority")}
        rec2["id"] = "r-y"
        e2 = qm.join(q, rec2, {"cpu": 1, "gpu": 0})
        self.assertEqual((e2["resource"], e2["priority"]), (None, None))


if __name__ == "__main__":
    unittest.main()
