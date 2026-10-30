"""Lock-window notices (kitlib.lock.notice): with machine_lock.notice set, a run that makes a
heavy or exclusive window writes a start notice when it starts holding and an end notice when it
ends; without it, none. A notice that cannot be written never fails the run. Scratch store and
scratch notice folder only."""
import json
import os
import sys
import unittest

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import notice
    from kitlib.lock import run as rm


class Notices(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(1)
        self.dir = os.path.join(self.tmp, "notices")

    def run_one(self, code=0, kind="engine-test"):
        return rm.run_command([sys.executable, "-c", "import sys; sys.exit(%d)" % code], kind,
                              "kit", "win", cores=1, store=self.store, env=clean_env())

    def notices(self):
        if not os.path.isdir(self.dir):
            return {}
        out = {}
        for n in os.listdir(self.dir):
            with open(os.path.join(self.dir, n), encoding="utf-8") as f:
                out[n] = json.load(f)
        return out

    def ended(self):
        names = os.listdir(self.store.path("ended"))
        self.assertEqual(len(names), 1)
        return self.store.read("ended/" + names[0])

    def test_off_by_default(self):
        self.store.site = {}
        self.assertEqual(self.run_one(), 0)
        self.assertEqual(self.notices(), {})
        self.assertNotIn("notice", self.ended())

    def test_a_named_kind_writes_a_start_and_an_end_notice(self):
        self.store.site = {"notice": {"dir": self.dir, "kinds": ["engine-test"]}}
        self.assertEqual(self.run_one(3), 3)
        rec = self.ended()
        rid = rec["id"]
        got = self.notices()
        self.assertEqual(sorted(got), ["%s-end.json" % rid, "%s-start.json" % rid])
        start, end = got["%s-start.json" % rid], got["%s-end.json" % rid]
        self.assertEqual(start["id"], "L-%s-start" % rid)
        self.assertEqual(start["title"], "Heavy window started: kit/win (engine-test)")
        self.assertIn("1 cpu slot", start["text"])
        self.assertEqual(end["id"], "L-%s-end" % rid)
        self.assertEqual(end["title"], "Heavy window ended: kit/win (engine-test)")
        self.assertIn("(done, exit 3)", end["text"])
        for doc in (start, end):
            self.assertEqual((doc["repo"], doc["from"], doc["kind"], doc["run"]),
                             ("kit", notice.FROM, notice.KIND, rid))
        self.assertEqual(rec["notice"], self.dir)

    def test_a_kind_not_named_writes_none(self):
        self.store.site = {"notice": {"dir": self.dir, "kinds": ["render"]}}
        self.assertEqual(self.run_one(), 0)
        self.assertEqual(self.notices(), {})

    def test_a_record_that_cannot_be_saved_still_gets_both_notices(self):
        """The record on disk cannot take the notice's folder: the start notice stays and the
        wrapper still writes the end notice when the run ends."""
        self.store.site = {"notice": {"dir": self.dir, "kinds": ["engine-test"]}}
        write = self.store.write

        def full_disk(rel, d):
            if isinstance(d, dict) and d.get("notice"):
                raise OSError(28, "no space left")
            return write(rel, d)

        self.store.write = full_disk
        self.assertEqual(self.run_one(), 0)
        rid = self.ended()["id"]
        self.assertEqual(sorted(self.notices()), ["%s-end.json" % rid, "%s-start.json" % rid])

    def test_an_unwritable_folder_never_fails_the_run(self):
        blocker = os.path.join(self.tmp, "a-file")
        with open(blocker, "w") as f:
            f.write("x")
        self.store.site = {"notice": {"dir": os.path.join(blocker, "sub"),
                                      "kinds": ["engine-test"]}}
        self.assertEqual(self.run_one(), 0)
        self.assertTrue(any("lock-window notice could not be written" in s for s in self.said),
                        self.said)
        self.assertNotIn("notice", self.ended())


class Target(StoreCase):
    """Which runs make a window when "kinds" is unset. Heavy is a size: agent_cap."""

    L = {"agent_cap": 6, "medium_cores": 2, "cpu_slots": 12}

    def test_defaults(self):
        self.store.site = {"notice": {"dir": self.tmp}}
        for k, whole, want in (({"cpu": "heavy", "gpu": 0}, False, True),
                               ({"cpu": "medium", "gpu": 0}, False, False),
                               ({"cpu": 2, "gpu": 0}, False, False),
                               ({"cpu": 5, "gpu": 0}, False, False),
                               ({"cpu": 6, "gpu": 0}, False, True),     # a measured heavy kind
                               ({"cpu": "medium", "gpu": "pool", "share": False}, False, True),
                               ({"cpu": "medium", "gpu": "pool", "share": True}, False, False),
                               ({"cpu": "medium", "gpu": 0}, True, True)):
            with self.subTest(k=k, whole=whole):
                self.assertEqual(notice.target(self.store, "x", k, self.L, whole),
                                 self.tmp if want else None)

    def test_a_kind_listed_under_its_retired_name_matches_and_the_reverse(self):
        """kinds.json "aliases": a machine config naming "engine-test" still gets notices for
        a "test" run, and a config naming "test" for a run booked under "engine-test". The kit's
        own table, whose aliases these are."""
        self.store.kinds_path = None
        k = {"cpu": "medium", "gpu": 0}
        for listed, kind in (("engine-test", "test"), ("test", "engine-test"),
                             ("engine-run", "app-run"), ("test", "test")):
            with self.subTest(listed=listed, kind=kind):
                self.store.site = {"notice": {"dir": self.tmp, "kinds": [listed]}}
                self.assertEqual(notice.target(self.store, kind, k, self.L), self.tmp)
        self.store.site = {"notice": {"dir": self.tmp, "kinds": ["engine-test"]}}
        self.assertIsNone(notice.target(self.store, "app-run", k, self.L))

    def test_off(self):
        self.store.site = {"daemons": "stop"}
        self.assertIsNone(notice.target(self.store, "x", {"cpu": "heavy"}, self.L, True))


class SaveFails(StoreCase):
    """A start notice is never taken back: the run keeps where it went in its record in memory, so
    its wrapper writes the end notice even when the record on disk cannot be updated."""

    class FakeRun:
        id = "r-1"

        def __init__(self, fail):
            self.fail, self.saved = fail, {}
            self.rec = {"id": "r-1", "repo": "kit", "slug": "s", "kind": "build",
                        "since": "2026-01-01T00:00:00Z"}

        def save(self, **kw):
            self.rec.update(kw)
            if self.fail:
                raise OSError(28, "no space left")
            self.saved = dict(self.rec)

    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.dir = os.path.join(self.tmp, "notices")
        self.store.site = {"notice": {"dir": self.dir, "kinds": ["build"]}}

    def test_a_record_that_cannot_be_saved_keeps_the_start_and_its_end_follows(self):
        run = self.FakeRun(True)
        notice.started(self.store, run, "build", {"cpu": 2}, Target.L)
        self.assertEqual(os.listdir(self.dir), ["r-1-start.json"])
        self.assertEqual(run.rec["notice"], self.dir)
        self.assertTrue(any("still writes the end notice" in s for s in self.said), self.said)
        notice.ended(self.store, dict(run.rec, how="done", code=0))
        self.assertEqual(sorted(os.listdir(self.dir)), ["r-1-end.json", "r-1-start.json"])

    def test_a_saved_record_keeps_it(self):
        run = self.FakeRun(False)
        notice.started(self.store, run, "build", {"cpu": 2}, Target.L)
        self.assertEqual(os.listdir(self.dir), ["r-1-start.json"])
        self.assertEqual(run.saved["notice"], self.dir)
        self.assertEqual(self.said, [])

    def test_an_owner_whole_hold_is_exclusive(self):
        rec = {"id": "r-1", "repo": "kit", "slug": "s", "kind": "build", "since":
               "2026-01-01T00:00:00Z", "hold_scope": "whole", "hold_owner": "owner"}
        self.assertTrue(notice.start_doc(rec)["title"].startswith("Exclusive window started"))
        rec["hold_owner"] = "agent"
        self.assertTrue(notice.start_doc(rec)["title"].startswith("Heavy window started"))


if __name__ == "__main__":
    unittest.main()
