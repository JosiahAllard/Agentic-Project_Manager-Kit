import datetime
import os
import sys
import tempfile
import unittest

from kitlib import hubbacklog
from kitlib.git import cmd as gitcmd
from tests.gitfixture import fixture_env

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "claude", "hub"))
import hub_backlog                                                  # noqa: E402

NOW = datetime.datetime(2026, 10, 7, 12, 0, tzinfo=datetime.timezone.utc)


def run(root, *args):
    gitcmd.subprocess_run(["git", "-C", root, *args], check=True, capture_output=True,
                          env=fixture_env({"GIT_AUTHOR_DATE": "2026-10-06T10:00:00Z",
                                       "GIT_COMMITTER_DATE": "2026-10-06T10:00:00Z"}))


def write(root, name, text):
    p = os.path.join(root, "docs", "backlog", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class ParseTest(unittest.TestCase):
    def test_repeats_and_skips(self):
        rec = hubbacklog.parse("id: A\nscope: x\nscope: y\n\n  indented: no\nfree text here\n")
        self.assertEqual(rec, {"id": ["A"], "scope": ["x", "y"]})

    def test_title_clipped(self):
        self.assertEqual(hubbacklog.title({"summary": ["x" * 200]}, cap=10), "x" * 9 + "…")

    def test_prio(self):
        self.assertEqual(hubbacklog.prio({"priority": ["2"]}), 2)
        self.assertIsNone(hubbacklog.prio({"priority": ["-"]}))
        self.assertIsNone(hubbacklog.prio({}))


class FitTest(unittest.TestCase):
    def test_shortens_then_drops(self):
        doc = {"repo": "r", "items": [{"id": str(i), "t": "y" * 150} for i in range(10)]}
        limit = hubbacklog.size({"repo": "r", "items": [{"id": "0", "t": "y" * 49 + "…"}] * 6, "cut": 4})
        out = hubbacklog.fit(doc, limit=limit)
        self.assertLessEqual(len(out["items"][0]["t"]), 50)
        self.assertEqual((len(out["items"]), out["cut"]), (6, 4))
        self.assertLessEqual(hubbacklog.size(out), limit)

    def test_small_untouched(self):
        doc = {"repo": "r", "items": [{"id": "a", "t": "short"}]}
        self.assertEqual(hubbacklog.fit(dict(doc)), doc)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        run(self.root, "init", "-q", "-b", "main")
        run(self.root, "config", "user.email", "t@t")
        run(self.root, "config", "user.name", "t")
        write(self.root, "OPEN-1.md", "id: OPEN-1\nsummary: first open\npriority: 2\nstate: blocked\n")
        write(self.root, "OPEN-2.md", "id: OPEN-2\nsummary: second\npriority: -\n")
        write(self.root, "SHUT.md", "id: SHUT\nsummary: closed in place\nclosed: 2026-10-05\n")
        write(self.root, "OLD.md", "id: OLD\nsummary: closed long ago\nclosed: 2026-01-01\n")
        write(self.root, "GONE.md", "id: GONE\nsummary: closed by deletion\n")
        write(self.root, "README.md", "how records work: see docs\n")
        run(self.root, "add", "-A")
        run(self.root, "commit", "-qm", "records")
        os.remove(os.path.join(self.root, "docs", "backlog", "GONE.md"))
        run(self.root, "commit", "-qam", "records: close GONE")
        write(self.root, "DRAFT.md", "id: DRAFT\nsummary: not landed\n")  # working tree only
        with open(os.path.join(self.root, ".kit.json"), "w") as f:
            f.write('{"backlog": {"store": "main"}}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot(self):
        b, c = hubbacklog.snapshot("r", self.root, now=NOW)
        self.assertEqual(b, {"repo": "r", "items": [
            {"id": "OPEN-1", "t": "first open", "p": 2, "s": "blocked"},
            {"id": "OPEN-2", "t": "second", "p": None, "s": "open"}]})
        self.assertEqual([(i["id"], i["t"]) for i in c["items"]],
                         [("GONE", "closed by deletion"), ("SHUT", "closed in place")])
        self.assertTrue(c["items"][0]["d"].startswith("2026-10-06T10:00:00"))

    def test_store_from_kit_json(self):
        with open(os.path.join(self.root, ".kit.json"), "w") as f:
            f.write('{"backlog": {"store": "elsewhere"}}')
        b, c = hubbacklog.snapshot("r", self.root, now=NOW)
        self.assertEqual((b["items"], c["items"]), ([], []))

    def test_an_invalid_kit_json_fails_that_repo_and_the_others_still_render(self):
        """store_at raises for a `.kit.json` the store cannot load; hub_backlog reports that repo
        and still writes the others."""
        with tempfile.TemporaryDirectory() as bad:
            run(bad, "init", "-q", "-b", "main")
            run(bad, "config", "user.email", "t@t")
            run(bad, "config", "user.name", "t")
            run(bad, "commit", "-q", "--allow-empty", "-m", "empty")
            for name, text in (("not json", '{"backlog": '), ("bad store", '{"backlog": {"store": 5}}')):
                with self.subTest(name):
                    with open(os.path.join(bad, ".kit.json"), "w") as f:
                        f.write(text)
                    with self.assertRaises(RuntimeError) as cm:
                        hubbacklog.store_at(bad, "main")
                    self.assertTrue(str(cm.exception).startswith("backlog store: "), cm.exception)
                    w, st, lines = hub_backlog.plan(["bad", "good"], {"bad": bad, "good": self.root}, {}, NOW)
                    self.assertEqual(lines[0], "bad: could not read: %s" % cm.exception)
                    self.assertEqual(lines[1], "good: 2 open, 2 completed; writes backlog, completed")
                    self.assertEqual([(x["collection"], x["doc_id"]) for x in w],
                                     [("backlog", "good"), ("completed", "good")])
                    self.assertEqual(sorted(st), ["backlog/good", "completed/good"])


class RefStoreTest(unittest.TestCase):
    """No `.kit.json` store key means the store's own default (the hidden ref), not docs/backlog."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        run(self.root, "init", "-q", "-b", "main")
        run(self.root, "config", "user.email", "t@t")
        run(self.root, "config", "user.name", "t")
        write(self.root, "ON-MAIN.md", "id: ON-MAIN\nsummary: not the store\n")
        run(self.root, "add", "-A")
        run(self.root, "commit", "-qm", "docs/backlog on main is not the store")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_default_store_is_the_ref(self):
        from kitlib.backlog import record, store
        backend = store.RefBackend(self.root, "origin", store.STORE_REF, store.STORE_REF)
        rules = record.Rules()
        backend.publish(backend.load(rules), {
            "OPEN-1": "id: OPEN-1\nsummary: first open\npriority: 2\n",
            "GONE": "id: GONE\nsummary: closed by deletion\n"}, "seed\n")
        backend.publish(backend.load(rules), {"GONE": None}, "retires GONE: gone\n")
        b, c = hubbacklog.snapshot("r", self.root, now=NOW)
        self.assertEqual(b["items"], [{"id": "OPEN-1", "t": "first open", "p": 2, "s": "open"}])
        self.assertEqual([(i["id"], i["t"]) for i in c["items"]], [("GONE", "closed by deletion")])

    def test_no_store_yet_is_empty(self):
        b, c = hubbacklog.snapshot("r", self.root, now=NOW)
        self.assertEqual((b["items"], c["items"]), ([], []))


class PlanTest(unittest.TestCase):
    DOCS = ({"repo": "r", "items": [{"id": "A"}]}, {"repo": "r", "days": 90, "items": []})

    def snap(self, n, root, now):
        return self.DOCS

    def test_writes_changed_only(self):
        w, st, lines = hub_backlog.plan(["r", "x"], {"r": "/r", "x": None}, {}, NOW, snap=self.snap)
        self.assertEqual([(x["collection"], x["doc_id"]) for x in w], [("backlog", "r"), ("completed", "r")])
        self.assertEqual(w[0]["data"]["posted_at"], "2026-10-07T12:00:00Z")
        self.assertIn("x: no folder, skipped", lines)
        self.assertNotIn("if_version", w[0])
        self.assertEqual(st["backlog/r"]["v"], 1)
        w2, _, _ = hub_backlog.plan(["r"], {"r": "/r"}, st, NOW, snap=self.snap)
        self.assertEqual(w2, [])
        w3, st3, _ = hub_backlog.plan(["r"], {"r": "/r"}, st, NOW, snap=self.snap, force=True)
        self.assertEqual([x["if_version"] for x in w3], [1, 1])
        self.assertEqual(st3["completed/r"]["v"], 2)

    def test_read_error_reported(self):
        def bad(n, root, now):
            raise RuntimeError("git log: boom")
        w, st, lines = hub_backlog.plan(["r"], {"r": "/r"}, {"backlog/r": {"d": "old", "v": 3}}, NOW, snap=bad)
        self.assertEqual((w, st), ([], {"backlog/r": {"d": "old", "v": 3}}))
        self.assertEqual(lines, ["r: could not read: git log: boom"])


if __name__ == "__main__":
    unittest.main()
