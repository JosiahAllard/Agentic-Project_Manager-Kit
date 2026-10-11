import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib import pmstart
from kitlib.pmstart import fglock

NOW = datetime.datetime(2026, 10, 6, 9, 0)
CFG = {"hub_url": "https://claude.ai/artifact/X", "hub_source": "claude/hub/action-hub.html",
       "pm_watch_cmd": "python launcher/pm_watch.py", "memory_dir": "M",
       "reminders_file": "r.json", "sessions_file": "s.json"}


def setUpModule():
    """The checklist must not read this machine's pm_data setting (kit_sync has its own tests)."""
    global _orig_settings
    _orig_settings = pmstart.pm_data_settings
    pmstart.pm_data_settings = lambda: None
    global _orig_read_live
    _orig_read_live = fglock.read_live
    fglock.read_live = lambda user32=None: 0
    global _orig_repos
    _orig_repos = pmstart.launcher_repos
    pmstart.launcher_repos = lambda: {}   # nor this machine's launcher.repos (the stubs check)


def tearDownModule():
    pmstart.launcher_repos = _orig_repos
    pmstart.pm_data_settings = _orig_settings
    fglock.read_live = _orig_read_live


def proc(name, cmd, exe=""):
    return {"Name": name, "CommandLine": cmd, "ExecutablePath": exe}


class StandingTest(unittest.TestCase):
    FAKES = dict(handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
                 liveness={"read": lambda p: "{}"})

    def test_lines(self):
        cfg = dict(CFG, standing=["a", "b"])
        self.assertEqual(pmstart.standing(cfg), ["standing rule: a", "standing rule: b"])

    def test_missing_gives_no_lines(self):
        self.assertEqual(pmstart.standing(CFG), [])

    def test_first_in_checklist(self):
        txt = pmstart.checklist(dict(CFG, standing=["x"]), NOW, procs=[], **self.FAKES)
        self.assertEqual(txt.split("\n")[1], "- standing rule: x")

    def test_bad_value_isolated(self):
        txt = pmstart.checklist(dict(CFG, standing=5), NOW, procs=[], **self.FAKES)
        self.assertIn("could not check standing:", txt)
        self.assertIn("republish", txt)


class HubTest(unittest.TestCase):
    def test_step_names_url(self):
        (s,) = pmstart.hub(CFG)
        self.assertIn("republish the kit's Action Hub page claude/hub/action-hub.html "
                      "to https://claude.ai/artifact/X with the Artifact tool", s)
        self.assertIn("docs/action-hub.md (For the PM)", s)
        self.assertNotIn("republish claude/hub/action-hub.html to", s)  # the old bare line
        self.assertIn("pm_ack", s)


class WatcherTest(unittest.TestCase):
    def test_running(self):
        self.assertEqual(pmstart.watcher(CFG, [proc("python.exe", "python launcher/pm_watch.py")]), [])

    def test_not_running_gives_start_step(self):
        # a non-python process mentioning pm_watch.py (e.g. an editor) does not count
        (s,) = pmstart.watcher(CFG, [proc("code.exe", "code pm_watch.py")])
        self.assertIn("python launcher/pm_watch.py", s)


class HandoffTest(unittest.TestCase):
    def test_newest_by_mtime(self):
        times = {"M/pm-handoff-a.md": 1, "M/pm-handoff-b.md": 5}
        seen = []
        out = pmstart.handoff(CFG, globber=lambda p: seen.append(p) or list(times), mtime=times.get)
        self.assertEqual(out, ["read first: M/pm-handoff-b.md"])
        self.assertTrue(seen[0].endswith("pm-handoff-*.md"))

    def test_none(self):
        (s,) = pmstart.handoff(CFG, globber=lambda p: [])
        self.assertIn("no handoff", s)


class RemindersTest(unittest.TestCase):
    def test_due_and_overdue_only(self):
        data = json.dumps([{"due": "2026-10-07", "text": "later"}, {"due": "2026-10-06", "text": "today"},
                           {"due": "2026-10-01", "text": "old"}])
        out = pmstart.reminders(CFG, NOW.date(), read=lambda p: data)
        self.assertEqual(out, ["reminder due 2026-10-01: old", "reminder due 2026-10-06: today"])


class LivenessTest(unittest.TestCase):
    SESS = json.dumps({"_note": "x", "alpha": {"name": "al-a2-n5"},
                       "Beta": {"name": "be-a2"}, "paused": {"name": "p", "autostart": False}})

    def test_rc_name_forms(self):
        self.assertEqual(pmstart.rc_name('claude --remote-control "a b"'), "a b")
        self.assertEqual(pmstart.rc_name("claude --remote-control=x1"), "x1")
        self.assertIsNone(pmstart.rc_name("claude"))

    def test_dead_listed_exact_match_store_app_ignored(self):
        procs = [proc("claude.exe", "claude --remote-control al-a2-n5", "C:/bin/claude.exe"),
                 proc("claude.exe", "claude --remote-control be-a2", "C:/X" + chr(92) + "WindowsApps" + chr(92) + "claude.exe")]
        out = pmstart.liveness(CFG, procs, read=lambda p: self.SESS, account=lambda: "2")
        self.assertEqual(len(out), 1)
        self.assertIn("launch.py start --session Beta", out[0])

    def test_prefix_name_not_a_match(self):
        procs = [proc("claude.exe", "claude --remote-control al-a2-n5-x", "C:/bin/claude.exe")]
        out = pmstart.liveness(CFG, procs, read=lambda p: self.SESS, account=lambda: "2")
        self.assertEqual(len(out), 2)

    def test_the_start_line_names_the_account_this_session_is_on_not_the_entrys(self):
        sess = json.dumps({"kit": {"name": "kit-a1", "account": 1}})
        (line,) = pmstart.liveness(CFG, [], read=lambda p: sess, account=lambda: "10")
        self.assertTrue(line.endswith("launch.py start --session kit --account 10"), line)

    def test_an_unidentified_account_is_left_to_pick_account(self):
        sess = json.dumps({"kit": {"name": "kit-a1", "account": 1}})
        (line,) = pmstart.liveness(CFG, [], read=lambda p: sess, account=lambda: "default")
        self.assertTrue(line.endswith("--session kit --account auto"), line)

    def test_the_account_is_not_read_when_every_coordinator_runs(self):
        def never():
            raise AssertionError("the account was read although nothing is down")
        procs = [proc("claude.exe", "claude --remote-control be-a2", "C:/bin/claude.exe")]
        sess = json.dumps({"Beta": {"name": "be-a2"}})
        self.assertEqual(pmstart.liveness(CFG, procs, read=lambda p: sess, account=never), [])


class ChecklistTest(unittest.TestCase):
    def test_all_pass(self):
        procs = [proc("python.exe", "python pm_watch.py")]
        txt = pmstart.checklist(CFG, NOW, procs=procs,
                                handoff={"globber": lambda p: ["h.md"], "mtime": lambda f: 1},
                                reminders={"read": lambda p: "[]"},
                                liveness={"read": lambda p: "{}"})
        self.assertIn("republish", txt)
        self.assertIn("read first: h.md", txt)
        self.assertNotIn("could not check", txt)

    def test_failures_isolated(self):
        def boom(p):
            raise OSError("disk gone")
        txt = pmstart.checklist({"hub_url": "u"}, NOW, procs=[],
                                handoff={"globber": boom}, reminders={"read": boom})
        self.assertIn("republish", txt)  # hub still ran
        for n in ("watcher", "handoff", "reminders", "liveness"):
            self.assertIn("could not check %s:" % n, txt)

    def test_process_listing_failure_isolated(self):
        orig = pmstart.list_processes
        def bad():
            raise subprocess.TimeoutExpired("powershell", 4)
        pmstart.list_processes = bad
        try:
            txt = pmstart.checklist(CFG, NOW, reminders={"read": lambda p: "[]"},
                                    handoff={"globber": lambda p: []})
        finally:
            pmstart.list_processes = orig
        self.assertIn("could not check watcher", txt)
        self.assertIn("could not check liveness", txt)
        self.assertIn("republish", txt)


class ConfigAndHookTest(unittest.TestCase):
    def test_load_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            self.assertIsNone(pmstart.load_config(p))
            with open(p, "w") as f:
                json.dump({"pm_start": {"hub_url": "u"}}, f)
            self.assertEqual(pmstart.load_config(p), {"hub_url": "u"})

    def test_hook_without_config_prints_json_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            hook = os.path.join(os.path.dirname(__file__), "..", "..", "claude", "hooks", "pm_session_start.py")
            env = dict(os.environ, USERPROFILE=d, HOME=d)
            r = subprocess.run([sys.executable, hook], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("no pm_start config", out["additionalContext"])


if __name__ == "__main__":
    unittest.main()


class KnownReposTest(unittest.TestCase):
    SESS = json.dumps({"_note": "x", "a": {"repo": "alpha"}, "b": {"repo": "Beta"},
                       "c": {"repo": "alpha"}, "d": {"name": "no repo"}})

    def run_it(self, cfg=CFG, top=lambda d: None):
        return pmstart.known_repos(cfg, read=lambda p: self.SESS, toplevel=top, kit_dir="K")

    def test_sessions_deduped_and_sorted(self):
        self.assertEqual(self.run_it(), ["alpha", "Beta"])

    def test_pm_and_kit_repos_added(self):
        tops = {"K": "kit-repo"}
        got = self.run_it(top=lambda d: tops.get(d, "manager"))
        self.assertEqual(got, ["alpha", "Beta", "kit-repo", "manager"])

    def test_extra_list_and_case_fold(self):
        got = self.run_it(cfg=dict(CFG, repos=["beta", "NewRepo"]))
        self.assertEqual(got, ["alpha", "Beta", "NewRepo"])

    def test_bad_extra_raises(self):
        with self.assertRaises(ValueError):
            self.run_it(cfg=dict(CFG, repos="x"))

    def test_no_sessions_file(self):
        with self.assertRaises(KeyError):
            pmstart.known_repos({})


class BomFileTest(unittest.TestCase):
    """PowerShell writes JSON with a UTF-8 BOM; every default file reader must take it."""

    def write(self, d, name, obj):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps(obj).encode("utf-8"))
        return p

    def test_liveness_reads_bom_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, "sessions.json", {"alpha": {"name": "al-a2"}})
            out = pmstart.liveness(dict(CFG, sessions_file=p), [])
        self.assertEqual(len(out), 1)
        self.assertIn("launch.py start --session alpha", out[0])

    def test_known_repos_reads_bom_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, "sessions.json", {"a": {"repo": "alpha"}})
            got = pmstart.known_repos(dict(CFG, sessions_file=p), toplevel=lambda x: None, kit_dir="K")
        self.assertEqual(got, ["alpha"])

    def test_reminders_reads_bom_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, "reminders.json", [{"due": "2026-10-01", "text": "old"}])
            out = pmstart.reminders(dict(CFG, reminders_file=p), NOW.date())
        self.assertEqual(out, ["reminder due 2026-10-01: old"])

    def test_load_config_reads_bom_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, "config.json", {"pm_start": {"hub_url": "u"}})
            self.assertEqual(pmstart.load_config(p), {"hub_url": "u"})


class EngReviewTest(unittest.TestCase):
    """pm_start.eng_review: the owner's design-review flags."""

    def test_set_lists_every_flag_and_says_to_hub_them(self):
        out = pmstart.eng_review(dict(CFG, eng_review=["EX-API-*", "gamma"]))
        self.assertEqual(len(out), 1)
        self.assertIn("EX-API-*, gamma", out[0])
        self.assertIn("hub", out[0])

    def test_unset_or_empty_gives_no_line(self):
        for cfg in (CFG, dict(CFG, eng_review=[])):
            self.assertEqual(pmstart.eng_review(cfg), [])
        fakes = dict(handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"}, liveness={"read": lambda p: "{}"})
        self.assertNotIn("eng_review", pmstart.checklist(CFG, NOW, procs=[], **fakes))

    def test_bad_value_raises(self):
        for bad in ("EX-API-*", "", [1], [""], 5, 0, False, {}, None):
            with self.assertRaises(TypeError):
                pmstart.eng_review(dict(CFG, eng_review=bad))

    def test_in_checklist_and_bad_value_isolated(self):
        fakes = dict(handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"}, liveness={"read": lambda p: "{}"})
        self.assertIn("EX-API-*", pmstart.checklist(dict(CFG, eng_review=["EX-API-*"]), NOW, procs=[], **fakes))
        txt = pmstart.checklist(dict(CFG, eng_review="x"), NOW, procs=[], **fakes)
        self.assertIn("could not check eng_review:", txt)
        self.assertIn("republish", txt)
