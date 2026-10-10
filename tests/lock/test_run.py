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
import threading
import time
import unittest
from unittest import mock

from .helpers import FAST, OWNER, REPO, StoreCase, clean_env, own_affinity

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
        # The installed PM-kit machine_lock.py refuses every run
        # while a live record's `layout` is not its own key. On this store's machine (24 threads,
        # SMT 2, one card) that key is the string below (lay.slot_key), not layout_key's.
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


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
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

    def test_ctrl_c_while_it_waits_in_the_queue(self):
        one_usable_slot(self)
        with rm.slot("engine-test", "kit", "first", cores=1, store=self.store,
                     env=clean_env()) as launch:
            first = launch.id
            t = threading.Timer(1.0, _thread.interrupt_main)
            t.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    rm.book("engine-test", "kit", "second", cores=1, store=self.store,
                            env=clean_env(), wait_s=30)
            finally:
                t.cancel()
        hist = [d for d in self.store.history_records() if d["id"] != first]
        ended = [self.store.read("ended/" + n) for n in os.listdir(self.store.path("ended"))
                 if n.startswith("r-") and n != first + ".json"]
        self.assertEqual([(e["how"], e["code"], e["slug"]) for e in ended],
                         [("interrupted", 130, "second")])
        self.assertEqual(hist, [])
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])

    def test_holds_unreadable_after_it_joined_keep_its_place_until_its_wait_runs_out(self):
        # A run already queued is not refused 10: it takes nothing and looks again, then leaves
        # the queue at its --max-wait (8).
        one_usable_slot(self)

        def spoil():
            with open(self.store.path("holds", "h-20261006T120000Z-abcd.json"), "w") as f:
                f.write("{")
        with rm.slot("engine-test", "kit", "first", cores=1, store=self.store,
                     env=clean_env()):
            t = threading.Timer(0.5, spoil)
            t.start()
            try:
                with self.assertRaises(LockError) as cm:
                    rm.book("engine-test", "kit", "second", cores=1, store=self.store,
                            env=clean_env(), wait_s=2)
            finally:
                t.cancel()
        self.assertEqual(cm.exception.code, store_mod.EXIT_TIMEOUT)
        self.assertIn("the holds are unreadable", str(cm.exception))
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


if __name__ == "__main__":
    unittest.main()
