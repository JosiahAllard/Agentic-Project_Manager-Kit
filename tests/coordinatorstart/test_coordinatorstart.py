import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib import coordinatorstart
from kitlib.config import user as kuser
from kitlib.lockmcp import CLAUDE_PLUGIN_NAME

HOOK = os.path.join(os.path.dirname(__file__), "..", "..", "claude", "hooks", "coordinator_session_start.py")
COND = {"PM_KIT_ROLE": "coordinator"}
P = CLAUDE_PLUGIN_NAME + ":"          # the kit plugin's agent prefix (kitlib.agentname.PREFIX)


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class RenderTest(unittest.TestCase):
    def test_role_unset_prints_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"standing": ["a"]}}))
            self.assertIsNone(coordinatorstart.render(env={}, path=p))
            self.assertIsNone(coordinatorstart.render(env={"PM_KIT_ROLE": "pm"}, path=p))

    def test_a_plugin_prefixed_mark_is_the_coordinator_and_no_other_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"standing": ["one"]}}))
            for role in (P + "coordinator", P.upper() + "coordinator", P + "conductor"):
                self.assertEqual(coordinatorstart.render(env={"PM_KIT_ROLE": role}, path=p, live=False),
                                 "Coordinator session start:\n- one", role)
            for role in ("some-plugin:coordinator", P + "pm", P):
                self.assertIsNone(coordinatorstart.render(env={"PM_KIT_ROLE": role}, path=p), role)

    def test_role_set_lists_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"standing": ["one", "two"]}}))
            out = coordinatorstart.render(env=COND, path=p, live=False)
        self.assertEqual(out, "Coordinator session start:\n- one\n- two")

    def test_pricing_line_prices_at_start_and_leaves_follow_ups_unpriced(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": {"pricing_pass": True}}))
            out = coordinatorstart.render(env=COND, path=p, live=False)
        self.assertEqual(len(out.splitlines()), 2, out)   # the heading and one line
        line = out.splitlines()[1]
        self.assertTrue(line.startswith("- Pricing pass and backlog audit before your first dispatch, "
                                        "follow-up records excepted"), line)
        for want in ("python tools/backlog.py list --needs-priority",
                     "A follow-up record (filed from a finding on a branch, a review or a worker's report; "
                     "a new one says so in its summary) stays unpriced until you or the PM promote it",
                     "Check every other unpriced open record (an owner story, a record from the PM's brief) "
                     "for duplicates, size and fit",
                     "--priority-by agent",
                     "Promote a follow-up at any time it enters a work set", "price before pull",
                     "When `next --all` lists fewer startable records than the set needs",
                     "price only those going into this set; never a sweep",
                     "`next` never offers an unpriced record",
                     "under `unprioritised+open`, not under `held` (`held` = blocked)",
                     "goes to the PM", "never ask him directly"):
            self.assertIn(want, line)
        # Withdrawn wording: no pass at all, unpriced called held, the capped-offer and duplicate triggers.
        for gone in ("No start-up pricing pass", "is held", "stay held", "held is not waiting",
                     "`next` offers fewer items", "duplicates or replaces a priced one", "each unpriced"):
            self.assertNotIn(gone, line)
        # "held" only where it is true: the banner's group, twice, and "owner-held".
        self.assertEqual(line.count("held"), 3, line)
        self.assertEqual(line.count("`held`"), 2, line)
        self.assertEqual(line.count("owner-held"), 1, line)

    def test_pricing_line_is_off_unless_the_config_switches_it_on(self):
        with tempfile.TemporaryDirectory() as d:
            for cfg in ({"standing": ["x"]}, {"pricing_pass": False}, {"pricing_pass": "yes"}):
                p = write(os.path.join(d, "c.json"), json.dumps({"coordinator_start": cfg}))
                out = coordinatorstart.render(env=COND, path=p, live=False)
                self.assertNotIn(coordinatorstart.PRICING_LINE, out)
                self.assertNotIn("pricing pass", out.lower())

    def test_missing_key_or_file_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), "{}")
            self.assertIn("skipped", coordinatorstart.render(env=COND, path=p, live=False))
            self.assertIn("skipped", coordinatorstart.render(env=COND, path=os.path.join(d, "none.json"), live=False))

    def test_bad_json_is_one_could_not_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.json"), "{not json")
            out = coordinatorstart.render(env=COND, path=p, live=False)
        self.assertIn("could not check config:", out)
        self.assertEqual(len(out.splitlines()), 1)


NOW = datetime.datetime(2026, 10, 6, 18, 30, tzinfo=datetime.timezone.utc)
TODAY = NOW.date()
ROOT = "D:/src"
ENTRY = {"repo": "alpha", "agent": True, "stem": "alpha-coordinator",
         "state": "STATE.md", "name": "alpha-coordinator-a2-n6"}


def reader(files):
    def read(p):
        if p not in files:
            raise FileNotFoundError(p)
        return files[p]
    return read


def proc(pid, ppid, name, cmd, exe="C:/tools/claude.exe", created="2026-10-06T10:00:00"):
    return {"ProcessId": pid, "ParentProcessId": ppid, "Name": name, "CommandLine": cmd,
            "ExecutablePath": exe, "Created": created}


class LoadConfigBomTest(unittest.TestCase):
    def test_reads_bom_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "wb") as f:
                f.write(b"\xef\xbb\xbf" + json.dumps({"coordinator_start": {"k": 1}}).encode("utf-8"))
            self.assertEqual(coordinatorstart.load_config(p), {"k": 1})


class FindEntryTest(unittest.TestCase):
    S = {"alpha": ENTRY, "gamma": dict(ENTRY, agent=False, stem="gamma-training"),
         "Beta": {"repo": "Beta", "agent": True, "stem": "beta-coordinator", "name": "g"}}

    def test_cwd_top_folder_and_agent_true(self):
        e, note = coordinatorstart.find_entry(self.S, ROOT + "/alpha/tools", root=ROOT)
        self.assertEqual((e["stem"], note), ("alpha-coordinator", None))
        self.assertEqual(coordinatorstart.find_entry(self.S, "d:/SRC/beta", root=ROOT)[0]["name"], "g")

    def test_no_repos_root_is_a_key_error(self):
        with self.assertRaises(KeyError):
            coordinatorstart.find_entry(self.S, ROOT + "/alpha")

    def test_not_under_root_or_no_entry(self):
        with self.assertRaises(ValueError):
            coordinatorstart.find_entry(self.S, "C:/x", root=ROOT)
        with self.assertRaises(LookupError):
            coordinatorstart.find_entry(self.S, ROOT + "/other", root=ROOT)

    def test_ambiguous_prefers_name_else_first_and_says_so(self):
        two = {"a": dict(ENTRY, name="n1", stem="s1"), "b": dict(ENTRY, name="n2", stem="s2")}
        self.assertEqual(coordinatorstart.find_entry(two, ROOT + "/alpha", "n2", ROOT)[0]["name"], "n2")
        e, note = coordinatorstart.find_entry(two, ROOT + "/alpha", None, ROOT)
        self.assertEqual(e["name"], "n1")
        self.assertIn("2 sessions entries", note)


class CapReviewTest(unittest.TestCase):
    CAPS = {"alpha": [{"cap": 6, "until": "2026-10-10"}, {"cap": 3}]}

    def test_cap_first_unexpired(self):
        self.assertEqual(coordinatorstart.cap({"cap": self.CAPS}, "alpha", TODAY),
                         ["agent cap 6 until 2026-10-10, then 3"])
        self.assertEqual(coordinatorstart.cap({"cap": self.CAPS}, "alpha", datetime.date(2026, 10, 11)),
                         ["agent cap 3"])

    def test_cap_missing(self):
        with self.assertRaises(KeyError):
            coordinatorstart.cap({}, "alpha", TODAY)
        with self.assertRaises(KeyError):
            coordinatorstart.cap({"cap": {}}, "alpha", TODAY)


STATUS = {"reservations": {"active": [], "upcoming": [
              {"start": "2026-10-06T20:00:00Z", "end": "2026-10-06T22:00:00Z", "start_local": "16:00",
               "end_local": "18:00", "note": "gaming"},
              {"start": "2026-10-07T20:00:00Z", "end": "2026-10-07T22:00:00Z", "start_local": "x", "end_local": "y"}]},
          "lock": {"bookings": [{"start": "2026-10-06T18:00:00Z", "end": "2026-10-06T19:00:00Z",
                                 "start_local": "14:00", "end_local": "15:00"}]},
          "gpus": [{"idx": 0, "holder_id": "a"}, {"idx": 1, "holder_id": None}],
          "cores": [{"holder_id": "a"}, {"holder_id": None}, {"holder_id": None}]}
LOCK = {"lane": "full", "branches": ["b1"], "pid": None, "phase": "gate", "started_utc": "T"}


class MachineTest(unittest.TestCase):
    def run_fake(self, status=STATUS):
        calls = []

        def run(args, timeout=10):
            calls.append(args)
            return json.dumps(status) if "machine_lock.py" in args[1] else ROOT + "/alpha/.git\n"
        return run, calls

    def test_summary_and_landing_lock(self):
        run, calls = self.run_fake()
        read = reader({os.path.join(ROOT + "/alpha/.git", "alpha-landing.lock"): json.dumps(LOCK)})
        out = coordinatorstart.machine(ENTRY, run, read, NOW, "H", ROOT, "alpha-landing.lock")
        self.assertEqual(out[0], "machine: owner reservation 16:00-18:00: gaming; booking 14:00-15:00")
        self.assertEqual(out[1], "machine: free gpu slots 1 of 2; free cpu slots 2 of 3")
        self.assertIn("HELD - lane full, branches b1, a token holder, phase gate", out[2])
        self.assertEqual(calls[0][-2:], ["status", "--json"])
        self.assertTrue(calls[0][1].replace("\\", "/").endswith("H/.claude/tools/machine_lock/machine_lock.py"))

    def test_lock_free_and_no_landing_lock_configured(self):
        run, _ = self.run_fake(dict(STATUS, reservations={}, lock={}))
        out = coordinatorstart.machine(ENTRY, run, reader({}), NOW, "H", ROOT, "alpha-landing.lock")
        self.assertIn("no owner reservation or booking within 4 h", out[0])
        self.assertEqual(out[2], "landing lock (alpha): free")
        for root, lock_file in ((ROOT, None), (None, "alpha-landing.lock")):
            out = coordinatorstart.machine(ENTRY, run, reader({}), NOW, "H", root, lock_file)
            self.assertEqual(len(out), 2)

    def test_lock_file_for_reads_the_config(self):
        cfg = {"landing_lock": {"alpha": "alpha-landing.lock"}}
        self.assertEqual(coordinatorstart.lock_file_for(cfg, "alpha"), "alpha-landing.lock")
        self.assertEqual(coordinatorstart.lock_file_for(cfg, "Alpha"), "alpha-landing.lock")
        self.assertIsNone(coordinatorstart.lock_file_for(cfg, "beta"))
        self.assertIsNone(coordinatorstart.lock_file_for({}, "alpha"))

    def test_runner_failure_propagates(self):
        def run(args, timeout=10):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            coordinatorstart.machine(ENTRY, run, reader({}), NOW, "H")


class OthersTest(unittest.TestCase):
    def procs(self):
        rc = '"C:/tools/claude.exe" --remote-control "%s" -n x'
        return [proc(1, 0, "claude.exe", rc % "alpha-coordinator-a2-n5", created="2026-10-06T08:00"),
                proc(2, 0, "claude.exe", rc % "alpha-coordinator-a2-n6", created="2026-10-06T09:00"),
                proc(3, 2, "bash.exe", "bash", exe="C:/bash.exe"),
                proc(4, 3, "python.exe", "hook", exe="C:/py.exe"),
                proc(5, 0, "claude.exe", rc % "beta-coordinator-a2"),
                proc(6, 0, "claude.exe", "Claude.exe", exe="C:/Program Files/WindowsApps/Claude/Claude.exe")]

    def test_own_name_from_process_tree(self):
        self.assertEqual(coordinatorstart.own_name(self.procs(), {}, 4), "alpha-coordinator-a2-n6")
        self.assertEqual(coordinatorstart.own_name(self.procs(), {"PM_KIT_SESSION_NAME": "z"}, 4), "z")
        self.assertIsNone(coordinatorstart.own_name(self.procs(), {}, 99))

    def test_lists_predecessor(self):
        out = coordinatorstart.others(ENTRY, self.procs(), {}, 4)
        self.assertEqual(len(out), 1)
        self.assertIn("also running: alpha-coordinator-a2-n5; if one is your winding-down", out[0])
        self.assertNotIn("beta", out[0])

    def test_unknown_self_is_newest_and_alone_prints_nothing(self):
        self.assertIn("a2-n5", coordinatorstart.others(ENTRY, self.procs(), {}, 99)[0])
        self.assertEqual(coordinatorstart.others(ENTRY, self.procs()[1:], {}, 4), [])
        self.assertEqual(coordinatorstart.others(ENTRY, [], {}, 4), [])

    def test_no_stem_raises(self):
        with self.assertRaises(KeyError):
            coordinatorstart.others({"repo": "x"}, [], {}, 1)


class ContextTest(unittest.TestCase):
    PM = {"sessions_file": "S.json"}

    def build(self, cfg, **kw):
        files = {"S.json": json.dumps({"alpha": ENTRY})}

        def run(args, timeout=10):
            return json.dumps(STATUS) if "machine_lock.py" in args[1] else "G"
        base = dict(env={"CLAUDE_CONFIG_DIR": "C:/u/.claude-account2"}, cwd=ROOT + "/alpha", now=NOW,
                    procs=[], run=run, read=reader(files), home="H", pid=1, pm_cfg=self.PM)
        base.update(kw)
        return coordinatorstart.context(dict(dict(repos_root=ROOT), **cfg), **base)

    def test_standing_first_then_checks(self):
        cfg = {"standing": ["wind down"], "cap": {"alpha": [{"cap": 4}]}}
        text = self.build(cfg)
        # The cap check is the first live check: its line follows the standing line.
        self.assertEqual(text.split("\n")[:3], ["Coordinator session start:", "- wind down", "- agent cap 4"])
        self.assertIn("- machine: free gpu slots 1 of 2", text)
        self.assertNotIn("also running", text)
        self.assertNotIn("usage", text.lower())

    def test_eng_filing_lines_follow_the_standing_lines(self):
        lines = self.build({"standing": ["wind down"], "eng_filing": True, "pricing_pass": True}).splitlines()
        self.assertEqual(lines[1], "- wind down")
        self.assertTrue(lines[2].startswith("- ENG rulings:"))
        self.assertEqual(lines[3], "- " + coordinatorstart.PRICING_LINE)
        self.assertEqual(lines[4], "- could not check cap: cap not configured")   # the first live check
        self.assertIn("ENG <repo> <item>: <question> | options: a) .. b) .. | rec: <x> | why: <one line>", lines[2])
        self.assertIn("consultant-<repo>", lines[2])
        for owner_only in ("owner ruling", "Realism", "Security", "Money and accounts", "Priority", "Go-live"):
            self.assertIn(owner_only, lines[2])

    def test_eng_filing_line_is_off_unless_the_config_switches_it_on(self):
        for cfg in ({"standing": ["x"]}, {"standing": ["x"], "eng_filing": False}, {"eng_filing": "yes"}):
            self.assertNotIn("ENG rulings", self.build(cfg))

    def test_missing_config_gives_one_line_per_check(self):
        text = self.build({})
        self.assertIn("could not check cap: cap not configured", text)
        self.assertNotIn("usage", text.lower())

    def test_no_sessions_file_and_no_cwd_match_isolated(self):
        text = self.build({}, pm_cfg={})
        self.assertIn("could not check cap: pm_start.sessions_file not configured", text)
        self.assertIn("could not check other coordinators:", text)
        self.assertIn("could not check cap: LookupError", self.build({}, cwd=ROOT + "/zzz"))
        self.assertIn("could not check cap: ValueError", self.build({}, cwd="C:/elsewhere"))

    def test_default_read_takes_a_bom_sessions_file(self):
        # regression-only: the default _read is utf-8-sig, so a PowerShell-saved sessions file loads
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sessions.json")
            with open(p, "wb") as f:
                f.write(b"\xef\xbb\xbf" + json.dumps({"alpha": ENTRY}).encode("utf-8"))
            text = coordinatorstart.context(
                {"repos_root": ROOT, "cap": {"alpha": [{"cap": 4}]}},
                env={}, cwd=ROOT + "/alpha", now=NOW, procs=[], run=lambda a, timeout=10: "G",
                home="H", pid=1, pm_cfg={"sessions_file": p})
        self.assertIn("- agent cap 4", text)

    def test_repos_root_comes_from_the_config(self):
        self.assertIn("could not check cap: repos_root not configured", self.build({"repos_root": None}))


class NoRulingsCheckTest(unittest.TestCase):
    """The start reads no rulings ledger: no check, no line, no reader, whatever the config says."""
    CAP = {"cap": {"alpha": [{"cap": 4}]}}

    def test_no_rulings_line_with_or_without_a_ledger_key(self):
        t = ContextTest()
        plain, keyed = t.build(self.CAP), t.build(dict(self.CAP, ledger="L.md"))
        self.assertEqual(plain, keyed)                   # a "ledger" key in the config changes nothing
        for text in (plain, keyed, t.build({}), t.build({"ledger": "L.md"})):
            self.assertNotIn("rulings newer", text)
            self.assertNotIn("could not check rulings", text)
            self.assertNotIn("rulings", text.lower())

    def test_no_rulings_line_when_no_check_can_run(self):
        # No sessions file, or a cwd outside repos_root: every check fails, and none is a rulings check.
        t = ContextTest()
        for text in (t.build({"ledger": "L.md"}, pm_cfg={}), t.build({"ledger": "L.md"}, cwd="C:/elsewhere")):
            self.assertIn("could not check cap:", text)
            self.assertNotIn("rulings", text.lower())

    def test_only_the_sessions_file_is_read(self):
        # The entry's state note and a configured ledger both exist; neither is opened.
        files = {"S.json": json.dumps({"alpha": ENTRY}), "STATE.md": "a note", "L.md": "a ledger"}
        seen = []

        def read(p):
            seen.append(p)
            return files[p]
        ContextTest().build(dict(self.CAP, ledger="L.md"), read=read)
        self.assertEqual(seen, ["S.json"])

    def test_the_reader_and_its_limits_are_gone(self):
        for name in ("rulings", "ledger_file", "LEDGER_CAP", "DECIDES_CHARS", "safepath"):
            self.assertFalse(hasattr(coordinatorstart, name), name)


class StubsLineTest(unittest.TestCase):

    def test_silent_when_current_and_one_line_when_not(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "alpha"))
            seen = []

            def check(p):
                seen.append(p)
                return None
            self.assertEqual(coordinatorstart.stubs_line(ENTRY, d, check), [])
            self.assertEqual(seen, [os.path.join(d, "alpha")])
            self.assertEqual(coordinatorstart.stubs_line(ENTRY, d, lambda p: "stubs (alpha): 1 missing/stale: gate"),
                             ["stubs (alpha): 1 missing/stale: gate"])

    def test_missing_folder_is_could_not_check_not_a_traceback(self):
        self.assertEqual(coordinatorstart.stubs_line(ENTRY, "D:/no/such/root", lambda p: None),
                         ["could not check stubs: no repository folder %s" % os.path.join("D:/no/such/root", "alpha")])

    def test_no_repos_root_is_a_key_error(self):
        with self.assertRaises(KeyError):
            coordinatorstart.stubs_line(ENTRY, None)

    def test_context_prints_the_check_last_and_isolates_it(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "alpha"))
            text = ContextTest().build({"repos_root": d, "cap": {"alpha": [{"cap": 4}]}},
                                       cwd=d + "/alpha", stubs_check=lambda p: "stubs (alpha): 2 missing/stale: a, b")
            self.assertEqual(text.splitlines()[-1], "- stubs (alpha): 2 missing/stale: a, b")

    def test_context_has_no_stubs_line_when_current(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "alpha"))
            t = ContextTest()
            text = t.build({"repos_root": d, "cap": {"alpha": [{"cap": 4}]}}, cwd=d + "/alpha", stubs_check=lambda p: None)
            self.assertNotIn("stubs", text)

    def test_a_raising_check_is_a_could_not_check_stubs_line(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "alpha"))

            def boom(p):
                raise OSError("disk gone")
            text = ContextTest().build({"repos_root": d, "cap": {"alpha": [{"cap": 4}]}}, cwd=d + "/alpha", stubs_check=boom)
            self.assertIn("could not check stubs: OSError: disk gone", text)


class HookTest(unittest.TestCase):
    def run_hook(self, role, config):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(kuser.config_dir(d))
            write(kuser.config_file(d), config)
            env = {k: v for k, v in os.environ.items() if k != "PM_KIT_ROLE"}
            env.update(USERPROFILE=d, HOME=d)
            if role:
                env["PM_KIT_ROLE"] = role
            return subprocess.run([sys.executable, HOOK], capture_output=True, text=True, env=env, timeout=30,
                                  input=json.dumps({"cwd": "D:/src/elsewhere"}))

    def test_role_unset_silent_exit_0(self):
        r = self.run_hook(None, json.dumps({"coordinator_start": {"standing": ["a"]}}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_role_coordinator_emits_context(self):
        r = self.run_hook("coordinator", json.dumps({"coordinator_start": {"standing": ["wind down"]}}))
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("- wind down", out["additionalContext"])

    def test_a_plugin_prefixed_mark_still_fires(self):
        r = self.run_hook(P + "coordinator", json.dumps({"coordinator_start": {"standing": ["wind down"]}}))
        self.assertEqual(r.returncode, 0)
        self.assertIn("- wind down", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_another_plugins_prefix_stays_silent(self):
        r = self.run_hook("some-plugin:coordinator", json.dumps({"coordinator_start": {"standing": ["a"]}}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_role_coordinator_checks_run_and_never_fail_the_hook(self):
        # temp HOME: no pm_start config, no machine_lock.py -> one could-not line per check
        r = self.run_hook("coordinator", json.dumps({"coordinator_start": {"standing": ["s"]}}))
        self.assertEqual(r.returncode, 0)
        text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(text.split("\n")[1], "- s")
        for name in ("cap", "machine state", "other coordinators", "stubs"):
            self.assertIn("could not check %s:" % name, text)
        self.assertNotIn("rulings", text.lower())

    def test_old_role_and_old_config_key_still_work(self):
        r = self.run_hook("conductor", json.dumps({"conductor_start": {"standing": ["wind down"]}}))
        self.assertEqual(r.returncode, 0)
        self.assertIn("- wind down", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_new_key_wins_over_the_old_key(self):
        r = self.run_hook("coordinator", json.dumps({"conductor_start": {"standing": ["old"]},
                                                     "coordinator_start": {"standing": ["new"]}}))
        text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- new", text)
        self.assertNotIn("- old", text)

    def test_bad_json_exit_0(self):
        r = self.run_hook("coordinator", "{bad")
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not check", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
