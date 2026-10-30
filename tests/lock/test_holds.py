"""kitlib.lock.holds (one hold record for owner, agent and game holds): windows,
admits (a whole hold is the owner's own) and the job every hold names, holds that cannot be
read failing closed, making, editing, ending and claiming holds."""
import datetime
import json
import os
import time
import unittest
from unittest import mock

from .helpers import OWNER, StoreCase, agent
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.lock import holds as hl
    from kitlib.lock import kinds
    from kitlib.lock.store import LockError, iso, utc_now
    from kitlib.proc import process

H = datetime.timedelta(hours=1)


def whole(**kw):
    return dict({"id": "h-x", "owner": "owner", "scope": "whole"}, **kw)


@windows_only("the lock is Windows only")
class Admits(unittest.TestCase):
    """A whole hold is the owner's (job OWNER); only the owner's own runs use it. The old
    rule let an agent's run of the named job, or any run of an open hold, in."""

    def test_the_owners_run_is_admitted(self):
        self.assertEqual(hl.admits(whole(job="OWNER"), {"role": "owner", "repo": "kit",
                                                       "slug": "t"}), (True, None))

    def test_an_agents_run_is_refused_whatever_job_it_names(self):
        for h in (whole(job="OWNER"), whole(repo="kit", slug="t"), whole(open=True)):
            with self.subTest(h=h):
                ok, why = hl.admits(h, {"role": "agent", "repo": "kit", "slug": "t"})
                self.assertFalse(ok)
                self.assertIn("only the owner's own runs", why)

    def test_a_run_with_no_role_is_refused(self):
        self.assertFalse(hl.admits(whole(), {"repo": "kit", "slug": "t"})[0])
        self.assertFalse(hl.admits(whole(), None)[0])


@windows_only("the lock is Windows only")
class Window(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.timezone.utc)

    def test_for_a_duration_from_now(self):
        self.assertEqual(hl.window(self.NOW, dur_s=3600), (self.NOW, self.NOW + H))

    def test_until_a_local_time(self):
        s, e = hl.window(self.NOW, until=datetime.time(0, 0))
        self.assertEqual(s, self.NOW)
        self.assertTrue(self.NOW < e <= self.NOW + 24 * H)

    def test_from_a_local_time_ends_after_it_starts(self):
        s, e = hl.window(self.NOW, start=datetime.time(23, 0), until=datetime.time(1, 0))
        self.assertEqual(e - s, 2 * H)
        self.assertGreater(e, self.NOW)

    def test_what_it_cannot_use_is_usage(self):
        t = datetime.time(1, 0)
        for kw in ({}, {"dur_s": 60, "until": t}, {"date": self.NOW.date()}, {"start": t},
                   {"start": t, "until": t, "dur_s": 60}):
            with self.subTest(kw=kw), self.assertRaises(LockError) as cm:
                hl.window(self.NOW, **kw)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("usage: machine_lock.py hold:", cm.exception.message)


class Holds(StoreCase):
    def setUp(self):
        super().setUp()
        self.L = self.layout()
        self.table = kinds.load(self.kinds_path)
        self.now = utc_now()

    def make(self, actor=OWNER, **spec):
        spec.setdefault("start", self.now)
        spec.setdefault("end", self.now + H)
        return hl.create(self.store, self.L, spec, actor, self.table)

    def agent_hold(self, kind="engine-test", session="sess-A", **kw):
        return self.make(agent(session), owner="agent", repo="kit", slug="t", kind=kind, **kw)

    def code(self, fn, *a, **kw):
        with self.assertRaises(LockError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def test_owner_half_hold_covers_half(self):
        h, _notes = self.make(owner="owner", scope="half")
        self.assertEqual(len(h["_slots"]["cpu"]), 6)
        self.assertEqual(len(h["_slots"]["gpu"]), 1)
        self.assertTrue(hl.strong(h))
        self.assertEqual(hl.find(self.store, self.L, h["id"])["id"], h["id"])

    def test_owner_slots_hold_names_its_slots(self):
        h, _ = self.make(owner="owner", scope="slots", slots={"cpu": [3, 4], "gpu": []})
        self.assertEqual(h["_slots"], {"cpu": [3, 4], "gpu": []})
        self.assertEqual(self.code(self.make, owner="owner", scope="slots",
                                   slots={"cpu": [99]}), 2)

    def test_agent_hold_takes_the_lowest_free_slots(self):
        h, _ = self.agent_hold()
        self.assertEqual(h["_slots"]["cpu"], [0, 1])
        self.assertEqual(h["session"], "sess-A")
        self.assertEqual(h["shares"], {"cpu": "medium", "gpu": None})

    def test_every_hold_names_its_job(self):
        self.assertEqual(self.agent_hold()[0]["job"], "kit/t")
        g, _ = self.make(owner="game", scope="domain", slots={"cpu": [5], "gpu": []},
                         game={"exe": "Other.exe", "name": "Other"})
        self.assertEqual(g["job"], "Other")
        self.assertEqual(self.make(owner="owner", scope="half")[0]["job"], "OWNER")
        self.assertEqual(self.make(owner="owner", scope="whole", job="OWNER")[0]["job"], "OWNER")
        self.assertEqual(sorted(h["job"] for h in hl.records(self.store, self.L)),
                         ["OWNER", "OWNER", "Other", "kit/t"])

    def test_a_job_other_than_its_own_is_usage(self):
        self.assertEqual(self.code(self.make, owner="owner", scope="half", job="kit/t"), 2)
        self.assertEqual(self.code(self.agent_hold, job="OWNER"), 2)
        self.assertEqual(self.code(self.agent_hold, job="kit/u"), 2)
        self.assertEqual(self.code(self.make, owner="game", scope="domain",
                                   slots={"cpu": [5], "gpu": []}, game={}), 2)

    def test_a_whole_hold_names_no_repo_slug_or_open(self):
        # This ends the old open whole hold: the old create took all three (red there).
        for kw in ({"slug": "t"}, {"repo": "kit", "slug": "t"}, {"open": True}):
            with self.subTest(kw=kw):
                self.assertEqual(self.code(self.make, owner="owner", scope="whole", **kw), 2)

    def test_an_owner_hold_edits_no_repo_slug_or_open(self):
        h, _ = self.make(owner="owner", scope="whole")
        for k, v in (("slug", "t"), ("repo", "kit"), ("open", True)):
            with self.subTest(k=k):
                self.assertEqual(self.code(hl.edit, self.store, self.L, h["id"], {k: v}), 2)

    def test_agent_hold_under_a_strong_hold_is_11(self):
        self.make(owner="owner", scope="whole")
        self.assertEqual(self.code(self.agent_hold), 11)

    def test_agent_holds_leaving_too_few_is_6(self):
        self.agent_hold("cpu-eval")
        self.agent_hold("cpu-eval")
        self.assertEqual(self.code(self.agent_hold), 6)

    def test_agent_hold_limits(self):
        self.assertEqual(self.code(self.agent_hold, end=self.now + 13 * H), 2)
        self.assertEqual(self.code(self.agent_hold, kind="no-such-kind"), 2)
        self.assertEqual(self.code(self.agent_hold, gpu_share="training"), 2)

    def test_bad_owner_or_scope_is_usage(self):
        self.assertEqual(self.code(self.make, owner="nobody", scope="half"), 2)
        self.assertEqual(self.code(self.make, owner="owner", scope="domain"), 2)
        self.assertEqual(self.code(self.make, owner="owner", scope="half",
                                   end=self.now - H), 2)

    def test_edit(self):
        h, _ = self.make(owner="owner", scope="half")
        self.assertEqual(self.code(hl.edit, self.store, self.L, h["id"],
                                   {"start": self.now + H}), 2)
        got = hl.edit(self.store, self.L, h["id"], {"note": "build", "end": self.now + 2 * H})
        self.assertEqual(got["note"], "build")
        self.assertEqual(got["_end"].replace(microsecond=0),
                         (self.now + 2 * H).replace(microsecond=0))
        later, _ = self.make(owner="owner", scope="whole", start=self.now + 3 * H,
                             end=self.now + 4 * H)
        moved = hl.edit(self.store, self.L, later["id"], {"start": self.now + 2 * H})
        self.assertLess(moved["_start"], later["_start"])
        self.assertEqual(self.code(hl.edit, self.store, self.L, "h-none", {"note": "x"}), 1)

    def test_agent_hold_edits_only_end_and_note(self):
        h, _ = self.agent_hold()
        self.assertEqual(self.code(hl.edit, self.store, self.L, h["id"], {"slug": "u"}), 2)
        self.assertEqual(hl.edit(self.store, self.L, h["id"], {"note": "n"})["note"], "n")

    def test_end_hold(self):
        h, _ = self.make(owner="owner", scope="half")
        hl.end_hold(self.store, self.L, h["id"])
        self.assertIsNone(hl.find(self.store, self.L, h["id"]))
        self.assertEqual(self.store.read("ended/%s.json" % h["id"])["how"], "released")
        with self.assertRaises(LockError) as cm:
            hl.end_hold(self.store, self.L, h["id"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("it ended: released", cm.exception.message)

    def drawn(self, *taken):
        """(a patch of the id builder create uses: each of `taken` in turn, then an id nothing
        has, that id). A draw after it is an error (StopIteration)."""
        fresh = next(i for i in (taken[0][:-4] + "%04x" % n for n in range(len(taken) + 1))
                     if i not in taken)
        return mock.patch.object(hl, "new_id", side_effect=list(taken) + [fresh]), fresh

    def test_an_id_a_live_hold_has_is_drawn_again(self):
        """An id is a second stamp and a 2-byte token, so two holds made in one second can draw
        the same one: the second create wrote its record over the first hold's (red there),
        which lost its slots while its holder still counted on them."""
        a, _ = self.agent_hold(session="sess-A")
        before = self.store.read("holds/%s.json" % a["id"])
        patch, fresh = self.drawn(a["id"])
        with patch as ids:
            b, _ = self.agent_hold(session="sess-B")
        self.assertEqual(self.store.read("holds/%s.json" % a["id"]), before)
        self.assertEqual({h["id"]: (h["session"], h["_slots"]["cpu"])
                          for h in hl.records(self.store, self.L)},
                         {a["id"]: ("sess-A", [0, 1]), fresh: ("sess-B", [2, 3])})
        self.assertEqual(b["id"], fresh)
        self.assertEqual(ids.call_count, 2)

    def test_an_id_an_ended_hold_has_is_drawn_again(self):
        """The same clash with a hold that has ended: the new hold took its id, and its own
        end then replaced ended/<id>.json (red there), the only record of the first."""
        a, _ = self.make(owner="owner", scope="half", note="first")
        hl.end_hold(self.store, self.L, a["id"])
        before = self.store.read("ended/%s.json" % a["id"])
        self.assertEqual(before["note"], "first")
        patch, fresh = self.drawn(a["id"])
        with patch as ids:
            b, _ = self.make(owner="owner", scope="slots", slots={"cpu": [3], "gpu": []},
                             note="second")
        hl.end_hold(self.store, self.L, b["id"])
        self.assertEqual(self.store.read("ended/%s.json" % a["id"]), before)
        self.assertEqual(b["id"], fresh)
        self.assertEqual(self.store.read("ended/%s.json" % fresh)["note"], "second")
        self.assertEqual(ids.call_count, 2)

    def test_an_id_is_drawn_until_one_is_free(self):
        """One more draw is not enough: the id drawn after a live hold's may be an ended
        hold's. create draws until neither folder has the id."""
        gone, _ = self.make(owner="owner", scope="slots", slots={"cpu": [5], "gpu": []},
                            note="ended")
        hl.end_hold(self.store, self.L, gone["id"])
        live, _ = self.agent_hold(session="sess-A")
        ended = self.store.read("ended/%s.json" % gone["id"])
        before = self.store.read("holds/%s.json" % live["id"])
        patch, fresh = self.drawn(live["id"], gone["id"])
        with patch as ids:
            b, _ = self.make(owner="owner", scope="slots", slots={"cpu": [4], "gpu": []},
                             note="third")
        hl.end_hold(self.store, self.L, b["id"])
        self.assertEqual(self.store.read("ended/%s.json" % gone["id"]), ended)
        self.assertEqual(self.store.read("holds/%s.json" % live["id"]), before)
        self.assertEqual(b["id"], fresh)
        self.assertEqual(self.store.read("ended/%s.json" % fresh)["note"], "third")
        self.assertEqual(ids.call_count, 3)

    def rec(self, rid, slug="t", kind="engine-test", role="agent"):
        return {"id": rid, "repo": "kit", "slug": slug, "kind": kind, "pid": os.getpid(),
                "ctime": process.own_ctime(), "role": role}

    def test_claim_an_owner_whole_hold_admits_the_owners_runs_only(self):
        h, _ = self.make(owner="owner", scope="whole")
        got, why = hl.claim(self.store, self.L, self.rec("r-1", role="owner"), h["id"], None)
        self.assertIsNone(why)
        self.assertEqual(got["id"], h["id"])
        got, why = hl.claim(self.store, self.L, self.rec("r-2"), h["id"], None)
        self.assertIsNone(got)
        self.assertIn("only the owner's own runs", why)

    def test_a_half_hold_cannot_be_run_inside(self):
        h, _ = self.make(owner="owner", scope="half")
        got, why = hl.claim(self.store, self.L, self.rec("r-1"), h["id"], None)
        self.assertIsNone(got)
        self.assertIn("cannot be run inside", why)

    def test_claim_an_agent_hold_once(self):
        h, _ = self.agent_hold()
        shares = hl.shares_for(self.table["engine-test"])
        got, why = hl.claim(self.store, self.L, self.rec("r-1"), h["id"], shares)
        self.assertIsNone(why)
        self.assertEqual(got["claim"]["id"], "r-1")
        got, why = hl.claim(self.store, self.L, self.rec("r-2"), h["id"], shares)
        self.assertIsNone(got)
        self.assertIn("claimed by run r-1", why)
        got, why = hl.claim(self.store, self.L, self.rec("r-3", kind="cpu-eval"), h["id"],
                            hl.shares_for(self.table["cpu-eval"]))
        self.assertIn("was made for", why)

    def test_a_hold_and_a_run_that_name_one_kind_by_old_and_new_name_match(self):
        """A hold made under a kind's retired name (by a kit from before its rename) is claimed
        by a run booked under the new name, and the other way round. Red before: claim compared
        the names as written, `was made for kind ...`."""
        with open(self.kinds_path, encoding="utf-8") as f:
            doc = json.load(f)
        doc["aliases"] = {"old-test": {"to": "engine-test", "why": "renamed"}}
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        shares = hl.shares_for(self.table["engine-test"])
        for held, run in (("old-test", "engine-test"), ("engine-test", "old-test")):
            h, _ = self.agent_hold()
            path = self.store.path("holds", h["id"] + ".json")
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            rec["kind"] = held
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rec, f)
            got, why = hl.claim(self.store, self.L, self.rec("r-" + held, kind=run), h["id"],
                                shares)
            self.assertIsNone(why, (held, run))
            self.assertEqual(got["claim"]["id"], "r-" + held)

    def test_claim_of_no_hold(self):
        self.assertEqual(hl.claim(self.store, self.L, self.rec("r-1"), "h-none", None),
                         (None, "no hold h-none"))

    def test_an_id_with_a_trailing_newline_names_no_store_file(self):
        """ID_RE ends in $, which re.match lets match before a final newline: end_hold and claim
        then read ended/<id>\\n.json. fullmatch keeps a non-id from naming a store file (the
        same fix as kitlib.lockmcp _id, 03216d0)."""
        h, _ = self.make(owner="owner", scope="half")
        hl.end_hold(self.store, self.L, h["id"])
        bad = h["id"] + "\n"
        read = []
        real = hl.read_json

        def spy(path):
            read.append(path)
            return real(path)

        with mock.patch.object(hl, "read_json", spy):
            with self.assertRaises(LockError) as cm:
                hl.end_hold(self.store, self.L, bad)
            got, why = hl.claim(self.store, self.L, self.rec("r-1"), bad, None)
        self.assertEqual(cm.exception.message, "no live hold %s" % bad)
        self.assertEqual((got, why), (None, "no hold %s" % bad))
        self.assertEqual([p for p in read if "\n" in p], [])


class Unreadable(StoreCase):
    """A hold file that cannot be read, or is not a hold, fails closed (HoldsUnreadable): the old
    _read_all skipped it, so the slots it covered read as free (red there: records gave [])."""

    def setUp(self):
        super().setUp()
        self.L = self.layout()
        self.now = utc_now()

    def put(self, hid, body):
        os.makedirs(self.store.path("holds"), exist_ok=True)
        with open(self.store.path("holds", hid + ".json"), "w", encoding="utf-8") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def half(self, hid="h-20261006T120000Z-abcd", **kw):
        return dict({"id": hid, "owner": "owner", "scope": "half", "start": iso(self.now),
                     "end": iso(self.now + H), "note": None}, **kw)

    def test_a_file_that_is_not_json_raises_after_the_read_window(self):
        self.put("h-20261006T120000Z-abcd", "{not json")
        t0 = time.monotonic()
        with self.assertRaises(hl.HoldsUnreadable) as cm:
            hl.records(self.store, self.L)
        self.assertGreaterEqual(time.monotonic() - t0, self.store.settings.queue_read_s)
        self.assertIn("h-20261006T120000Z-abcd.json", str(cm.exception))
        self.assertIsInstance(cm.exception, OSError)

    def test_a_record_in_another_shape_raises(self):
        for body in (self.half(owner="nobody"), self.half(job="kit/t"), self.half(note=7),
                     self.half(id="h-other"), [1, 2]):
            with self.subTest(body=body):
                self.put("h-20261006T120000Z-abcd", body)
                with self.assertRaises(hl.HoldsUnreadable):
                    hl.records(self.store, self.L)

    def test_a_folder_that_cannot_be_listed_raises(self):
        # Review of 945d2bf: only FileNotFoundError was caught, so a PermissionError from listdir
        # escaped as itself and acquire did not refuse with 10 (red there: PermissionError).
        def denied(path):
            raise PermissionError(13, "Access is denied", path)
        with mock.patch.object(hl.os, "listdir", denied):
            with self.assertRaises(hl.HoldsUnreadable) as cm:
                hl.records(self.store, self.L)
        self.assertIsInstance(cm.exception.__cause__, PermissionError)

    def test_a_record_from_before_r1409_reads_with_its_job(self):
        self.put("h-20261006T120000Z-abcd", self.half())
        got = hl.records(self.store, self.L)
        self.assertEqual([h["job"] for h in got], ["OWNER"])

    def test_creating_a_hold_over_an_unreadable_one_is_refused(self):
        self.put("h-20261006T120000Z-abcd", "")
        with self.assertRaises(LockError) as cm:
            hl.create(self.store, self.L, {"owner": "owner", "scope": "half", "start": self.now,
                                           "end": self.now + H}, OWNER)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("holds unreadable", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
