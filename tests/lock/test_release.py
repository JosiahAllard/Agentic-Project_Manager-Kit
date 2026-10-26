"""kitlib.lock.release (tools/lock_release_check.py): the lock release pre-check. A
change to the lock tool reaches the release checkout (a fast-forward or a merge) only while no
run is live in the store.

Every case runs on a scratch store (helpers.StoreCase) and a throwaway repository
(tests/gitfixture.py); the machine's store is never read or written. A run is live when its
wrapper (pid and creation time) is a live process: a child this test starts for another
session's run, this test process (or an ancestor) for the caller's own run, and this process's
pid with another creation time for a gone one.
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests import gitfixture

from .helpers import REPO, StoreCase
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.lock import release
    from kitlib.lock.store import real_root
    from kitlib.proc import process

TOOL = os.path.join(REPO, "tools", "lock_release_check.py")


def reached_kitlib_files(root, entry="tools/machine_lock.py"):
    """Repo-relative .py files of kitlib that `entry` reaches through imports at any depth
    (function-level ones too), with each package's __init__.py on the way."""
    seen, todo = set(), [entry]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        pkg = rel.rsplit("/", 1)[0].replace("/", ".") if rel.startswith("kitlib/") else ""
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".")
                    parts = parts[:len(parts) - (node.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                names = [base] + ["%s.%s" % (base, a.name) for a in node.names]
            for name in names:
                if not name.startswith("kitlib"):
                    continue
                parts = name.split(".")
                for i in range(1, len(parts) + 1):
                    stem = "/".join(parts[:i])
                    for cand in (stem + "/__init__.py", stem + ".py"):
                        if os.path.isfile(os.path.join(root, *cand.split("/"))):
                            todo.append(cand)
    return sorted(r for r in seen if r != entry)


def ff(kit, sha):
    return 'git -C "%s" merge --ff-only %s' % (kit, sha)


class LockPaths(unittest.TestCase):
    """LOCK_PATHS is what a release must not change while a run is live: the entry point and every
    kitlib module it imports. A new import outside them fails here, not in a live release."""

    @windows_only("the lock is Windows only")
    def test_every_module_the_lock_tool_reaches_is_a_lock_path(self):
        reached = reached_kitlib_files(REPO)
        self.assertIn("kitlib/lock/store.py", reached)       # the walk itself works
        missing = [r for r in reached if not release.is_lock_path(r)]
        self.assertEqual(missing, [], "lock tool modules outside release.LOCK_PATHS")
        self.assertTrue(release.is_lock_path("tools/machine_lock.py"))
        self.assertTrue(release.is_lock_path("kitlib/lock/kinds.json"))

    @windows_only("the lock is Windows only")
    def test_every_lock_path_exists(self):
        for p in release.LOCK_PATHS:
            self.assertTrue(os.path.exists(os.path.join(REPO, *p.rstrip("/").split("/"))), p)

    @windows_only("the lock is Windows only")
    def test_other_paths_are_not_lock_paths(self):
        for p in ("docs/go-live.md", "kitlib/lockmcp/server.py", "kitlib/locker.py",
                  "tools/machine_lock.pyc", "tools/gate.py", "kitlib/gate/land.py"):
            self.assertFalse(release.is_lock_path(p), p)

    @windows_only("the lock is Windows only")
    def test_backslash_paths_are_normalised(self):
        self.assertTrue(release.is_lock_path("kitlib\\lock\\store.py"))
        self.assertTrue(release.is_lock_path("tools\\machine_lock.py"))
        self.assertFalse(release.is_lock_path("kitlib\\lockmcp\\server.py"))


@windows_only("the lock is Windows only")
class OwnerLive(unittest.TestCase):
    """release._owner_live(d, alive): False only when alive() says the owner is surely gone."""

    def test_a_gone_owner_is_not_live(self):
        self.assertFalse(release._owner_live({"pid": 1234, "ctime": 5}, lambda p, c: False))
        self.assertFalse(release._owner_live({"pid": 1234}, lambda p, c: False))

    def test_what_cannot_name_a_process_is_live(self):
        never = lambda p, c: False                           # noqa: E731
        for d in (None, "x", {}, {"pid": "1234"}, {"pid": 12.0}, {"pid": True}, {"pid": 0},
                  {"pid": -4}, {"pid": 1234, "ctime": "5"}, {"pid": 1234, "ctime": 5.5}):
            self.assertTrue(release._owner_live(d, never), d)

    def test_an_alive_that_fails_counts_as_live(self):
        for err in (OSError(5, "denied"), ValueError("v"), TypeError("t"), OverflowError("o")):
            def boom(p, c, err=err):
                raise err
            self.assertTrue(release._owner_live({"pid": 1234, "ctime": 5}, boom), err)

    def test_an_unexpected_failure_is_not_swallowed(self):
        def boom(p, c):
            raise RuntimeError("bug")
        with self.assertRaises(RuntimeError):
            release._owner_live({"pid": 1234, "ctime": 5}, boom)


class RunCase(StoreCase):
    def other_run(self, rid, **fields):
        """A live record owned by a child process: another session's run, not the caller's."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return self.live_record(rid, pid=p.pid, ctime=process.proc_ctime(p.pid), **fields)

    def make_root(self):
        os.makedirs(self.store.root, exist_ok=True)


class LiveRuns(RunCase):
    """release.live_runs(store, mine): (live, gone, own), the store's runs as 'active/<file>' or
    'queue.json:<id>'; live unless surely gone; what cannot be read counts as live; own is the
    live ones whose owner is in mine."""

    def entry(self, rid, **fields):
        e = {"id": rid, "ticket": 1, "state": "held", "pid": os.getpid(),
             "ctime": process.own_ctime(), "need": {"cpu": 1, "gpu": 0}}
        e.update(fields)
        return e

    def write_queue(self, entries):
        self.make_root()
        with open(self.store.path("queue.json"), "w", encoding="utf-8") as f:
            json.dump({"ticket": 1, "entries": entries}, f)

    def test_an_empty_store_has_no_runs(self):
        self.make_root()
        self.assertEqual(release.live_runs(self.store), ([], [], []))

    def test_a_missing_store_folder_counts_as_live(self):
        live, gone, own = release.live_runs(self.store)
        self.assertEqual(live, ["store folder (missing)"])
        self.assertEqual((gone, own), ([], []))

    def test_a_live_record_is_live_and_a_gone_one_is_not(self):
        self.live_record("r-20261008T120000Z-aaaaaa")
        self.live_record("r-20261008T120000Z-bbbbbb", ctime=process.own_ctime() + 1)
        live, gone, _ = release.live_runs(self.store)
        self.assertEqual(live, ["active/r-20261008T120000Z-aaaaaa.json"])
        self.assertEqual(gone, ["active/r-20261008T120000Z-bbbbbb.json"])

    def test_own_runs_are_the_live_ones_mine_names(self):
        self.live_record("r-20261008T120000Z-aaaaaa")
        self.other_run("r-20261008T120000Z-cccccc")
        mine = {(os.getpid(), process.own_ctime())}
        live, _, own = release.live_runs(self.store, mine=mine)
        self.assertEqual(live, ["active/r-20261008T120000Z-aaaaaa.json",
                                "active/r-20261008T120000Z-cccccc.json"])
        self.assertEqual(own, ["active/r-20261008T120000Z-aaaaaa.json"])

    def test_an_unreadable_or_ownerless_record_counts_as_live(self):
        os.makedirs(self.store.path("active"), exist_ok=True)
        with open(self.store.path("active", "r-20261008T120000Z-cccccc.json"), "w") as f:
            f.write("{not json")
        self.live_record("r-20261008T120000Z-dddddd", pid=None)
        live, gone, _ = release.live_runs(self.store)
        self.assertEqual(live, ["active/r-20261008T120000Z-cccccc.json",
                                "active/r-20261008T120000Z-dddddd.json"])
        self.assertEqual(gone, [])

    def test_an_unlistable_active_folder_counts_as_live(self):
        self.make_root()
        with open(self.store.path("active"), "w") as f:       # a file where the folder goes
            f.write("x")
        live, _, _ = release.live_runs(self.store)
        self.assertEqual(live, ["active/ (unreadable)"])

    def test_queue_entries_live_gone_and_malformed(self):
        self.write_queue([self.entry("r-1"), self.entry("r-2", ctime=process.own_ctime() + 1),
                          self.entry("r-3", state="bg"), {"id": "r-4", "state": "waiting"},
                          "not an entry"])
        live, gone, _ = release.live_runs(self.store)
        self.assertEqual(live, ["queue.json:r-1", "queue.json:r-3", "queue.json:r-4",
                                "queue.json:?"])
        self.assertEqual(gone, ["queue.json:r-2"])

    def test_an_unreadable_queue_counts_as_live(self):
        self.make_root()
        with open(self.store.path("queue.json"), "w", encoding="utf-8") as f:
            f.write("[1, 2")
        live, _, _ = release.live_runs(self.store)
        self.assertEqual(live, ["queue.json (unreadable)"])

    def test_it_writes_nothing_to_the_store(self):
        self.live_record("r-20261008T120000Z-eeeeee")
        self.write_queue([self.entry("r-1")])
        before = self.listing()
        release.live_runs(self.store)
        self.assertEqual(self.listing(), before)

    def listing(self):
        out = {}
        for d, _dirs, files in os.walk(self.store.root):
            for n in files:
                p = os.path.join(d, n)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.store.root)] = f.read()
        return out


class Check(RunCase):
    """release.check(kit, commit, store): refused (1) when moving the release checkout to commit
    changes a lock path and a run is live; else 0 with the command to run at once. 2 for usage,
    3 for an unexpected failure."""

    def setUp(self):
        super().setUp()
        self.fx = gitfixture.TempRepo()
        self.addCleanup(self.fx.cleanup)
        self.kit = self.fx.root
        self.fx.write("kitlib/lock/store.py", "x = 1\n")
        self.fx.write("tools/machine_lock.py", "y = 1\n")
        self.head = self.fx.commit("lock", "kitlib/lock/store.py", "tools/machine_lock.py")

    def branch_commit(self, rel, text, message="change", base=None):
        """A commit on a side branch (from base, default HEAD) changing rel; HEAD stays put."""
        self.fx.git("checkout", "-q", "-b", "side-%s" % message.replace(" ", "-"), base or "HEAD")
        self.fx.write(rel, text)
        sha = self.fx.commit(message, rel)
        self.fx.git("checkout", "-q", "main")
        return sha

    def test_a_lock_change_with_a_live_run_is_refused(self):
        commit = self.branch_commit("kitlib/lock/store.py", "x = 2\n")
        self.other_run("r-20261008T120000Z-aaaaaa")
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.REFUSED)
        text = "\n".join(out.lines)
        self.assertIn("kitlib/lock/store.py", text)
        self.assertIn("active/r-20261008T120000Z-aaaaaa.json", text)
        self.assertNotIn("your own", text)
        self.assertIsNone(out.command)

    def test_the_callers_own_run_is_refused_with_its_own_reason(self):
        commit = self.branch_commit("kitlib/lock/store.py", "x = 2\n")
        self.live_record("r-20261008T120000Z-aaaaaa")
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertIn("STOP: this check runs inside a lock run (your own process or one of its "
                      "ancestors): active/r-20261008T120000Z-aaaaaa.json", out.lines)

    def test_an_ancestors_run_is_the_callers_own(self):
        # Nearest first, and only live ones: an exited ancestor whose process object is still
        # held open keeps its ctime but is gone, and its run is rightly ignored.
        anc = [a for a in process.ancestors_of(os.getpid(), process.own_ctime())
               if process.alive(*a)]
        if not anc:
            self.skipTest("this process has no live ancestor it can open")
        commit = self.branch_commit("kitlib/lock/store.py", "x = 2\n")
        self.live_record("r-20261008T120000Z-aaaaaa", pid=anc[0][0], ctime=anc[0][1])
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertIn("your own process", "\n".join(out.lines))

    def test_a_missing_store_folder_stops(self):
        commit = self.branch_commit("kitlib/lock/store.py", "x = 2\n")
        self.assertFalse(os.path.exists(self.store.root))
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertIn("STOP: the lock store %s does not exist; cannot tell whether a run is live."
                      % self.store.root, out.lines)

    def test_a_lock_change_with_no_live_run_passes_with_the_fast_forward(self):
        commit = self.branch_commit("tools/machine_lock.py", "y = 2\n")
        self.live_record("r-20261008T120000Z-bbbbbb", ctime=process.own_ctime() + 1)
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.OK, out.lines)
        self.assertEqual(out.command, ff(self.kit, commit))
        self.assertIn("ignored (its process is gone): active/r-20261008T120000Z-bbbbbb.json",
                      out.lines)

    def test_a_queue_entry_alone_refuses(self):
        commit = self.branch_commit("kitlib/lock/store.py", "x = 3\n")
        self.make_root()
        with open(self.store.path("queue.json"), "w", encoding="utf-8") as f:
            json.dump({"ticket": 1, "entries": [{"id": "r-q", "state": "waiting", "pid": os.getpid(),
                                                 "ctime": process.own_ctime(),
                                                 "need": {"cpu": 1, "gpu": 0}}]}, f)
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertIn("queue.json:r-q", "\n".join(out.lines))

    def test_a_change_outside_the_lock_passes_while_a_run_is_live(self):
        commit = self.branch_commit("docs/x.md", "doc\n")
        self.other_run("r-20261008T120000Z-cccccc")
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.OK, out.lines)
        self.assertEqual(out.command, ff(self.kit, commit))

    def test_a_rename_out_of_the_lock_is_a_lock_change(self):
        self.fx.git("checkout", "-q", "-b", "side-rename")
        self.fx.git("mv", "kitlib/lock/store.py", "store.py")
        self.fx.git("commit", "-q", "-m", "rename")
        commit = self.fx.git("rev-parse", "HEAD")
        self.fx.git("checkout", "-q", "main")
        self.other_run("r-20261008T120000Z-cccccc")
        out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertIn("  kitlib/lock/store.py", out.lines)

    def test_a_merge_that_brings_a_lock_change_is_refused(self):
        side = self.branch_commit("kitlib/lock/store.py", "x = 4\n", "side lock")
        self.fx.write("docs/y.md", "main moved\n")
        self.fx.commit("main moves", "docs/y.md")
        self.other_run("r-20261008T120000Z-dddddd")
        out = release.check(self.kit, side, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertIn("kitlib/lock/store.py", "\n".join(out.lines))

    def test_a_merge_with_no_live_run_passes_with_the_merge_command(self):
        side = self.branch_commit("kitlib/lock/store.py", "x = 5\n", "side lock")
        self.fx.write("docs/y.md", "main moved\n")
        self.fx.commit("main moves", "docs/y.md")
        self.make_root()
        out = release.check(self.kit, side, self.store)
        self.assertEqual(out.code, release.OK, out.lines)
        self.assertEqual(out.command, 'git -C "%s" merge --no-ff %s' % (self.kit, side))

    def test_a_commit_the_release_already_holds_needs_nothing(self):
        self.live_record("r-20261008T120000Z-eeeeee")
        for rev in (self.head, self.fx.initial):
            out = release.check(self.kit, rev, self.store)
            self.assertEqual(out.code, release.OK, out.lines)
            self.assertIsNone(out.command)

    def test_an_unknown_commit_or_option_is_usage(self):
        for rev in ("no-such-branch", "--help", ""):
            self.assertEqual(release.check(self.kit, rev, self.store).code, release.USAGE, rev)

    def test_no_checkout_is_usage(self):
        missing = os.path.join(self.fx.base, "nope")
        self.assertEqual(release.check(missing, "HEAD", self.store).code, release.USAGE)

    def test_release_plan_says_why_it_cannot_tell(self):
        not_repo = os.path.join(self.fx.base, "plain")
        os.makedirs(not_repo)
        _, _, why = release.release_plan(not_repo, self.head)
        self.assertTrue(why)
        _, _, why = release.release_plan(self.kit, "0" * 40)
        self.assertTrue(why)

    def test_unrelated_history_stops(self):
        self.fx.git("checkout", "-q", "--orphan", "lonely")
        self.fx.git("rm", "-rqf", ".")
        self.fx.write("kitlib/lock/store.py", "x = 9\n")
        lonely = self.fx.commit("lonely", "kitlib/lock/store.py")
        self.fx.git("checkout", "-q", "-f", "main")
        self.make_root()
        self.assertEqual(release.release_plan(self.kit, lonely),
                         (None, None, "%s and HEAD share no history" % lonely))
        out = release.check(self.kit, lonely, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertEqual(out.lines, ["STOP: cannot tell what releasing %s would change: %s and "
                                     "HEAD share no history" % (lonely, lonely)])

    def test_a_failed_diff_stops(self):
        commit = self.branch_commit("kitlib/lock/store.py", "x = 2\n")
        self.assertEqual(release.changed_paths(self.kit, "0" * 40, commit)[0], None)
        self.assertTrue(release.changed_paths(self.kit, "0" * 40, commit)[1])
        self.make_root()
        with mock.patch.object(release, "changed_paths", return_value=(None, "boom")):
            out = release.check(self.kit, commit, self.store)
        self.assertEqual(out.code, release.REFUSED)
        self.assertEqual(out.lines, ["STOP: cannot list what releasing %s would change: boom"
                                     % commit])

    def test_an_unexpected_failure_is_error_not_ok_or_stop(self):
        commit = self.branch_commit("kitlib/lock/store.py", "x = 2\n")
        with mock.patch.object(release, "default_store", side_effect=RuntimeError("no store")):
            out = release.check(self.kit, commit)
        self.assertEqual(out.code, release.ERROR)
        self.assertNotIn(release.ERROR, (release.OK, release.REFUSED, release.USAGE))
        self.assertIn("RuntimeError: no store", "\n".join(out.lines))
        self.assertIsNone(out.command)

        def boom(p, c):
            raise RuntimeError("alive broke")
        self.other_run("r-20261008T120000Z-aaaaaa")
        out = release.check(self.kit, commit, self.store, alive=boom)
        self.assertEqual(out.code, release.ERROR)
        self.assertIn("RuntimeError: alive broke", "\n".join(out.lines))

    def test_the_default_store_is_the_machines(self):
        self.assertEqual(os.path.normcase(release.default_store().root),
                         os.path.normcase(real_root()))


class Cli(StoreCase):
    """tools/lock_release_check.py: parses, calls release.check, prints, exits with its code."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        spec = importlib.util.spec_from_file_location("lock_release_check_under_test", TOOL)
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def setUp(self):
        super().setUp()
        self.fx = gitfixture.TempRepo()
        self.addCleanup(self.fx.cleanup)
        self.fx.git("checkout", "-q", "-b", "side")
        self.fx.write("kitlib/proc/job.py", "z = 1\n")
        self.commit = self.fx.commit("proc", "kitlib/proc/job.py")
        self.fx.git("checkout", "-q", "main")

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.tool.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_refused_then_ok(self):
        self.live_record("r-20261008T120000Z-ffffff")
        code, out, _ = self.call("-C", self.fx.root, self.commit, "--test-root", self.root)
        self.assertEqual(code, 1, out)
        self.assertIn("STOP", out)
        os.remove(self.store.path("active", "r-20261008T120000Z-ffffff.json"))
        code, out, _ = self.call("-C", self.fx.root, self.commit, "--test-root", self.root)
        self.assertEqual(code, 0, out)
        self.assertIn(ff(self.fx.root, self.commit), out)

    def test_usage(self):
        self.assertEqual(self.call()[0], 2)
        self.assertEqual(self.call("-C", self.fx.root, "no-such-rev", "--test-root", self.root)[0], 2)

    def test_an_unexpected_failure_exits_3(self):
        with mock.patch.object(self.tool.release, "default_store",
                               side_effect=RuntimeError("no store")):
            code, out, _ = self.call("-C", self.fx.root, self.commit)
        self.assertEqual(code, 3, out)
        self.assertIn("ERROR", out)
        with mock.patch.object(self.tool, "Store", side_effect=RuntimeError("bad root")):
            code, out, _ = self.call("-C", self.fx.root, self.commit, "--test-root", self.root)
        self.assertEqual(code, 3, out)
        self.assertIn("RuntimeError: bad root", out)


if __name__ == "__main__":
    unittest.main()
