"""The PM's side of the lock-window notices: the watcher's LOCKNOTICE check and
hosts/claude/shell/hub/hub_notices.py (the ArtifactData batch, then marking it posted). Scratch folders only."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from kitlib import pmwatch as W

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "hosts", "claude", "shell", "hub"))
import hub_notices as cli                                           # noqa: E402

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

    def test_unreadable_is_retried_and_posted_ones_drop_out(self):
        self.put(doc("start", "2026-10-08T12:00:00Z"), raw="{")
        self.assertEqual(W.notice_events(set(), self.dir), ([], []))
        self.put(doc("start", "2026-10-08T12:00:00Z"))
        ev, seen = W.notice_events(set(), self.dir)
        self.assertEqual(len(ev), 1)
        W.mark_posted(self.dir, seen)
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

    def test_notice_file_takes_only_notice_ids(self):
        self.assertEqual(W.notice_file("L-%s-end" % RID), "%s-end.json" % RID)
        for bad in ("M-001", "L-../x-end", "L-x-middle", None, 3):
            self.assertIsNone(W.notice_file(bad), bad)


class Cli(Case):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv), config_path=os.path.join(self.tmp.name, "none.json"))
        return code, out.getvalue(), err.getvalue()

    def test_no_dir_exits_2(self):
        code, _out, err = self.main()
        self.assertEqual(code, 2)
        self.assertIn("machine_lock.notice.dir", err)

    def test_batch_then_posted(self):
        self.put(doc("end", "2026-10-08T12:30:00Z"))
        self.put(doc("start", "2026-10-08T12:00:00Z"))
        batch = os.path.join(self.tmp.name, "batch.json")
        code, out, _err = self.main("--dir", self.dir, "--out", batch)
        self.assertEqual(code, 0)
        self.assertIn("2 to post", out)
        with open(batch, encoding="utf-8") as f:
            writes = json.load(f)
        self.assertEqual([(w["op"], w["collection"], w["doc_id"]) for w in writes],
                         [("set", "inbox", "L-%s-start" % RID), ("set", "inbox", "L-%s-end" % RID)])
        self.assertNotIn("id", writes[0]["data"])
        self.assertEqual(writes[0]["data"]["kind"], "lock-window")
        self.put(doc("start", "2026-10-08T13:00:00Z", rid="r-later"))   # came after the batch
        code, out, _err = self.main("--dir", self.dir, "--posted", batch)
        self.assertEqual((code, out.strip()), (0, "2 marked posted"))
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, W.POSTED))),
                         ["%s-end.json" % RID, "%s-start.json" % RID])
        code, out, _err = self.main("--dir", self.dir)
        self.assertEqual(out.split(), ["L-r-later-start"] + "Heavy window started: kit/x (build)".split())


if __name__ == "__main__":
    unittest.main()
