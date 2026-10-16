"""kitlib.lock.api: every call in docs/lock-api.md on a scratch store and fake
hardware. Reads: snapshot, wait_change, stats, run_record, hold_record. Writes: set_division,
hold_create, hold_edit, hold_end, set_games, set_game_mode, end_run, each {ok, result|error,
snapshot}. The owner check is the one hook (set_owner_check)."""
import datetime
import json
import os
import sys
import threading
import time
import unittest

from .helpers import (OWNER, REPO, StoreCase, agent, claude_of, clean_env, under_agent_host,
                      unopenable)

if os.name == "nt":
    from kitlib.lock import api
    from kitlib.lock import game
    from kitlib.lock import holds as hl
    from kitlib.lock import layout as lay
    from kitlib.lock import run as rm
    from kitlib.lock import status as st
    from kitlib.lock import store as st_mod
    from kitlib.lock.store import iso, utc_now
    from kitlib.proc import process

# Ids as the lock makes them (store.ID_RE): the record reads take nothing else (they name files).
R_LIVE, R_NONE, R_A, R_B = ("r-20261006T120000Z-%s" % x for x in ("aaaaaa", "000000", "a1a1a1", "b1b1b1"))
H_NONE = "h-20261006T120000Z-0000"


class ApiCase(StoreCase):
    def ok(self, res):
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["snapshot"]["schema_version"], api.SCHEMA_VERSION)
        return res["result"]

    def err(self, res, code, field=None):
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], code, res["error"])
        self.assertIn(res["error"]["code"], api.ERROR_CODES)
        if field is not None:
            self.assertEqual(res["error"]["field"], field)
        self.assertIsNotNone(res["snapshot"])
        return res["error"]

    def snap(self):
        return api.snapshot(self.store)


class Snapshot(ApiCase):
    def test_shape(self):
        s = self.snap()
        for k in ("schema_version", "revision", "at", "store", "hardware", "layout", "slots",
                  "runs", "queue", "holds", "game", "kinds", "kinds_error", "stats"):
            self.assertIn(k, s)
        self.assertEqual(s["schema_version"], 1)
        self.assertEqual(s["hardware"]["threads"], 24)
        self.assertEqual(len(s["hardware"]["ccds"]), 2)
        self.assertEqual(s["hardware"]["gpus"][0]["vendor"], "NVIDIA")
        self.assertEqual(len(s["slots"]["cpu"]), 12)
        self.assertEqual(len(s["slots"]["gpu"]), 2)
        self.assertEqual(s["slots"]["cpu"][0]["threads"], [0, 1])
        self.assertIsNone(s["slots"]["cpu"][0]["holder"])
        self.assertEqual(s["queue"], {"entries": [], "error": None})
        self.assertIn("engine-test", s["kinds"])
        self.assertIsNone(s["kinds_error"])
        self.assertEqual(s["layout"]["source"], "default")
        self.assertFalse(s["layout"]["hardware_change"]["changed"])
        self.assertEqual(s["layout"]["other_labels"], [])
        self.assertEqual(s["holds"], [])
        self.assertFalse(s["game"]["enabled"])
        self.assertEqual(s["stats"]["rows"], [])

    def test_a_live_run_is_listed(self):
        self.live_record(R_LIVE, repo="kit", slug="t")
        s = self.snap()
        self.assertEqual([r["id"] for r in s["runs"]], [R_LIVE])
        self.assertEqual(s["runs"][0]["tree"][0], os.getpid())
        self.assertIsNone(s["runs"][0]["gpu_mem_mb"])
        self.assertTrue(api.run_record(R_LIVE, self.store)["live"])
        self.assertIsNone(api.run_record(R_NONE, self.store))

    def test_a_live_record_with_another_slot_label_is_named_and_warned(self):
        # A record this kit writes carries slot_key of the layout in force: named nowhere. One
        # with another label (here a two-card count on this one-card machine) comes from an
        # older writer: the snapshot names its label once, and status warns in one line.
        other = "t24 g2 cpu 12 x 2 gpu 4 m0123abcd"
        self.live_record(R_A, layout=lay.slot_key(self.layout()))
        self.live_record(R_LIVE)
        s = self.snap()
        self.assertEqual(s["layout"]["other_labels"], [])
        self.assertFalse(any("WARNING" in x for x in st.render(s)))
        self.live_record(R_B, layout=other)
        s = self.snap()
        self.assertEqual(s["layout"]["other_labels"], [other])
        warn = [x for x in st.render(s) if other in x]
        self.assertEqual(len(warn), 1)
        self.assertTrue(warn[0].startswith("WARNING: "))
        self.assertIn("older lock tool", warn[0])
        # A foreign label is another writer's text: one with a line break stays escaped in the
        # one WARNING line instead of starting a line of its own.
        self.live_record(R_B, layout=other + "\nlayout (forged)")
        self.assertEqual([x for x in "\n".join(st.render(self.snap())).split("\n") if "WARNING: a live" in x or "forged" in x],
                         ["WARNING: a live run carries another slot numbering (%r): an older lock tool "
                          "that counts this machine's cpu or gpu slots its own way writes to this store, "
                          "and its slot numbers are not this layout's" % (other + "\nlayout (forged)")])

    def test_an_unreadable_queue_is_reported_not_emptied(self):
        os.makedirs(os.path.dirname(self.store.path("queue.json")), exist_ok=True)
        with open(self.store.path("queue.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        s = self.snap()
        self.assertIsNone(s["queue"]["entries"])
        self.assertIn("queue unreadable", s["queue"]["error"])
        self.assertTrue(any("queue unreadable" in line for line in st.render(s)))
        with open(self.store.path("queue.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_status_text_renders_from_the_snapshot(self):
        lines = st.render(self.snap())
        self.assertIn("of a physical core", lines[0])
        self.assertIn("lock: free (no run holds cores or gpu slots, waits, or runs in the "
                      "background)", lines)
        self.assertIn("holds: none", lines)
        self.ok(api.set_division(OWNER, {"cpu_slot_threads": 4}, self.store))
        lines = st.render(self.snap())
        self.assertIn("6 cpu slots of 4 threads", lines[0])
        self.assertIn("the owner's division", lines[0])

    def test_a_bad_kinds_table_is_reported(self):
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            f.write("{}")
        s = self.snap()
        self.assertEqual(s["kinds"], {})
        self.assertIn("names no kinds", s["kinds_error"])

    def test_a_kind_past_the_agent_cap_is_reported(self):
        # Cpu is 1..agent_cap (6 on the fake layout); 7 is a table the lock cannot size.
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            json.dump({"kinds": {"big": {"cpu": 7, "gpu": 0, "warn_min": 30}}}, f)
        s = self.snap()
        self.assertEqual(s["kinds"], {})
        self.assertIn("more than the agent cap 6", s["kinds_error"])


class PreferredCcd(ApiCase):
    HW = {"threads": 24, "smt": 2, "l3": 2, "sched": {t: 1 for t in range(12, 24)}}

    def test_the_preferred_ccd_is_marked_and_picked(self):
        ccds = self.snap()["hardware"]["ccds"]
        self.assertEqual([c["preferred"] for c in ccds], [False, True])
        self.assertEqual([c["game_pick"] for c in ccds], [False, True])
        self.assertEqual(ccds[1]["cpu_slots"], list(range(6, 12)))


class WaitChange(ApiCase):
    def test_times_out_at_the_same_revision(self):
        rev = self.store.revision()
        t0 = time.monotonic()
        s = api.wait_change(rev, 0.3, self.store)
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        self.assertEqual(s["revision"], rev)

    def test_wakes_on_a_write(self):
        rev = self.store.revision()
        timer = threading.Timer(0.2, self.store.bump)
        timer.start()
        self.addCleanup(timer.cancel)
        t0 = time.monotonic()
        s = api.wait_change(rev, 10, self.store)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertNotEqual(s["revision"], rev)


class OwnerHook(ApiCase):
    def test_default_check(self):
        self.assertTrue(api.default_owner_check(OWNER))
        self.assertFalse(api.default_owner_check(dict(OWNER, host=[4, 1])))
        self.assertFalse(api.default_owner_check(agent("s")))
        self.assertFalse(api.default_owner_check(None))

    def test_the_hook_swaps_and_restores(self):
        self.addCleanup(api.set_owner_check, None)
        old = api.set_owner_check(lambda a: a.get("name") == "boss")
        self.assertIs(old, api.default_owner_check)
        self.assertTrue(api.is_owner({"name": "boss", "role": "agent"}))
        self.assertFalse(api.is_owner(OWNER))
        self.ok(api.set_game_mode({"name": "boss"}, True, self.store))
        self.err(api.set_game_mode(OWNER, False, self.store), "forbidden")
        api.set_owner_check(None)
        self.assertTrue(api.is_owner(OWNER))

    def test_a_check_that_raises_fails_closed(self):
        self.addCleanup(api.set_owner_check, None)
        api.set_owner_check(lambda a: 1 / 0)
        self.assertFalse(api.is_owner(OWNER))

    def test_authorize(self):
        self.assertIsNone(api.authorize(OWNER, "anything"))
        self.assertIn("unknown action", api.authorize(agent("s"), "frobnicate"))
        self.assertIn("owner's", api.authorize(agent("s"), "set_division"))
        self.assertEqual(api.authorize(None, "set_games"), "no actor")


class Division(ApiCase):
    def test_agent_is_forbidden(self):
        self.err(api.set_division(agent("s"), {"cpu_slots": 6}, self.store), "forbidden")

    def test_bad_division_is_invalid(self):
        self.err(api.set_division(OWNER, {"bogus": 1}, self.store), "invalid", "division")
        self.err(api.set_division(OWNER, "six", self.store), "invalid", "division")
        self.err(api.set_division(OWNER, {"cpu_slots": 99}, self.store), "invalid", "division")

    def test_applies_at_once_with_nothing_live_and_none_restores(self):
        r = self.ok(api.set_division(OWNER, {"cpu_slots": 6}, self.store))
        self.assertEqual(r, {"applied": True, "pending": None})
        self.assertEqual(self.snap()["layout"]["cpu_slots"], 6)
        self.assertEqual(self.snap()["layout"]["source"], "config")
        r = self.ok(api.set_division(OWNER, None, self.store))
        self.assertTrue(r["applied"])
        self.assertIsNone(self.store.read("machine.json"))
        self.assertEqual(self.snap()["layout"]["cpu_slots"], 12)

    def test_notes_in_machine_json_are_kept(self):
        self.store.write("machine.json", {"_comment": "by hand", "_seen": 1, "cpu_slots": 6})
        self.ok(api.set_division(OWNER, {"cpu_slots": 4, "_seen": 2}, self.store))
        self.assertEqual(self.store.read("machine.json"), {"_comment": "by hand", "_seen": 2, "cpu_slots": 4})
        self.assertEqual(self.snap()["layout"]["owner_division"], {"cpu_slots": 4})
        self.ok(api.set_division(OWNER, None, self.store))      # every default; the notes stay
        self.assertEqual(self.store.read("machine.json"), {"_comment": "by hand", "_seen": 2})
        L = self.snap()["layout"]
        self.assertEqual((L["source"], L["cpu_slots"], L["owner_division"]), ("default", 12, {}))

    def test_waits_while_a_run_is_live(self):
        self.layout()
        self.live_record("r-live")
        res = api.set_division(OWNER, {"cpu_slots": 6}, self.store)
        r = self.ok(res)
        self.assertFalse(r["applied"])
        self.assertTrue(r["pending"])
        lay_ = res["snapshot"]["layout"]
        self.assertEqual(lay_["cpu_slots"], 12)
        self.assertEqual(lay_["wanted_division"]["cpu_slots"], 6)
        self.assertTrue(lay_["hardware_change"]["changed"])
        self.assertTrue(lay_["hardware_change"]["pending"])
        self.assertTrue(any("hardware or division changed" in x for x in st.render(res["snapshot"])))


class Holds(ApiCase):
    def owner_hold(self, **spec):
        return api.hold_create(OWNER, dict({"owner": "owner", "scope": "half", "for_s": 3600},
                                           **spec), self.store)

    def agent_hold(self, session="sess-A", kind="engine-test", **spec):
        return api.hold_create(agent(session), dict(
            {"owner": "agent", "repo": "kit", "slug": "t", "kind": kind, "for_s": 600}, **spec),
            self.store)

    def test_owner_half_hold(self):
        res = self.owner_hold(note="build")
        h = self.ok(res)["hold"]
        self.assertEqual(len(h["covers"]["cpu"]), 6)
        self.assertTrue(h["live"])
        self.assertEqual([x["id"] for x in res["snapshot"]["holds"]], [h["id"]])
        self.assertEqual(sum(1 for d in res["snapshot"]["slots"]["cpu"] if d["hold"] == h["id"]), 6)
        self.assertTrue(api.hold_record(h["id"], self.store)["live_record"])
        self.assertIsNone(api.hold_record(H_NONE, self.store))

    def test_a_start_with_any_offset(self):
        start = (utc_now() + datetime.timedelta(hours=2)).astimezone(
            datetime.timezone(datetime.timedelta(hours=2)))
        h = self.ok(self.owner_hold(start=start.isoformat(timespec="seconds")))["hold"]
        self.assertFalse(h["live"])
        self.assertEqual(h["start"], iso(start.astimezone(datetime.timezone.utc)))
        self.err(self.owner_hold(start="2026-10-06T14:00:00"), "invalid", "start")

    def test_spec_errors(self):
        self.err(api.hold_create(OWNER, {"owner": "owner", "scope": "half"}, self.store),
                 "invalid", "end")
        self.err(self.owner_hold(for_s="soon"), "invalid", "for_s")
        self.err(api.hold_create(OWNER, {"owner": "game", "scope": "domain", "for_s": 60},
                                 self.store), "invalid", "owner")
        self.err(api.hold_create(OWNER, ["half"], self.store), "invalid", "spec")
        self.err(self.owner_hold(scope="everything"), "invalid")

    def test_agents_make_only_agent_holds(self):
        for scope in ("half", "whole"):
            self.err(api.hold_create(agent("s"), {"owner": "owner", "scope": scope, "for_s": 60},
                                     self.store), "forbidden")

    def test_a_whole_hold_names_no_job_but_the_owners(self):
        # Red before this rule: a whole hold took slug and open.
        self.err(self.owner_hold(scope="whole", slug="t"), "invalid")
        self.err(self.owner_hold(scope="whole", open=True), "invalid")
        h = self.ok(self.owner_hold(scope="whole"))["hold"]
        self.assertEqual(h["job"], "OWNER")
        self.err(api.hold_edit(OWNER, h["id"], {"slug": "t"}, self.store), "invalid")

    def test_an_unreadable_hold_file_is_reported_and_blocks_new_holds(self):
        self.assertIsNone(self.snap()["holds_error"])
        os.makedirs(self.store.path("holds"), exist_ok=True)
        with open(self.store.path("holds", "h-20261006T120000Z-abcd.json"), "w") as f:
            f.write("{")
        s = self.snap()
        self.assertEqual(s["holds"], [])
        self.assertIn("h-20261006T120000Z-abcd.json", s["holds_error"])
        self.assertFalse(self.owner_hold()["ok"])
        e = self.err(api.hold_record("h-20261006T120000Z-abcd", self.store), "store")
        self.assertIn("h-20261006T120000Z-abcd.json", e["message"])
        self.err(api.hold_record(H_NONE, self.store), "store")    # never read as no hold

    def test_agent_hold_belongs_to_its_session(self):
        h = self.ok(self.agent_hold())["hold"]
        self.assertEqual(h["state"], "open")
        self.assertEqual(h["covers"]["cpu"], [0, 1])
        self.err(api.hold_end(agent("sess-B"), h["id"], self.store), "forbidden")
        self.err(api.hold_edit(agent("sess-B"), h["id"], {"note": "x"}, self.store), "forbidden")
        got = self.ok(api.hold_edit(agent("sess-A"), h["id"], {"note": "mine"}, self.store))
        self.assertEqual(got["hold"]["note"], "mine")
        self.ok(api.hold_end(agent("sess-A"), h["id"], self.store))
        e = self.err(api.hold_end(agent("sess-A"), h["id"], self.store), "not_found", "id")
        self.assertIn("it ended: released", e["message"])

    def test_owner_holds_are_the_owners(self):
        h = self.ok(self.owner_hold())["hold"]
        self.err(api.hold_end(agent("s"), h["id"], self.store), "forbidden")
        self.err(api.hold_edit(agent("s"), h["id"], {"note": "x"}, self.store), "forbidden")
        self.ok(api.hold_end(OWNER, h["id"], self.store))
        self.assertEqual(self.store.read("ended/%s.json" % h["id"])["how"], "ended by owner")

    def test_edit_errors(self):
        h = self.ok(self.owner_hold())["hold"]
        later = iso(utc_now() + datetime.timedelta(hours=1))
        self.err(api.hold_edit(OWNER, h["id"], {"start": later}, self.store), "invalid")
        self.err(api.hold_edit(OWNER, h["id"], {}, self.store), "invalid", "changes")
        self.err(api.hold_edit(OWNER, h["id"], {"end": "tomorrow"}, self.store), "invalid", "end")
        self.err(api.hold_edit(OWNER, "h-20261006T120000Z-abcd", {"note": "x"}, self.store),
                 "not_found", "id")
        self.err(api.hold_edit(OWNER, 7, {"note": "x"}, self.store), "invalid", "id")

    def test_an_agent_hold_under_a_strong_hold_is_a_conflict(self):
        self.ok(self.owner_hold(scope="whole"))
        self.err(self.agent_hold(), "conflict")

    def test_agent_holds_leaving_too_few_is_busy(self):
        self.ok(self.agent_hold(kind="cpu-eval"))
        self.ok(self.agent_hold(kind="cpu-eval"))
        self.err(self.agent_hold(), "busy")

    def test_a_kind_that_is_not_text_is_refused_not_raised(self):
        for odd in (["x"], {"x": 1}):
            res = self.agent_hold(kind=odd)
            self.assertFalse(res["ok"], odd)
            self.err(res, "invalid")

    def test_an_aliased_kind_makes_a_hold_of_the_canonical_kind_a_run_can_claim(self):
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            json.dump({"kinds": {"graph-build": {"cpu": 2, "gpu": 0, "warn_min": 30}},
                       "aliases": {"worldgraph": {"to": "graph-build", "why": "retired name"}}}, f)
        h = self.ok(self.agent_hold(kind="worldgraph"))["hold"]
        self.assertEqual(h["kind"], "graph-build")
        with rm.slot("worldgraph", "kit", "t", cores=1, hold=h["id"], store=self.store,
                     wait_s=30, env=clean_env()) as launch:
            rec = self.store.read("active/%s.json" % launch.id)
            self.assertEqual(rec["kind"], "graph-build")
        self.assertEqual(self.store.read("ended/%s.json" % h["id"])["how"], "claimed")

    def test_a_bad_kinds_table_is_a_store_error(self):
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            f.write("[]")
        self.err(self.agent_hold(), "store")

    def test_a_kind_past_the_agent_cap_is_a_store_error(self):
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            json.dump({"kinds": {"engine-test": {"cpu": 7, "gpu": 0, "warn_min": 30}}}, f)
        self.err(self.agent_hold(), "store")


class Games(ApiCase):
    def game_hold(self):
        return self.snap()["game"]

    def test_set_games_defaults_pin_by_anti_cheat(self):
        r = self.ok(api.set_games(OWNER, [{"exe": "VALORANT.exe"}, {"exe": "Other.exe"},
                                          {"exe": "Pinned.exe", "pin": False}], self.store))
        self.assertEqual([g["pin"] for g in r["games"]], [False, True, False])
        g = self.game_hold()
        self.assertEqual(g["games"][0]["anti_cheat"], "Vanguard")
        self.assertIsNone(g["games"][1]["anti_cheat"])

    def test_set_games_errors(self):
        self.err(api.set_games(agent("s"), [], self.store), "forbidden")
        self.err(api.set_games(OWNER, [{"exe": "notes.txt"}], self.store), "invalid", "games")
        self.err(api.set_games(OWNER, [{"exe": "a.exe"}, {"exe": "A.exe"}], self.store),
                 "invalid", "games")
        self.err(api.set_game_mode(OWNER, "on", self.store), "invalid", "enabled")
        self.err(api.set_game_mode(agent("s"), True, self.store), "forbidden")

    def test_a_running_game_books_the_domain_and_pin_off_never_pins(self):
        self.ok(api.set_games(OWNER, [{"exe": "VALORANT.exe"}], self.store))
        self.procs = [(4242, 1, "Valorant.exe")]
        res = api.set_game_mode(OWNER, True, self.store)
        self.assertEqual(self.ok(res), {"enabled": True})
        g = res["snapshot"]["game"]
        self.assertIsNotNone(g["hold"])
        self.assertEqual(g["slots"], list(range(6)))
        self.assertEqual(g["slots"], game.domain_slots(self.layout(), self.hw))
        self.assertEqual(g["running"]["pid"], 4242)
        h = next(x for x in res["snapshot"]["holds"] if x["id"] == g["hold"])
        self.assertEqual(h["owner"], "game")
        self.assertEqual(h["covers"]["gpu"], [])
        self.assertEqual(self.pins, [])
        self.assertIsNone(g["pinned"])

    def test_pin_on_pins_once_onto_the_domain(self):
        self.procs = [(4343, 1, "Other.exe")]
        self.ok(api.set_games(OWNER, [{"exe": "Other.exe"}], self.store))
        self.ok(api.set_game_mode(OWNER, True, self.store))
        L = self.layout()
        game.sync(self.store, L, force=True)
        self.assertEqual(self.pins, [(4343, lay.cores_mask(L, list(range(6))))])
        self.assertEqual(self.game_hold()["pinned"], "pinned")

    def test_switching_games_moves_the_hold(self):
        self.ok(api.set_games(OWNER, [{"exe": "VALORANT.exe"}, {"exe": "Other.exe"}], self.store))
        self.procs = [(4242, 1, "Valorant.exe")]
        self.ok(api.set_game_mode(OWNER, True, self.store))
        first = self.game_hold()["hold"]
        self.procs = [(4343, 1, "Other.exe")]
        game.sync(self.store, self.layout(), force=True)
        g = self.game_hold()
        self.assertNotEqual(g["hold"], first)
        self.assertEqual(g["running"]["exe"], "Other.exe")
        self.assertEqual(self.store.read("ended/%s.json" % first)["how"], "game exited")
        self.assertEqual(len(self.pins), 1)

    def test_the_hold_ends_when_the_game_exits_or_mode_goes_off(self):
        self.ok(api.set_games(OWNER, [{"exe": "Other.exe"}], self.store))
        self.procs = [(4343, 1, "Other.exe")]
        self.ok(api.set_game_mode(OWNER, True, self.store))
        first = self.game_hold()["hold"]
        self.procs = []
        game.sync(self.store, self.layout(), force=True)
        self.assertIsNone(self.game_hold()["hold"])
        self.assertEqual(self.store.read("ended/%s.json" % first)["how"], "game exited")
        self.procs = [(4343, 1, "Other.exe")]
        game.sync(self.store, self.layout(), force=True)
        second = self.game_hold()["hold"]
        self.assertIsNotNone(second)
        self.ok(api.set_game_mode(OWNER, False, self.store))
        self.assertIsNone(self.game_hold()["hold"])
        self.assertEqual(self.store.read("ended/%s.json" % second)["how"], "game mode off")

    def pinned_game(self, games=({"exe": "Other.exe"},)):
        self.ok(api.set_games(OWNER, list(games), self.store))
        self.procs = [(4343, 1, games[0]["exe"])]
        self.ok(api.set_game_mode(OWNER, True, self.store))
        game.sync(self.store, self.layout(), force=True)
        return self.game_hold()["hold"]

    def test_a_pin_never_writes_back_a_hold_that_ended(self):
        # The old _pin wrote the hold it was handed, so a pin racing the hold's end brought the
        # ended hold back (red there: the file was written again).
        hid = self.pinned_game()
        L = self.layout()
        h = next(x for x in hl.records(self.store, L) if x["id"] == hid)
        self.ok(api.hold_end(OWNER, hid, self.store))
        self.assertIsNone(game._pin(self.store, L, h, 4444))
        self.assertFalse(os.path.exists(self.store.path("holds", hid + ".json")))
        self.assertEqual(self.pins, [(4343, lay.cores_mask(L, list(range(6))))])

    def test_a_pin_records_the_pid_it_pinned(self):
        hid = self.pinned_game()
        g = self.store.read("holds/%s.json" % hid)["game"]
        self.assertEqual((g["pinned"], g["pinned_pid"]), ("pinned", 4343))

    def test_mode_off_gives_a_pinned_game_its_affinity_back(self):
        hid = self.pinned_game()
        self.ok(api.set_game_mode(OWNER, False, self.store))
        self.assertEqual(self.unpins, [4343])
        ended = self.store.read("ended/%s.json" % hid)
        self.assertEqual((ended["how"], ended["unpinned"]), ("game mode off", "restored"))

    def test_a_game_that_exited_is_not_opened_again(self):
        hid = self.pinned_game()
        self.procs = []
        game.sync(self.store, self.layout(), force=True)
        self.assertEqual(self.unpins, [])
        self.assertNotIn("unpinned", self.store.read("ended/%s.json" % hid))

    def test_pin_off_is_never_unpinned(self):
        # Pin-on only. A game whose pin is off, in the hold or in the list by the time the
        # hold ends, is never opened to put its affinity back.
        hid = self.pinned_game([{"exe": "VALORANT.exe"}])
        self.assertEqual(self.pins, [])
        self.ok(api.set_game_mode(OWNER, False, self.store))
        self.assertEqual(self.unpins, [])
        self.assertNotIn("unpinned", self.store.read("ended/%s.json" % hid))
        hid = self.pinned_game()
        self.ok(api.set_games(OWNER, [{"exe": "Other.exe", "pin": False}], self.store))
        self.ok(api.set_game_mode(OWNER, False, self.store))
        self.assertEqual(self.unpins, [])
        self.assertNotIn("unpinned", self.store.read("ended/%s.json" % hid))

    def expire(self, hid):
        """Moves hold hid's start and end into the past, as when no sync rolled it."""
        d = self.store.read("holds/%s.json" % hid)
        now = utc_now()
        ago = lambda h: iso(now - datetime.timedelta(hours=h))
        d["start"], d["end"] = ago(2), ago(1)
        self.store.write("holds/%s.json" % hid, d)

    def test_an_expired_hold_is_unpinned_when_mode_goes_off(self):
        # Review of 945d2bf: records() drops an expired game hold, so mode off found no hold and
        # the game stayed pinned (red there: no unpin, and the hold was never ended by sync).
        hid = self.pinned_game()
        self.expire(hid)
        self.ok(api.set_game_mode(OWNER, False, self.store))
        self.assertEqual(self.unpins, [4343])
        ended = self.store.read("ended/%s.json" % hid)
        self.assertEqual((ended["how"], ended["unpinned"]), ("expired", "restored"))

    def test_a_pruned_expired_hold_is_unpinned_while_its_game_runs(self):
        # The prune path (records(prune=True), as create, edit and end use) ended it with no
        # unpin (red there). A game that exited is not opened.
        hid = self.pinned_game()
        self.expire(hid)
        with self.store.lock("holds"):
            hl.records(self.store, self.layout(), prune=True)
        self.assertEqual(self.unpins, [4343])
        self.assertEqual(self.store.read("ended/%s.json" % hid)["unpinned"], "restored")
        self.unpins.clear()
        hid = self.pinned_game()
        self.expire(hid)
        self.procs = []
        with self.store.lock("holds"):
            hl.records(self.store, self.layout(), prune=True)
        self.assertEqual(self.unpins, [])
        ended = self.store.read("ended/%s.json" % hid)
        self.assertEqual(ended["how"], "expired")
        self.assertNotIn("unpinned", ended)

    def test_a_repin_after_expiry_keeps_the_first_affinity(self):
        # The old re-pin read `before` from the process it had pinned, so a later restore put the
        # pinned mask back (red there: before was the domain mask).
        hid = self.pinned_game()
        L = self.layout()
        dom = lay.cores_mask(L, list(range(6)))
        self.assertEqual(self.store.read("holds/%s.json" % hid)["game"]["before"], self.FIRST_MASK)
        self.expire(hid)
        game.sync(self.store, L, force=True)
        new = self.game_hold()["hold"]
        self.assertNotEqual(new, hid)
        g = self.store.read("holds/%s.json" % new)["game"]
        self.assertEqual((g["pinned_pid"], g["before"]), (4343, self.FIRST_MASK))
        self.assertNotIn("carry", g)
        self.assertEqual(self.masks[4343], dom)
        self.assertEqual(self.unpins, [])
        self.assertNotIn("unpinned", self.store.read("ended/%s.json" % hid))

    def test_mode_off_books_nothing(self):
        self.ok(api.set_games(OWNER, [{"exe": "Other.exe"}], self.store))
        self.procs = [(4343, 1, "Other.exe")]
        game.sync(self.store, self.layout(), force=True)
        self.assertIsNone(self.game_hold()["hold"])
        self.assertEqual(self.pins, [])


class EndRun(ApiCase):
    def stop_file(self, rid):
        return self.store.read("active/%s.stop" % rid)

    def test_no_such_run(self):
        self.err(api.end_run(OWNER, R_NONE, self.store), "not_found", "id")
        self.err(api.end_run(OWNER, 5, self.store), "invalid", "id")

    def test_another_sessions_run_is_the_owners_to_end_force_or_not(self):
        # The old check let any agent end any run by saying force (stop --force): red.
        self.live_record(R_A, session="sess-A", ancestors=[claude_of("sess-A")])
        for who in (agent("sess-B"), dict(agent("sess-B"), force=True),
                    dict(agent("sess-B"), session="sess-A")):
            with self.subTest(who=who):
                self.err(api.end_run(who, R_A, self.store), "forbidden")
        self.assertIsNone(self.stop_file(R_A))
        r = self.ok(api.end_run(OWNER, R_A, self.store))
        self.assertEqual(r, {"id": R_A, "requested": True})
        self.assertEqual(self.stop_file(R_A)["by"], "owner")

    def test_own_session_and_owner(self):
        self.live_record(R_B, session="sess-A", ancestors=[claude_of("sess-A")])
        self.ok(api.end_run(agent("sess-A"), R_B, self.store))
        self.ok(api.end_run(OWNER, R_B, self.store))
        self.assertEqual(self.stop_file(R_B)["by"], "owner")

    def test_a_live_record_that_stays_unreadable_is_a_store_error_never_no_run(self):
        self.live_record(R_A)
        with unopenable(self.store.path("active", R_A + ".json")):
            e = self.err(api.end_run(OWNER, R_A, self.store), "store", "id")
            r = api.run_record(R_A, self.store)
        self.assertIn("could not be read", e["message"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "store")
        self.assertTrue(api.run_record(R_A, self.store)["live"])


R_H, R_C = ("r-20261006T120000Z-%s" % x for x in ("c1c1c1", "d1d1d1"))


class MoveToTop(ApiCase):
    """move_to_top: the owner moves a waiting run to the front of the queue; runs holding slots
    keep them. An agent is refused; a run that has started, ended or never was is an error."""

    def entry(self, rid, ticket, state="waiting", cores=(), **kw):
        """A queue entry this test process keeps live (its pid, creation time and run mutex)."""
        m = self.store.run_mutex(rid)
        self.addCleanup(m.release)
        e = {"id": rid, "ticket": ticket, "state": state, "pid": os.getpid(),
             "ctime": process.own_ctime(), "repo": "kit", "slug": "s%d" % ticket,
             "kind": "engine-test", "need": {"cpu": len(cores) or 2, "gpu": 0}, "expect_s": 600,
             "since": iso(), "held_at": time.time() if state == "held" else None,
             "cores": list(cores), "gpus": [], "hold": None, "avoid": {}, "prefer": {},
             "parked": False, "blocked_by": None, "clear_at": None, "avoid_after": {},
             "parent": None}
        e.update(kw)
        return e

    def setUp(self):
        ApiCase.setUp(self)
        n = self.layout()["cpu_slots"]
        self.store.write("queue.json", {"ticket": 3, "entries": [
            self.entry(R_H, 1, "held", cores=range(n)),
            self.entry(R_A, 2), self.entry(R_B, 3)]})
        self.live_record(R_B, state="waiting", held_since=None)

    def queue(self):
        return self.store.read("queue.json")

    def order(self):
        return [q["id"] for q in self.snap()["queue"]["entries"]]

    def test_the_owner_moves_a_waiting_run_to_the_front(self):
        self.assertEqual(self.order(), [R_A, R_B])
        held = self.queue()["entries"][0]
        r = self.ok(api.move_to_top(OWNER, R_B, self.store))
        self.assertEqual((r["id"], r["position"]), (R_B, 1))
        self.assertEqual((r["moved"]["by"], r["moved"]["name"]), ("owner", "test-owner"))
        s = self.snap()
        self.assertEqual([q["id"] for q in s["queue"]["entries"]], [R_B, R_A])
        self.assertEqual(s["queue"]["entries"][0]["moved"]["by"], "owner")
        self.assertIsNone(s["queue"]["entries"][1]["moved"])
        row = next(x for x in s["runs"] if x["id"] == R_B)
        self.assertEqual(row["moved"]["by"], "owner")
        self.assertEqual(row["position"], 1)
        self.assertIn("moved to the top by the owner", "\n".join(st.render(s)))
        self.assertEqual(self.queue()["entries"][0], held)      # the holder keeps its slots

    def test_the_moved_run_plans_to_start_now_once_the_slots_are_free(self):
        self.ok(api.move_to_top(OWNER, R_B, self.store))
        q = self.queue()
        q["entries"] = q["entries"][1:]                          # the holder ends
        self.store.write("queue.json", q)
        s = self.snap()
        first = s["queue"]["entries"][0]
        self.assertEqual((first["id"], first["position"]), (R_B, 1))
        late = st_mod.parse_iso(first["est_start"]) - st_mod.parse_iso(s["at"])
        self.assertLess(late.total_seconds(), 60)               # now, not after a 600 s holder

    def test_an_agent_is_refused_and_nothing_changes(self):
        before = self.queue()
        for who in (agent("sess-A"), dict(agent("sess-A"), role="owner")):
            with self.subTest(who=who):
                e = self.err(api.move_to_top(who, R_B, self.store), "forbidden")
                self.assertIn("move_to_top is the owner's", e["message"])
        self.assertEqual(self.queue(), before)

    def test_a_run_that_has_started_is_an_error(self):
        e = self.err(api.move_to_top(OWNER, R_H, self.store), "conflict", "id")
        self.assertIn("has started", e["message"])
        self.assertNotIn("moved", self.queue()["entries"][0])

    def test_a_run_that_is_gone_is_an_error(self):
        self.err(api.move_to_top(OWNER, R_NONE, self.store), "not_found", "id")
        self.store.write("ended/%s.json" % R_C, {"id": R_C, "how": "done"})
        e = self.err(api.move_to_top(OWNER, R_C, self.store), "not_found", "id")
        self.assertIn("ended (done)", e["message"])

    def test_a_waiting_entry_whose_caller_died_is_gone(self):
        q = self.queue()
        q["entries"].append(dict(q["entries"][2], id=R_C, ticket=4, pid=0))
        self.store.write("queue.json", q)
        self.err(api.move_to_top(OWNER, R_C, self.store), "not_found", "id")

    def test_a_bad_id_is_invalid(self):
        for bad in ("../queue", 5, None, "h-20261006T120000Z-abcd"):
            with self.subTest(bad=bad):
                self.err(api.move_to_top(OWNER, bad, self.store), "invalid", "id")

    def test_the_latest_move_goes_first(self):
        self.ok(api.move_to_top(OWNER, R_B, self.store))
        r = self.ok(api.move_to_top(OWNER, R_A, self.store))
        self.assertEqual(r["position"], 1)
        self.assertEqual(self.order(), [R_A, R_B])


class RecordIds(ApiCase):
    """An id names a file (active/<id>.json, ended/<id>.json), so the record reads and end_run take
    only an id the lock makes (store.ID_RE) with the right prefix (red before it: run_record and
    end_run joined any string into a path)."""

    def plant(self):
        """A run-shaped record outside active/ and ended/ that this test process keeps live."""
        rec = self.live_record(R_A)
        self.store.write("secret.json", rec)
        return rec

    def test_a_path_is_not_a_run_id(self):
        self.plant()
        for bad in ("../secret", "..\\secret", "r-../../secret", R_A + "\n", R_A + "/..", "", 5):
            with self.subTest(bad=bad):
                self.err(api.run_record(bad, self.store), "invalid", "id")
                self.err(api.end_run(OWNER, bad, self.store), "invalid", "id")
        self.assertFalse(os.path.exists(self.store.path("secret.stop")))
        self.assertTrue(api.run_record(R_A, self.store)["live"])

    def test_each_read_takes_its_own_prefix(self):
        h = self.ok(api.hold_create(OWNER, {"owner": "owner", "scope": "half", "for_s": 3600},
                                    self.store))["hold"]
        run_like = "r" + h["id"][1:]
        self.store.write("ended/%s.json" % run_like, {"id": run_like})
        self.err(api.hold_record(run_like, self.store), "invalid", "id")
        self.err(api.run_record(h["id"], self.store), "invalid", "id")
        self.err(api.end_run(OWNER, h["id"], self.store), "invalid", "id")
        for bad in ("h-../../secret", h["id"] + "\n", 5, None):
            with self.subTest(bad=bad):
                self.err(api.hold_record(bad, self.store), "invalid", "id")
                self.err(api.hold_end(OWNER, bad, self.store), "invalid", "id")
        self.assertTrue(api.hold_record(h["id"], self.store)["live_record"])


# Run on the machine's store (Store()) under a process named claude.exe: nothing is written; it
# prints who() of a forged actor and what that actor may do.
FORGED = "\n".join([
    "import json, sys",
    "sys.path.insert(0, sys.argv[1])",
    "from kitlib.lock import api",
    "from kitlib.lock.store import Store",
    "s = Store()",
    "forged = {'role': 'owner', 'host': None, 'ancestors': [], 'session': 'sess-A',",
    "          'force': True, 'name': 'forger'}",
    "me = api.who(forged, s)",
    "run = {'id': 'r-x', 'session': 'sess-A', 'ancestors': [[1, 1]]}",
    "print(json.dumps({'testing': s.testing, 'who': me, 'owner': api.is_owner(me),",
    "                  'division': api.authorize(me, 'set_division'),",
    "                  'end_run': api.authorize(me, 'end_run', run)}))"])


class Caller(StoreCase):
    """Who is asking comes from the process tree, never from the caller."""

    def test_the_nearest_agent_host_is_found_by_exe_name(self):
        chain = [(1, 11), (2, 22), (3, 33)]
        self.assertEqual(st_mod.chain_host(chain, {1: "python.exe", 2: "Claude.exe",
                                                   3: "claude.exe"}), [2, 22])
        self.assertEqual(st_mod.chain_host(chain, {1: "node.exe"}), [1, 11])
        self.assertIsNone(st_mod.chain_host(chain, {1: "pwsh.exe", 2: "explorer.exe"}))

    def test_an_agent_host_above_makes_an_agent_whatever_the_environment(self):
        code, out, err = under_agent_host(self.tmp, [sys.executable, "-c", (
            "import json, sys; sys.path.insert(0, sys.argv[1]); "
            "from kitlib.lock.store import caller; print(json.dumps(caller()))"), REPO])
        self.assertEqual(code, 0, err)
        me = json.loads(out)
        self.assertEqual(me["role"], "agent")
        self.assertIsNotNone(me["host"])
        self.assertNotIn("claude", me)             # the legacy key is read, never written
        self.assertIsNone(me["session"])           # the environment was stripped

    def test_a_forged_role_force_or_session_counts_for_nothing_on_the_machines_store(self):
        script = os.path.join(self.tmp, "forged.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(FORGED)
        code, out, err = under_agent_host(self.tmp, [sys.executable, script, REPO])
        self.assertEqual(code, 0, err)
        got = json.loads(out)
        self.assertFalse(got["testing"])
        self.assertEqual(got["who"]["role"], "agent")
        self.assertEqual(got["who"]["name"], "forger")
        self.assertNotIn("force", got["who"])
        self.assertFalse(got["owner"])
        self.assertIn("owner's", got["division"])
        self.assertIn("another session", got["end_run"])

    def test_a_copied_session_id_owns_nothing(self):
        rec = {"session": "sess-A", "ancestors": [claude_of("sess-A"), [5, 5]]}
        self.assertTrue(st_mod.owns(agent("sess-A"), rec))
        self.assertFalse(st_mod.owns(dict(agent("sess-B"), session="sess-A"), rec))
        self.assertFalse(st_mod.owns({"session": "sess-A"}, rec))
        self.assertFalse(st_mod.owns(None, rec))


class Stats(ApiCase):
    def ended(self, rid, how, code):
        t = utc_now()
        self.store.history_append({
            "id": rid, "repo": "kit", "slug": "t", "kind": "engine-test", "how": how,
            "code": code, "since": iso(t - datetime.timedelta(seconds=70)),
            "held_since": iso(t - datetime.timedelta(seconds=60)), "ended": iso(t), "cpu_s": 5})

    def test_rows_by_kind_and_repo(self):
        self.ended("r-1", "done", 0)
        self.ended("r-2", "done", 1)
        self.ended("r-3", "stopped", None)
        rows = api.stats(self.store)["rows"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["kind"], r["repo"], r["runs"], r["done"], r["failed"],
                          r["interrupted"]), ("engine-test", "kit", 3, 1, 1, 1))
        self.assertEqual(r["held_s"]["median"], 60)
        self.assertEqual(r["wait_s"]["median"], 10)
        self.assertEqual(r["cpu_s"], 15)
        self.assertEqual(self.snap()["stats"]["rows"][0]["runs"], 3)

    def test_range(self):
        self.ended("r-1", "done", 0)
        later = iso(utc_now() + datetime.timedelta(hours=1))
        self.assertEqual(api.stats(self.store, since=later)["rows"], [])
        self.assertEqual(len(api.stats(self.store, since="2020-01-01T00:00:00+02:00")["rows"]), 1)
        with self.assertRaises(api.ApiError) as cm:
            api.stats(self.store, since="last week")
        self.assertEqual((cm.exception.code, cm.exception.field), ("invalid", "since"))


if __name__ == "__main__":
    unittest.main()
