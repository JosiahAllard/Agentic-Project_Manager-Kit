"""tools/machine_lock.py, the thin entry point: argparse subcommands, the API's error codes as exit
codes, one `hold` verb, and no --lock, --require-lock or -C. Every call runs on
a scratch store with fake hardware through the hidden --test-* flags."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

from .helpers import REPO, StoreCase, clean_env, under_agent_host

TOOL = os.path.join(REPO, "tools", "machine_lock.py")


def load_tool():
    spec = importlib.util.spec_from_file_location("machine_lock_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Cli(StoreCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tool = load_tool()

    def setUp(self):
        super().setUp()
        self.hw_path = os.path.join(self.tmp, "hardware.json")
        with open(self.hw_path, "w", encoding="utf-8") as f:
            json.dump(self.hw, f)

    def flags(self):
        return ["--test-root", self.root, "--test-hardware", self.hw_path,
                "--test-kinds", self.kinds_path]

    def call(self, *argv, agent=False, owner=False, cmd=None):
        """(exit code, stdout, stderr) of main(argv + the test flags [+ -- cmd]). owner / agent:
        --test-actor, the scratch store's stand-in for the caller check (else the process tree
        decides: an agent under the gate)."""
        args = list(argv) + self.flags() + (["--test-actor", "owner"] if owner else
                                            ["--test-actor", "agent"] if agent else [])
        if cmd is not None:
            args += ["--"] + list(cmd)
        env = clean_env(**({"CLAUDE_CODE_SESSION_ID": "sess-cli"} if agent else {}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.tool.main(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_status(self):
        code, out, _ = self.call("status", "--json")
        self.assertEqual(code, 0)
        snap = json.loads(out)
        self.assertIn("schema_version", snap)
        self.assertIn("revision", snap)
        self.assertEqual(self.call("status")[0], 0)
        self.assertEqual(self.call("status", "r-20261006T120000Z-abcdef")[0], 1)

    def test_status_json_carries_resource_and_priority_for_runs_and_queue(self):
        from kitlib.lock import store as store_mod
        from kitlib.proc import process
        land = "landing:q:\\repo\\.git"
        held, wait = "r-20261006T120000Z-aaaaaa", "r-20261006T120000Z-bbbbbb"
        self.live_record(held, resource=land, priority="land", cores=[0])
        for rid in (held, wait):                # live wrappers: this process, run mutexes held
            m = self.store.run_mutex(rid)
            self.addCleanup(m.release)
        base = {"pid": os.getpid(), "ctime": process.own_ctime(), "repo": "kit",
                "kind": "engine-test", "need": {"cpu": 1, "gpu": 0}, "expect_s": 60,
                "gpus": [], "resource": land, "priority": "land"}
        self.store.write("queue.json", {"ticket": 2, "entries": [
            dict(base, id=held, ticket=1, state="held", slug="t", cores=[0],
                 held_at=time.time()),
            dict(base, id=wait, ticket=2, state="waiting", slug="next", cores=[],
                 since=store_mod.iso())]})
        code, out, _ = self.call("status", "--json")
        self.assertEqual(code, 0)
        snap = json.loads(out)
        [row] = [r for r in snap["runs"] if r["id"] == held]
        self.assertEqual((row["resource"], row["priority"]), (land, "land"))
        [w] = snap["queue"]["entries"]
        for key in ("id", "position", "ticket", "repo", "slug", "kind", "needs", "since",
                    "expect_s", "est_start", "backfill", "parked", "blocked_by", "hold", "moved",
                    "resource", "priority", "within_slots", "why"):
            self.assertIn(key, w)
        self.assertEqual((w["id"], w["resource"], w["priority"]), (wait, land, "land"))
        self.assertIn("resource %s is held by" % land, w["why"])
        self.assertIn("error", snap["queue"])

    def test_status_of_a_hold_when_holds_are_unreadable_is_a_store_error(self):
        os.makedirs(self.store.path("holds"), exist_ok=True)
        with open(self.store.path("holds", "h-20261006T120000Z-abcd.json"), "w") as f:
            f.write("{")
        code, _, err = self.call("status", "h-20261006T120000Z-abcd")
        self.assertEqual(code, 10)
        self.assertIn("holds unreadable", err)

    def test_only_run_takes_a_command(self):
        code, _, err = self.call("status", cmd=["python", "-V"])
        self.assertEqual(code, 2)
        self.assertIn("only run and background take a command", err)

    def test_run_usage(self):
        self.assertEqual(self.call("run", "--kind", "engine-test", "--repo", "kit", "--slug",
                                   "t")[0], 2)
        self.assertEqual(self.call("run", "--kind", "no-such-kind", "--repo", "kit", "--slug",
                                   "t", cmd=[sys.executable, "-V"])[0], 2)

    def test_run_within_reaches_the_run_and_bad_syntax_is_usage(self):
        import datetime
        from kitlib.lock import holds as hl
        from kitlib.lock.store import utc_now
        from .helpers import OWNER
        now = utc_now()
        hl.create(self.store, self.layout(), {"owner": "owner", "scope": "whole", "start": now,
                                              "end": now + datetime.timedelta(hours=1)},
                  OWNER, None)
        code, _, err = self.call("run", "--kind", "engine-test", "--repo", "kit", "--slug", "t",
                                 "--within", "8-15", "--cores", "1", "--no-wait",
                                 cmd=[sys.executable, "-V"])
        self.assertEqual(code, 11, err)             # sized and booked, then the owner hold refuses
        ended = [self.store.read("ended/" + n) for n in os.listdir(self.store.path("ended"))
                 if n.startswith("r-")]
        self.assertEqual([(r["within"], r["within_slots"]) for r in ended],
                         [("8-15", {"cpu": [4, 5, 6, 7]})])
        for bad in ("8-", "x", ""):
            with self.subTest(bad=bad):
                code, _, err = self.call("run", "--kind", "engine-test", "--repo", "kit",
                                         "--slug", "t", "--within", bad,
                                         cmd=[sys.executable, "-V"])
                self.assertEqual(code, 2)
                self.assertIn("--within", err)

    def test_run_returns_the_command_code(self):
        self.confine(1)
        code, _, err = self.call("run", "--kind", "engine-test", "--repo", "kit", "--slug", "t",
                                 "--cores", "1", "--no-wait",
                                 cmd=[sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(code, 3, err)
        self.assertEqual(len(self.store.history_records()), 1)

    def test_a_bare_bash_with_no_git_bash_is_refused_before_it_detaches_or_books(self):
        with mock.patch("kitlib.proc.bash.git_bash", return_value=None), \
                mock.patch.object(self.tool, "detach", return_value=0) as det:
            for extra in ((), ("--detach",)):
                with self.subTest(extra=extra):
                    code, _, err = self.call("run", "--kind", "engine-test", "--repo", "kit",
                                             "--slug", "t", "--cores", "1", "--no-wait", *extra,
                                             cmd=["bash", "-c", "exit 0"])
                    self.assertEqual(code, 1, err)
                    self.assertIn("could not start the command", err)
                    self.assertIn("WSL", err)
        det.assert_not_called()
        self.assertFalse(os.path.isdir(self.store.path("ended"))
                         and os.listdir(self.store.path("ended")), "a refused run was booked")

    def test_stop_of_no_run(self):
        self.assertEqual(self.call("stop", "r-20261006T120000Z-abcdef")[0], 1)

    def test_owner_holds_and_agents_cannot_make_them(self):
        self.assertEqual(self.call("hold", "half", "--for", "1h", agent=True)[0], 2)
        self.assertEqual(self.call("hold", "half", owner=True)[0], 2)          # no window
        code, out, _ = self.call("hold", "half", "--for", "1h", owner=True)
        self.assertEqual(code, 0)
        self.assertIn("made: owner hold h-", out)
        code, out, _ = self.call("hold", "list")
        self.assertEqual(code, 0)
        self.assertIn("owner hold h-", out)

    def test_hold_list_when_empty(self):
        code, out, _ = self.call("hold", "list")
        self.assertEqual(code, 0)
        self.assertIn("holds: none", out)

    def test_removed_flags_are_usage(self):
        """--lock, --require-lock and -C are gone; argparse refuses them."""
        for flag in ("--lock", "--require-lock", "-C"):
            with self.subTest(flag=flag):
                code, _, err = self.call("run", "--kind", "engine-test", "--repo", "kit",
                                         "--slug", "t", flag, cmd=[sys.executable, "-V"])
                self.assertEqual(code, 2)
                self.assertIn("unrecognized arguments", err)

    def test_priority_is_not_a_cli_flag_and_resource_is_on_run_only(self):
        """Only library callers (kitlib.gate.land) set a run's priority. --resource is run's
        alone (background does not book through kitlib.lock.run.book), never abbreviated."""
        cases = [("run", ["--priority", "land"]), ("run", ["--prio", "land"]),
                 ("run", ["--res", "x"]), ("background", ["--resource", "x"]),
                 ("background", ["--priority", "land"]), ("background", ["--prio", "land"]),
                 ("background", ["--res", "x"])]
        for verb, flag in cases:
            with self.subTest(verb=verb, flag=flag):
                code, _, err = self.call(verb, "--repo", "kit", "--slug", "t", *flag,
                                         *(["--kind", "engine-test"] if verb == "run" else []),
                                         cmd=[sys.executable, "-V"])
                self.assertEqual(code, 2)
                self.assertIn("unrecognized arguments", err)
        self.assertIsNone(self.store.read("queue.json"))

    def run_with(self, slug, *extra):
        return self.call("run", "--kind", "engine-test", "--repo", "kit", "--slug", slug,
                         "--cores", "1", "--no-wait", *extra, cmd=[sys.executable, "-V"])

    def ended(self, slug):
        return [self.store.read("ended/" + n) for n in os.listdir(self.store.path("ended"))
                if n.startswith("r-") and self.store.read("ended/" + n)["slug"] == slug]

    def test_run_resource_reaches_book_and_a_second_run_naming_it_waits(self):
        """run --resource R books with resource=R (kitlib.lock.run.book): the record carries it,
        and while another live run holds R a second --no-wait run naming R is busy (6) even with
        slots free; one naming another resource or none is not kept out."""
        from kitlib.lock import run as rm
        from kitlib.lock import store as store_mod
        from .helpers import clean_env
        ports = "ports:e2e-a"
        self.confine(2)                         # slots alone would let both run at once
        code, _, err = self.run_with("solo", "--resource", ports)
        self.assertEqual(code, 0, err)
        self.assertEqual([r.get("resource") for r in self.ended("solo")], [ports])
        first = rm.book("engine-test", "kit", "first", cores=1, store=self.store,
                        env=clean_env(), resource=ports)
        try:
            code, _, err = self.run_with("second", "--resource", ports)
            self.assertEqual(code, store_mod.EXIT_BUSY, err)
            self.assertIn("resource %s is held by" % ports, err)
            self.assertIn(first.id, err)
            for extra in (("--resource", "ports:e2e-b"), ()):
                with self.subTest(extra=extra):
                    code, _, err = self.run_with("other", *extra)
                    self.assertEqual(code, 0, err)
        finally:
            first.release()
        code, _, err = self.run_with("second", "--resource", ports)    # free again: it runs
        self.assertEqual(code, 0, err)

    def test_an_empty_resource_is_usage_and_books_nothing(self):
        """An empty --resource is usage (exit 2) before anything is booked, detached or not: a
        detached run must not print '<run id> pid N' and exit 0 with the refusal only in its log."""
        with mock.patch.object(self.tool, "detach", return_value=0) as det:
            for extra in ((), ("--detach",)):
                with self.subTest(extra=extra):
                    code, _, err = self.run_with("t", "--resource", "", *extra)
                    self.assertEqual(code, 2, err)
                    self.assertIn("a resource is a non-empty string", err)
        det.assert_not_called()
        self.assertIsNone(self.store.read("queue.json"))
        self.assertFalse(os.path.isdir(self.store.path("ended")))

    def test_a_landing_resource_is_usage_and_books_nothing(self):
        """landing:<git dir> is land.py's resource (kitlib.gate.land): run --resource refuses
        it before anything is booked, detached or not."""
        with mock.patch.object(self.tool, "detach", return_value=0) as det:
            for extra in ((), ("--detach",)):
                with self.subTest(extra=extra):
                    code, _, err = self.run_with("t", "--resource", "landing:q:\\repo\\.git",
                                                 *extra)
                    self.assertEqual(code, 2, err)
                    self.assertIn("usage: --resource landing:q:\\repo\\.git: landing "
                                  "resources belong to land.py", err)
        det.assert_not_called()
        self.assertIsNone(self.store.read("queue.json"))
        self.assertFalse(os.path.isdir(self.store.path("ended")))

    def test_division_is_refused_under_an_agent_host_with_a_stripped_environment(self):
        # The old CLI made any caller without CLAUDE_CODE_SESSION_ID / CLAUDE_PID the
        # owner: this exited 0 there.
        code, out, err = under_agent_host(self.tmp, [sys.executable, TOOL, "division",
                                                     "--default"] + self.flags())
        self.assertEqual(code, 2, out + err)
        self.assertIn("refused: set_division is the owner's", err)

    def test_the_test_actor_needs_a_scratch_store(self):
        code, _, err = self.call_real("stop", "r-20261006T120000Z-abcdef", "--test-actor", "owner")
        self.assertEqual(code, 2)
        self.assertIn("--test-actor needs --test-root", err)

    def call_real(self, *argv):
        """main(argv) on the machine's store: only for calls refused before anything is read."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_division(self):
        code, _, err = self.call("division", "--set", '{"bogus": 1}', owner=True)
        self.assertEqual(code, 2)
        self.assertIn("unknown key bogus", err)
        code, out, _ = self.call("division", "--set", '{"medium_cores": 1}', owner=True)
        self.assertEqual(code, 0)
        shown = json.loads(out[out.index("{"):])
        self.assertEqual(shown["wanted_division"]["medium_cores"], 1)
        self.assertEqual(self.call("division", "--default", owner=True)[0], 0)

    def test_division_on_a_failed_look_says_why_it_is_not_in_force(self):
        """A look that could not read the gpus is never adopted, idle or not: `division --set`
        gives that reason, not that the division applies once nothing is live or queued."""
        self.layout()
        with open(self.hw_path, "w", encoding="utf-8") as f:
            json.dump(dict(self.hw, gpus=[], source=dict(self.hw["source"], gpu="none"),
                           failed=["gpus"], problems=["Win32_VideoController: exit 1"]), f)
        code, out, _ = self.call("division", "--set", '{"cpu_slots": 6}', owner=True)
        self.assertEqual(code, 0)
        first = out.splitlines()[0]
        self.assertIn("the gpus could not be read (Win32_VideoController: exit 1)", first)
        self.assertNotIn("applies once nothing is live", out)

    def test_division_with_an_invalid_layout_json_under_a_live_run_is_not_in_force_now(self):
        """The wanted layout is used but layout.json is not written: the CLI says that, not
        "in force now"."""
        self.layout()
        self.live_record("r-20261006T120000Z-abcdef")
        self.store.drop("layout.json")
        code, out, _ = self.call("division", "--set", '{"cpu_slots": 6}', owner=True)
        self.assertEqual(code, 0)
        first = out.splitlines()[0]
        self.assertNotIn("in force now", first)
        self.assertIn("is written once no run is live or queued (1 now)", first)

    def test_division_on_a_fresh_failed_look_says_it_is_provisional(self):
        with open(self.hw_path, "w", encoding="utf-8") as f:
            json.dump(dict(self.hw, gpus=[], source=dict(self.hw["source"], gpu="none"),
                           failed=["gpus"], problems=["Win32_VideoController: exit 1"]), f)
        code, out, _ = self.call("division", "--set", '{"cpu_slots": 6}', owner=True)
        self.assertEqual(code, 0)
        first = out.splitlines()[0]
        self.assertNotIn("in force now", first)
        self.assertIn("provisional", first)

    def test_division_under_a_live_run_applies_once_idle(self):
        self.layout()
        self.live_record("r-20261006T120000Z-abcdef")
        code, out, _ = self.call("division", "--set", '{"cpu_slots": 6}', owner=True)
        self.assertEqual(code, 0)
        self.assertIn("division written; it applies once nothing is live or queued",
                      out.splitlines()[0])

    def test_top_is_the_owners(self):
        rid = "r-20261006T120000Z-abcdef"
        code, _, err = self.call("top", rid, agent=True)
        self.assertEqual(code, 2)
        self.assertIn("refused: move_to_top is the owner's", err)
        code, out, err = under_agent_host(self.tmp, [sys.executable, TOOL, "top", rid]
                                          + self.flags())
        self.assertEqual(code, 2, out + err)
        self.assertIn("refused: move_to_top is the owner's", err)
        self.assertEqual(self.call("top", rid, owner=True)[0], 1)              # no such run
        self.assertEqual(self.call("top", "nope", owner=True)[0], 2)

    def test_top_moves_a_waiting_run(self):
        from kitlib.lock.store import iso
        from kitlib.proc import process
        ids = ["r-20261006T120000Z-%s" % x for x in ("a1a1a1", "b1b1b1")]
        es = []
        for n, rid in enumerate(ids, 1):
            m = self.store.run_mutex(rid)
            self.addCleanup(m.release)
            es.append({"id": rid, "ticket": n, "state": "waiting", "pid": os.getpid(),
                       "ctime": process.own_ctime(), "repo": "kit", "slug": "t%d" % n,
                       "kind": "engine-test", "need": {"cpu": 99, "gpu": 0}, "expect_s": 60,
                       "since": iso(), "cores": [], "gpus": []})
        self.store.write("queue.json", {"ticket": 2, "entries": es})
        code, out, err = self.call("top", ids[1], owner=True)
        self.assertEqual(code, 0, err)
        self.assertIn("run %s moved to the top of the queue (position 1)" % ids[1], out)
        code, out, _ = self.call("status")
        self.assertIn("moved to the top by the owner", out)

    def test_game(self):
        code, out, _ = self.call("game", "set", '[{"exe": "NoSuchGame.exe"}]', owner=True)
        self.assertEqual(code, 0)
        self.assertIn("1 game(s) listed", out)
        self.assertEqual(self.call("game", "set", '[{"exe": "NoSuchGame"}]', owner=True)[0], 2)
        self.assertEqual(self.call("game", "on", agent=True)[0], 2)
        code, out, _ = self.call("game")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["enabled"])


class LoadNodes(Cli):
    """run's --node, --artifact and the hidden --registry, and status's `node:` lines. The
    registry is a temp file (registry.ignored stood in), ssh a runner, the node NodeJob.run."""

    def setUp(self):
        super().setUp()
        from kitlib.loadnode import registry
        from tests import gitfixture
        from tests.loadnode.test_registry import node, text
        self.repo = gitfixture.TempRepo(branch="main")
        self.addCleanup(self.repo.cleanup)
        self.repo.write("a.txt", "one" + chr(10))
        self.repo.commit("add a", "a.txt")
        self.file = os.path.join(self.tmp, "load-nodes.json")
        with open(self.file, "w", encoding="utf-8") as f:
            f.write(text(node(memory_mb=4096, repos=["kit"])))
        p = mock.patch.object(registry, "ignored", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.repo.root)
        self.node_runs = []

    def node_run(self, code):
        def run(nj):
            self.node_runs.append((nj.node.name, nj.cmd, nj.artifacts))
            return code
        return mock.patch("kitlib.loadnode.job.NodeJob.run", run)

    def ssh(self, runner):
        return mock.patch("kitlib.loadnode.remote.default_runner", runner)

    def run_on(self, *extra, cmd=None):
        return self.call("run", "--kind", "engine-test", "--repo", "kit", "--slug", "t",
                         "--no-wait", *extra, cmd=cmd or ["python", "x.py"])

    def test_no_registry_is_no_node_line_and_no_routing(self):
        def no_ssh(argv, timeout_s):
            raise AssertionError("ssh was asked")
        with self.ssh(no_ssh), self.node_run(5):
            code, out, _ = self.call("status")
            self.assertEqual(code, 0)
            self.assertNotIn("node:", out)
            self.confine(1)
            code, _, err = self.run_on("--cores", "1", cmd=[sys.executable, "-c",
                                                            "import sys; sys.exit(3)"])
        self.assertEqual(code, 3, err)
        self.assertEqual(self.node_runs, [])
        self.assertEqual(len(self.store.history_records()), 1)

    def test_status_says_a_node_line_for_each_node(self):
        from tests.loadnode.test_remote import Runner, ok_reply
        with self.ssh(Runner(out=ok_reply())):
            code, out, _ = self.call("status", "--registry", self.file)
        self.assertEqual(code, 0)
        self.assertIn("node: box1  ok  cpu 3/4 free", out)
        with self.ssh(Runner(rc=255, err=b"Host key verification failed.")):
            code, out, _ = self.call("status", "--registry", self.file)
        self.assertIn("node: box1  host key mismatch", out)
        code, out, _ = self.call("status", "--json", "--registry", self.file)
        self.assertNotIn("node:", out)

    def test_a_named_node_runs_it_there_and_its_code_comes_back(self):
        with self.node_run(5):
            code, _, err = self.run_on("--node", "box1", "--registry", self.file,
                                       "--artifact", "out/*.txt")
        self.assertEqual(code, 5, err)
        self.assertEqual(self.node_runs, [("box1", ["python", "x.py"], ["out/*.txt"])])
        self.assertFalse(os.path.isdir(self.store.path("ended"))
                         and os.listdir(self.store.path("ended")), "a node's run was booked here")

    def test_node_main_and_a_resource_run_it_here(self):
        def no_ssh(argv, timeout_s):
            raise AssertionError("ssh was asked")
        self.confine(1)
        for extra in (("--node", "main"), ("--node", "box1", "--node", "main"),
                      ("--resource", "vm:x")):
            with self.subTest(extra=extra), self.node_run(5), self.ssh(no_ssh):
                code, _, err = self.run_on("--registry", self.file, *extra,
                                           cmd=["python", "-c", "import sys; sys.exit(3)"])
                self.assertEqual(code, 3, err)
                self.assertNotIn("load node", err)
        self.assertEqual(self.node_runs, [])
        self.assertEqual(len(self.store.history_records()), 3)

    def test_node_usage_is_2_and_books_nothing(self):
        for extra, want in ((("--node", "nope"), "no node named nope"),
                            (("--node", "box1", "--cores", "1"), "does not take --cores"),
                            (("--node", "box1", "--detach"), "does not detach"),
                            (("--artifact", "../x"), "not a relative glob"),
                            (("--artifact", "out/caf%s.txt" % chr(0xe9)), "ASCII")):
            with self.subTest(extra=extra), self.node_run(5):
                code, _, err = self.run_on("--registry", self.file, *extra)
                self.assertEqual(code, 2, err)
                self.assertIn(want, err)
        self.assertEqual(self.node_runs, [])
        self.assertFalse(os.path.isdir(self.store.path("ended"))
                         and os.listdir(self.store.path("ended")))
        self.assertNotIn("--registry", self.tool.parser().format_help())


if __name__ == "__main__":
    unittest.main()
