import datetime
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib import hubbacklog
from kitlib.git import cmd as gitcmd
from tests.gitfixture import fixture_env
from tests.realgit import real_git


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
        write(self.root, "EX-OPEN-1.md", "id: EX-OPEN-1\nsummary: first open\npriority: 2\nstate: blocked\n")
        write(self.root, "EX-OPEN-2.md", "id: EX-OPEN-2\nsummary: second\npriority: -\n")
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
            {"id": "EX-OPEN-1", "t": "first open", "p": 2, "s": "blocked"},
            {"id": "EX-OPEN-2", "t": "second", "p": None, "s": "open"}]})
        self.assertEqual([(i["id"], i["t"]) for i in c["items"]],
                         [("GONE", "closed by deletion"), ("SHUT", "closed in place")])
        self.assertTrue(c["items"][0]["d"].startswith("2026-10-06T10:00:00"))

    def test_store_from_kit_json(self):
        with open(os.path.join(self.root, ".kit.json"), "w") as f:
            f.write('{"backlog": {"store": "elsewhere"}}')
        b, c = hubbacklog.snapshot("r", self.root, now=NOW)
        self.assertEqual((b["items"], c["items"]), ([], []))

    def test_an_invalid_kit_json_is_a_backlog_store_error(self):
        """store_at raises for a `.kit.json` the store cannot load."""
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


class RefStoreTest(unittest.TestCase):
    """No `.kit.json` store key means the store's own default (the hidden ref), not docs/backlog."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        run(self.root, "init", "-q", "-b", "main")
        run(self.root, "config", "user.email", "t@t")
        run(self.root, "config", "user.name", "t")
        write(self.root, "EX-ON-MAIN.md", "id: EX-ON-MAIN\nsummary: not the store\n")
        run(self.root, "add", "-A")
        run(self.root, "commit", "-qm", "docs/backlog on main is not the store")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_default_store_is_the_ref(self):
        from kitlib.backlog import record, store
        backend = store.RefBackend(self.root, "origin", store.STORE_REF, store.STORE_REF)
        rules = record.Rules()
        backend.publish(backend.load(rules), {
            "EX-OPEN-1": "id: EX-OPEN-1\nsummary: first open\npriority: 2\n",
            "GONE": "id: GONE\nsummary: closed by deletion\n"}, "seed\n")
        backend.publish(backend.load(rules), {"GONE": None}, "retires GONE: gone\n")
        b, c = hubbacklog.snapshot("r", self.root, now=NOW)
        self.assertEqual(b["items"], [{"id": "EX-OPEN-1", "t": "first open", "p": 2, "s": "open"}])
        self.assertEqual([(i["id"], i["t"]) for i in c["items"]], [("GONE", "closed by deletion")])

    def test_no_store_yet_is_empty(self):
        b, c = hubbacklog.snapshot("r", self.root, now=NOW)
        self.assertEqual((b["items"], c["items"]), ([], []))


# A git that is a launcher whose child holds the pipes (see tests/git/test_launcher.py): a bare
# subprocess timeout kills only the launcher and then waits on the pipes until the child ends.
LAUNCHER = (
    "import subprocess, sys\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
    "                     stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)\n"
    "sys.exit(c.wait())\n")


@real_git("the process tree a timed-out call ends (git is stubbed by a python launcher)")
class GitTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.started = []
        real = subprocess.Popen

        def fake(args, *a, **kw):
            args = list(args)
            if args and args[0] == "git":
                self.started.append(real([sys.executable, "-c", LAUNCHER], *a, **kw))
                return self.started[-1]
            return real(args, *a, **kw)
        patcher = mock.patch.object(subprocess, "Popen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        timeout = mock.patch.object(hubbacklog, "TIMEOUT", 3)
        timeout.start()
        self.addCleanup(timeout.stop)

    def cut_off(self, call):
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            call()
        self.assertLess(time.monotonic() - t0, 15)      # the child sleeps 30 holding the pipes
        self.assertIn("timed out", str(ctx.exception))

    def test_git_is_cut_off_when_a_child_holds_the_pipes(self):
        self.cut_off(lambda: hubbacklog.git(self.tmp.name, "status"))

    def test_main_branch_probe_is_cut_off(self):
        self.cut_off(lambda: hubbacklog.main_branch(self.tmp.name, {}))


if __name__ == "__main__":
    unittest.main()
