"""The PM's side of the lock-window notices: the watcher's LOCKNOTICE check. Scratch folders
only."""
import json
import os
import tempfile
import unittest

from kitlib import pmwatch as W

RID = "r-20261008T120000Z-abcdef"


def doc(phase, at, rid=RID):
    return {"id": "L-%s-%s" % (rid, phase), "phase": phase, "run": rid,
            "title": "Heavy window %s: kit/x (build)" % ("started" if phase == "start" else "ended"),
            "text": "t", "at": at, "repo": "kit", "from": "machine lock", "kind": "lock-window"}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "notices")
        os.makedirs(self.dir)

    def put(self, d, raw=None):
        with open(os.path.join(self.dir, "%s-%s.json" % (d["run"], d["phase"])), "w",
                  encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(d))


class Events(Case):
    def test_each_new_notice_fires_once(self):
        self.put(doc("start", "2026-10-08T12:00:00Z"))
        ev, seen = W.notice_events(set(), self.dir)
        self.assertEqual(ev, ["LOCKNOTICE L-%s-start Heavy window started: kit/x (build)" % RID])
        self.put(doc("end", "2026-10-08T12:30:00Z"))
        ev, seen = W.notice_events(set(seen), self.dir)
        self.assertEqual(ev, ["LOCKNOTICE L-%s-end Heavy window ended: kit/x (build)" % RID])
        self.assertEqual(W.notice_events(set(seen), self.dir)[0], [])

    def test_unreadable_is_retried_and_one_moved_into_a_subfolder_drops_out(self):
        self.put(doc("start", "2026-10-08T12:00:00Z"), raw="{")
        self.assertEqual(W.notice_events(set(), self.dir), ([], []))
        self.put(doc("start", "2026-10-08T12:00:00Z"))
        ev, seen = W.notice_events(set(), self.dir)
        self.assertEqual(len(ev), 1)
        os.makedirs(os.path.join(self.dir, "posted"))
        os.replace(os.path.join(self.dir, seen[0]), os.path.join(self.dir, "posted", seen[0]))
        self.assertEqual(W.notice_events(set(seen), self.dir), ([], []))
        self.assertEqual(W.notice_events(set(), os.path.join(self.dir, "none")), ([], []))

    def test_watcher_baselines_then_reports(self):
        self.put(doc("start", "2026-10-08T12:00:00Z"))
        w = W.Watcher({"branches": {}, "usage": {}, "bg_alerts": [], "idle": {}}, [],
                      dict, os.path.join(self.dir, "no-alerts"), None, lambda n: None,
                      notice_dir=self.dir)
        w.start(1000.0)
        self.assertEqual(w.s["lock_notices"], ["%s-start.json" % RID])
        w.next_b = w.next_u = w.next_i = float("inf")
        self.assertEqual(w.poll(1000.0), [])
        self.put(doc("end", "2026-10-08T12:30:00Z"))
        self.assertEqual(w.poll(1001.0),
                         ["LOCKNOTICE L-%s-end Heavy window ended: kit/x (build)" % RID])

    def test_configured_dir(self):
        p = os.path.join(self.tmp.name, "config.json")
        self.assertIsNone(W.configured_notice_dir(p))
        for cfg, want in (({"machine_lock": {"notice": {"dir": self.dir}}}, self.dir),
                          ({"machine_lock": {"daemons": "stop"}}, None),
                          ({"machine_lock": {"notice": {"dir": ""}}}, None)):
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            self.assertEqual(W.configured_notice_dir(p), want)


if __name__ == "__main__":
    unittest.main()
