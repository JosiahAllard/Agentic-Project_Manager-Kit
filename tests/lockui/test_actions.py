"""kitlib.lockui.actions: what each schedule-window action asks of kitlib.lock.api, and the
parsers behind its dialogs. A fake api records the calls; Integration runs them on a scratch
store (tests/lock/helpers.StoreCase), never the machine's."""
import datetime as dt
import json
import os
import unittest
from unittest import mock

from kitlib.lockui import actions as a
from tests.lock.helpers import OWNER, StoreCase

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class FakeApi:
    def __init__(self):
        self.calls = []

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        return {"ok": True, "result": {}, "snapshot": None}

    def snapshot(self, store=None, **kw):
        self.calls.append(("snapshot", (), dict(kw, store=store)))
        return {"revision": 1}

    def history(self, store=None, days=7, **kw):
        self.calls.append(("history", (), dict(kw, store=store, days=days)))
        return []

    def __getattr__(self, name):
        if name in ("hold_create", "hold_edit", "hold_end", "set_games", "set_game_mode",
                    "set_division", "end_run", "move_to_top"):
            return lambda *args, **kw: self._rec(name, *args, **kw)
        raise AttributeError(name)


class Mapping(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.store = object()
        self.c = a.Client(store=self.store, api_mod=self.api)

    def last(self):
        return self.api.calls[-1]

    def test_default_actor_is_named_and_carries_no_role(self):
        self.assertEqual(self.c.actor, {"name": "reserve_ui"})

    def test_one_store_made_once_for_every_call(self):
        made = []
        c = a.Client(api_mod=self.api, make_store=lambda: made.append(object()) or made[-1])
        c.snapshot()
        c.history()
        c.release("h-1")
        self.assertEqual(len(made), 1)
        self.assertTrue(all(call[2]["store"] is made[0] for call in self.api.calls))

    def test_reads(self):
        self.c.snapshot()
        self.assertEqual(self.last(), ("snapshot", (), {"store": self.store, "since": a.NO_STATS,
                                                        "until": a.NO_STATS}))
        self.c.history(days=3)
        self.assertEqual(self.last(), ("history", (), {"store": self.store, "days": 3}))

    def test_reserve_maps_to_hold_create(self):
        s, e = NOW + dt.timedelta(hours=1), NOW + dt.timedelta(hours=2)
        self.c.reserve("half", s, e, "", now=NOW)
        self.assertEqual(self.last(), ("hold_create", (
            {"name": "reserve_ui"},
            {"owner": "owner", "scope": "half", "start": "2026-10-08T13:00:00Z",
             "end": "2026-10-08T14:00:00Z", "note": "reserve window"}), {"store": self.store}))

    def test_hold_edit_end_games_mode_division(self):
        self.c.hold({"owner": "owner", "scope": "whole", "for_s": 60})
        self.assertEqual(self.last()[0:2], ("hold_create", (
            {"name": "reserve_ui"}, {"owner": "owner", "scope": "whole", "for_s": 60})))
        self.c.edit("h-1", {"note": "x"})
        self.assertEqual(self.last()[0:2], ("hold_edit", ({"name": "reserve_ui"}, "h-1", {"note": "x"})))
        self.c.release("h-1")
        self.assertEqual(self.last()[0:2], ("hold_end", ({"name": "reserve_ui"}, "h-1")))
        self.c.game_mode(True)
        self.assertEqual(self.last()[0:2], ("set_game_mode", ({"name": "reserve_ui"}, True)))
        self.c.games([{"exe": "a.exe"}])
        self.assertEqual(self.last()[0:2], ("set_games", ({"name": "reserve_ui"}, [{"exe": "a.exe"}])))
        self.c.division(None)
        self.assertEqual(self.last()[0:2], ("set_division", ({"name": "reserve_ui"}, None)))
        self.assertTrue(all(c[2] == {"store": self.store} for c in self.api.calls))

    def test_move_to_top_maps_to_the_api_call(self):
        self.c.move_to_top("r-1")
        self.assertEqual(self.last(), ("move_to_top", ({"name": "reserve_ui"}, "r-1"),
                                       {"store": self.store}))


class Specs(unittest.TestCase):
    def test_iso_utc(self):
        local = NOW.astimezone(dt.timezone(dt.timedelta(hours=2)))
        self.assertEqual(a.iso_utc(local), "2026-10-08T12:00:00Z")
        with self.assertRaises(ValueError):
            a.iso_utc(dt.datetime(2026, 10, 8, 12, 0))

    def test_reserve_spec_starts_now_when_the_window_starts_now(self):
        spec = a.reserve_spec("whole", NOW + dt.timedelta(seconds=20), NOW + dt.timedelta(hours=1),
                              "  play  ", now=NOW)
        self.assertEqual(spec, {"owner": "owner", "scope": "whole",
                                "end": "2026-10-08T13:00:00Z", "note": "play"})

    def test_reserve_spec_refuses_bad_windows(self):
        with self.assertRaises(ValueError):
            a.reserve_spec("half", NOW, NOW, "", now=NOW)
        with self.assertRaises(ValueError):
            a.reserve_spec("everything", NOW, NOW + dt.timedelta(hours=1), "", now=NOW)

    def test_parse_ranges(self):
        self.assertEqual(a.parse_ranges("0-2, 5"), [0, 1, 2, 5])
        self.assertEqual(a.parse_ranges(""), [])
        self.assertEqual(a.parse_ranges(" 3 "), [3])
        for bad in ("a", "3-1", "1,,2", "-1"):
            with self.assertRaises(ValueError):
                a.parse_ranges(bad)

    def test_parse_local(self):
        ref = dt.datetime(2026, 10, 8, 9, 30).astimezone()
        t = a.parse_local("14:15", ref)
        self.assertEqual((t.year, t.month, t.day, t.hour, t.minute), (2026, 10, 8, 14, 15))
        self.assertIsNotNone(t.tzinfo)
        t = a.parse_local("2026-10-09 07:05", ref)
        self.assertEqual((t.day, t.hour, t.minute), (9, 7, 5))
        t = a.parse_local("+90m", ref)
        self.assertEqual(t - ref, dt.timedelta(minutes=90))
        t = a.parse_local("+2h", ref)
        self.assertEqual(t - ref, dt.timedelta(hours=2))
        for bad in ("", "25:00", "tomorrow", "+5x"):
            with self.assertRaises(ValueError):
                a.parse_local(bad, ref)

    def test_hold_spec_for_each_scope(self):
        ref = dt.datetime(2026, 10, 8, 9, 30).astimezone()
        self.assertEqual(a.hold_spec("whole", "", "+1h", "", "", "", ref),
                         {"owner": "owner", "scope": "whole", "end": a.iso_utc(ref + dt.timedelta(hours=1)),
                          "note": "reserve window"})
        spec = a.hold_spec("slots", "10:00", "11:00", "x", "0-1", "", ref)
        self.assertEqual(spec["slots"], {"cpu": [0, 1], "gpu": []})
        self.assertEqual(spec["start"], a.iso_utc(ref.replace(hour=10, minute=0)))
        with self.assertRaises(ValueError):
            a.hold_spec("slots", "", "+1h", "", "", "", ref)        # slots names some slot
        with self.assertRaises(ValueError):
            a.hold_spec("whole", "", "", "", "", "", ref)           # an end is needed

    def test_parsers_turn_overflow_into_value_error(self):
        ref = dt.datetime(2026, 10, 8, 9, 30).astimezone()
        for bad in ("+99999999999h", "+99999999999999999m"):
            with self.assertRaises(ValueError):
                a.hold_spec("whole", "", bad, "", "", "", ref)
        with self.assertRaises(ValueError):
            a.parse_ranges("0-99999999999")
        with self.assertRaises(ValueError):
            a.parse_ranges(str(a.MAX_SLOT + 1))
        self.assertEqual(len(a.parse_ranges("0-%d" % a.MAX_SLOT)), a.MAX_SLOT + 1)

    def test_a_time_at_the_end_of_year_9999_is_refused_not_raised(self):
        # A fixed UTC-4 ref, so nothing depends on the machine's zone: +N h lands on
        # 9999-12-31 22:00 at -04:00, which is past the last datetime once made UTC (_iso).
        ref = dt.datetime(2026, 10, 8, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
        last = dt.datetime(9999, 12, 31, 22, 0, tzinfo=ref.tzinfo)
        n = (last - ref) // dt.timedelta(hours=1)
        self.assertEqual(ref + dt.timedelta(hours=n), last)
        with self.assertRaisesRegex(ValueError, "out of range"):
            a.hold_spec("whole", "", "+%dh" % n, "", "", "", ref)
        # A bare end not after its start rolls a day; from 9999-12-31 there is no next day.
        # (A bare time on 9999-12-31 cannot be parsed in every zone, so the parse is fixed.)
        with mock.patch.object(a, "parse_local", lambda text, ref, day=None: last):
            with self.assertRaisesRegex(ValueError, "out of range"):
                a._end("21:00", last, last)

    def test_a_bare_end_not_after_the_start_rolls_to_the_next_day(self):
        ref = dt.datetime(2026, 10, 8, 9, 30).astimezone()
        spec = a.hold_spec("whole", "22:00", "02:00", "", "", "", ref)
        self.assertEqual(spec["end"], a.iso_utc(dt.datetime(2026, 10, 9, 2, 0).astimezone()))
        spec = a.hold_spec("whole", "", "08:00", "", "", "", ref)      # start now (09:30)
        self.assertEqual(spec["end"], a.iso_utc(dt.datetime(2026, 10, 9, 8, 0).astimezone()))
        spec = a.hold_spec("whole", "", "10:00", "", "", "", ref)      # after now: same day
        self.assertEqual(spec["end"], a.iso_utc(dt.datetime(2026, 10, 8, 10, 0).astimezone()))
        with self.assertRaises(ValueError):                            # a dated end does not roll
            a.hold_spec("whole", "22:00", "2026-10-08 02:00", "", "", "", ref)

    def test_edit_resolves_a_bare_time_on_the_holds_own_day(self):
        ref = dt.datetime(2026, 10, 8, 9, 30).astimezone()
        s0 = dt.datetime(2026, 10, 10, 20, 0).astimezone()
        hold = {"owner": "owner", "start": a.iso_utc(s0),
                "end": a.iso_utc(s0 + dt.timedelta(hours=2)), "note": ""}
        self.assertEqual(a.edit_changes(hold, "", "23:00", "", ref),
                         {"end": a.iso_utc(dt.datetime(2026, 10, 10, 23, 0).astimezone())})
        self.assertEqual(a.edit_changes(hold, "", "01:00", "", ref),        # rolls past midnight
                         {"end": a.iso_utc(dt.datetime(2026, 10, 11, 1, 0).astimezone())})
        self.assertEqual(a.edit_changes(hold, "19:00", "", "", ref),
                         {"start": a.iso_utc(dt.datetime(2026, 10, 10, 19, 0).astimezone())})

    def test_attempt_turns_any_exception_into_an_error_result(self):
        self.assertEqual(a.attempt(lambda: {"ok": True}), {"ok": True})
        got = a.attempt(lambda: 1 / 0)
        self.assertFalse(got["ok"])
        self.assertIn("ZeroDivisionError", a.result_text(got))
        got = a.attempt(lambda: a.parse_ranges("x"))
        self.assertEqual(got["error"]["code"], "invalid")

    def test_edit_changes(self):
        ref = dt.datetime(2026, 10, 8, 9, 30).astimezone()
        hold = {"owner": "owner", "start": a.iso_utc(ref.replace(hour=10)),
                "end": a.iso_utc(ref.replace(hour=11)), "note": "a"}
        self.assertEqual(a.edit_changes(hold, "", "", "a", ref), {})
        self.assertEqual(a.edit_changes(hold, "", "+1h", "b", ref),
                         {"end": a.iso_utc(ref + dt.timedelta(hours=1)), "note": "b"})
        self.assertEqual(a.edit_changes(hold, "10:45", "", "a", ref),
                         {"start": a.iso_utc(ref.replace(hour=10, minute=45))})
        with self.assertRaises(ValueError):           # only an owner hold's start moves
            a.edit_changes(dict(hold, owner="agent"), "10:45", "", "a", ref)

    def test_division_from_fields(self):
        self.assertIsNone(a.division_from_fields({"cpu_slots": " ", "agent_cap": ""}))
        self.assertEqual(a.division_from_fields({"cpu_slots": "6", "agent_cap": "", "gpu_share": "2"}),
                         {"cpu_slots": 6, "gpu_share": 2})
        self.assertEqual(a.division_from_fields({"gpu_share": "4, 2"}), {"gpu_share": [4, 2]})
        with self.assertRaises(ValueError):
            a.division_from_fields({"cpu_slots": "six"})
        with self.assertRaises(ValueError):
            a.division_from_fields({"nonsense": "1"})

    def test_division_fields_round_trip(self):
        f = a.division_fields({"cpu_slots": 6, "gpu_share": [4, 2]})
        self.assertEqual(f["cpu_slots"], "6")
        self.assertEqual(f["gpu_share"], "4, 2")
        self.assertEqual(f["agent_cap"], "")
        self.assertEqual(a.division_from_fields(f), {"cpu_slots": 6, "gpu_share": [4, 2]})

    def test_games_text_round_trip(self):
        rows = a.games_from_text("Game.exe\n# a note\nOther.exe  nopin  Other Game\n\nThird.exe pin\n")
        self.assertEqual(rows, [{"exe": "Game.exe"},
                                {"exe": "Other.exe", "pin": False, "name": "Other Game"},
                                {"exe": "Third.exe", "pin": True}])
        text = a.games_text([{"exe": "Game.exe", "pin": True, "name": "Game"},
                             {"exe": "Other.exe", "pin": False, "name": "Other.exe"}])
        self.assertEqual(text, "Game.exe  pin  Game\nOther.exe  nopin\n")

    def test_result_text(self):
        self.assertIsNone(a.result_text({"ok": True, "result": {}}))
        self.assertEqual(a.result_text({"ok": True, "result": {"notes": ["n1", "n2"]}}), "n1\nn2")
        self.assertEqual(a.result_text({"ok": False, "error": {"code": "forbidden",
                                                                "message": "owner only"}}),
                         "forbidden: owner only")


class HistoryCacheTest(unittest.TestCase):
    """The window's history lines: read again only when they may have changed."""

    def setUp(self):
        self.reads = 0
        self.cache = a.HistoryCache(max_age_s=300)

    def fetch(self):
        self.reads += 1
        return ["lines %d" % self.reads]

    def look(self, rev, at, force=False):
        return self.cache.get(rev, self.fetch, at, force)

    def test_an_unchanged_revision_is_not_read_again(self):
        self.assertEqual(self.look(5, 0), ["lines 1"])
        self.assertEqual(self.look(5, 30), ["lines 2"])     # the look after a change reads again
        for at in (60, 90, 120, 299):
            self.assertEqual(self.look(5, at), ["lines 2"])
        self.assertEqual(self.reads, 2)

    def test_a_moved_revision_is_read_then_read_once_more(self):
        self.look(5, 0)
        self.look(5, 30)
        self.look(6, 60)
        self.look(6, 90)        # the history line of a run that just ended comes after its bump
        self.look(6, 120)
        self.assertEqual(self.reads, 4)

    def test_forced_old_or_unknown_revision_reads(self):
        self.look(5, 0)
        self.look(5, 30)
        self.look(5, 40, force=True)
        self.assertEqual(self.reads, 3)
        self.look(5, 340)       # max_age_s after the last read
        self.assertEqual(self.reads, 4)
        self.look(None, 341)
        self.assertEqual(self.reads, 5)

    def test_a_failed_read_leaves_the_cache_as_it_was(self):
        self.look(5, 0)
        self.look(5, 30)

        def boom():
            raise OSError("gone")
        with self.assertRaises(OSError):
            self.cache.get(6, boom, 60)
        self.assertEqual(self.look(6, 61), ["lines 3"])


class Integration(StoreCase):
    """The client against a real (scratch) store: the owner actor is taken as given there."""

    def setUp(self):
        super().setUp()
        self.c = a.Client(store=self.store, actor=OWNER)

    def test_the_snapshot_reads_no_history_for_stats(self):
        from kitlib.lock import api
        from kitlib.lock.store import iso, utc_now
        end = utc_now() - dt.timedelta(hours=1)
        self.store.history_append({"id": "r-20261008T000000Z-aaaa", "repo": "kit", "slug": "g",
                                   "kind": "engine-test", "since": iso(end - dt.timedelta(hours=1)),
                                   "held_since": iso(end - dt.timedelta(minutes=30)),
                                   "ended": iso(end), "code": 0, "how": "done", "cores": [0],
                                   "gpus": []})
        self.assertEqual(len(api.snapshot(self.store)["stats"]["rows"]), 1)
        with mock.patch.object(self.store, "history_records",
                               wraps=self.store.history_records) as seen:
            snap = self.c.snapshot()
        self.assertEqual(snap["stats"]["rows"], [])
        self.assertEqual(seen.call_count, 1)
        self.assertEqual(seen.call_args.kwargs["since"].year, 1970)    # a range with no file
        self.assertEqual(len(self.c.history()), 1)

    def test_reserve_then_edit_then_release(self):
        now = dt.datetime.now(UTC)
        got = self.c.reserve("half", now + dt.timedelta(hours=1), now + dt.timedelta(hours=2), "",
                             now=now)
        self.assertTrue(got["ok"], got)
        hid = got["result"]["hold"]["id"]
        snap = self.c.snapshot()
        self.assertEqual([h["id"] for h in snap["holds"]], [hid])
        got = self.c.edit(hid, {"note": "moved"})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["result"]["hold"]["note"], "moved")
        got = self.c.release(hid)
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.c.snapshot()["holds"], [])

    def test_division_set_and_back_to_default(self):
        got = self.c.division({"agent_cap": 1})
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.c.snapshot()["layout"]["agent_cap"], 1)
        got = self.c.division(None)
        self.assertTrue(got["ok"], got)
        L = self.c.snapshot()["layout"]
        self.assertEqual(L["division"], L["default_division"])

    def machine_json(self):
        with open(os.path.join(self.root, "machine.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_division_dialog_keeps_only_what_the_owner_set(self):
        self.assertEqual(self.c.snapshot()["layout"]["owner_division"], {})
        self.assertEqual(a.division_fields(self.c.snapshot()["layout"]["owner_division"]),
                         {k: "" for k in a.DIVISION_KEYS})
        got = self.c.division(a.division_from_fields({"cpu_slots": "6"}))
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.machine_json(), {"cpu_slots": 6})
        fields = a.division_fields(self.c.snapshot()["layout"]["owner_division"])   # reopen
        self.assertEqual(fields["cpu_slots"], "6")
        self.assertEqual(fields["half_cpu_slots"], "")
        got = self.c.division(a.division_from_fields(fields))                       # save, no edit
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.machine_json(), {"cpu_slots": 6})
        fields["cpu_slots"] = "2"
        got = self.c.division(a.division_from_fields(fields))
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.c.snapshot()["layout"]["cpu_slots"], 2)

    def test_division_saves_keep_the_notes_in_machine_json(self):
        os.makedirs(self.root, exist_ok=True)       # the store makes its folder on first write
        with open(os.path.join(self.root, "machine.json"), "w", encoding="utf-8") as f:
            json.dump({"_comment": "set by hand", "cpu_slots": 6}, f)
        fields = a.division_fields(self.c.snapshot()["layout"]["owner_division"])
        self.assertEqual(fields["cpu_slots"], "6")
        fields["cpu_slots"] = "2"
        got = self.c.division(a.division_from_fields(fields))
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.machine_json(), {"_comment": "set by hand", "cpu_slots": 2})
        got = self.c.division(None)                 # Defaults: the notes stay, the default applies
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.machine_json(), {"_comment": "set by hand"})
        L = self.c.snapshot()["layout"]
        self.assertEqual((L["source"], L["owner_division"]), ("default", {}))
        self.assertEqual(L["division"], L["default_division"])

    def test_the_default_division_the_dialog_writes_for_all_blank_is_accepted(self):
        L = self.c.snapshot()["layout"]
        default = {k: v for k, v in L["default_division"].items() if k in a.DIVISION_KEYS}
        got = self.c.division(default)
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.machine_json(), default)
        L2 = self.c.snapshot()["layout"]
        self.assertEqual(L2["division"], L["default_division"])
        self.assertEqual(L2["key"], L["key"])

    def test_games_and_game_mode(self):
        got = self.c.games(a.games_from_text("Game.exe nopin"))
        self.assertTrue(got["ok"], got)
        got = self.c.game_mode(True)
        self.assertTrue(got["ok"], got)
        g = self.c.snapshot()["game"]
        self.assertTrue(g["enabled"])
        self.assertEqual([x["exe"] for x in g["games"]], ["Game.exe"])
        self.assertTrue(self.c.game_mode(False)["ok"])

    def test_an_unnamed_actor_cannot_write_on_a_scratch_store(self):
        got = a.Client(store=self.store).reserve(
            "whole", dt.datetime.now(UTC), dt.datetime.now(UTC) + dt.timedelta(hours=1), "")
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"]["code"], "forbidden")
        self.assertIn("forbidden", a.result_text(got))

    def test_history_comes_from_the_store(self):
        now = dt.datetime.now(UTC)
        self.store.history_append({"id": "r-20261008T100000Z-abcd", "repo": "kit", "slug": "g",
                                   "kind": "engine-test", "since": a.iso_utc(now - dt.timedelta(hours=2)),
                                   "held_since": a.iso_utc(now - dt.timedelta(hours=2)),
                                   "ended": a.iso_utc(now - dt.timedelta(hours=1)), "code": 0,
                                   "how": "done"})
        rows = self.c.history()
        self.assertEqual([r["id"] for r in rows], ["r-20261008T100000Z-abcd"])
        self.assertEqual(rows[0]["held_s"], 3600)


if __name__ == "__main__":
    unittest.main()
