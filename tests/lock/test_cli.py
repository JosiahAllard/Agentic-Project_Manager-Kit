"""tools/machine_lock.py, the thin entry point: argparse subcommands, the API's error codes as exit
codes, one `hold` verb, and no --lock, --require-lock or -C. Every call runs on
a scratch store with fake hardware through the hidden --test-* flags."""
import contextlib
import importlib.util
import io
import json
import os
import sys
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

    def test_resource_and_priority_are_not_cli_flags(self):
        """Only library callers (kitlib.gate.land) set a run's resource and priority."""
        for verb in ("run", "background"):
            for flag in (["--resource", "x"], ["--priority", "land"], ["--res", "x"],
                         ["--prio", "land"]):
                with self.subTest(verb=verb, flag=flag):
                    code, _, err = self.call(verb, "--kind", "engine-test", "--repo", "kit",
                                             "--slug", "t", *flag, cmd=[sys.executable, "-V"])
                    self.assertEqual(code, 2)
                    self.assertIn("unrecognized arguments", err)
        self.assertIsNone(self.store.read("queue.json"))

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


if __name__ == "__main__":
    unittest.main()
