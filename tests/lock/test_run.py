"""kitlib.lock.run: slot() yields a launcher whose commands run pinned to the run's cores
in its job, see MACHINE_LOCK_RUN, and print nothing; release records the run (ended record and
one history line). book() refuses what it cannot size (fail closed). Runs take one cpu
slot (cores=1) on slots this process may run on (StoreCase.confine), so their jobs pin inside
the gate's own run. Two processes booking at once never share a slot; Ctrl+C or any other
exception while booking settles the run (queue entry gone, record ended, history written)."""
import _thread
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

from .helpers import FAST, OWNER, REPO, StoreCase, clean_env, own_affinity
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.lock import api
    from kitlib.lock import holds as hl
    from kitlib.lock import layout as lay
    from kitlib.lock import run as rm
    from kitlib.lock import store as store_mod
    from kitlib.lock.store import LockError, utc_now
    from kitlib.proc import process

# A second process booking on the test's store: it holds its slot for a second and prints when.
BOOK_ONE = """
import json, os, sys, time
from kitlib.lock import run as rm
from kitlib.lock.store import Settings, Store
a = json.loads(sys.argv[1])
s = Store(root=a["root"], settings=Settings(**a["fast"]), hardware=a["hw"], kinds_path=a["kinds"])
with rm.slot("engine-test", "kit", a["slug"], cores=1, store=s, wait_s=60,
             env=dict(os.environ)) as launch:
    t0 = time.time()
    time.sleep(1.0)
    print(json.dumps({"id": launch.id, "cores": launch.cores, "t0": t0, "t1": time.time()}))
"""


def one_usable_slot(case):
    """Leaves runs on case's store one cpu slot, one this process may run on (an owner slots
    hold over every other); returns it."""
    L = case.layout()
    own = own_affinity()
    ok = [i for i, ts in enumerate(L["slots_cpu"]) if set(ts) <= own]
    if not ok:
        case.skipTest("this process may run on none of the fake cpu slots")
    now = utc_now()
    hl.create(case.store, L, {"owner": "owner", "scope": "slots", "start": now,
                              "end": now + datetime.timedelta(hours=1),
                              "slots": {"cpu": [i for i in range(L["cpu_slots"]) if i != ok[0]],
                                        "gpu": []}}, OWNER, None)
    return ok[0]


WAIT_S = 3600           # a --max-wait no test waits out: Settling ends it on run.py's clock

WRITE_ENV = ("import os,sys; open(sys.argv[1], 'w').write(os.environ.get('MACHINE_LOCK_RUN', '')"
             " + '|' + os.environ.get('MACHINE_LOCK_CPUMASK', ''))")


class Slot(StoreCase):
    def test_the_launcher_runs_pinned_with_the_nest_token_and_prints_nothing(self):
        usable = self.confine(1)
        L = self.layout()
        seen = os.path.join(self.tmp, "seen.txt")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with rm.slot("engine-test", "kit", "t", cores=1, store=self.store,
                         env=clean_env()) as launch:
                rid, mask = launch.id, launch.mask
                token = launch.env[rm.NEST_ENV]
                rec = self.store.read("active/%s.json" % rid)
                self.assertEqual(rec["state"], "held")
                self.assertEqual(len(launch.cores), 1)
                self.assertLessEqual(set(launch.cores), set(usable))
                self.assertEqual(mask, lay.cores_mask(L, launch.cores))
                self.assertEqual(launch.env[rm.CPUMASK_ENV], hex(mask))
                code = launch.run([sys.executable, "-c", WRITE_ENV, seen])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(code, 0)
        m = rm.NEST_RE.match(token)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), rid)
        self.assertEqual(int(m.group(2)), os.getpid())
        with open(seen, encoding="utf-8") as f:
            self.assertEqual(f.read(), "%s|%s" % (token, hex(mask)))
        self.assertIsNone(self.store.read("active/%s.json" % rid))
        ended = self.store.read("ended/%s.json" % rid)
        self.assertEqual((ended["how"], ended["code"]), ("done", 0))
        hist = [d for d in self.store.history_records() if d["id"] == rid]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["kind"], "engine-test")
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])

    def test_a_held_runs_record_carries_the_installed_tools_layout_key(self):
        # A separately installed machine_lock.py (not this kit's) refuses every run while a
        # live record's `layout` is not its own key. On this store's fake hardware (HW in
        # helpers.py) that key is the string below (lay.slot_key), not layout_key's.
        self.confine(1)
        with rm.slot("engine-test", "kit", "t", cores=1, store=self.store,
                     env=clean_env()) as launch:
            rec = self.store.read("active/%s.json" % launch.id)
        self.assertEqual(rec["layout"], "t24 g1 cpu 12 x 2 gpu 2 m9b5a6751")

    def test_a_command_exit_code_comes_back(self):
        self.confine(1)
        with rm.slot("engine-test", "kit", "t", cores=1, store=self.store,
                     env=clean_env()) as launch:
            rid = launch.id
            self.assertEqual(launch.run([sys.executable, "-c", "import sys; sys.exit(3)"]), 3)
        self.assertEqual(self.store.read("ended/%s.json" % rid)["how"], "done")

    def test_an_error_in_the_block_ends_the_run_as_error(self):
        self.confine(1)
        with self.assertRaises(RuntimeError):
            with rm.slot("engine-test", "kit", "t", cores=1, store=self.store,
                         env=clean_env()) as launch:
                rid = launch.id
                raise RuntimeError("boom")
        ended = self.store.read("ended/%s.json" % rid)
        self.assertEqual((ended["how"], ended["code"]), ("error", 1))

    def test_run_command(self):
        self.confine(1)
        code = rm.run_command([sys.executable, "-c", "import sys; sys.exit(4)"], "engine-test",
                              "kit", "t", cores=1, store=self.store, env=clean_env())
        self.assertEqual(code, 4)
        self.assertEqual(len(self.store.history_records()), 1)

    def test_a_route_that_takes_the_run_books_nothing_here(self):
        """route= (a load node, kitlib.loadnode.dispatch): its number is the run's exit code and
        this machine books nothing; it is asked with the run's own arguments."""
        self.confine(1)
        asked = []

        def route(cmd, kind, repo, slug, kw):
            asked.append((cmd, kind, repo, slug, kw.get("cores"), kw.get("note")))
            return 7
        code = rm.run_command(["never-started.exe"], "engine-test", "kit", "t", cores=1,
                              note="n", store=self.store, env=clean_env(), route=route)
        self.assertEqual(code, 7)
        self.assertEqual(asked, [(["never-started.exe"], "engine-test", "kit", "t", 1, "n")])
        self.assertEqual(self.store.history_records(), [])
        self.assertEqual(self.store.live_runs(), [])

    def test_a_route_that_declines_runs_the_command_here(self):
        self.confine(1)
        code = rm.run_command([sys.executable, "-c", "import sys; sys.exit(4)"], "engine-test",
                              "kit", "t", cores=1, store=self.store, env=clean_env(),
                              route=lambda *a: None)
        self.assertEqual(code, 4)
        self.assertEqual(len(self.store.history_records()), 1)


NO_CWD = "NoDefaultCurrentDirectoryInExePath"


class CallerFolder(StoreCase):
    """run_command starts its command in the caller's folder. With NoDefaultCurrentDirectoryInExePath
    set (agent hosts set it), Windows does not look there for a bare command name: CreateProcess
    reads this process's environment, cmd.exe its own. A bare name that is a file there and fails
    to start is said with the path form that runs."""

    def setUp(self):
        super().setUp()
        self.confine(1)
        self.said = []
        self.store.say = self.said.append
        self.folder = os.path.join(self.tmp, "caller")
        os.mkdir(self.folder)
        with open(os.path.join(self.folder, "hello.bat"), "w") as f:
            f.write("@echo off\r\necho ran> ran.txt\r\n")
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.folder)

    @staticmethod
    def no_cwd_env():
        """clean_env with NoDefaultCurrentDirectoryInExePath set once, whatever case it had."""
        env = {k: v for k, v in clean_env().items() if k.upper() != NO_CWD.upper()}
        env[NO_CWD] = "1"
        return env

    def run_cmd(self, cmd, env):
        return rm.run_command(cmd, "engine-test", "kit", "cwd", cores=1, store=self.store, env=env)

    def test_the_command_runs_in_the_callers_folder(self):
        code = self.run_cmd([sys.executable, "-c",
                             "import os; open('cwd.txt', 'w').write(os.getcwd())"], clean_env())
        self.assertEqual(code, 0, self.said)
        with open(os.path.join(self.folder, "cwd.txt"), encoding="utf-8") as f:
            self.assertEqual(os.path.normcase(f.read()), os.path.normcase(self.folder))

    def test_a_bare_name_in_the_callers_folder_is_said_with_its_path(self):
        with mock.patch.dict(os.environ, {NO_CWD: "1"}):
            code = self.run_cmd(["hello.bat"], self.no_cwd_env())
        self.assertEqual(code, 1)
        line = next((s for s in self.said if s.startswith("could not start the command")), "")
        self.assertIn(self.folder, line)
        self.assertIn(NO_CWD, line)
        self.assertIn(".\\hello.bat", line)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "ran.txt")))

    def test_the_path_form_it_says_runs(self):
        with mock.patch.dict(os.environ, {NO_CWD: "1"}):
            code = self.run_cmd([".\\hello.bat"], self.no_cwd_env())
        self.assertEqual(code, 0, self.said)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "ran.txt")))

    def test_a_bare_name_not_in_the_folder_gets_no_hint(self):
        with mock.patch.dict(os.environ, {NO_CWD: "1"}):
            code = self.run_cmd(["no-such-tool-here.bat"], self.no_cwd_env())
        self.assertEqual(code, 1)
        line = next((s for s in self.said if s.startswith("could not start the command")), "")
        self.assertTrue(line, self.said)
        self.assertNotIn(".\\", line)

    # bare_hint's own conditions. With the setting unset a bare name in the folder starts, so
    # the failure these need is made up here.
    def test_the_hint_needs_the_setting_set(self):
        missing = FileNotFoundError(2, "The system cannot find the file specified")
        with mock.patch.dict(os.environ, {NO_CWD: "1"}):
            self.assertIn(".\\hello.bat", rm.bare_hint(["hello.bat"], missing))
        with mock.patch.dict(os.environ):
            os.environ.pop(NO_CWD, None)
            self.assertEqual(rm.bare_hint(["hello.bat"], missing), "")

    def test_a_folder_qualified_name_gets_no_hint(self):
        missing = FileNotFoundError(2, "The system cannot find the file specified")
        os.mkdir(os.path.join(self.folder, "sub"))
        with open(os.path.join(self.folder, "sub", "hello.bat"), "w") as f:
            f.write("@echo off\r\n")
        with mock.patch.dict(os.environ, {NO_CWD: "1"}):
            for name in ("sub\\hello.bat", ".\\hello.bat", os.path.join(self.folder, "hello.bat")):
                self.assertEqual(rm.bare_hint([name], missing), "", name)

    def test_an_unreadable_folder_gets_no_hint(self):
        missing = FileNotFoundError(2, "The system cannot find the file specified")
        with mock.patch.dict(os.environ, {NO_CWD: "1"}), \
                mock.patch.object(rm.os, "getcwd", side_effect=FileNotFoundError(2, "gone")):
            self.assertEqual(rm.bare_hint(["hello.bat"], missing), "")


class Refusals(StoreCase):
    def code(self, **kw):
        kw.setdefault("store", self.store)
        kw.setdefault("env", clean_env())
        with self.assertRaises(LockError) as cm:
            rm.book(kw.pop("kind", "engine-test"), kw.pop("repo", "kit"), kw.pop("slug", "t"), **kw)
        return cm.exception.code

    def test_usage(self):
        self.assertEqual(self.code(kind="no-such-kind"), 2)
        self.assertEqual(self.code(repo=""), 2)
        self.assertEqual(self.code(gpu_share="training"), 2)
        self.assertEqual(self.code(kind="local-ai", gpu_share="inference"), 2)
        self.assertEqual(self.code(cores=7), 2)
        self.assertEqual(self.code(expect_s=0), 2)
        self.assertIsNone(self.store.read("queue.json"))

    def test_no_kinds_table_fails_closed(self):
        os.remove(self.kinds_path)
        self.assertEqual(self.code(), 10)

    def test_an_owner_hold_over_everything_refuses_with_11(self):
        L = self.layout()
        now = utc_now()
        hl.create(self.store, L, {"owner": "owner", "scope": "whole", "start": now,
                                  "end": now + datetime.timedelta(hours=1)}, OWNER, None)
        self.assertEqual(self.code(no_wait=True), 11)
        ended = [n for n in os.listdir(self.store.path("ended")) if n.startswith("r-")]
        self.assertEqual(len(ended), 1)
        self.assertEqual(self.store.history_records(), [])

    def test_unreadable_holds_refuse_with_10(self):
        # Fail closed: the old _read_all skipped the file, so the run took the slots
        # the hold covered (red there: the run was booked).
        os.makedirs(self.store.path("holds"), exist_ok=True)
        with open(self.store.path("holds", "h-20261006T120000Z-abcd.json"), "w") as f:
            f.write("{")
        self.assertEqual(self.code(no_wait=True), 10)
        self.assertEqual(self.code(no_wait=True, hold="h-20261006T120000Z-abcd"), 10)
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])

    def test_an_agents_run_never_runs_inside_an_owner_whole_hold(self):
        # A detached run whose launcher ancestry cannot be named counts as an agent's.
        L = self.layout()
        now = utc_now()
        h, _ = hl.create(self.store, L, {"owner": "owner", "scope": "whole", "start": now,
                                         "end": now + datetime.timedelta(hours=1)}, OWNER, None)
        self.assertEqual(self.code(hold=h["id"], detached=True, no_wait=True), 12)


def store_files(root):
    """{path relative to root: bytes} of every file under root."""
    out = {}
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


class RunIds(StoreCase):
    """A run id is a second stamp and a 3-byte token. It was fixed before any mutex, so a run could
    take an id a live run, an ended run or a dead run's leftover file has. book() now claims it
    (rm.claim_id) under its run mutex before any record exists. A drawn id that is taken is drawn
    again. A given one (rid=, the CLI's --detached-id) that is taken is refused with exit 10, and
    nothing is written. One not shaped like a run id is usage, exit 2."""

    def book(self, slug, **kw):
        kw.setdefault("store", self.store)
        kw.setdefault("env", clean_env())
        return rm.book("engine-test", "kit", slug, cores=1, no_wait=True, **kw)

    def refused(self, slug, **kw):
        with self.assertRaises(LockError) as cm:
            self.book(slug, **kw)
        return cm.exception

    def drawn(self, *taken):
        """(a patch of the id builder book uses: each of `taken` in turn, then an id nothing has,
        that id). A draw after it is an error (StopIteration)."""
        fresh = next(i for i in (taken[0][:-6] + "%06x" % n for n in range(len(taken) + 1))
                     if i not in taken)
        return mock.patch.object(rm, "new_id", side_effect=list(taken) + [fresh]), fresh

    def entry(self, rid):
        """(state, cores) of rid's queue entry, or None."""
        for e in (self.store.read("queue.json") or {}).get("entries", []):
            if e["id"] == rid:
                return e["state"], e.get("cores")
        return None

    def ended_run(self, note):
        s = self.book("old", note=note)
        s.release()
        return s.id

    def test_an_id_an_ended_run_has_is_drawn_again(self):
        """Red before the claim: the second run took the id, and its end replaced
        ended/<id>.json, the only record of the first run."""
        self.confine(1)
        old = self.ended_run("first")
        before = self.store.read("ended/%s.json" % old)
        patch, fresh = self.drawn(old)
        with patch as ids:
            s = self.book("new", note="second")
            s.release()
        self.assertEqual(self.store.read("ended/%s.json" % old), before)
        self.assertEqual(s.id, fresh)
        self.assertEqual(self.store.read("ended/%s.json" % fresh)["note"], "second")
        self.assertEqual(ids.call_count, 2)

    def test_an_id_a_live_run_has_is_drawn_again_and_the_live_run_is_untouched(self):
        """Red before the claim: the second run was refused (its run mutex is the live run's),
        and its settle then dropped the live run's queue entry and record."""
        self.confine(2)
        first = self.book("first")
        try:
            rec = self.store.read("active/%s.json" % first.id)
            entry = self.entry(first.id)
            self.assertIsNotNone(entry)
            patch, fresh = self.drawn(first.id)
            with patch as ids:
                second = self.book("second")
                second.release()
            self.assertEqual(second.id, fresh)
            self.assertEqual(ids.call_count, 2)
            self.assertEqual(self.store.read("active/%s.json" % first.id), rec)
            self.assertEqual(self.entry(first.id), entry)
            self.assertIsNone(self.store.read("ended/%s.json" % first.id))
        finally:
            first.release()

    def test_a_given_id_a_live_run_has_is_refused_and_the_live_run_is_untouched(self):
        """Red before the claim: the refusal settled the refused run under the live run's id, so
        the live run's record and queue entry went and an ended record was written for it while
        it still held its slots."""
        self.confine(2)
        first = self.book("first")
        try:
            rec = self.store.read("active/%s.json" % first.id)
            entry = self.entry(first.id)
            err = self.refused("second", rid=first.id)
            self.assertEqual(self.store.read("active/%s.json" % first.id), rec)
            self.assertEqual(self.entry(first.id), entry)
            self.assertIsNone(self.store.read("ended/%s.json" % first.id))
            self.assertEqual(err.code, store_mod.EXIT_REQUIRE)
            self.assertIn("run id %s is taken" % first.id, err.message)
        finally:
            first.release()

    def test_a_given_id_with_anything_of_an_earlier_run_left_is_refused_and_nothing_is_written(self):
        """Each thing claim_id counts as taken, alone: the run mutex, the active record, the stop
        file, the ended record, the background folder."""
        self.confine(1)
        left = ("mutex", "active/%s.json", "active/%s.stop", "ended/%s.json",
                "background/%s.d/control.json")
        for i, what in enumerate(left):
            rid = "r-20261006T12000%dZ-abc123" % i
            with self.subTest(left=what):
                if what == "mutex":
                    m = self.store.run_mutex(rid)
                    self.addCleanup(m.release)
                else:
                    self.store.write(what % rid, {"id": rid, "left": what})
                before = store_files(self.root)
                err = self.refused("t", rid=rid)
                self.assertEqual(store_files(self.root), before)
                self.assertEqual(err.code, store_mod.EXIT_REQUIRE)
                self.assertIn("run id %s is taken" % rid, err.message)

    def test_a_given_id_not_shaped_like_a_run_id_is_usage_and_nothing_is_written(self):
        """--detached-id reached store paths unchecked: '..\\\\x' named a file outside active/."""
        self.confine(1)
        before = store_files(self.root)
        for rid in ("r-1", "..\\x", "r-20261006T120000Z-abcd", "h-20261006T120000Z-abcd",
                    "r-20261006T120000Z-ABC123", "r-20261006T120000Z-abc123\n", 5):
            with self.subTest(rid=rid):
                err = self.refused("t", rid=rid)
                self.assertEqual(err.code, store_mod.EXIT_USAGE)
                self.assertIn("not a run id", err.message)
        self.assertEqual(store_files(self.root), before)

    def test_a_draw_that_finds_every_id_taken_gives_up_with_10_and_writes_nothing(self):
        self.confine(1)
        old = self.ended_run("first")
        before = store_files(self.root)
        with mock.patch.object(rm, "new_id", return_value=old) as ids:
            err = self.refused("t")
        self.assertEqual(store_files(self.root), before)
        self.assertEqual(err.code, store_mod.EXIT_REQUIRE)
        self.assertIn("no free run id", err.message)
        self.assertEqual(ids.call_count, rm.CLAIM_TRIES)


SIZED_KINDS = {"kinds": {
    "engine-test": {"cpu": "medium", "gpu": 0, "warn_min": 30},
    "three": {"cpu": 3, "gpu": 0, "warn_min": 30},
    "one": {"cpu": 1, "gpu": 0, "warn_min": 30},
    "cpu-eval": {"cpu": "heavy", "gpu": 0, "warn_min": 60},
}}


class CoreRequests(StoreCase):
    """A run books its kind's size; cores (--cores) asks for fewer (1..the kind's size),
    never more. An owner whole hold over everything makes a sized run exit 11 at its first look,
    so a request the sizing accepts is told from one it refuses (2) without taking any slot."""

    def setUp(self):
        StoreCase.setUp(self)
        self.use_kinds(SIZED_KINDS)

    def use_kinds(self, table):
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            json.dump(table, f)

    code = Refusals.code

    def everything_held(self):
        now = utc_now()
        hl.create(self.store, self.layout(), {"owner": "owner", "scope": "whole", "start": now,
                                              "end": now + datetime.timedelta(hours=1)},
                  OWNER, None)

    def ended(self):
        d = self.store.path("ended")
        return [self.store.read("ended/" + n) for n in (os.listdir(d) if os.path.isdir(d) else [])
                if n.startswith("r-")]

    def ended_need(self):
        ended = self.ended()
        self.assertEqual(len(ended), 1)
        return ended[0]["need"]["cpu"]

    def test_more_than_the_kinds_size_is_usage(self):
        self.everything_held()
        self.assertEqual(self.code(kind="engine-test", cores=3, no_wait=True), 2)  # medium: 2
        self.assertEqual(self.code(kind="three", cores=4, no_wait=True), 2)
        self.assertEqual(self.code(kind="one", cores=2, no_wait=True), 2)
        self.assertEqual(self.code(kind="cpu-eval", cores=7, no_wait=True), 2)     # heavy: 6
        self.assertEqual(self.ended(), [])

    def test_less_than_one_or_not_a_whole_number_is_usage(self):
        self.everything_held()
        for bad in (0, -1, 1.5, "2", True):
            self.assertEqual(self.code(kind="three", cores=bad, no_wait=True), 2, repr(bad))
        self.assertEqual(self.ended(), [])

    def test_the_refusal_names_the_kinds_size(self):
        with self.assertRaises(LockError) as cm:
            rm.book("three", "kit", "t", cores=4, store=self.store, env=clean_env())
        self.assertIn("1 to 3", str(cm.exception))
        self.assertIn("a run asks for fewer, never more", str(cm.exception))

    def test_an_integer_kind_books_its_size(self):
        self.everything_held()
        self.assertEqual(self.code(kind="three", no_wait=True), 11)
        self.assertEqual(self.ended_need(), 3)

    def test_fewer_cores_than_the_kind_is_booked_as_asked(self):
        self.everything_held()
        self.assertEqual(self.code(kind="three", cores=2, no_wait=True), 11)
        self.assertEqual(self.ended_need(), 2)

    def test_an_aliased_kind_books_the_canonical_kinds_slots_and_is_recorded_as_it(self):
        # `old-name` is a retired name the table maps to graph-build (an `aliases` entry).
        self.use_kinds({"kinds": {"graph-build": {"cpu": 3, "gpu": 0, "warn_min": 120}},
                        "aliases": {"old-name": {"to": "graph-build", "why": "retired name"}}})
        self.everything_held()
        self.assertEqual(self.code(kind="old-name", no_wait=True), 11)
        self.assertEqual(self.ended_need(), 3)
        self.assertEqual(self.ended()[0]["kind"], "graph-build")
        self.assertEqual(self.code(kind="no-such-kind", no_wait=True), 2)

    def test_the_kinds_full_size_may_be_asked_for(self):
        self.everything_held()
        self.assertEqual(self.code(kind="cpu-eval", cores=6, no_wait=True), 11)
        self.assertEqual(self.ended_need(), 6)

    def test_a_kind_past_the_agent_cap_fails_closed(self):
        # The fake layout's agent_cap is 6: a table naming 7 is not one the lock can size, so
        # No run books from it, this kind's or another's.
        self.use_kinds({"kinds": {"engine-test": {"cpu": "medium", "gpu": 0, "warn_min": 30},
                                  "big": {"cpu": 7, "gpu": 0, "warn_min": 30}}})
        self.assertEqual(self.code(kind="big"), 10)
        self.assertEqual(self.code(kind="engine-test"), 10)
        self.assertIsNone(self.store.read("queue.json"))

    def test_a_given_table_past_the_agent_cap_fails_closed_too(self):
        table = {"big": {"cpu": 7, "gpu": 0, "share": False, "warn_min": 30}}
        self.assertEqual(self.code(kind="big", kinds_table=table), 10)

    def test_a_given_table_with_a_bad_cpu_fails_closed(self):
        for bad in (0, True, "3", "huge"):
            table = {"odd": {"cpu": bad, "gpu": 0, "share": False, "warn_min": 30}}
            self.assertEqual(self.code(kind="odd", kinds_table=table), 10, repr(bad))

    def test_a_one_core_kind_holds_one_slot(self):
        self.confine(1)
        with rm.slot("one", "kit", "t", store=self.store, env=clean_env()) as launch:
            self.assertEqual(len(launch.cores), 1)


CPU_FLAGS = ["-e", "CUDA_VISIBLE_DEVICES=", "-e", "JAX_PLATFORMS=cpu"]


@windows_only("the lock is Windows only")
class DockerCpuEnv(unittest.TestCase):
    """A `docker exec` run holding no gpu slot keeps the container's process off the
    gpus: both variables right after `exec`, before any other flag."""

    def test_no_gpu_docker_exec_gets_both(self):
        for docker in ("docker", "docker.exe", r"C:\Program Files\Docker\docker.EXE"):
            with self.subTest(docker=docker):
                self.assertEqual(rm.docker_cpu_env([docker, "exec", "-it", "box", "python", "t.py"],
                                                   False),
                                 [docker, "exec"] + CPU_FLAGS + ["-it", "box", "python", "t.py"])

    def test_a_gpu_slot_leaves_it_unchanged(self):
        cmd = ["docker", "exec", "box", "python", "t.py"]
        self.assertEqual(rm.docker_cpu_env(cmd, True), cmd)

    def test_other_commands_are_unchanged(self):
        for cmd in (["python", "-c", "pass"], ["docker", "run", "img"], ["docker"],
                    ["docker", "compose", "exec", "box", "sh"], "docker exec box sh",
                    ["dockerd", "exec"]):
            with self.subTest(cmd=cmd):
                self.assertEqual(rm.docker_cpu_env(cmd, False), cmd)

    def test_the_callers_own_setting_wins(self):
        for given in (["-e", "JAX_PLATFORMS=cuda"], ["--env", "JAX_PLATFORMS"],
                      ["--env=JAX_PLATFORMS=cpu"], ["-eJAX_PLATFORMS=tpu"]):
            with self.subTest(given=given):
                got = rm.docker_cpu_env(["docker", "exec"] + given + ["box", "sh"], False)
                self.assertEqual(got, ["docker", "exec", "-e", "CUDA_VISIBLE_DEVICES="] + given
                                 + ["box", "sh"])
        got = rm.docker_cpu_env(["docker", "exec", "-u", "-e", "-w", "/x", "box", "env", "-e",
                                 "CUDA_VISIBLE_DEVICES=0"], False)
        self.assertEqual(got[2:6], CPU_FLAGS, "an option's value and the container's own "
                                              "arguments set nothing")
        both = ["docker", "exec", "-e", "CUDA_VISIBLE_DEVICES=1", "-e", "JAX_PLATFORMS=gpu", "b"]
        self.assertEqual(rm.docker_cpu_env(both, False), both)


class DockerRuns(StoreCase):
    """The foreground launcher passes the docker flags to the job; the runs prove the path."""

    def started(self, kind, cmd):
        seen = []
        with rm.slot(kind, "kit", "t", store=self.store, env=clean_env(),
                     **({"cores": 1} if kind == "engine-test" else {})) as launch:
            job = launch._slot.job
            with mock.patch.object(job, "start", side_effect=lambda c, **kw: seen.append(c)):
                launch.start(cmd)
            gpus = launch._slot.gpus
        return seen[0], gpus

    def test_a_cpu_run_gets_the_flags(self):
        self.confine(1)
        got, gpus = self.started("engine-test", ["docker", "exec", "box", "sh"])
        self.assertEqual(gpus, [])
        self.assertEqual(got, ["docker", "exec"] + CPU_FLAGS + ["box", "sh"])

    def test_a_gpu_run_is_left_alone(self):
        self.confine(self.layout()["medium_cores"])
        got, gpus = self.started("local-ai", ["docker", "exec", "box", "sh"])
        self.assertTrue(gpus)
        self.assertEqual(got, ["docker", "exec", "box", "sh"])


class Role(StoreCase):
    """Who started a run, from the process tree; a detached run's from its launcher's
    ancestry (MACHINE_LOCK_ORIGIN), failing closed to agent."""

    def test_tree_role(self):
        me = (os.getpid(), process.own_ctime())
        self.assertEqual(store_mod.tree_role([]), "agent")
        self.assertEqual(store_mod.tree_role([me]), "owner")
        self.assertEqual(store_mod.tree_role([(me[0], me[1] + 1)]), "agent")
        self.assertEqual(store_mod.tree_role([(4, 0), me]), "agent")

    def test_a_live_agent_host_in_the_ancestry_makes_an_agent(self):
        fake = os.path.join(self.tmp, "claude.exe")
        shutil.copy(os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), fake)
        p = subprocess.Popen([fake, "/d", "/q"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             env=clean_env())
        try:
            ct = process.proc_ctime(p.pid)
            self.assertIsNotNone(ct)
            self.assertEqual(store_mod.tree_role([(p.pid, ct)]), "agent")
        finally:
            p.communicate(b"exit\r\n", timeout=30)

    def test_a_detached_run_takes_its_launchers_role(self):
        L = self.layout()
        me = "%d:%d" % (os.getpid(), process.own_ctime())
        for env, want in (({rm.ORIGIN_ENV: me}, "owner"), ({}, "agent"),
                          ({rm.ORIGIN_ENV: "garbage"}, "agent")):
            with self.subTest(env=env):
                run = rm.Run(self.store, L, "kit", "t", "engine-test", None, None, "waiting",
                             None, True, clean_env(**env), None)
                self.assertEqual(run.rec["role"], want)


class Settling(StoreCase):
    """A booking that ends any way but held leaves nothing behind: its queue entry goes and its
    record is ended with how and code. It held nothing, so it writes no history line."""

    def gone(self, how, code):
        ended = [self.store.read("ended/" + n) for n in os.listdir(self.store.path("ended"))
                 if n.startswith("r-")]
        self.assertEqual([(e["how"], e["code"]) for e in ended], [(how, code)])
        self.assertEqual([n for n in os.listdir(self.store.path("active"))
                          if n.startswith("r-")], [])
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])
        self.assertEqual(self.store.history_records(), [])

    def waiting(self):
        """The slugs of the runs waiting in the queue, as queue.json has them."""
        return [e["slug"] for e in (self.store.read("queue.json") or {}).get("entries", [])
                if e["state"] == "waiting"]

    def pauses(self, pause, skip=(0.0,)):
        """Patches kitlib.lock.run's clock while a run books: acquire's pause between looks calls
        pause(seconds) and does not sleep, and its monotonic clock reads skip[0] seconds ahead.
        acquire pauses only after a look that left the run in the queue, so what a test does in
        its first pause happens once the run has joined, however long booking took to get
        there (no timer to race on a loaded machine)."""
        return mock.patch.object(rm, "time", types.SimpleNamespace(
            monotonic=lambda: time.monotonic() + skip[0], sleep=pause, time=time.time))

    def test_ctrl_c_while_it_waits_in_the_queue(self):
        # Ctrl+C lands in the run's first pause between two looks, so once it has joined.
        one_usable_slot(self)
        steps = []

        def pause(seconds):
            steps.append(self.waiting())
            self.assertEqual(len(steps), 1, "Ctrl+C did not reach the waiting run")
            _thread.interrupt_main()            # the main thread runs the test
            time.sleep(seconds)
        with rm.slot("engine-test", "kit", "first", cores=1, store=self.store,
                     env=clean_env()) as launch:
            first = launch.id
            with self.pauses(pause):
                with self.assertRaises(KeyboardInterrupt):
                    rm.book("engine-test", "kit", "second", cores=1, store=self.store,
                            env=clean_env(), wait_s=WAIT_S)
        self.assertEqual(steps, [["second"]])               # it was waiting when Ctrl+C came
        hist = [d for d in self.store.history_records() if d["id"] != first]
        ended = [self.store.read("ended/" + n) for n in os.listdir(self.store.path("ended"))
                 if n.startswith("r-") and n != first + ".json"]
        self.assertEqual([(e["how"], e["code"], e["slug"]) for e in ended],
                         [("interrupted", 130, "second")])
        self.assertEqual(hist, [])
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])

    def test_holds_unreadable_after_it_joined_keep_its_place_until_its_wait_runs_out(self):
        # A run already queued is not refused 10: it takes nothing and looks again, then leaves
        # the queue at its --max-wait (8). The holds go bad at its first pause, and its wait ends
        # when the clock is moved on after a look that found them bad.
        one_usable_slot(self)
        said, steps, skip = [], [], [0.0]
        self.store.say = said.append
        bad = self.store.path("holds", "h-20261006T120000Z-abcd.json")

        def pause(seconds):
            steps.append((os.path.exists(bad), self.waiting()))
            # Two pauses are enough; more, and the wait would spin to the real WAIT_S unslept.
            self.assertLessEqual(len(steps), 5, "its wait did not run out; it said %r" % (said,))
            if not os.path.exists(bad):
                with open(bad, "w") as f:
                    f.write("{")
            elif any("it keeps its place and looks again" in text for text in said):
                skip[0] = WAIT_S
        with rm.slot("engine-test", "kit", "first", cores=1, store=self.store,
                     env=clean_env()):
            with self.pauses(pause, skip):
                with self.assertRaises(LockError) as cm:
                    rm.book("engine-test", "kit", "second", cores=1, store=self.store,
                            env=clean_env(), wait_s=WAIT_S)
        self.assertEqual(cm.exception.code, store_mod.EXIT_TIMEOUT)
        self.assertIn("the holds are unreadable", str(cm.exception))
        self.assertEqual(steps[0], (False, ["second"]))     # the holds were good until it joined
        self.assertEqual(steps[-1], (True, ["second"]))     # bad holds seen, and its place kept
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])

    def test_ctrl_c_in_acquire(self):
        with mock.patch.object(rm, "acquire", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                rm.book("engine-test", "kit", "t", store=self.store, env=clean_env())
        self.gone("interrupted", 130)

    def test_any_other_exception_in_acquire(self):
        for exc in (SystemExit(5), RuntimeError("boom")):
            with self.subTest(exc=exc):
                shutil.rmtree(self.store.path("ended"), True)
                shutil.rmtree(self.store.path("history"), True)
                with mock.patch.object(rm, "acquire", side_effect=exc):
                    with self.assertRaises(type(exc)):
                        rm.book("engine-test", "kit", "t", store=self.store, env=clean_env())
                self.gone("not acquired", 1)


class Concurrent(StoreCase):
    def test_two_processes_booking_at_once_never_share_a_slot(self):
        s = one_usable_slot(self)
        arg = json.dumps({"root": self.root, "fast": FAST, "hw": self.hw,
                          "kinds": self.kinds_path, "slug": "t"})
        ps = [subprocess.Popen([sys.executable, "-c", BOOK_ONE, arg], cwd=REPO, env=clean_env(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
              for _ in range(2)]
        outs = [p.communicate(timeout=120) for p in ps]
        for p, (out, err) in zip(ps, outs):
            self.assertEqual(p.returncode, 0, err)
        got = sorted((json.loads(out) for out, _err in outs), key=lambda d: d["t0"])
        self.assertEqual([d["cores"] for d in got], [[s], [s]])
        self.assertLessEqual(got[0]["t1"], got[1]["t0"])
        self.assertEqual(sorted(d["id"] for d in self.store.history_records()),
                         sorted(d["id"] for d in got))
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])


class MovedToTop(StoreCase):
    """A run the owner moved to the top of the queue takes the slot next when it frees, ahead of
    a run that joined before it, and its run record says it was moved."""

    def book_one(self, slug):
        arg = json.dumps({"root": self.root, "fast": FAST, "hw": self.hw,
                          "kinds": self.kinds_path, "slug": slug})
        p = subprocess.Popen([sys.executable, "-c", BOOK_ONE, arg], cwd=REPO, env=clean_env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def waiting(self, slug):
        end = time.monotonic() + 60
        while time.monotonic() < end:
            for e in (self.store.read("queue.json") or {}).get("entries", []):
                if e.get("slug") == slug and e.get("state") == "waiting":
                    return e["id"]
            time.sleep(0.05)
        self.fail("run %s never waited in the queue" % slug)

    def test_the_moved_run_starts_next(self):
        one_usable_slot(self)
        with rm.slot("engine-test", "kit", "first", cores=1, store=self.store,
                     env=clean_env()):
            pb = self.book_one("b")
            b = self.waiting("b")
            pc = self.book_one("c")
            c = self.waiting("c")
            res = api.move_to_top(OWNER, c, self.store)
            self.assertTrue(res["ok"], res.get("error"))
        outs = {}
        for slug, p in (("b", pb), ("c", pc)):
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
            outs[slug] = json.loads(out)
        self.assertEqual((outs["b"]["id"], outs["c"]["id"]), (b, c))
        self.assertLessEqual(outs["c"]["t1"], outs["b"]["t0"])
        self.assertEqual(self.store.read("ended/%s.json" % c)["moved"]["by"], "owner")
        self.assertNotIn("moved", self.store.read("ended/%s.json" % b))


LAND = "landing:q:\\repo\\.git"


class Resources(StoreCase):
    """book(resource=, priority=): the run holds the resource for its whole life; the queue
    entry and run record carry both; the resource is free again when the run releases or its
    holder is found dead; an unknown priority or a resource that is not a string is usage (2)."""

    def book(self, slug, **kw):
        kw.setdefault("store", self.store)
        kw.setdefault("env", clean_env())
        return rm.book("engine-test", "kit", slug, cores=1, **kw)

    def busy(self, slug, **kw):
        with self.assertRaises(LockError) as cm:
            self.book(slug, no_wait=True, **kw)
        return cm.exception

    def entries(self):
        return (self.store.read("queue.json") or {}).get("entries", [])

    def test_usage(self):
        for kw in ({"priority": "urgent"}, {"priority": "LAND"}, {"priority": 1},
                   {"resource": ""}, {"resource": 5}, {"resource": ["x"]}):
            with self.subTest(kw=kw):
                e = self.busy("t", **kw)
                self.assertEqual(e.code, 2)
        self.assertIn("unknown priority 'urgent' (known: land)", self.busy("t", priority="urgent").message)
        self.assertIsNone(self.store.read("queue.json"))

    def test_a_second_run_naming_it_waits_until_the_first_releases(self):
        self.confine(2)                         # slots alone would let both run at once
        first = self.book("first", resource=LAND, priority="land")
        try:
            rec = self.store.read("active/%s.json" % first.id)
            self.assertEqual((rec["resource"], rec["priority"]), (LAND, "land"))
            [e] = self.entries()
            self.assertEqual((e["state"], e["resource"], e["priority"]), ("held", LAND, "land"))
            err = self.busy("second", resource=LAND, priority="land")
            self.assertEqual(err.code, store_mod.EXIT_BUSY)
            self.assertIn("resource %s is held by" % LAND, err.message)
            self.assertIn(first.id, err.message)
            other = self.book("other", resource="landing:q:\\other\\.git")
            other.release()
            plain = self.book("plain")          # a run naming no resource is not kept out
            plain.release()
        finally:
            first.release()
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.store.read("ended/%s.json" % first.id)["resource"], LAND)
        second = self.book("second", resource=LAND, no_wait=True)
        second.release()

    def test_a_dead_holders_resource_is_freed_by_the_prune(self):
        self.confine(2)
        rid = "r-20261006T120000Z-d0d0d0"
        m = self.store.run_mutex(rid)
        self.addCleanup(m.release)
        self.store.write("queue.json", {"ticket": 1, "entries": [{
            "id": rid, "ticket": 1, "state": "held", "pid": os.getpid(),
            "ctime": process.own_ctime(), "repo": "kit", "slug": "gone", "kind": "engine-test",
            "need": {"cpu": 0, "gpu": 0}, "expect_s": 600, "cores": [], "gpus": [],
            "held_at": time.time(), "resource": LAND, "priority": "land"}]})
        self.assertEqual(self.busy("t", resource=LAND).code, store_mod.EXIT_BUSY)
        m.release()                             # its wrapper is gone: the run mutex with it
        said = []
        self.store.say = said.append
        s = self.book("t", resource=LAND, no_wait=True)
        try:
            self.assertEqual([e["id"] for e in self.entries()], [s.id])
        finally:
            s.release()
        self.assertIn("run %s died holding resource %s; it is free" % (rid, LAND), said)

    def test_a_dead_run_holding_slots_and_the_resource_frees_both_for_the_next_claimant(self):
        self.confine(2)
        rid = "r-20261006T120000Z-d1d1d1"
        m = self.store.run_mutex(rid)
        self.addCleanup(m.release)
        self.store.write("queue.json", {"ticket": 1, "entries": [{
            "id": rid, "ticket": 1, "state": "held", "pid": os.getpid(),
            "ctime": process.own_ctime(), "repo": "kit", "slug": "gone", "kind": "engine-test",
            "need": {"cpu": 1, "gpu": 0}, "expect_s": 600, "cores": [0], "gpus": [],
            "held_at": time.time(), "resource": LAND, "priority": "land"}]})
        m.release()                             # its wrapper is gone
        said = []
        self.store.say = said.append
        s = self.book("next", resource=LAND, priority="land", no_wait=True)
        try:
            self.assertEqual([e["id"] for e in self.entries()], [s.id])
        finally:
            s.release()
        [line] = [t for t in said if rid in t]
        self.assertIn("died holding it; it is free; so is resource %s" % LAND, line)

    def test_a_dead_waiter_naming_the_resource_is_dropped_and_does_not_keep_it(self):
        self.confine(2)
        rid = "r-20261006T120000Z-d2d2d2"
        m = self.store.run_mutex(rid)
        self.addCleanup(m.release)
        self.store.write("queue.json", {"ticket": 1, "entries": [{
            "id": rid, "ticket": 1, "state": "waiting", "pid": os.getpid(),
            "ctime": process.own_ctime(), "repo": "kit", "slug": "gone", "kind": "engine-test",
            "since": store_mod.iso(), "need": {"cpu": 1, "gpu": 0}, "expect_s": 600,
            "cores": [], "gpus": [], "resource": LAND, "priority": "land"}]})
        m.release()
        said = []
        self.store.say = said.append
        s = self.book("next", resource=LAND, no_wait=True)
        s.release()
        self.assertIn("run %s's caller died while it waited; its queue entry is dropped" % rid,
                      said)
        self.assertEqual(self.entries(), [])

    def test_an_unknown_priority_is_refused_before_anything_is_written(self):
        # Refused, not defaulted to ordinary (the planner's default is only for old disk entries).
        self.confine(2)
        for prio in ("", "Land", "urgent", ("land",), True):
            with self.subTest(priority=prio):
                e = self.busy("t", resource=LAND, priority=prio)
                self.assertEqual(e.code, 2)
                self.assertIn("unknown priority", e.message)
        self.assertIsNone(self.store.read("queue.json"))
        self.assertEqual(os.listdir(self.store.path("active")) if os.path.isdir(
            self.store.path("active")) else [], [])
        s = self.book("t", priority="land", no_wait=True)       # known, with no resource
        try:
            [e] = self.entries()
            self.assertEqual((e["priority"], e["resource"]), ("land", None))
        finally:
            s.release()

    def test_a_nested_run_naming_one_its_parent_lacks_joins_the_queue_for_it(self):
        anc = process.own_ancestors()
        pp, ct = os.getppid(), process.proc_ctime(os.getppid())
        if ct is None or (pp, ct) not in anc:
            self.skipTest("this process's parent cannot be read as an ancestor")
        prid = "r-20261006T120000Z-abcdef"
        self.live_record(prid, pid=pp, ctime=ct, has={"cpu": 2}, cores=[0, 1])
        env = clean_env(**{rm.NEST_ENV: "%s:%d:%d" % (prid, pp, ct)})
        s = self.book("child", env=env, resource=LAND)
        try:
            self.assertFalse(s.nested)
            [e] = self.entries()
            self.assertEqual((e["id"], e["state"], e["resource"], e["parent"]),
                             (s.id, "held", LAND, prid))
            self.assertEqual(e["need"], {"cpu": 0, "gpu": 0})
            self.assertEqual(self.busy("t", resource=LAND).code, store_mod.EXIT_BUSY)
        finally:
            s.release()
        self.assertEqual(self.entries(), [])
        # A parent that holds the resource covers it: the child inherits, as before.
        self.live_record(prid, pid=pp, ctime=ct, has={"cpu": 2}, cores=[0, 1], resource=LAND)
        s = self.book("child", env=env, resource=LAND)
        try:
            self.assertTrue(s.nested)
            self.assertEqual(self.entries(), [])
        finally:
            s.release()

    def test_the_snapshot_and_status_show_both(self):
        from kitlib.lock import status
        self.confine(2)
        first = self.book("first", resource=LAND, priority="land")
        try:
            # A live waiting landing behind it (its wrapper: this process, its run mutex held).
            wid = "r-20261006T120000Z-e1e1e1"
            m = self.store.run_mutex(wid)
            self.addCleanup(m.release)
            q = self.store.read("queue.json")
            q["entries"].append({"id": wid, "ticket": q["ticket"] + 1, "state": "waiting",
                                 "pid": os.getpid(), "ctime": process.own_ctime(), "repo": "kit",
                                 "slug": "next", "kind": "engine-test", "since": store_mod.iso(),
                                 "need": {"cpu": 1, "gpu": 0}, "expect_s": 60, "cores": [],
                                 "gpus": [], "resource": LAND, "priority": "land"})
            q["ticket"] += 1
            self.store.write("queue.json", q)
            snap = api.snapshot(self.store)
            [row] = [r for r in snap["runs"] if r["id"] == first.id]
            self.assertEqual((row["resource"], row["priority"]), (LAND, "land"))
            [w] = snap["queue"]["entries"]
            self.assertEqual((w["id"], w["resource"], w["priority"]), (wid, LAND, "land"))
            self.assertIn("resource %s is held by" % LAND, w["why"])
            text = "\n".join(status.render(snap))
            self.assertIn("resource %s: held by kit/first (run %s, priority land)"
                          % (LAND, first.id), text)
            self.assertIn("run %s, priority land, resource %s)" % (wid, LAND), text)
        finally:
            first.release()
        row = [r for r in api.snapshot(self.store)["runs"]]
        self.assertEqual(row, [])


if __name__ == "__main__":
    unittest.main()
