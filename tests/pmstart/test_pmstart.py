import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import pmstart
from kitlib.pmstart import fglock
from tests.home import temp_home
from tests.pmstart import HANDOFF_FAKE, HUB_KEPT_OFF, keep_off_hub_site, keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 6, 9, 0)
CFG = {"pm_watch_cmd": "python launcher/pm_watch.py", "memory_dir": "M",
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
    keep_off_launcher_repos()   # nor this machine's launcher.repos
    keep_off_hub_site()         # nor its hub key (the hub check; the home fence is not caught)


def tearDownModule():
    pmstart.pm_data_settings = _orig_settings
    fglock.read_live = _orig_read_live
    pmstart.read_heartbeat = _orig_beat


def proc(name, cmd, exe=""):
    return {"Name": name, "CommandLine": cmd, "ExecutablePath": exe}


class StandingTest(unittest.TestCase):
    FAKES = dict(handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
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
        self.assertIn(HUB_KEPT_OFF, txt)


class RulebooksCheckTest(unittest.TestCase):
    def test_the_checklist_carries_the_rulebooks_line(self):
        txt = pmstart.checklist(dict(CFG, standing=["x"]), NOW, procs=[], machine={}, **StandingTest.FAKES)
        self.assertIn("- rulebooks (standing rules): none - no ledger_dir or ledger_path is set", txt.splitlines())

    def test_an_unreadable_machine_config_is_unknown_not_none(self):
        with mock.patch.object(pmstart, "_user_config", lambda: None):
            txt = pmstart.checklist(dict(CFG, standing=["x"]), NOW, procs=[], **StandingTest.FAKES)
        rows = [l for l in txt.splitlines() if l.startswith("- rulebooks")]
        self.assertEqual(rows, ["- rulebooks (standing rules): unknown - the machine config cannot be read (see the machine config line)"])


def wproc(pid, name="python.exe", cmd="python -u pm_watch.py"):
    return {"Name": name, "CommandLine": cmd, "ProcessId": pid}


class WatcherTest(unittest.TestCase):
    """watcher: alive by heartbeat plus pid (pmstart.BEAT_STALE); run in the background
    without --stream, so a streaming one is a line (docs/pm-start.md)."""
    T = 1000000.0

    def run_it(self, procs, hb, cfg=CFG, job=None):
        return pmstart.watcher(cfg, procs, beat=lambda: hb, now=self.T, job=lambda: job)

    def test_a_covering_session_watch_job_counts_as_up(self):
        job = {"failures": 0, "ok_at": self.T - 60, "every_s": 60}
        self.assertEqual(self.run_it([], None, job=job), [], "no pm_watch at all, the job covers")
        stale = {"pid": 42, "beat": self.T - pmstart.BEAT_STALE - 120, "stream": False}
        self.assertEqual(self.run_it([wproc(42)], stale, job=job), [])
        for bad in ({"failures": 3, "ok_at": self.T - 60}, {"failures": 0, "ok_at": None},
                    {"failures": 0, "ok_at": self.T - 301}, None):
            (s,) = self.run_it([], None, job=bad)
            self.assertTrue(s.startswith("pm_watch.py is not running"), (bad, s))

    def test_the_hub_is_not_asked_while_pm_watch_is_up(self):
        hb = {"pid": 42, "beat": self.T - 30, "stream": False}
        self.assertEqual(pmstart.watcher(CFG, [wproc(42)], beat=lambda: hb, now=self.T,
                                         job=lambda: self.fail("asked the hub")), [])

    def test_fresh_heartbeat_and_live_pid_is_running(self):
        hb = {"pid": 42, "beat": self.T - 30, "stream": False}
        self.assertEqual(self.run_it([wproc(42)], hb), [])

    def test_stale_heartbeat_with_live_pid_says_it_stopped_polling(self):
        hb = {"pid": 42, "beat": self.T - pmstart.BEAT_STALE - 120, "stream": False}
        (s,) = self.run_it([wproc(42)], hb)
        self.assertIn("pm_watch.py (pid 42) has not polled for 7m: stop that pid and start it in "
                      "the background", s)
        self.assertIn("python launcher/pm_watch.py", s)

    def test_fresh_heartbeat_with_pid_gone_is_not_running(self):
        hb = {"pid": 42, "beat": self.T - 10, "stream": False}
        (s,) = self.run_it([wproc(43, name="code.exe")], hb)
        self.assertEqual(s, "pm_watch.py is not running (it last polled 10s ago): start it in the "
                            "background (it exits with its events; start it again at once, then "
                            "handle them): python launcher/pm_watch.py")

    def test_a_live_watcher_with_stream_says_it_never_exits(self):
        hb = {"pid": 42, "beat": self.T - 30, "stream": True}
        (s,) = self.run_it([wproc(42, cmd="python pm_watch.py --stream")], hb)
        self.assertEqual(s, "pm_watch.py (pid 42) runs with --stream: it never exits, so a "
                            "background run reports no event; stop that pid and start it in the "
                            "background without --stream: python launcher/pm_watch.py")

    def test_a_heartbeat_without_a_stream_field_counts_as_not_streaming(self):
        self.assertEqual(self.run_it([wproc(42)], {"pid": 42, "beat": self.T - 30}), [])

    def test_a_pm_watch_cmd_with_stream_is_kept_and_warned_about(self):
        cfg = dict(CFG, pm_watch_cmd="python launcher/pm_watch.py --stream")
        start, warn = self.run_it([], None, cfg)
        self.assertTrue(start.startswith("pm_watch.py is not running: start it in the background"),
                        start)
        self.assertTrue(start.endswith(": python launcher/pm_watch.py --stream"),
                        start)     # as configured, not rewritten
        self.assertEqual(warn, "pm_start.pm_watch_cmd has --stream: a watcher started with it never "
                               "exits, so a background run reports no event; remove --stream from "
                               "pm_watch_cmd")
        hb = {"pid": 42, "beat": self.T - 30, "stream": False}
        self.assertEqual(self.run_it([wproc(42)], hb, cfg), [warn])  # even while one runs without
        for ok in ("python w.py", "python w.py --branch a=b", "w.py --streams"):
            self.assertEqual(self.run_it([wproc(42)], hb, dict(CFG, pm_watch_cmd=ok)), [])
        self.assertEqual(len(self.run_it([wproc(42)], hb,
                                         dict(CFG, pm_watch_cmd="w.py --stream --branch a=b"))), 1)

    def test_a_reused_pid_started_at_another_time_is_not_the_watcher(self):
        created = "2026-10-06T09:00:00.1234567+00:00"         # list_processes' CreationDate 'o' form
        at = datetime.datetime(2026, 10, 6, 9, 0, tzinfo=datetime.timezone.utc).timestamp()
        procs = [dict(wproc(42), Created=created)]
        hb = {"pid": 42, "beat": self.T - 30, "stream": False, "started": at + 1}
        self.assertEqual(self.run_it(procs, hb), [])                 # same start, within START_SLACK
        (s,) = self.run_it(procs, dict(hb, started=at - 600))        # pid 42 is now another process
        self.assertIn("running without a heartbeat", s)
        # either start time unknown: the pid alone decides
        self.assertEqual(self.run_it(procs, dict(hb, started=None)), [])
        self.assertEqual(self.run_it([wproc(42)], hb), [])
        self.assertEqual(self.run_it([dict(wproc(42), Created="garbled")], dict(hb, started=0.0)), [])

    def test_the_heartbeat_start_time_matches_the_process_lists(self):
        """pm_watch's own start time (kernel32), written as list_processes' CreationDate 'o' form
        (local time with its offset), reads back as the same pid's start: a live watcher passes."""
        from kitlib import pmwatch
        started = pmwatch.own_start_time()
        created = datetime.datetime.fromtimestamp(started).astimezone().isoformat()
        procs = [dict(wproc(os.getpid()), Created=created)]
        hb = {"pid": os.getpid(), "beat": self.T - 30, "stream": False, "started": started}
        self.assertLessEqual(abs(pmstart._created(procs[0]) - started), pmstart.START_SLACK)
        self.assertEqual(self.run_it(procs, hb), [])

    def test_a_pm_watch_process_without_a_heartbeat_is_an_older_copy(self):
        for hb in (None, {"pid": 7, "beat": self.T}):     # none, or another process's
            (s,) = self.run_it([wproc(42, cmd="python launcher/pm_watch.py")], hb)
            self.assertIn("running without a heartbeat", s)
            self.assertIn("restart it in the background", s)

    def test_nothing_running_gives_start_step(self):
        # a non-python process mentioning pm_watch.py (e.g. an editor) does not count
        (s,) = self.run_it([wproc(42, name="code.exe", cmd="code pm_watch.py")], None)
        self.assertEqual(s, "pm_watch.py is not running: start it in the background (it exits with "
                            "its events; start it again at once, then handle them): "
                            "python launcher/pm_watch.py")

    def test_no_line_names_monitor(self):
        for hb, procs in ((None, []), ({"pid": 42, "beat": self.T - 10}, []),
                          ({"pid": 42, "beat": self.T - 900}, [wproc(42)]), (None, [wproc(42)]),
                          ({"pid": 42, "beat": self.T - 30, "stream": True}, [wproc(42)])):
            for line in self.run_it(procs, hb, dict(CFG, pm_watch_cmd="w.py --stream")):
                self.assertNotIn("Monitor", line)

    def test_no_pm_watch_cmd_names_the_kits_command_without_stream(self):
        cfg = dict(CFG)
        del cfg["pm_watch_cmd"]
        (s,) = self.run_it([], None, cfg)
        tool = os.path.join(pmstart.KIT_ROOT, "tools", "pm_watch.py")
        self.assertTrue(os.path.isfile(tool))
        self.assertTrue(s.endswith(': python -u "%s"' % tool), s)
        self.assertNotIn("--stream", pmstart.PM_WATCH_DEFAULT_CMD)

    def test_the_configured_command_is_used_as_written(self):
        (s,) = self.run_it([], None, dict(CFG, pm_watch_cmd="run the watcher"))
        self.assertTrue(s.endswith("then handle them): run the watcher"), s)

    def test_checklist_passes_the_watcher_fake(self):
        hb = {"pid": 42, "beat": 0}
        txt = pmstart.checklist(CFG, NOW, procs=[wproc(42)], watcher={"beat": lambda: hb, "now": 10},
                                handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
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
                self.assertEqual(_orig_beat(), {"pid": 42, "beat": 5.0, "stream": True, "started": None,
                                                  "yielded": False})
                pmwatch.write_heartbeat(pmwatch.heartbeat_file(state), 42, 5.0, True, 3.5)
                self.assertEqual(_orig_beat()["started"], 3.5)
            finally:
                pmwatch.load_config = orig


class HandoffTest(unittest.TestCase):
    TOP = os.path.normpath("X:/pm")

    def run_handoff(self, cfg=CFG, files=(), strays=(), seen=None):
        def globber(p):
            if seen is not None:
                seen.append(p)
            return list(strays)
        return pmstart.handoff(cfg, globber=globber, exists=lambda p: p in files, pm_repo=lambda c: self.TOP)

    def test_the_one_file_in_the_pm_repo_is_read_first(self):
        path = os.path.join(self.TOP, "HANDOFF.md")
        self.assertEqual(self.run_handoff(files=[path]), ["read first: %s" % path])

    def test_handoff_file_names_another_file_relative_or_absolute(self):
        rel = os.path.join(self.TOP, "notes", "H.md")
        self.assertEqual(self.run_handoff(dict(CFG, handoff_file="notes/H.md"), files=[rel]),
                         ["read first: %s" % rel])
        ab = os.path.abspath("Q:/h/HAND.md")
        self.assertEqual(self.run_handoff(dict(CFG, handoff_file=ab), files=[ab]), ["read first: %s" % ab])

    def test_a_missing_file_is_a_line_that_names_it(self):
        (s,) = self.run_handoff()
        self.assertIn("no hand-off file " + os.path.join(self.TOP, "HANDOFF.md"), s)

    def test_stray_notes_in_memory_dir_are_told_to_merge_and_delete(self):
        path = os.path.join(self.TOP, "HANDOFF.md")
        seen = []
        got = self.run_handoff(files=[path], strays=["M/pm-handoff-b.md", "M/pm-handoff-a.md"], seen=seen)
        self.assertEqual(got, ["read first: %s" % path,
                               "hand-off notes belong in %s: merge and delete M/pm-handoff-a.md, M/pm-handoff-b.md"
                               % path])
        self.assertTrue(seen[0].endswith("pm-handoff*.md"))
        self.assertEqual(seen[0], os.path.join("M", "pm-handoff*.md"))

    def test_a_missing_file_and_strays_give_both_lines(self):
        got = self.run_handoff(strays=["M/pm-handoff-a.md"])
        self.assertEqual(len(got), 2)
        self.assertIn("no hand-off file", got[0])
        self.assertIn("merge and delete", got[1])

    def test_no_memory_dir_looks_for_no_strays(self):
        seen = []
        self.run_handoff({k: v for k, v in CFG.items() if k != "memory_dir"}, seen=seen)
        self.assertEqual(seen, [])

    def test_a_relative_file_with_no_pm_repo_is_refused(self):
        with self.assertRaises(KeyError):
            pmstart.handoff(CFG, pm_repo=lambda c: None)


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

    def test_a_repo_with_no_live_consultant_gets_one_line(self):
        sess = json.dumps({"a": {"name": "a-1", "repo": "alpha"}, "b": {"name": "b-1", "repo": "alpha"},
                           "c": {"name": "c-1", "repo": "beta"}})
        procs = [proc("claude.exe", "claude --remote-control a-1", "C:/bin/claude.exe"),
                 proc("claude.exe", "claude --remote-control b-1", "C:/bin/claude.exe"),
                 proc("claude.exe", "claude --remote-control c-1", "C:/bin/claude.exe"),
                 proc("claude.exe", "claude --remote-control consultant-beta", "C:/bin/claude.exe")]
        self.assertEqual(pmstart.liveness(CFG, procs, read=lambda p: sess, account=lambda: "2", repos={}),
                         ["consultant-alpha is not running: launch.py consultant --repo alpha "
                          "--repo-path <its checkout>"])

    def test_the_consultant_line_names_the_repo_folder_when_launcher_repos_has_it(self):
        sess = json.dumps({"a": {"name": "a-1", "repo": "alpha"}})
        out = pmstart.liveness(CFG, [], read=lambda p: sess, account=lambda: "2",
                               repos={"alpha": "C:/r/alpha", "beta": "C:/r/beta"})
        self.assertEqual(out[-1], "consultant-alpha is not running: launch.py consultant --repo alpha "
                                  "--repo-path C:/r/alpha")

    def test_the_placeholder_stays_when_launcher_repos_cannot_be_read(self):
        sess = json.dumps({"a": {"name": "a-1", "repo": "alpha"}})
        orig = pmstart.launcher_repos
        pmstart.launcher_repos = lambda: 1 / 0
        try:
            out = pmstart.liveness(CFG, [], read=lambda p: sess, account=lambda: "2")
        finally:
            pmstart.launcher_repos = orig
        self.assertTrue(out[-1].endswith("--repo-path <its checkout>"), out)

    def test_a_consultant_entry_is_left_to_the_coordinator_line(self):
        sess = json.dumps({"k": {"name": "consultant-kit", "repo": "kit", "agent": "pm-kit:consultant"}})
        (line,) = pmstart.liveness(CFG, [], read=lambda p: sess, account=lambda: "2")
        self.assertTrue(line.startswith("coordinator k "), line)


class ChecklistTest(unittest.TestCase):
    def test_all_pass(self):
        procs = [proc("python.exe", "python pm_watch.py")]
        txt = pmstart.checklist(CFG, NOW, procs=procs,
                                handoff={"globber": lambda p: [], "exists": lambda p: True,
                                         "pm_repo": lambda c: "X:/pm"},
                                reminders={"read": lambda p: "[]"},
                                liveness={"read": lambda p: "{}"})
        self.assertIn(HUB_KEPT_OFF, txt)
        self.assertIn("read first: " + os.path.normpath("X:/pm/HANDOFF.md"), txt)
        self.assertNotIn("could not check", txt)

    def test_failures_isolated(self):
        def boom(p):
            raise OSError("disk gone")
        txt = pmstart.checklist({}, NOW, procs=[],
                                handoff={"globber": boom}, reminders={"read": boom})
        self.assertIn(HUB_KEPT_OFF, txt)  # hub still ran
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
                                    handoff=HANDOFF_FAKE)
        finally:
            pmstart.list_processes = orig
        self.assertIn("could not check watcher", txt)
        self.assertIn("could not check liveness", txt)
        self.assertIn(HUB_KEPT_OFF, txt)


class ConfigAndHookTest(unittest.TestCase):
    def test_load_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            self.assertIsNone(pmstart.load_config(p))
            with open(p, "w") as f:
                json.dump({"pm_start": {"memory_dir": "u"}}, f)
            self.assertEqual(pmstart.load_config(p), {"memory_dir": "u"})

    def test_hook_without_config_prints_json_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            hook = os.path.join(os.path.dirname(__file__), "..", "..", "boot", "hook.py")
            env = dict(os.environ, USERPROFILE=d, HOME=d)
            r = subprocess.run([sys.executable, hook, "claude", "SessionStart", "pm"], capture_output=True, text=True,
                               env=env, timeout=30, input="{}")
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
        temp_home(self)     # liveness's current account reads an empty machine config
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
            p = self.write(d, "config.json", {"pm_start": {"memory_dir": "u"}})
            self.assertEqual(pmstart.load_config(p), {"memory_dir": "u"})


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
        fakes = dict(handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"}, liveness={"read": lambda p: "{}"})
        self.assertNotIn("eng_review", pmstart.checklist(CFG, NOW, procs=[], **fakes))

    def test_bad_value_raises(self):
        for bad in ("EX-API-*", "", [1], [""], 5, 0, False, {}, None):
            with self.assertRaises(TypeError):
                pmstart.eng_review(dict(CFG, eng_review=bad))

    def test_in_checklist_and_bad_value_isolated(self):
        fakes = dict(handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"}, liveness={"read": lambda p: "{}"})
        self.assertIn("EX-API-*", pmstart.checklist(dict(CFG, eng_review=["EX-API-*"]), NOW, procs=[], **fakes))
        txt = pmstart.checklist(dict(CFG, eng_review="x"), NOW, procs=[], **fakes)
        self.assertIn("could not check eng_review:", txt)
        self.assertIn(HUB_KEPT_OFF, txt)


class MemoryTest(unittest.TestCase):
    """pmstart.memory: each launcher repo's and the PM repo's autoMemoryDirectory
    (kitlib.stubs.memory.problem_line), and pm_start.memory_dir against the PM repo's value. Every
    reader is faked; the folders are throwaway ones."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.a, self.b = os.path.join(d.name, "a"), os.path.join(d.name, "b")
        os.makedirs(self.a)
        os.makedirs(self.b)

    def test_a_repo_with_the_key_has_no_line_and_one_without_has_the_command(self):
        def check(path, label=None):
            return None if path == self.a else "memory (%s): add autoMemoryDirectory: python x" % label
        got = pmstart.memory(CFG, repos={"a": self.a, "b": self.b}, check=check, pm_repo=lambda c: None)
        self.assertEqual(got, ["memory (b): add autoMemoryDirectory: python x"])

    def test_a_missing_folder_and_a_check_that_raises_are_could_not_check_lines(self):
        gone = os.path.join(self.a, "gone")

        def check(path, label=None):
            raise OSError("disk gone")
        got = pmstart.memory(CFG, repos={"a": self.a, "g": gone}, check=check, pm_repo=lambda c: None)
        self.assertEqual(got, ["could not check memory (a): OSError: disk gone",
                               "could not check memory (g): no repository folder %s" % gone])

    def test_the_pm_repo_is_checked_and_memory_dir_must_match_its_value(self):
        seen = []

        def check(path, label=None):
            seen.append((path, label))
            return None
        folder = lambda path: "X:/Agent-memory/pm"
        got = pmstart.memory(dict(CFG, memory_dir="M"), repos={}, check=check, pm_repo=lambda c: self.a, folder=folder)
        self.assertEqual(seen, [(self.a, "PM repo")])
        self.assertEqual(got, ["pm_start.memory_dir is M; the PM repo's memory folder is X:/Agent-memory/pm: "
                               "set pm_start.memory_dir to X:/Agent-memory/pm"])
        same = pmstart.memory(dict(CFG, memory_dir="x:\\Agent-memory\\pm\\"), repos={}, check=check,
                              pm_repo=lambda c: self.a, folder=folder)
        self.assertEqual(same, [])
        unset = pmstart.memory({}, repos={}, check=check, pm_repo=lambda c: self.a, folder=folder)
        self.assertEqual(unset, ["pm_start.memory_dir is not set; the PM repo's memory folder is "
                                 "X:/Agent-memory/pm: set pm_start.memory_dir to X:/Agent-memory/pm"])

    def test_memory_dir_is_compared_with_the_folder_the_pm_repos_settings_name(self):
        # The owner chose another folder (the OTHER state): memory_dir naming it is no problem.
        from unittest import mock
        from kitlib.stubs import memory as kmemory
        chosen = kmemory.Memory(kmemory.OTHER, "p", "Q:/mine", "X:/Agent-memory/pm", None, None, None, None)
        with mock.patch.object(kmemory, "survey", lambda top, *a, **k: chosen), \
                mock.patch.object(kmemory, "wanted", lambda top, *a, **k: "X:/Agent-memory/pm"):
            got = pmstart.memory(dict(CFG, memory_dir="q:\\mine"), repos={}, check=lambda p, label=None: None,
                                 pm_repo=lambda c: self.a)
            self.assertEqual(got, [])
            got = pmstart.memory(dict(CFG, memory_dir="X:/Agent-memory/pm"), repos={},
                                 check=lambda p, label=None: None, pm_repo=lambda c: self.a)
            self.assertEqual(got, ["pm_start.memory_dir is X:/Agent-memory/pm; the PM repo's memory folder is "
                                   "Q:/mine: set pm_start.memory_dir to Q:/mine"])

    def test_a_pm_repo_check_that_raises_keeps_the_repo_lines(self):
        def check(path, label=None):
            if label == "PM repo":
                raise OSError("disk gone")
            return "memory (%s): add it" % label
        from unittest import mock
        from kitlib.stubs import memory as kmemory
        ok = kmemory.Memory(kmemory.OK, "p", "X:/m", "X:/m", None, None, None, None)
        with mock.patch.object(kmemory, "survey", lambda top, *a, **k: ok), \
                mock.patch.object(kmemory, "wanted", lambda top, *a, **k: "X:/m"):
            got = pmstart.memory(dict(CFG, memory_dir="X:/m"), repos={"b": self.b}, check=check,
                                 pm_repo=lambda c: self.a)
        self.assertEqual(got, ["memory (b): add it", "could not check memory (PM repo): OSError: disk gone"])

    def test_the_pm_repo_is_the_main_checkout_holding_the_sessions_file(self):
        self.assertIsNone(pmstart.pm_memory_repo(CFG))      # s.json is not there: no PM repo
        p = os.path.join(self.a, "sessions.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        top = pmstart.pm_memory_repo(dict(CFG, sessions_file=p), main_top=lambda path: "T:\\pm")
        self.assertEqual(top, "T:\\pm")

    def test_in_the_checklist(self):
        fakes = dict(handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
                     liveness={"read": lambda p: "{}"})
        txt = pmstart.checklist(CFG, NOW, procs=[], **fakes, memory={
            "repos": {"b": self.b}, "check": lambda p, label=None: "memory (b): add it", "pm_repo": lambda c: None})
        self.assertIn("- memory (b): add it", txt)
        txt = pmstart.checklist(CFG, NOW, procs=[], **fakes, memory={
            "repos": {"b": self.b}, "check": lambda p, label=None: 1 / 0, "pm_repo": lambda c: None})
        self.assertIn("could not check memory (b): ZeroDivisionError", txt)
