"""kitlib.hubsite.backlog: the live backlog views. A view follows the repository's store with no
publishing run in between, normal reads never fetch and write nothing, a fetch round runs one at a
time, and no state file is written anywhere."""
import datetime
import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from kitlib import hubbacklog
from kitlib.backlog import store as bstore
from kitlib.hubsite import backlog
from tests.gitfixture import TempRepo, fixture_env

NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
RECENT = (NOW - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
LONG_AGO = (NOW - datetime.timedelta(days=400)).strftime("%Y-%m-%d")
WAIT = 30       # seconds a test waits for a thread before it fails


def record(rid, summary, **more):
    lines = ["id: %s" % rid, "summary: %s" % summary]
    lines += ["%s: %s" % kv for kv in more.items()]
    return "\n".join(lines) + "\n"


def tree(folder):
    """{relative path: sha256 of the bytes} for every file under folder, git's own included."""
    out = {}
    for base, _dirs, files in os.walk(folder):
        for name in files:
            full = os.path.join(base, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, folder)] = hashlib.sha256(f.read()).hexdigest()
    return out


def outside_git(listing):
    return {p: h for p, h in listing.items() if p.split(os.sep)[0] != ".git"}


def ids(view, name, key):
    entry = [r for r in view["repos"] if r["repo"] == name][0]
    return [i["id"] for i in entry[key]]


class Case(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="kit-hub-views-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        patch = mock.patch.dict(os.environ, fixture_env({"USERPROFILE": self.home, "HOME": self.home}),
                                clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def main_repo(self, **records):
        """A repository whose store is docs/backlog on main, holding `records` {id: text}."""
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        repo.write(".kit.json", '{"backlog": {"store": "main"}}')
        self.put(repo, "store", _kit=True, **records)
        return repo

    def put(self, repo, message, _kit=False, **records):
        """Commit `records` {id: text, or None to delete the file} to a main store."""
        paths = [".kit.json"] if _kit else []
        for rid, text in records.items():
            rel = "docs/backlog/%s.md" % rid
            if text is None:
                os.remove(repo.path(*rel.split("/")))
            else:
                repo.write(rel, text)
            paths.append(rel)
        repo.commit(message, *paths)

    def views(self, *pairs, **kw):
        kw.setdefault("clock", lambda: NOW)
        return backlog.Views(list(pairs), **kw)


class TheViewFollowsTheStore(Case):

    def test_the_view_changes_when_a_record_changes_with_no_publishing_run(self):
        repo = self.main_repo(A=record("A", "first", priority="2", state="blocked"))
        v = self.views(("r", repo.root))
        self.assertEqual(v.get()["repos"], [{
            "repo": "r", "days": backlog.DAYS, "error": None, "fetched": None, "fetch_error": None,
            "open": [{"id": "A", "t": "first", "p": 2, "s": "blocked"}], "completed": [], "parked": []}])
        self.put(repo, "close A, add B", A=record("A", "first", closed=RECENT), B=record("B", "second"))
        after = v.get()
        self.assertEqual(after["repos"][0]["open"], [{"id": "B", "t": "second", "p": None, "s": "open"}])
        self.assertEqual(after["repos"][0]["completed"], [{"id": "A", "t": "first", "d": RECENT}])
        self.assertEqual(after["at"], NOW.isoformat())

    def test_completed_is_a_closed_line_or_a_deleted_file_within_the_days(self):
        repo = self.main_repo(SHUT=record("SHUT", "closed in place", closed=RECENT),
                              OLD=record("OLD", "closed long ago", closed=LONG_AGO),
                              GONE=record("GONE", "closed by deletion"),
                              README="how records work\n")
        self.put(repo, "close GONE", GONE=None)
        entry = self.views(("r", repo.root)).get()["repos"][0]
        self.assertEqual([(c["id"], c["t"]) for c in entry["completed"]],
                         [("GONE", "closed by deletion"), ("SHUT", "closed in place")])
        self.assertEqual(entry["open"], [])

    def test_parked_records_show_their_question(self):
        repo = self.main_repo(P=record("P", "waits", state="parked", q="which way?", parked_at=RECENT,
                                       session="pm-one", pulled_by="kit/some-branch"),
                              O=record("O", "plain"))
        entry = self.views(("r", repo.root)).get()["repos"][0]
        self.assertEqual(entry["parked"], [{"id": "P", "t": "waits", "q": "which way?", "parked_at": RECENT,
                                            "session": "pm-one", "branch": "kit/some-branch"}])
        self.assertEqual([(o["id"], o["s"]) for o in entry["open"]], [("O", "open"), ("P", "parked")])

    def test_two_hub_clients_see_the_same_view(self):
        repo = self.main_repo(A=record("A", "first"))
        v = self.views(("r", repo.root))
        one, two = v.get(), v.get()
        self.assertEqual(one, two)
        one["repos"][0]["open"].append("scribble")              # a client's copy is its own
        self.assertEqual(v.get(), two)
        self.put(repo, "add B", B=record("B", "second"))
        self.assertEqual(v.get(), v.get())
        self.assertEqual(ids(v.get(), "r", "open"), ["A", "B"])

    def test_a_repo_that_cannot_be_read_does_not_hide_the_others(self):
        good, bad = self.main_repo(A=record("A", "first")), self.main_repo()
        bad.write(".kit.json", '{"backlog": ')
        got = self.views(("bad", bad.root), ("good", good.root), ("absent", None)).get()["repos"]
        self.assertEqual([r["repo"] for r in got], ["bad", "good", "absent"])
        self.assertIn("backlog store", got[0]["error"])
        self.assertEqual((got[0]["open"], got[0]["completed"], got[0]["parked"]), ([], [], []))
        self.assertEqual((got[1]["error"], [o["id"] for o in got[1]["open"]]), (None, ["A"]))
        self.assertEqual(got[2]["error"], backlog.NO_CHECKOUT)

    def test_the_store_is_read_again_only_when_it_moved(self):
        repo = self.main_repo(A=record("A", "first"))
        v = self.views(("r", repo.root))
        with mock.patch.object(hubbacklog, "read_store", wraps=hubbacklog.read_store) as read:
            v.get()
            v.get()
            self.assertEqual(read.call_count, 1)
            self.put(repo, "add B", B=record("B", "second"))
            self.assertEqual(ids(v.get(), "r", "open"), ["A", "B"])
            self.assertEqual(read.call_count, 2)

    def test_the_repo_list_may_be_a_callable_asked_on_every_read(self):
        one, two = self.main_repo(A=record("A", "first")), self.main_repo(B=record("B", "second"))
        known = [("one", one.root)]
        v = backlog.Views(lambda: known, clock=lambda: NOW)
        self.assertEqual([r["repo"] for r in v.get()["repos"]], ["one"])
        known.append(("two", two.root))
        self.assertEqual([r["repo"] for r in v.get()["repos"]], ["one", "two"])

    def test_views_is_the_same_read_without_the_object(self):
        repo = self.main_repo(A=record("A", "first"))
        got = backlog.views([("r", repo.root)], NOW)
        self.assertEqual(got["at"], NOW.isoformat())
        self.assertEqual(ids(got, "r", "open"), ["A"])
        self.assertEqual(sorted(got["repos"][0]), ["completed", "days", "error", "open", "parked", "repo"])


class NothingIsWritten(Case):

    def test_no_state_file_is_written_anywhere(self):
        repo = self.main_repo(A=record("A", "first"), G=record("G", "goes"))
        self.put(repo, "close G", G=None)
        scratch = tempfile.mkdtemp(prefix="kit-hub-views-cwd-")
        self.addCleanup(shutil.rmtree, scratch, True)
        before, here = tree(repo.root), os.getcwd()
        os.chdir(scratch)
        try:
            v = self.views(("r", repo.root), refresh_s=300)
            v.get()
            v.get(fresh=True)
            v.refresh()
            v.get()
        finally:
            os.chdir(here)
        self.assertEqual(ids(v.get(), "r", "completed"), ["G"])     # the reads did happen
        self.assertEqual(tree(repo.root), before)       # the work tree and git's own folder
        self.assertEqual(os.listdir(self.home), [])     # no machine config or state folder
        self.assertEqual(os.listdir(scratch), [])       # nothing beside the process


class ARefStore(Case):
    """A hidden-ref store with a remote: the local ref is one commit behind the remote's."""

    def setUp(self):
        super().setUp()
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        backend = bstore.RefBackend(self.repo.root, "origin", bstore.STORE_REF, bstore.STORE_REF)
        backend.publish(backend.load(None), {"A": record("A", "first")}, "seed A\n")
        behind = self.repo.git("rev-parse", bstore.STORE_REF)
        backend.publish(backend.load(None), {"B": record("B", "second")}, "seed B\n")
        self.repo.git("update-ref", bstore.STORE_REF, behind)
        self.config = os.path.join(self.repo.root, ".git", "config")
        with open(self.config, encoding="utf-8") as f:
            kept = [line for line in f if bstore.STORE_REF not in line]
        with open(self.config, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(kept)
        self.views_ = self.views(("r", self.repo.root))

    def test_a_normal_read_does_not_fetch_and_writes_nothing_in_the_repo(self):
        before = tree(self.repo.root)
        for _ in range(3):
            got = self.views_.get()
        self.assertEqual(ids(got, "r", "open"), ["A"])          # the local ref, not the remote's
        self.assertEqual((got["repos"][0]["fetched"], got["repos"][0]["fetch_error"]), (None, None))
        self.assertEqual(tree(self.repo.root), before)          # git config and refs included

    def test_a_fresh_read_fetches_and_only_git_s_own_folder_changes(self):
        before = tree(self.repo.root)
        got = self.views_.get(fresh=True)
        self.assertEqual(ids(got, "r", "open"), ["A", "B"])
        self.assertEqual(got["repos"][0]["fetched"], NOW.isoformat())
        after = tree(self.repo.root)
        self.assertEqual(outside_git(after), outside_git(before))
        self.assertNotEqual(after, before)
        self.assertEqual(ids(self.views_.get(), "r", "open"), ["A", "B"])   # later plain reads see it

    def test_open_read_names_the_commit_the_store_is_at(self):
        behind = self.repo.git("rev-parse", bstore.STORE_REF)
        self.assertEqual(backlog.open_read(self.repo.root, False), (behind, bstore.STORE_DIR))
        commit, folder = backlog.open_read(self.repo.root, True)
        self.assertNotEqual(commit, behind)
        self.assertEqual((commit, folder), (self.repo.git("rev-parse", bstore.STORE_REF), bstore.STORE_DIR))

    def test_a_repo_with_no_store_yet_reads_as_empty(self):
        bare = TempRepo()
        self.addCleanup(bare.cleanup)
        self.assertEqual(backlog.open_read(bare.root, False), (None, bstore.STORE_DIR))
        entry = self.views(("n", bare.root)).get()["repos"][0]
        self.assertEqual((entry["error"], entry["open"], entry["completed"]), (None, [], []))


class FakeOpener(object):
    """Stands in for open_read: records each call, answers "no store yet". A fetch may be told to
    fail, or to wait until released."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.hold = None                # an Event a fetch waits on
        self.entered = threading.Semaphore(0)
        self.active = self.most = 0
        self.guard = threading.Lock()

    def __call__(self, root, fetch):
        self.calls.append((root, fetch))
        if not fetch:
            return None, "backlog"
        with self.guard:
            self.active += 1
            self.most = max(self.most, self.active)
        try:
            self.entered.release()
            if self.hold is not None and not self.hold.wait(WAIT):
                raise AssertionError("the fetch was never released")
            if root in self.fail:
                raise RuntimeError("backlog store: cannot fetch from origin")
        finally:
            with self.guard:
                self.active -= 1
        return None, "backlog"

    def fetches(self):
        return [root for root, fetch in self.calls if fetch]


class Fetching(unittest.TestCase):

    def setUp(self):
        self.opener = FakeOpener()
        self.v = backlog.Views([("one", "/one"), ("two", "/two"), ("absent", None)], clock=lambda: NOW,
                               opener=self.opener)

    def test_a_normal_read_opens_every_repo_without_a_fetch(self):
        self.v.get()
        self.v.get()
        self.assertEqual(self.opener.calls, [("/one", False), ("/two", False)] * 2)

    def test_a_fresh_read_fetches_each_repo_once_in_order_then_reads(self):
        got = self.v.get(fresh=True)
        self.assertEqual(self.opener.calls, [("/one", True), ("/two", True), ("/one", False), ("/two", False)])
        self.assertEqual([r["fetched"] for r in got["repos"]], [NOW.isoformat(), NOW.isoformat(), None])

    def test_a_failed_fetch_is_named_and_the_local_copy_still_shows(self):
        self.opener.fail.add("/one")
        got = self.v.get(fresh=True)["repos"]
        self.assertEqual((got[0]["error"], got[0]["fetched"]), (None, None))
        self.assertIn("cannot fetch", got[0]["fetch_error"])
        self.assertEqual((got[1]["fetch_error"], got[1]["fetched"]), (None, NOW.isoformat()))
        self.opener.fail.clear()
        self.assertIsNone(self.v.refresh()["repos"][0]["fetch_error"])

    def test_a_fetch_round_never_runs_beside_another(self):
        """Three fresh reads at once: the first fetches; the two that arrived during it share one
        more round, and no two fetches ever overlap."""
        self.opener.hold = threading.Event()
        arrived, lock = threading.Semaphore(0), self.v._fetching

        class Counting(object):
            def __enter__(self):
                arrived.release()
                lock.acquire()

            def __exit__(self, *exc):
                lock.release()

        self.v._fetching = Counting()
        errors = []

        def fresh():
            try:
                self.v.get(fresh=True)
            except BaseException as exc:        # a thread cannot fail a test
                errors.append(repr(exc))

        threads = [threading.Thread(target=fresh, daemon=True) for _ in range(3)]
        threads[0].start()
        self.assertTrue(self.opener.entered.acquire(timeout=WAIT))      # the first round is fetching
        for t in threads[1:]:
            t.start()
        for _ in threads:
            self.assertTrue(arrived.acquire(timeout=WAIT))              # all three reached the lock
        self.opener.hold.set()
        for t in threads:
            t.join(WAIT)
        self.assertEqual(([t.is_alive() for t in threads], errors), ([False] * 3, []))
        self.assertEqual(self.opener.most, 1)
        self.assertEqual(self.opener.fetches(), ["/one", "/two"] * 2)

    def test_the_timer_fetches_every_refresh_s_until_stopped(self):
        self.v.refresh_s = 0.01
        self.assertTrue(self.v.start())
        self.assertFalse(self.v.start())                                # already running
        for _ in range(4):                                              # two rounds of two repos
            self.assertTrue(self.opener.entered.acquire(timeout=WAIT))
        thread = self.v._thread
        self.v.stop(WAIT)
        self.assertFalse(thread.is_alive())
        done = len(self.opener.calls)
        self.assertGreaterEqual(len(self.opener.fetches()), 4)
        self.assertEqual(self.opener.most, 1)
        self.assertFalse(threading.Event().wait(0.05))
        self.assertEqual(len(self.opener.calls), done)                  # nothing after the stop
        self.assertIsNone(self.v.timer_error)

    def test_no_interval_means_no_timer(self):
        for none in (None, 0):
            self.v.refresh_s = none
            self.assertFalse(self.v.start())
            self.assertIsNone(self.v._thread)


class RepoList(unittest.TestCase):

    def test_names_become_pairs_with_the_folder_or_none(self):
        base = tempfile.mkdtemp(prefix="kit-hub-views-list-")
        self.addCleanup(shutil.rmtree, base, True)
        os.makedirs(os.path.join(base, "there", ".git"))
        os.makedirs(os.path.join(base, "pm"))
        self.assertEqual(backlog.repo_list(["there", "gone"], os.path.join(base, "pm")),
                         [("there", os.path.join(base, "there")), ("gone", None)])


if __name__ == "__main__":
    unittest.main()
