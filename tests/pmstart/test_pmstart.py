import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib import pmstart
from kitlib.pmstart import fglock
from tests.pmstart import keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 6, 9, 0)
CFG = {"hub_url": "https://claude.ai/artifact/X", "hub_source": "claude/hub/action-hub.html",
       "pm_watch_cmd": "python launcher/pm_watch.py --stream", "memory_dir": "M",
       "reminders_file": "r.json", "sessions_file": "s.json"}


def setUpModule():
    """The checklist must not read this machine's pm_data setting (kit_sync has its own tests)."""
    global _orig_settings
    _orig_settings = pmstart.pm_data_settings
    pmstart.pm_data_settings = lambda: None
    global _orig_read_live
    _orig_read_live = fglock.read_live
    fglock.read_live = lambda user32=None: 0
    global _orig_beat
    _orig_beat = pmstart.read_heartbeat
    pmstart.read_heartbeat = lambda: None     # nor this machine's pm_watch heartbeat
    keep_off_launcher_repos()   # nor this machine's launcher.repos (the stubs check)


def tearDownModule():
    pmstart.pm_data_settings = _orig_settings
    fglock.read_live = _orig_read_live
    pmstart.read_heartbeat = _orig_beat


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
        s = pmstart.hub(CFG)[0]
        src = os.path.join(pmstart.KIT_ROOT, "claude", "hub", "action-hub.html")
        self.assertIn("republish the kit's Action Hub page %s "
                      "to https://claude.ai/artifact/X with the Artifact tool" % src, s)
        self.assertIn("docs/action-hub.md (For the PM)", s)
        self.assertNotIn("republish claude/hub/action-hub.html to", s)  # the old bare line
        self.assertIn("pm_ack", pmstart.hub(CFG)[-1])


def wproc(pid, name="python.exe", cmd="python -u pm_watch.py --stream"):
    return {"Name": name, "CommandLine": cmd, "ProcessId": pid}


class WatcherTest(unittest.TestCase):
    """watcher: alive by heartbeat plus pid (pmstart.BEAT_STALE)."""
    T = 1000000.0

    def run_it(self, procs, hb, cfg=CFG):
        return pmstart.watcher(cfg, procs, beat=lambda: hb, now=self.T)

    def test_fresh_heartbeat_and_live_pid_is_running(self):
        hb = {"pid": 42, "beat": self.T - 30, "stream": True}
        self.assertEqual(self.run_it([wproc(42)], hb), [])

    def test_stale_heartbeat_with_live_pid_says_it_stopped_polling(self):
        hb = {"pid": 42, "beat": self.T - pmstart.BEAT_STALE - 120, "stream": True}
        (s,) = self.run_it([wproc(42)], hb)
        self.assertIn("pm_watch.py (pid 42) has not polled for 7m: stop that pid", s)
        self.assertIn("python launcher/pm_watch.py --stream", s)

    def test_fresh_heartbeat_with_pid_gone_is_not_running(self):
        hb = {"pid": 42, "beat": self.T - 10, "stream": False}
        (s,) = self.run_it([wproc(43, name="code.exe")], hb)
        self.assertEqual(s, "pm_watch.py is not running (it last polled 10s ago): start it under "
                            "Monitor: python launcher/pm_watch.py --stream")

    def test_a_live_watcher_without_stream_says_it_exits_after_one_batch(self):
        hb = {"pid": 42, "beat": self.T - 30, "stream": False}
        (s,) = self.run_it([wproc(42, cmd="python pm_watch.py")], hb)
        self.assertEqual(s, "pm_watch.py (pid 42) runs without --stream: it exits after one batch of "
                            "events; stop that pid and start it under Monitor with --stream: "
                            "python launcher/pm_watch.py --stream")

    def test_a_pm_watch_cmd_without_stream_is_kept_and_warned_about(self):
        cfg = dict(CFG, pm_watch_cmd="python launcher/pm_watch.py")
        start, warn = self.run_it([], None, cfg)
        self.assertEqual(start, "pm_watch.py is not running: start it under Monitor: "
                                "python launcher/pm_watch.py")     # as configured, not rewritten
        self.assertEqual(warn, "pm_start.pm_watch_cmd has no --stream: a watcher started with it exits "
                               "after one batch of events; add --stream to run it under Monitor")
        hb = {"pid": 42, "beat": self.T - 30, "stream": True}
        self.assertEqual(self.run_it([wproc(42)], hb, cfg), [warn])  # even while one streams
        for ok in ("python w.py --stream", "python w.py --stream --branch a=b"):
            self.assertEqual(self.run_it([wproc(42)], hb, dict(CFG, pm_watch_cmd=ok)), [])
        self.assertEqual(len(self.run_it([wproc(42)], hb, dict(CFG, pm_watch_cmd="w.py --streams"))), 1)

    def test_a_reused_pid_started_at_another_time_is_not_the_watcher(self):
        created = "2026-10-06T09:00:00.1234567+00:00"         # list_processes' CreationDate 'o' form
        at = datetime.datetime(2026, 10, 6, 9, 0, tzinfo=datetime.timezone.utc).timestamp()
        procs = [dict(wproc(42), Created=created)]
        hb = {"pid": 42, "beat": self.T - 30, "stream": True, "started": at + 1}
        self.assertEqual(self.run_it(procs, hb), [])                 # same start, within START_SLACK
        (s,) = self.run_it(procs, dict(hb, started=at - 600))        # pid 42 is now another process
        self.assertIn("running without a heartbeat", s)
        # either start time unknown: the pid alone decides
        self.assertEqual(self.run_it(procs, dict(hb, started=None)), [])
        self.assertEqual(self.run_it([wproc(42)], hb), [])
        self.assertEqual(self.run_it([dict(wproc(42), Created="garbled")], dict(hb, started=0.0)), [])

    def test_the_heartbeat_start_time_matches_the_process_lists(self):
        """pm_watch's own start time (kernel32) and list_processes' CreationDate agree for one pid."""
        from kitlib import pmwatch
        (me,) = [p for p in pmstart.list_processes() if p.get("ProcessId") == os.getpid()]
        self.assertLessEqual(abs(pmwatch.own_start_time() - pmstart._created(me)), pmstart.START_SLACK)

    def test_a_pm_watch_process_without_a_heartbeat_is_an_older_copy(self):
        for hb in (None, {"pid": 7, "beat": self.T}):     # none, or another process's
            (s,) = self.run_it([wproc(42, cmd="python launcher/pm_watch.py")], hb)
            self.assertIn("running without a heartbeat", s)
            self.assertIn("restart it under Monitor", s)

    def test_nothing_running_gives_start_step(self):
        # a non-python process mentioning pm_watch.py (e.g. an editor) does not count
        (s,) = self.run_it([wproc(42, name="code.exe", cmd="code pm_watch.py")], None)
        self.assertEqual(s, "pm_watch.py is not running: start it under Monitor: "
                            "python launcher/pm_watch.py --stream")

    def test_no_pm_watch_cmd_names_the_kits_stream_command(self):
        cfg = dict(CFG)
        del cfg["pm_watch_cmd"]
        (s,) = self.run_it([], None, cfg)
        tool = os.path.join(pmstart.KIT_ROOT, "tools", "pm_watch.py")
        self.assertTrue(os.path.isfile(tool))
        self.assertTrue(s.endswith('python -u "%s" --stream' % tool), s)

    def test_the_configured_command_is_used_as_written(self):
        (s,) = self.run_it([], None, dict(CFG, pm_watch_cmd="run the watcher --stream"))
        self.assertTrue(s.endswith("Monitor: run the watcher --stream"), s)

    def test_checklist_passes_the_watcher_fake(self):
        hb = {"pid": 42, "beat": 0}
        txt = pmstart.checklist(CFG, NOW, procs=[wproc(42)], watcher={"beat": lambda: hb, "now": 10},
                                handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
                                liveness={"read": lambda p: "{}"})
        self.assertNotIn("pm_watch.py", txt)

    def test_read_heartbeat_reads_the_file_beside_pm_watchs_state(self):
        from kitlib import pmwatch
        with tempfile.TemporaryDirectory() as d:
            cfgp = os.path.join(d, "config.json")
            state = os.path.join(d, "st", "state.json")
            with open(cfgp, "w", encoding="utf-8") as f:
                json.dump({"pm_watch": {"state_file": state}}, f)
            orig = pmwatch.load_config
            pmwatch.load_config = lambda path=None: orig(cfgp)
            try:
                self.assertIsNone(_orig_beat())
                pmwatch.write_heartbeat(pmwatch.heartbeat_file(state), 42, 5.0, True)
                self.assertEqual(_orig_beat(), {"pid": 42, "beat": 5.0, "stream": True, "started": None})
                pmwatch.write_heartbeat(pmwatch.heartbeat_file(state), 42, 5.0, True, 3.5)
                self.assertEqual(_orig_beat()["started"], 3.5)
            finally:
                pmwatch.load_config = orig


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
        for n in ("handoff", "reminders", "liveness"):
            self.assertIn("could not check %s:" % n, txt)
        self.assertIn("pm_watch.py is not running", txt)  # no pm_watch_cmd: the kit's own command

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
