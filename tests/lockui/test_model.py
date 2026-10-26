"""kitlib.lockui.model: the schedule window's view of one api.snapshot() (and api.history()),
with no tkinter."""
import datetime as dt
import unittest

from kitlib.lockui import model as m

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def z(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(minutes):
    return z(NOW + dt.timedelta(minutes=minutes))


def snap(**over):
    """A snapshot of a 4-slot machine: run A held on cpu 0-1, run B waiting, an owner whole hold
    from +60 to +120 min, an unfixed half hold, and an agent hold the whole hold takes cpu 3 from."""
    s = {
        "schema_version": 1, "revision": 5, "at": z(NOW),
        "hardware": {"threads": 8, "memory_mb": {"total": 65536, "free": 16384}, "problems": []},
        "layout": {"cpu_slots": 4, "gpu_slots": 1, "agent_cap": 2, "threads": 8,
                   "warning": None, "hardware_change": {"pending": False, "changed": False}},
        "slots": {"cpu": [
            {"slot": 0, "holder": "r-A", "background": False, "hold": None, "hold_owner": None,
             "threads": [0, 1]},
            {"slot": 1, "holder": "r-A", "background": False, "hold": None, "hold_owner": None,
             "threads": [2, 3]},
            {"slot": 2, "holder": None, "background": False, "hold": "h-half", "hold_owner": "owner",
             "threads": [4, 5]},
            {"slot": 3, "holder": None, "background": False, "hold": "h-agent", "hold_owner": "agent",
             "threads": [6, 7]}],
            "gpu": [{"slot": 0, "holder": None, "background": False, "hold": None,
                     "hold_owner": None, "card": 0}]},
        "runs": [
            {"id": "r-A", "state": "held", "repo": "kit", "slug": "suite", "kind": "engine-test",
             "since": at(-30), "held_since": at(-20), "expect_s": 600, "expect_from": "history",
             "cores": [0, 1], "gpus": [], "background": False,
             "expected_end": at(-10), "overrun": {"s": 600, "planned_end": at(5)},
             "usage": {"peak_cpu_cores": 3.5, "peak_mem_mb": 2048}},
            {"id": "r-B", "state": "waiting", "repo": "game", "slug": "e2e", "kind": "measure",
             "since": at(-5), "cores": [], "gpus": [], "background": False, "expected_end": None,
             "overrun": None, "usage": {}}],
        "queue": {"error": None, "entries": [
            {"id": "r-B", "position": 1, "repo": "game", "slug": "e2e", "kind": "measure",
             "needs": {"cpu": 2, "gpu": 1}, "since": at(-5), "expect_s": 1200, "est_start": at(5),
             "backfill": True, "parked": False, "why": "waits for r-A"}]},
        "holds": [
            {"id": "h-whole", "owner": "owner", "scope": "whole", "start": at(60), "end": at(120),
             "note": "gaming", "covers": {"cpu": [0, 1, 2, 3], "gpu": [0]}, "slots_fixed": True,
             "live": False},
            {"id": "h-half", "owner": "owner", "scope": "half", "start": at(-60), "end": at(30),
             "note": None, "covers": {"cpu": [2], "gpu": []}, "slots_fixed": False, "live": True},
            {"id": "h-agent", "owner": "agent", "scope": "slots", "start": at(-10), "end": at(90),
             "repo": "svc", "slug": "train", "kind": "local-ai", "state": "open",
             "slots": {"cpu": [3], "gpu": [0]}, "covers": {"cpu": [3], "gpu": []},
             "start_local": "11:50", "end_local": "13:30", "slots_fixed": True, "live": True}],
        "holds_error": None, "kinds_error": None,
        "game": {"enabled": False, "games": [], "running": None, "hold": None},
    }
    s.update(over)
    return s


HISTORY = [
    {"id": "r-old", "repo": "kit", "slug": "gate", "kind": "engine-test", "since": at(-200),
     "held_since": at(-190), "ended": at(-150), "expect_s": 1200, "cores": [0, 1], "gpus": [],
     "peak_cpu_cores": 2.0, "peak_mem_mb": 900, "partial": False, "code": 0},
    {"id": "r-bad", "repo": "kit", "slug": "x", "kind": "engine-test", "since": "garbage",
     "ended": at(-100)},
]


class Text(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(m.ranges([5, 0, 1, 2]), "0-2,5")
        self.assertEqual(m.ranges([]), "")

    def test_span(self):
        self.assertEqual(m.span(40), "40 s")
        self.assertEqual(m.span(720), "12 min")
        self.assertEqual(m.span(9000), "2.5 h")

    def test_when(self):
        now = NOW.astimezone()
        self.assertEqual(m.when(now, now), now.strftime("%H:%M"))
        later = now + dt.timedelta(days=2)
        self.assertEqual(m.when(later, now), later.strftime("%a %H:%M"))

    def test_holds_and_needs_text(self):
        self.assertEqual(m.holds_text([0, 1, 2], [1]), "slots 0-2 + gpu 1")
        self.assertEqual(m.holds_text([], []), "no slots")
        self.assertEqual(m.needs_text(1, 0), "1 slot")
        self.assertEqual(m.needs_text(2, 1), "2 slots + 1 gpu")

    def test_to_local_reads_api_times(self):
        t = m.to_local("2026-10-08T12:00:00Z")
        self.assertEqual(t, NOW)
        self.assertIsNotNone(t.tzinfo)
        self.assertIsNone(m.to_local(None))
        self.assertIsNone(m.to_local("not a time"))

    def test_hold_slots_text(self):
        self.assertEqual(m.slots_text({"covers": {"cpu": [0, 1], "gpu": [0]}}), "cpu 0/1, gpu 0")
        self.assertEqual(m.slots_text({"covers": {"cpu": [2]}, "slots_fixed": False}, short=True),
                         "c2 for now")
        self.assertEqual(m.slots_text({"covers": {}}), "no slots")

    def test_assign_lanes(self):
        a = {"_s": NOW, "_e": NOW + dt.timedelta(hours=1)}
        b = {"_s": NOW + dt.timedelta(minutes=30), "_e": NOW + dt.timedelta(hours=2)}
        c = {"_s": NOW + dt.timedelta(hours=1), "_e": NOW + dt.timedelta(hours=3)}
        self.assertEqual(m.assign_lanes([a, b, c]), 2)
        self.assertEqual((a["_lane"], b["_lane"], c["_lane"]), (0, 1, 0))


class Build(unittest.TestCase):
    def setUp(self):
        self.v = m.build(snap(), HISTORY, now=NOW)

    def test_schedule_boxes_are_the_owner_and_game_holds(self):
        self.assertEqual([h["id"] for h in self.v["holds"]], ["h-whole", "h-half"])
        self.assertEqual(self.v["holds"][0]["_s"], NOW + dt.timedelta(minutes=60))

    def test_running_runs_get_colours_waiting_ones_do_not(self):
        self.assertEqual(list(self.v["colour"]), ["r-A"])

    def test_strip_holds_runs_queue_bookings_and_history(self):
        kinds = sorted(it["type"] for it in self.v["strip"])
        self.assertEqual(kinds, ["booking", "hist", "run", "wait"])
        run = next(it for it in self.v["strip"] if it["type"] == "run")
        self.assertEqual(run["_e"], NOW)            # past its expected end: drawn to now
        wait = next(it for it in self.v["strip"] if it["type"] == "wait")
        self.assertEqual(wait["_e"] - wait["_s"], dt.timedelta(seconds=1200))
        self.assertIn("backfill", wait["text"])
        hist = next(it for it in self.v["strip"] if it["type"] == "hist")
        self.assertTrue(hist["late"])              # 40 min held against 20 min expected
        self.assertIn("peak 2.0 cores, 900 MB", hist["text"])

    def test_a_booking_shows_what_a_strong_hold_took(self):
        b = next(it for it in self.v["strip"] if it["type"] == "booking")
        self.assertIn("lost gpu 0 to h-whole", b["text"])
        txt = "".join(t for t, _tags in self.v["bookings"])
        self.assertIn("LOST gpu 0 to h-whole", txt)

    def test_details_say_overrun_and_peaks(self):
        txt = "".join(t for t, _tags in self.v["details"])
        self.assertIn("kit/suite", txt)
        self.assertIn("holds slots 0-1", txt)
        self.assertIn("(from its history)", txt)
        self.assertIn("peak 3.5 cores, 2048 MB", txt)
        self.assertIn("OVERRUN 10 min past its expected end", txt)
        self.assertIn(("  OVERRUN", ("bad",)), [(t[:9], tags) for t, tags in self.v["details"]])

    def test_queue_lines(self):
        txt = "".join(t for t, _tags in self.v["queue"])
        self.assertIn("#1 game/e2e  measure  needs 2 slots + 1 gpu", txt)
        self.assertIn("waits for r-A", txt)
        self.assertIn("backfill", txt)
        self.assertNotIn("moved to the top", txt)

    def test_each_waiting_run_carries_its_move_to_top_marker(self):
        self.assertEqual(self.v["queue"][0], ("", ("move:r-B",)))

    def test_a_moved_run_says_so(self):
        q = snap()["queue"]
        q["entries"][0]["moved"] = {"by": "owner", "name": "reserve_ui", "at": at(-2), "seq": 1}
        v = m.build(snap(queue=q), now=NOW)
        self.assertIn(", moved to the top by the owner at %s" % m.when(m.to_local(at(-2)), NOW),
                      "".join(t for t, _tags in v["queue"]))

    def test_core_map(self):
        cpu = self.v["cores"]["cpu"]
        self.assertEqual([b["holder"] for b in cpu], ["r-A", "r-A", None, None])
        self.assertEqual([b["outline"] for b in cpu], [None, None, "half", "agent"])
        self.assertEqual(cpu[0]["label"], "suite")
        self.assertEqual(cpu[0]["title"], "slot 0  t0-1")
        self.assertEqual(self.v["cores"]["gpu"][0]["title"], "gpu slot 0 (card 0)")
        self.assertIn("4 cpu slots", self.v["cores"]["head"])
        self.assertIn("at most 2", self.v["cores"]["head"])
        self.assertEqual(self.v["memory"], {"free_mb": 16384, "total_mb": 65536})

    def test_holders_lists_holds_and_agent_work(self):
        hits = m.holders(self.v, NOW + dt.timedelta(minutes=60), NOW + dt.timedelta(minutes=90))
        self.assertTrue(hits[0].startswith("owner whole hold h-whole"))
        self.assertTrue(any(h.startswith("hold h-agent") for h in hits))
        self.assertFalse(any(h.startswith("owner") for h in m.holders(
            self.v, NOW + dt.timedelta(minutes=60), NOW + dt.timedelta(minutes=90), owner=False)))

    def test_warnings(self):
        self.assertEqual(self.v["warnings"], [])
        v = m.build(snap(queue={"entries": None, "error": "queue unreadable"},
                         holds_error="holds unreadable: x",
                         layout=dict(snap()["layout"], warning="machine.json is not used",
                                     hardware_change={"pending": True, "changed": True})), now=NOW)
        self.assertEqual(len(v["warnings"]), 4)
        self.assertEqual(v["queue"], [])

    def wait(self, pending, changed=False):
        lay = dict(snap()["layout"], hardware_change={"pending": pending, "changed": changed})
        return m.build(snap(layout=lay), now=NOW)["warnings"]

    def test_a_failed_look_is_said_as_one_not_as_a_division_wait(self):
        # Written as kitlib.lock.layout.in_force writes it; the snapshot reads changed False.
        got = self.wait("the gpus could not be read (DXGI said no); no layout is adopted from "
                        "that look until one succeeds")
        self.assertEqual(len(got), 1, got)
        self.assertIn("a look at the hardware failed", got[0])
        self.assertNotIn("a new division waits", got[0])

    def test_a_provisional_layout_whose_look_keeps_failing_is_said_as_provisional(self):
        got = self.wait("the cores could not be read (no answer); the layout in use came from "
                        "such a look and is provisional: the first look that succeeds replaces "
                        "it while no run is live or queued")
        self.assertEqual(len(got), 1, got)
        self.assertIn("the layout in force is provisional", got[0])
        self.assertNotIn("a new division waits", got[0])

    def test_a_division_change_is_still_the_division_wait(self):
        got = self.wait("the machine or division changed (a -> b); it is used once no run is "
                        "live or queued (1 now)", changed=True)
        self.assertEqual(got, ["a new division waits until nothing runs or queues"])

    def test_an_unwritten_layout_says_so(self):
        got = self.wait("layout.json is missing, unreadable or not valid to this kit version; "
                        "abc is written once no run is live or queued (1 now)")
        self.assertEqual(len(got), 1, got)
        self.assertIn("layout.json is missing", got[0])

    def test_an_unchecked_layout_shows_its_own_text(self):
        text = "the layout could not be checked (queue.json unreadable)"
        self.assertEqual(self.wait(text), [text])

    def test_a_text_this_kit_does_not_give_shows_its_own_text(self):
        text = "an older kit's words for a pending layout"
        self.assertEqual(self.wait(text), [text])

    def test_the_snapshots_reason_is_read_not_derived_again(self):
        # The snapshot carries hardware_change.reason (kitlib.lock.api); the window says that
        # reason, whatever words the pending text has.
        lay = dict(snap()["layout"], hardware_change={
            "pending": "words of a newer kit", "reason": "failed_look", "changed": False})
        got = m.build(snap(layout=lay), now=NOW)["warnings"]
        self.assertEqual(len(got), 1, got)
        self.assertIn("a look at the hardware failed", got[0])

    def test_a_bare_snapshot_does_not_raise(self):
        v = m.build({}, now=NOW)
        self.assertEqual(v["holds"], [])
        self.assertIsNone(v["memory"])
        self.assertEqual(v["cores"]["cpu"], [])

    def test_game_hold_is_a_schedule_box(self):
        g = {"id": "h-game", "owner": "game", "scope": "domain", "start": at(-5), "end": at(1),
             "covers": {"cpu": [0, 1], "gpu": []}, "game": {"exe": "Game.exe", "name": "Game"},
             "slots_fixed": True}
        v = m.build(snap(holds=[g]), now=NOW)
        self.assertEqual(v["holds"][0]["id"], "h-game")
        self.assertIn("game Game", m.hold_line(v["holds"][0]))


class ToJson(unittest.TestCase):
    """to_json: the view as the hub's GET lock/view sends it. One allow-list for every listener;
    over the tunnel (remote) a warning that can name a file is a fixed phrase."""

    SECRET = "Q:\\made-up\\store"          # a path the snapshot may carry and no answer may

    def full(self, **over):
        s = snap(store=self.SECRET, **over)
        s["runs"][0].update(pid=4242, ctime=99, tree=[4242, 4343], log=self.SECRET + "\\r-A.log",
                            session="made-up-session", note="a run's own note")
        s["hardware"]["problems"] = ["the gpus could not be read at %s" % self.SECRET]
        s["layout"].update(warning="machine.json at %s is not used" % self.SECRET,
                           hardware_change={"pending": "a pending text naming %s" % self.SECRET,
                                            "changed": False},
                           division={"cpu_slot_threads": 2}, default_division={"cpu_slot_threads": 2},
                           owner_division={}, wanted_division={"cpu_slot_threads": 2})
        s["queue"]["error"] = "queue unreadable: %s\\queue.json" % self.SECRET
        s["holds_error"] = "holds unreadable: %s\\holds" % self.SECRET
        s["kinds_error"] = "kinds table %s\\kinds.json names no kinds" % self.SECRET
        s["game"] = {"enabled": True, "games": [{"exe": "Game.exe", "name": "Game", "pin": True,
                                                 "anti_cheat": False}],
                     "running": {"exe": "Game.exe", "name": "Game", "pid": 777}, "hold": None,
                     "slots": [], "would_take": [2, 3], "pinned": "pinned",
                     "anti_cheat_services": []}
        return s

    def json_of(self, remote, **over):
        import json
        s = self.full(**over)
        out = m.to_json(m.build(s, HISTORY, now=NOW), s, remote=remote)
        return out, json.dumps(out)     # it is plain JSON: dumps raises on a datetime

    def hist_items(self, rows):
        s = snap()
        out = m.to_json(m.build(s, rows, now=NOW), s)
        return [it for it in out["strip"] if it["type"] == "hist"]

    def line(self, i, **over):
        d = {"id": "r-%d" % i, "repo": "kit", "slug": "gate", "kind": "engine-test",
             "since": at(-200), "held_since": at(-190), "ended": at(-150), "code": 0}
        d.update(over)
        return d

    def test_the_memo_makes_history_items_once_for_the_same_lines_and_day(self):
        rows, memo = [self.line(1), self.line(2)], {}
        plain = m.build(snap(), rows, now=NOW)
        first = m.build(snap(), rows, now=NOW, memo=memo)
        hist = [it for it in first["strip"] if it["type"] == "hist"]
        self.assertEqual(hist, [it for it in plain["strip"] if it["type"] == "hist"])
        again = m.build(snap(), rows, now=NOW + dt.timedelta(minutes=1), memo=memo)
        self.assertTrue(all(any(x is y for y in again["strip"]) for x in hist))    # not remade
        other = m.build(snap(), list(rows), now=NOW, memo=memo)                  # new lines
        self.assertFalse(any(x is y for x in hist for y in other["strip"]))
        before = memo["items"]
        later = m.build(snap(), memo["rows"], now=NOW + dt.timedelta(days=1), memo=memo)
        self.assertIsNot(memo["items"], before)                                  # a new day
        self.assertEqual([it["text"] for it in later["strip"] if it["type"] == "hist"],
                         [it["text"] for it in m.build(snap(), rows, now=NOW + dt.timedelta(days=1))[
                             "strip"] if it["type"] == "hist"])

    def test_history_items_carry_repo_kind_and_result(self):
        rows = [self.line(1), self.line(2, code=3, how="done", repo="game", kind="measure"),
                self.line(3, how="cancelled", code=1), self.line(4, how="interrupted", code=130)]
        got = {it["id"]: (it["repo"], it["kind"], it["result"]) for it in self.hist_items(rows)}
        self.assertEqual(got, {"r-1": ("kit", "engine-test", "ok"), "r-2": ("game", "measure", "failed"),
                               "r-3": ("kit", "engine-test", "cancelled"),
                               "r-4": ("kit", "engine-test", "interrupted")})

    def test_history_items_carry_the_slug(self):
        it, = self.hist_items([self.line(1)])
        self.assertEqual(it["slug"], "gate")
        it, = self.hist_items([self.line(2, slug=None)])
        self.assertIsNone(it["slug"])

    def test_queue_items_are_structured_and_allow_listed(self):
        s = self.full()
        s["queue"]["entries"][0].update(pid=4242, log=self.SECRET + "\q.log", note="a queued note")
        out = m.to_json(m.build(s, now=NOW), s)
        it, = out["queue_items"]
        self.assertEqual(set(it), {"id", "position", "repo", "slug", "kind", "needs_cpu", "needs_gpu",
                                   "est_start", "since", "backfill", "parked", "why"})
        self.assertEqual((it["id"], it["position"], it["repo"], it["slug"], it["kind"]),
                         ("r-B", 1, "game", "e2e", "measure"))
        self.assertEqual((it["needs_cpu"], it["needs_gpu"], it["backfill"], it["parked"], it["why"]),
                         (2, 1, True, False, "waits for r-A"))
        self.assertEqual(it["est_start"], m.to_local(at(5)).isoformat(timespec="seconds"))
        for remote in (False, True):
            _, text = self.json_of(remote)
            self.assertNotIn("a queued note", text)

    def test_queue_items_of_a_bare_or_unreadable_queue_are_empty(self):
        self.assertEqual(m.to_json(m.build({}, now=NOW), {})["queue_items"], [])
        s = snap(queue={"entries": None, "error": "queue unreadable"})
        self.assertEqual(m.to_json(m.build(s, now=NOW), s)["queue_items"], [])

    def test_history_fields_are_null_when_not_derivable(self):
        it, = self.hist_items([self.line(1, repo=None, kind="", code=None)])
        self.assertEqual((it["repo"], it["kind"], it["result"]), (None, None, None))
        it, = self.hist_items([{k: v for k, v in self.line(2).items() if k not in ("repo", "kind", "code")}])
        self.assertEqual((it["repo"], it["kind"], it["result"]), (None, None, None))

    def test_history_items_keep_their_existing_keys(self):
        it, = self.hist_items([self.line(1)])
        for k in ("type", "id", "open", "late", "text", "start", "end", "expect_end", "lane"):
            self.assertIn(k, it)
        self.assertEqual((it["type"], it["id"], it["open"]), ("hist", "r-1", False))
        self.assertIn("ran kit/gate (engine-test", it["text"])

    def test_other_strip_items_gain_no_history_fields(self):
        s = snap()
        out = m.to_json(m.build(s, HISTORY, now=NOW), s)
        for it in out["strip"]:
            if it["type"] != "hist":
                self.assertFalse({"repo", "slug", "kind", "result"} & set(it))

    def test_the_view_is_plain_json_with_local_times(self):
        out, _ = self.json_of(False)
        self.assertEqual(out["now"], NOW.astimezone().isoformat(timespec="seconds"))
        self.assertEqual(out["revision"], 5)
        self.assertEqual([h["id"] for h in out["holds"]], ["h-whole", "h-half"])
        self.assertEqual(out["holds"][0]["start"], at(60))
        self.assertEqual(out["holds"][0]["note"], "gaming")
        agent = out["agent_holds"][0]
        self.assertEqual((agent["id"], agent["lost"], agent["lost_to"]),
                         ("h-agent", {"gpu": [0]}, ["h-whole"]))
        self.assertEqual([r["id"] for r in out["runs"]], ["r-A"])
        self.assertEqual(out["colour"], {"r-A": m.RUN_COLOURS[0]})
        types = sorted(it["type"] for it in out["strip"])
        self.assertEqual(types, ["booking", "hist", "run", "wait"])
        hist = next(it for it in out["strip"] if it["type"] == "hist")
        self.assertTrue(hist["late"])
        self.assertEqual(hist["expect_end"], (m.to_local(at(-190)) + dt.timedelta(seconds=1200))
                         .isoformat(timespec="seconds"))
        self.assertIn(hist["lane"], range(out["lanes"]))
        self.assertEqual(out["queue"][0], ["", ["move:r-B"]])
        self.assertEqual(out["cores"]["cpu"][0]["holder"], "r-A")
        self.assertEqual(out["memory"], {"free_mb": 16384, "total_mb": 65536})
        self.assertEqual(out["game"]["running"], {"exe": "Game.exe", "name": "Game"})
        # The games list carries pin, so the hub's games editor writes each line back as it was.
        self.assertEqual(out["game"]["games"], [{"name": "Game", "exe": "Game.exe", "anti_cheat": False, "pin": True}])
        self.assertEqual(out["game"]["would_take"], [2, 3])
        self.assertEqual(out["division"]["agent_cap"], 2)
        self.assertEqual(out["division"]["cpu_slots"], 4)

    def test_no_process_detail_and_no_store_folder_on_any_listener(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                out, text = self.json_of(remote)
                for key in ("pid", "ctime", "tree", "log", "session", "store", "pinned",
                            "anti_cheat_services"):
                    self.assertNotIn('"%s"' % key, text)
                self.assertNotIn("a run's own note", text)
                self.assertNotIn("made-up-session", text)
                if remote:
                    self.assertNotIn("made-up", text)

    def test_the_local_owner_reads_the_warnings_as_the_window_does(self):
        s = self.full()
        self.assertEqual(m.to_json(m.build(s, now=NOW), s)["warnings"],
                         m.build(s, now=NOW)["warnings"])

    def test_over_the_tunnel_a_warning_that_can_name_a_file_is_a_fixed_phrase(self):
        out, _ = self.json_of(True)
        self.assertEqual(out["warnings"], [
            m.REMOTE_SAYS["layout"], m.REMOTE_SAYS["pending"], m.REMOTE_SAYS["queue"],
            m.REMOTE_SAYS["holds_error"], m.REMOTE_SAYS["kinds_error"],
            m.REMOTE_SAYS["hardware"] % 1])
        # the kit's own sentences for a layout wait name no file and are kept
        lay = dict(snap()["layout"], hardware_change={"pending": True, "changed": True})
        s = snap(layout=lay)
        self.assertEqual(m.to_json(m.build(s, now=NOW), s, remote=True)["warnings"],
                         ["a new division waits until nothing runs or queues"])

    def test_a_bare_snapshot_does_not_raise(self):
        out = m.to_json(m.build({}, now=NOW), {}, remote=True)
        self.assertEqual((out["holds"], out["runs"], out["warnings"]), ([], [], []))
        self.assertIsNone(out["memory"])
        self.assertIsNone(out["game"]["running"])


class Grid(unittest.TestCase):
    """The schedule's days and half-hour rows, with no tkinter."""
    DAY0 = dt.datetime(2026, 10, 8, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))

    def test_48_half_hour_rows_make_a_day(self):
        self.assertEqual(m.ROWS, 48)
        self.assertEqual(m.cell_time(self.DAY0, 0, m.ROWS), self.DAY0 + dt.timedelta(days=1))
        self.assertEqual(m.cell_time(self.DAY0, 2, 3), self.DAY0 + dt.timedelta(days=2, minutes=90))

    def test_earlier_stops_14_days_back_and_later_at_today(self):
        self.assertEqual(m.BACK_DAYS, 14)
        self.assertEqual(m.clamp_offset(-20), -14)
        self.assertEqual(m.clamp_offset(-14), -14)
        self.assertEqual(m.clamp_offset(-3), -3)
        self.assertEqual(m.clamp_offset(2), 0)
        now = self.DAY0 + dt.timedelta(hours=15, minutes=7)
        self.assertEqual(m.day_start(now, -2), self.DAY0 - dt.timedelta(days=2))

    def test_a_drag_becomes_a_window_that_starts_no_earlier_than_now(self):
        now = self.DAY0 + dt.timedelta(hours=9, minutes=10)
        self.assertEqual(m.drag_window(self.DAY0, (1, 4, 7), now),
                         (self.DAY0 + dt.timedelta(days=1, hours=2), self.DAY0 + dt.timedelta(days=1, hours=4)))
        self.assertEqual(m.drag_window(self.DAY0, (0, 18, 19), now),     # 09:00-10:00, now 09:10
                         (now, self.DAY0 + dt.timedelta(hours=10)))

    def test_segments_split_at_midnight_and_clip_to_the_days_shown(self):
        s = self.DAY0 + dt.timedelta(hours=23)
        self.assertEqual(m.segments(self.DAY0, s, s + dt.timedelta(hours=2)), [(0, 46.0, 48.0), (1, 0.0, 2.0)])
        self.assertEqual(m.segments(self.DAY0, self.DAY0 - dt.timedelta(hours=1),
                                    self.DAY0 + dt.timedelta(minutes=45)), [(0, 0.0, 1.5)])
        self.assertEqual(m.segments(self.DAY0, self.DAY0 + dt.timedelta(days=m.DAYS),
                                    self.DAY0 + dt.timedelta(days=m.DAYS, hours=1)), [])
        self.assertEqual(m.segments(self.DAY0, self.DAY0 - dt.timedelta(hours=3), self.DAY0), [])


class Prompts(unittest.TestCase):
    def test_release_of_a_game_hold_says_game_mode_remakes_it(self):
        h = {"id": "h-1", "owner": "game", "game": {"name": "Game"}, "covers": {"cpu": [0]},
             "_s": NOW, "_e": NOW + dt.timedelta(hours=1)}
        self.assertIn("game mode makes it again while the game runs", m.release_prompt(h))
        own = dict(h, owner="owner", scope="whole")
        self.assertNotIn("game mode", m.release_prompt(own))
        self.assertTrue(m.release_prompt(own).startswith("End owner whole hold h-1"))


if __name__ == "__main__":
    unittest.main()
