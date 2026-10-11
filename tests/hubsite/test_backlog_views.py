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
from kitlib.git import cmd as gitcmd
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

    def put(self, repo, message, _kit=False, _when=None, **records):
        """Commit `records` {id: text, or None to delete the file} to a main store; `_when` is
        the commit's date."""
        paths = [".kit.json"] if _kit else []
        for rid, text in records.items():
            rel = "docs/backlog/%s.md" % rid
            if text is None:
                os.remove(repo.path(*rel.split("/")))
            else:
                repo.write(rel, text)
            paths.append(rel)
        dated = {"GIT_COMMITTER_DATE": _when, "GIT_AUTHOR_DATE": _when} if _when else {}
        with mock.patch.dict(os.environ, dated):
            repo.commit(message, *paths)

    def views(self, *pairs, **kw):
        kw.setdefault("clock", lambda: NOW)
        kw.setdefault("reuse_s", 0)         # every get() opens the store again
        return backlog.Views(list(pairs), **kw)


class TheViewFollowsTheStore(Case):

    def test_the_view_changes_when_a_record_changes_with_no_publishing_run(self):
        repo = self.main_repo(A=record("A", "first", priority="2", state="blocked"))
        v = self.views(("r", repo.root))
        first = v.get()
        self.assertEqual((first["at"], first["error"]), (NOW.isoformat(), None))
        self.assertEqual(first["repos"], [{
            "repo": "r", "days": backlog.DAYS, "error": None, "stale": False, "fetched": None,
            "fetch_error": None,
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
        # As the backlog tool leaves a parked record: parking strips the pull's fields.
        repo = self.main_repo(P=record("P", "waits", priority="3", state="parked", session="pm-one",
                                       q="Q-012", parked_at=RECENT),
                              O=record("O", "plain"))
        entry = self.views(("r", repo.root)).get()["repos"][0]
        self.assertEqual(entry["parked"], [{"id": "P", "t": "waits", "q": "Q-012", "parked_at": RECENT,
                                            "session": "pm-one"}])
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
        self.assertEqual((got[0]["open"], got[0]["completed"], got[0]["parked"], got[0]["stale"]),
                         ([], [], [], False))                   # a first read that failed
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
        self.assertEqual(sorted(got), ["at", "error", "repos"])
        self.assertEqual(sorted(got["repos"][0]),
                         ["completed", "days", "error", "open", "parked", "repo", "stale"])


def no_answer(*subcommands):
    """A stand-in for kitlib.git.cmd.call under which git gives no answer to `subcommands`: exit 1
    and no output, as a git ended from outside leaves."""
    real = gitcmd.call

    def call(args, cwd, *a, **kw):
        if args and args[0] in subcommands:
            return gitcmd.Result(1, b"", "")
        return real(args, cwd, *a, **kw)
    return mock.patch.object(gitcmd, "call", call)


class AFailureIsNeverAnEmptyStore(Case):

    def test_a_git_that_gives_no_answer_is_an_error_not_an_empty_store(self):
        main, hidden = self.main_repo(A=record("A", "first")), TempRepo()
        self.addCleanup(hidden.cleanup)
        with no_answer("for-each-ref"):
            for repo in (main, hidden):
                with self.assertRaises(RuntimeError):
                    backlog.open_read(repo.root, False)
            got = self.views(("main", main.root), ("hidden", hidden.root)).get()["repos"]
        for entry in got:
            self.assertIn("exited 1", entry["error"])
            self.assertEqual((entry["open"], entry["stale"]), ([], False))
        self.assertEqual(backlog.open_read(hidden.root, False), (None, bstore.STORE_DIR, False))

    def test_a_misnamed_main_branch_is_an_error_not_an_empty_store(self):
        repo = self.main_repo(A=record("A", "first"))
        repo.write(".kit.json", '{"backlog": {"store": "main"}, "repo": {"main_branch": "trunk"}}')
        entry = self.views(("r", repo.root)).get()["repos"][0]
        self.assertIn("trunk", entry["error"])
        self.assertEqual(entry["open"], [])

    def test_a_failed_read_keeps_the_last_good_lists_and_says_stale(self):
        repo = self.main_repo(A=record("A", "first"), S=record("S", "shut", closed=RECENT))
        v = self.views(("r", repo.root))
        good = v.get()["repos"][0]
        self.assertEqual((good["error"], good["stale"]), (None, False))
        with no_answer("for-each-ref"):
            bad = v.get()["repos"][0]
        self.assertEqual((bad["stale"], bad["open"], bad["completed"], bad["parked"]),
                         (True, good["open"], good["completed"], good["parked"]))
        self.assertIn("exited 1", bad["error"])
        self.assertEqual(v.get()["repos"][0], good)             # and it mends on the next good read

    def test_no_error_names_a_folder_of_this_machine(self):
        repo = self.main_repo(A=record("A", "first"))
        repo.write(".kit.json", '{"backlog": ')
        gone = os.path.join(self.home, "no-such-repo")
        got = self.views(("named", repo.root), ("lost", gone)).get()["repos"]
        for entry, root in zip(got, (repo.root, gone)):
            self.assertTrue(entry["error"])
            for spelt in (root, root.replace("\\", "/"), os.path.basename(root)):
                self.assertNotIn(spelt.lower(), entry["error"].lower())
        self.assertIn("named", got[0]["error"])

    def test_say_puts_the_name_for_the_folder_and_hides_an_address(self):
        said = backlog.say(RuntimeError("git ls-tree in c:/Work\\Repo/.git: no; see https://host.invalid/a/b.git "
                                        "and git@host.invalid:a/b.git"), "kit", "C:\\work\\repo")
        self.assertEqual(said, "git ls-tree in kit/.git: no; see <address> and <address>")
        self.assertEqual(backlog.say(ValueError(), "kit", None), "ValueError")
        self.assertEqual(backlog.fetch_say(RuntimeError("anything at all")), backlog.FETCH_FAILED)


class TheCompletedWindow(Case):
    """`days` days back from the UTC date of the read, from that day's midnight UTC."""

    DAY = datetime.datetime(2026, 3, 10, tzinfo=datetime.timezone.utc)

    def test_the_window_does_not_depend_on_the_time_of_day_of_the_read(self):
        repo = self.main_repo(IN=record("IN", "closed on the first day", closed="2026-03-03"),
                              OUT=record("OUT", "closed the day before", closed="2026-03-02"),
                              EDGE=record("EDGE", "deleted just after midnight"),
                              EARLY=record("EARLY", "deleted just before midnight"),
                              _when="2026-03-01T12:00:00+00:00")
        self.put(repo, "close EARLY", EARLY=None, _when="2026-03-02T23:59:30+00:00")
        self.put(repo, "close EDGE", EDGE=None, _when="2026-03-03T00:00:30+00:00")
        for now in (self.DAY.replace(minute=1), self.DAY.replace(hour=23, minute=59)):
            with mock.patch.object(hubbacklog, "deleted", wraps=hubbacklog.deleted) as deleted:
                entry = self.views(("r", repo.root), clock=lambda: now, days=7).get()["repos"][0]
            self.assertEqual(deleted.call_args[0][3], "2026-03-03T00:00:00+00:00")
            self.assertEqual((sorted(c["id"] for c in entry["completed"]), entry["days"]),
                             (["EDGE", "IN"], 7))

    def test_the_first_day_is_a_utc_date(self):
        self.assertEqual(backlog.window(self.DAY.replace(hour=23, minute=59), 7), "2026-03-03")
        self.assertEqual(backlog.window(self.DAY), "2025-12-10")            # DAYS, 90
        east = datetime.timezone(datetime.timedelta(hours=14))
        self.assertEqual(backlog.window(datetime.datetime(2026, 3, 11, 9, 0, tzinfo=east), 7), "2026-03-03")

    def test_a_new_day_reads_the_store_again(self):
        repo = self.main_repo(S=record("S", "shut", closed="2026-03-03"))
        clock = Clock()
        clock.now = self.DAY
        v = self.views(("r", repo.root), clock=clock, days=7)
        self.assertEqual(ids(v.get(), "r", "completed"), ["S"])
        clock.now += datetime.timedelta(days=1)                 # the window now opens on 03-04
        self.assertEqual(ids(v.get(), "r", "completed"), [])


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
        self.assertEqual(backlog.open_read(self.repo.root, False), (behind, bstore.STORE_DIR, False))
        commit, folder, fetched = backlog.open_read(self.repo.root, True)
        self.assertNotEqual(commit, behind)
        self.assertEqual((commit, folder, fetched),
                         (self.repo.git("rev-parse", bstore.STORE_REF), bstore.STORE_DIR, True))

    def test_a_repo_with_no_store_yet_reads_as_empty(self):
        bare = TempRepo()
        self.addCleanup(bare.cleanup)
        self.assertEqual(backlog.open_read(bare.root, False), (None, bstore.STORE_DIR, False))
        entry = self.views(("n", bare.root)).get()["repos"][0]
        self.assertEqual((entry["error"], entry["open"], entry["completed"]), (None, [], []))

    def test_a_store_with_nothing_to_fetch_is_never_stamped_fetched(self):
        local = TempRepo()                                      # a ref store, no remote at all
        self.addCleanup(local.cleanup)
        main = self.main_repo(A=record("A", "first"))
        got = self.views(("local", local.root), ("main", main.root), ("r", self.repo.root)).get(fresh=True)
        self.assertEqual([(r["fetched"], r["fetch_error"], r["error"]) for r in got["repos"]],
                         [(None, None, None), (None, None, None), (NOW.isoformat(), None, None)])

    def test_a_failed_fetch_says_so_in_fixed_words_and_the_local_copy_still_shows(self):
        gone = (self.repo.root + "-no-such-remote").replace("\\", "/")
        with open(self.config, encoding="utf-8") as f:
            lines = ["\turl = %s\n" % gone if line.strip().startswith("url =") else line for line in f]
        with open(self.config, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(lines)
        entry = self.views_.get(fresh=True)["repos"][0]
        self.assertTrue(entry["fetch_error"].startswith(backlog.FETCH_FAILED), entry["fetch_error"])
        self.assertLessEqual(len(entry["fetch_error"]), len(backlog.FETCH_FAILED) + 20)
        self.assertNotIn("no-such-remote", entry["fetch_error"])
        self.assertEqual((entry["error"], entry["fetched"], [o["id"] for o in entry["open"]]),
                         (None, None, ["A"]))


class Clock(object):
    """A clock a test moves: `now`, and `fail` to make the next readings raise."""

    def __init__(self):
        self.now = NOW
        self.fail = False

    def __call__(self):
        if self.fail:
            raise RuntimeError("the clock is broken")
        return self.now


class Pause(object):
    """Stands in for the timer's wait between rounds, so no test waits on real time: `ticks`
    pauses end at once, the next one lasts until the timer is stopped (WAIT only bounds a test
    that has already failed). `hook` runs at the start of every pause."""

    def __init__(self):
        self.ticks = 0
        self.seconds = []
        self.hook = None
        self.parked = threading.Semaphore(0)

    def __call__(self, event, seconds):
        self.seconds.append(seconds)
        if self.hook:
            self.hook()
        if self.ticks > 0:
            self.ticks -= 1
            return event.is_set()
        self.parked.release()
        return event.wait(WAIT)


class FakeOpener(object):
    """Stands in for open_read: records each call, answers "no store yet". A fetch may be told to
    fail, or to wait until released."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.words = "backlog store: cannot fetch from origin"
        self.fetched = True             # what a fetch answers as `fetched`
        self.hold = None                # an Event a fetch waits on
        self.entered = threading.Semaphore(0)
        self.active = self.most = 0
        self.guard = threading.Lock()

    def __call__(self, root, fetch):
        self.calls.append((root, fetch))
        if not fetch:
            return None, "backlog", False
        with self.guard:
            self.active += 1
            self.most = max(self.most, self.active)
        try:
            self.entered.release()
            if self.hold is not None and not self.hold.wait(WAIT):
                raise AssertionError("the fetch was never released")
            if root in self.fail:
                raise RuntimeError(self.words)
        finally:
            with self.guard:
                self.active -= 1
        return None, "backlog", self.fetched

    def fetches(self):
        return [root for root, fetch in self.calls if fetch]


class Fetching(unittest.TestCase):

    def setUp(self):
        self.opener = FakeOpener()
        self.pause = Pause()
        self.clock = Clock()
        self.v = backlog.Views([("one", "/one"), ("two", "/two"), ("absent", None)], clock=self.clock,
                               opener=self.opener, reuse_s=0, refresh_s=300, wait=self.pause)
        self.addCleanup(self.v.stop, WAIT)

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
        self.assertEqual(got[0]["fetch_error"], backlog.FETCH_FAILED)
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

    def test_a_fetch_error_never_carries_git_s_words(self):
        self.opener.fail.add("/one")
        self.opener.words = ("backlog store: cannot fetch the backlog store from origin: fatal: unable "
                             "to access 'https://host.invalid/team/repo.git/': git fetch exited 128")
        entry = self.v.get(fresh=True)["repos"][0]
        self.assertEqual(entry["fetch_error"], backlog.FETCH_FAILED + " (git exited 128)")

    def test_fetched_is_stamped_only_when_a_fetch_ran(self):
        self.opener.fetched = False         # a main store, or a store with no remote
        got = self.v.get(fresh=True)["repos"]
        self.assertEqual([(r["fetched"], r["fetch_error"]) for r in got], [(None, None)] * 3)
        self.opener.fetched = True
        self.assertEqual([r["fetched"] for r in self.v.refresh()["repos"]],
                         [NOW.isoformat(), NOW.isoformat(), None])

    def test_a_read_inside_reuse_s_starts_no_git(self):
        self.v.reuse_s = 5
        first = self.v.get()
        self.assertEqual(self.v.get(), first)
        self.clock.now += datetime.timedelta(seconds=4)
        self.v.get()
        self.assertEqual(self.opener.calls, [("/one", False), ("/two", False)])     # opened once
        self.clock.now += datetime.timedelta(seconds=2)
        self.v.get()
        self.assertEqual(self.opener.calls, [("/one", False), ("/two", False)] * 2)
        del self.opener.calls[:]
        self.v.get(fresh=True)
        self.v.get()
        self.assertEqual(self.opener.calls, [("/one", True), ("/two", True)])   # the round's open serves

    def test_a_repo_list_that_raises_gives_the_last_good_answer_with_an_error(self):
        known, broken = [("one", "/one")], []

        def repos():
            if broken:
                raise OSError("cannot read C:\\somewhere\\private")
            return known

        v = backlog.Views(repos, clock=self.clock, opener=self.opener, reuse_s=0)
        good = v.get()
        self.assertEqual(([r["repo"] for r in good["repos"]], good["error"]), (["one"], None))
        broken.append(True)
        for got in (v.get(), v.get(fresh=True), v.refresh()):
            self.assertEqual(got["repos"], good["repos"])
            self.assertEqual(got["error"], backlog.NO_REPOS + ": OSError")
        del broken[:]
        self.assertIsNone(v.get()["error"])

    def test_a_repo_list_that_never_worked_is_an_empty_answer_with_an_error(self):
        def repos():
            raise ValueError("no")

        got = backlog.Views(repos, clock=self.clock, opener=self.opener).get()
        self.assertEqual(got, {"at": NOW.isoformat(), "repos": [], "error": backlog.NO_REPOS + ": ValueError"})

    # --- the timer: no test here waits on real time; Pause stands in for the wait ---

    def parked(self):
        self.assertTrue(self.pause.parked.acquire(timeout=WAIT), "the timer never reached its pause")

    def test_the_timer_runs_a_round_at_start_then_one_every_refresh_s(self):
        self.pause.ticks = 1                                    # one pause ends at once
        self.assertTrue(self.v.start())
        self.parked()
        self.assertEqual(self.opener.fetches(), ["/one", "/two"] * 2)
        self.assertEqual(self.pause.seconds, [300, 300])
        self.assertEqual(self.opener.most, 1)
        thread = self.v._thread
        self.assertTrue(self.v.stop(WAIT))
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.v.timer_error)
        self.assertEqual(self.opener.fetches(), ["/one", "/two"] * 2)     # nothing after the stop

    def test_the_first_round_runs_at_start_not_after_refresh_s(self):
        self.assertTrue(self.v.start())
        self.parked()                                           # in its first pause
        self.assertEqual((self.opener.fetches(), self.pause.seconds), (["/one", "/two"], [300]))
        self.assertEqual([r["fetched"] for r in self.v.get()["repos"]],
                         [NOW.isoformat(), NOW.isoformat(), None])

    def test_start_twice_runs_one_thread(self):
        self.assertTrue(self.v.start())
        thread = self.v._thread
        self.assertFalse(self.v.start())
        self.assertIs(self.v._thread, thread)
        self.parked()
        self.assertEqual(self.opener.fetches(), ["/one", "/two"])

    def test_stop_before_start_and_stop_twice(self):
        self.assertTrue(self.v.stop())                          # nothing to stop
        self.assertTrue(self.v.start())
        self.parked()
        self.assertTrue(self.v.stop(WAIT))
        self.assertTrue(self.v.stop(WAIT))
        self.assertIsNone(self.v._thread)

    def test_the_timer_starts_again_after_a_stop(self):
        self.assertTrue(self.v.start())
        self.parked()
        first = self.v._thread
        self.assertTrue(self.v.stop(WAIT))
        self.assertTrue(self.v.start())
        self.parked()
        self.assertIsNot(self.v._thread, first)
        self.assertEqual(self.opener.fetches(), ["/one", "/two"] * 2)

    def test_stop_ends_the_round_after_the_repo_it_is_on(self):
        self.opener.hold = threading.Event()
        self.assertTrue(self.v.start())
        self.assertTrue(self.opener.entered.acquire(timeout=WAIT))      # fetching the first repo
        thread = self.v._thread
        self.assertFalse(self.v.stop(0))                        # the wait ran out: still on it
        self.assertFalse(self.v.start())                        # and no second thread beside it
        self.assertIs(self.v._thread, thread)
        self.opener.hold.set()
        thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.opener.fetches(), ["/one"])       # the second repo was never fetched
        self.assertTrue(self.v.stop(WAIT))
        self.parked()                                           # the ended thread's last pause
        self.assertTrue(self.v.start())                         # a new one may start now
        self.parked()
        self.assertEqual(self.opener.fetches(), ["/one", "/one", "/two"])

    def test_a_round_that_raises_is_kept_in_timer_error_and_the_timer_goes_on(self):
        seen = []

        def between_rounds():
            seen.append(self.v.timer_error)
            self.clock.fail = False                             # mended before round two

        self.clock.fail = True
        self.pause.ticks = 1
        self.pause.hook = between_rounds
        self.assertTrue(self.v.start())
        self.parked()
        self.assertEqual(seen, ["RuntimeError", None])
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
