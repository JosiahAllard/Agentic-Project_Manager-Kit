"""Tests for pmwatch: the usage, alert and idle events, the Watcher and the CLI."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kitlib import pmwatch as W
from hosts.claude.adapter import claude_host as H

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools"))
import pm_watch as cli                                              # noqa: E402
from tests.home import TempHomeCase, machine_config, pm_repo       # noqa: E402


class UsageEvents(unittest.TestCase):
    def test_week_band_crossings_fire_once(self):
        p = {"2": {"five": 10, "week": 89}}
        self.assertEqual(W.usage_events(p, {"2": {"five": 10, "week": 95}}),
                         ["USAGE account 2 seven_day 95% (now 95%)"])
        self.assertEqual(W.usage_events({"2": {"five": 10, "week": 95}}, {"2": {"five": 10, "week": 96}}), [])

    def test_five_hour_98(self):
        self.assertEqual(W.usage_events({"1": {"five": 96, "week": 0}}, {"1": {"five": 98, "week": 0}}),
                         ["USAGE account 1 five_hour 98% (now 98%)"])

    def test_no_90_band(self):
        self.assertEqual(W.usage_events({"1": {"five": 89, "week": 0}}, {"1": {"five": 90, "week": 0}}), [])

    def test_reset_is_a_drop_of_20_or_more(self):
        self.assertEqual(W.usage_events({"1": {"five": 0, "week": 80}}, {"1": {"five": 0, "week": 0}}),
                         ["RESET account 1 (weekly 80% -> 0%)"])
        self.assertEqual(W.usage_events({"1": {"five": 0, "week": 30}}, {"1": {"five": 0, "week": 15}}), [])

    def test_new_account_is_baseline_only(self):
        self.assertEqual(W.usage_events({}, {"3": {"five": 99, "week": 99}}), [])

    def test_an_unknown_window_is_skipped_never_read_as_0(self):
        # Unknown after 80% would be a RESET if read as 0; unknown before 99% a band jump from 0.
        self.assertEqual(W.usage_events({"1": {"five": 10, "week": 80}}, {"1": {"five": None, "week": None}}), [])
        self.assertEqual(W.usage_events({"1": {"five": None, "week": None}}, {"1": {"five": 99, "week": 99}}), [])

    def test_merge_keeps_the_last_known_figure_so_a_band_crossed_meanwhile_fires(self):
        s = {"1": {"five": 10, "week": 90, "stale": False}}
        s = W.merge_usage(s, {"1": {"five": None, "week": None, "stale": True, "missing": True}})
        self.assertEqual(s, {"1": {"five": 10, "week": 90, "stale": True, "missing": True}})
        self.assertEqual(W.usage_events(s, {"1": {"five": 10, "week": 96}}),
                         ["USAGE account 1 seven_day 95% (now 96%)"])

    def test_line_parser(self):
        txt = ("account 1: five_hour=reset  seven_day=reset  [updated 2 min ago]\n"
               "account 2: five_hour=39% (resets Fri 15:40)  seven_day=51% (resets Tue 00:00)")
        got = {m.group(1): (m.group(2), m.group(3)) for m in W.LINE.finditer(txt)}
        self.assertEqual(got, {"1": ("reset", "reset"), "2": ("39", "51")})

    def test_parse_usage(self):
        self.assertEqual(W.parse_usage("account codex: five_hour=reset  seven_day=40% (resets x)"),
                         {"codex": {"five": 0, "week": 40, "age": None, "stale": True, "missing": False}})
        with self.assertRaises(RuntimeError):
            W.parse_usage("current account: unknown")

    def test_unknown_stale_and_missing_readings_are_kept_not_dropped_or_zeroed(self):
        got = W.parse_usage("account 1: five_hour=?  seven_day=98% (resets Fri 12:00)  [updated 3735 min ago]\n"
                            "account 2: five_hour=8% (resets Thu 02:30)  seven_day=18% (resets Mon 23:00)  "
                            "[updated 0 min ago]\n"
                            "account 3: five_hour=reset  seven_day=?  [updated ? min ago]\n"
                            "account 4: no usage file\n"
                            "current account: 2\n"
                            "account codex: no usage data")
        self.assertEqual(got, {
            "1": {"five": None, "week": 98, "age": 3735, "stale": True, "missing": False},
            "2": {"five": 8, "week": 18, "age": 0, "stale": False, "missing": False},
            "3": {"five": 0, "week": None, "age": None, "stale": True, "missing": False},
            "4": {"five": None, "week": None, "age": None, "stale": True, "missing": True},
            "codex": {"five": None, "week": None, "age": None, "stale": True, "missing": True}})
        line = "account 5: five_hour=1%%  seven_day=2%%  [updated %d min ago]"
        self.assertFalse(W.parse_usage(line % W.STALE_MINUTES)["5"]["stale"])
        self.assertTrue(W.parse_usage(line % (W.STALE_MINUTES + 1))["5"]["stale"])


class AlertEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rid, reason, raw=None):
        with open(os.path.join(self.d, rid + ".json"), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps({"id": rid, "reason": reason}))

    def test_new_alert_fires_once(self):
        self.put("r-1", "failed: exit 1 on launch 2 after 6 s;\nthe relaunch failed within 60 s")
        ev, seen = W.alert_events(set(), self.d)
        self.assertEqual(ev, ["BGFAIL run r-1 failed: exit 1 on launch 2 after 6 s; "
                              "the relaunch failed within 60 s"])
        self.assertEqual(W.alert_events(set(seen), self.d), ([], ["r-1.json"]))

    def test_missing_folder_is_quiet(self):
        self.assertEqual(W.alert_events({"r-9.json"}, os.path.join(self.d, "none")), ([], []))

    def test_unreadable_retried_and_removed_dropped(self):
        self.put("r-2", None, raw="{half")
        self.assertEqual(W.alert_events(set(), self.d), ([], []))
        self.put("r-2", "failed: exit 3 on launch 1")
        ev, seen = W.alert_events({"r-gone.json"}, self.d)
        self.assertEqual((ev, seen), (["BGFAIL run r-2 failed: exit 3 on launch 1"], ["r-2.json"]))

    def test_baseline_is_seen(self):
        self.put("r-3", "old")
        _, seen = W.alert_events([], self.d)
        self.assertEqual(W.alert_events(set(seen), self.d)[0], [])


class IdleEvents(unittest.TestCase):
    NOW = 1_000_000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.proj = os.path.join(self.root, "acct1", "projects")
        os.makedirs(self.proj)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, folder, rel, age_min, title=None):
        path = os.path.join(self.proj, folder, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stem = os.path.basename(path)[:-6]
        with open(path, "w", encoding="utf-8") as f:
            if title:
                f.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": stem}) + "\n")
            f.write("{}\n")
        t = self.NOW - age_min * 60
        os.utime(path, (t, t))
        return t

    def run_idle(self, fired=None, nolog=None, minutes=20, names=("n7",)):
        return W.idle_events(list(names), H.newest_finder([self.proj]), fired or {}, nolog or [],
                             self.NOW, minutes)

    def test_quiet_session_fires_once(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7")
        ev, fired, _ = self.run_idle()
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].startswith("IDLE n7 30m since "), ev[0])
        self.assertEqual(fired, {"n7": t})
        self.assertEqual(self.run_idle(fired)[0], [])

    def test_active_session_is_quiet(self):
        self.put("D--src-alpha", "a.jsonl", 5, "n7")
        self.assertEqual(self.run_idle()[:2], ([], {}))

    def test_two_sessions_one_folder_judged_separately(self):
        self.put("D--src-alpha", "a.jsonl", 90, "n7")
        self.put("D--src-alpha", "b.jsonl", 1, "gamma")
        ev, fired, _ = self.run_idle(names=("n7", "gamma"))
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].startswith("IDLE n7 90m since "), ev[0])
        self.assertEqual(list(fired), ["n7"])

    def test_own_subagent_transcript_counts_as_active(self):
        self.put("D--src-alpha", "a.jsonl", 60, "n7")
        self.put("D--src-alpha", "a/subagents/agent-1.jsonl", 2)
        self.assertEqual(self.run_idle()[0], [])

    def test_other_sessions_subagent_does_not_count(self):
        self.put("D--src-alpha", "a.jsonl", 60, "n7")
        self.put("D--src-alpha", "b.jsonl", 60, "gamma")
        self.put("D--src-alpha", "b/subagents/agent-1.jsonl", 2)
        self.assertEqual(len(self.run_idle()[0]), 1)

    def test_fires_again_after_activity_resumes(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7")
        _, fired, _ = self.run_idle({"n7": t - 500})
        self.assertEqual(fired, {"n7": t})
        self.put("D--src-alpha", "a.jsonl", 3, "n7")
        ev, fired, _ = self.run_idle(fired)
        self.assertEqual((ev, fired), ([], {}))
        self.put("D--src-alpha", "a.jsonl", 25, "n7")
        self.assertEqual(len(self.run_idle(fired)[0]), 1)

    def test_no_transcript_errors_once(self):
        ev, _, nolog = self.run_idle()
        self.assertEqual(ev, ["ERROR idle: no transcript for n7"])
        self.assertEqual(nolog, ["n7"])
        self.assertEqual(self.run_idle(nolog=nolog)[0], [])

    def test_name_in_text_is_not_a_title(self):
        self.put("D--src-alpha", "a.jsonl", 60)
        path = os.path.join(self.proj, "D--src-alpha", "a.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "text": "customTitle n7"}) + "\n")
        self.assertEqual(self.run_idle()[0], ["ERROR idle: no transcript for n7"])

    def test_threshold_is_idle_minutes(self):
        self.put("D--src-alpha", "a.jsonl", 30, "n7")
        self.assertEqual(self.run_idle(minutes=45)[0], [])

    def test_watched_sessions(self):
        p = os.path.join(self.root, "sessions.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"_note": "x", "r": {"repo": "alpha", "agent": True, "name": "n7"},
                       "c": {"repo": "alpha", "agent": False, "name": "gamma"},
                       "g": {"repo": "Beta", "agent": True, "name": "gb", "autostart": False},
                       "n": {"repo": "x", "agent": True}}, f)
        self.assertEqual(W.watched_sessions(p), ["gamma", "n7"])
        self.assertEqual(W.watched_sessions(os.path.join(self.root, "none.json")), [])

    def test_watched_sessions_reads_bom_file(self):
        p = os.path.join(self.root, "bom-sessions.json")
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"r": {"name": "n7"}}).encode("utf-8"))
        self.assertEqual(W.watched_sessions(p), ["n7"])

    def test_baseline_records_only_currently_idle(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7")
        self.put("D--src-alpha", "b.jsonl", 2, "gamma")
        self.assertEqual(W.idle_baseline(["n7", "gamma", "none"], H.newest_finder([self.proj]),
                                         self.NOW, 20), {"n7": t})

    def stop_log(self, name, at, successor="-"):
        """A stop.log beside sessions.json with the line kitlib.launcher.stop writes; returns the
        stopped(name, mtime) reader over it."""
        p = os.path.join(self.root, "sessions.json")
        with open(os.path.join(self.root, "stop.log"), "a", encoding="utf-8") as f:
            f.write("%s stopped '%s' claude PID 41, window PID 40, conhost PID 39; successor '%s'\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)), name, successor))
        return W.stop_log_reader(p)

    def test_stopped_without_successor_reads_stopped_never_idle(self):
        t = self.put("D--src-alpha", "a.jsonl", 21, "n7")
        stopped = self.stop_log("n7", t + 60)
        ev, fired, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), {}, [], self.NOW, 20, stopped)
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].startswith("STOPPED n7 at "), ev[0])
        self.assertFalse(any(e.startswith("IDLE") for e in ev))
        ev, _, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), fired, [], self.NOW, 20, stopped)
        self.assertEqual(ev, [])                    # once per stop

    def test_stop_after_idle_fired_still_reads_stopped(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7")
        stopped = self.stop_log("n7", t + 60)
        ev, _, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), {"n7": t}, [], self.NOW, 20,
                                 stopped)
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].startswith("STOPPED n7 at "), ev[0])

    def test_stop_in_the_same_second_as_the_last_write_reads_stopped(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7") + 0.7     # stop.log keeps whole seconds
        os.utime(os.path.join(self.proj, "D--src-alpha", "a.jsonl"), (t, t))
        stopped = self.stop_log("n7", int(t))
        ev, _, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), {}, [], self.NOW, 20, stopped)
        self.assertTrue(ev[0].startswith("STOPPED n7"), ev[0])

    def test_stop_with_a_successor_is_not_stopped(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7")
        stopped = self.stop_log("n7", t + 60, successor="n8")
        ev, _, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), {}, [], self.NOW, 20, stopped)
        self.assertTrue(ev[0].startswith("IDLE n7 30m"), ev[0])

    def test_started_again_after_the_stop_reads_idle_when_quiet(self):
        self.stop_log("n7", self.NOW - 3 * 3600)
        self.put("D--src-alpha", "a.jsonl", 30, "n7")      # activity after the stop
        stopped = W.stop_log_reader(os.path.join(self.root, "sessions.json"))
        ev, _, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), {}, [], self.NOW, 20, stopped)
        self.assertTrue(ev[0].startswith("IDLE n7 30m"), ev[0])

    def test_other_sessions_stop_does_not_count_and_no_log_is_idle(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7")
        stopped = self.stop_log("gamma", t + 60)
        ev, _, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), {}, [], self.NOW, 20, stopped)
        self.assertTrue(ev[0].startswith("IDLE n7"), ev[0])
        self.assertIsNone(W.stop_log_reader(os.path.join(self.root, "x", "sessions.json"))("n7", t))

    def test_baseline_marks_a_stopped_session_so_the_first_run_is_silent(self):
        t = self.put("D--src-alpha", "a.jsonl", 30, "n7")
        stopped = self.stop_log("n7", t + 60)
        base = W.idle_baseline(["n7"], H.newest_finder([self.proj]), self.NOW, 20, stopped)
        self.assertEqual(base, {"n7": [t, "stopped"]})
        ev, _, _ = W.idle_events(["n7"], H.newest_finder([self.proj]), base, [], self.NOW, 20, stopped)
        self.assertEqual(ev, [])

    def test_project_roots_are_claude_config_folders(self):
        for d in (".claude", ".claude-ex2", ".codex", "other"):
            os.makedirs(os.path.join(self.root, d))
        self.assertEqual(H.project_roots(self.root),
                         [os.path.join(self.root, d, "projects") for d in (".claude", ".claude-ex2")])


class WatcherRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.alerts = os.path.join(self.tmp.name, "alerts")
        os.makedirs(self.alerts)
        self.sessions = os.path.join(self.tmp.name, "sessions.json")
        with open(self.sessions, "w", encoding="utf-8") as f:
            json.dump({"r": {"name": "n7"}}, f)
        self.use = {"1": {"five": 10, "week": 50}}
        self.mt = {"n7": 1000.0}

    def tearDown(self):
        self.tmp.cleanup()

    def watcher(self, state, usage_reader=None, tip=lambda r, ref: "abcd1234"):
        return W.Watcher(state, [("repo", "main")], usage_reader or (lambda: dict(self.use)),
                         self.alerts, self.sessions, self.mt.get, tip=tip)

    def test_first_run_is_baseline_only(self):
        with open(os.path.join(self.alerts, "old.json"), "w") as f:
            json.dump({"id": "old", "reason": "x"}, f)
        w = self.watcher({})
        w.start(1000.0 + 3600)
        self.assertEqual(w.s, {"branches": {"repo main": "abcd1234"}, "usage": self.use,
                               "bg_alerts": ["old.json"], "idle": {"n7": 1000.0}})
        self.assertEqual(w.poll(1000.0 + 3600), [])
        self.assertEqual(w.s["idle_nolog"], [])

    def test_each_event_kind(self):
        w = self.watcher({"branches": {"repo main": "old"}, "usage": {"1": {"five": 10, "week": 90}},
                          "bg_alerts": [], "idle": {}}, )
        with open(os.path.join(self.alerts, "r-5.json"), "w") as f:
            json.dump({"id": "r-5", "reason": "failed"}, f)
        self.use = {"1": {"five": 10, "week": 98}}
        w.start(5000.0)
        ev = w.poll(5000.0)
        self.assertEqual(ev[:2], ["BRANCH repo main abcd1234", "USAGE account 1 seven_day 98% (now 98%)"])
        self.assertTrue(ev[2].startswith("IDLE n7 66m since "), ev[2])
        self.assertEqual(ev[3], "BGFAIL run r-5 failed")
        self.assertEqual(w.s["branches"]["repo main"], "abcd1234")

    def test_watcher_reads_stop_log_beside_sessions_file(self):
        with open(os.path.join(self.tmp.name, "stop.log"), "w", encoding="utf-8") as f:
            f.write("%s stopped 'n7' claude PID 41, window PID -, conhost PID -; successor '-'\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1060.0)))
        w = self.watcher({"branches": {"repo main": "abcd1234"}, "usage": self.use, "bg_alerts": [],
                          "idle": {}})
        w.start(5000.0)
        ev = w.poll(5000.0)
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0].startswith("STOPPED n7 at "), ev)

    def test_error_on_third_failure_only(self):
        def bad():
            raise RuntimeError("usage.ps1 printed no account lines")
        w = self.watcher({"branches": {"repo main": "abcd1234"}, "usage": {}, "bg_alerts": [],
                          "idle": {}}, usage_reader=bad)
        w.usage_poll = 0
        got = [w.poll(1000.0 + i) for i in range(4)]
        self.assertEqual([g for g in got if g], [["ERROR usage check: usage.ps1 printed no account lines"]])


class Cli(TempHomeCase):
    # TempHomeCase (tests/home.py): parse's defaults and main's load_config read the machine
    # config (an empty one here), never the real home's.
    def test_missing_sessions_exits_2(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as c:
            cli.parse([], {}, launcher_set=True)
        self.assertEqual(c.exception.code, 2)
        self.assertIn("pm_watch.sessions_file", err.getvalue())

    def test_no_launcher_key_exits_2(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as c:
            cli.parse(["--sessions", "s.json"], {}, launcher_set=False)
        self.assertEqual(c.exception.code, 2)
        self.assertIn("no usage source", err.getvalue())

    def test_usage_script_flag_is_refused_with_a_pointer(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as c:
            cli.parse(["--sessions", "s.json", "--usage-script", "u.ps1"], {}, launcher_set=True)
        self.assertEqual(c.exception.code, 2)
        self.assertIn("retired", err.getvalue())
        self.assertIn("kitlib.launcher.usage", err.getvalue())

    def test_usage_script_config_key_is_refused_with_a_pointer(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as c:
            cli.parse(["--sessions", "s.json"], {"usage_script": "u.ps1"}, launcher_set=True)
        self.assertEqual(c.exception.code, 2)
        self.assertIn("pm_watch.usage_script", err.getvalue())
        self.assertIn("retired", err.getvalue())

    def test_usage_takes_no_script_and_starts_no_process(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("started a process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("started a process")):
            got = W.usage(report=lambda: ["account 1: five_hour=3%  seven_day=4%"])
        self.assertEqual(got, {"1": {"five": 3, "week": 4, "age": None, "stale": True, "missing": False}})
        with self.assertRaises(TypeError):
            W.usage("u.ps1", report=lambda: [])

    def test_launcher_configured_reads_the_machine_config(self):
        with tempfile.TemporaryDirectory() as home:
            pm_repo(self)                                   # the fenced pointer's PM repository
            self.assertFalse(W.launcher_configured(home))
            machine_config(home, {"launcher": {"accounts": {"1": ["~/.claude"]}}}, case=self)
            self.assertTrue(W.launcher_configured(home))

    def test_usage_is_read_in_process(self):
        got = W.usage(report=lambda: ["account 1: five_hour=reset  seven_day=51% (resets Mon 10:00)  "
                                            "[updated 3 min ago]", "account 2: no usage file",
                                            "current account: 1"])
        self.assertEqual(got, {"1": {"five": 0, "week": 51, "age": 3, "stale": False, "missing": False},
                               "2": {"five": None, "week": None, "age": None, "stale": True, "missing": True}})

    def test_config_keys_are_defaults_and_flags_win(self):
        cfg = {"sessions_file": "s.json", "alerts_dir": "A", "state_file": "S"}
        a = cli.parse([], cfg, launcher_set=True)
        self.assertEqual((a.sessions, a.alerts_dir, a.state, a.idle_minutes, a.idle_poll),
                         ("s.json", "A", "S", 20, 300))
        a = cli.parse(["--sessions", "x.json", "--state", "T", "--idle-minutes", "5", "--idle-poll", "60"], cfg,
                      launcher_set=True)
        self.assertEqual((a.sessions, a.state, a.idle_minutes, a.idle_poll), ("x.json", "T", 5, 60))

    def test_load_config_reads_pm_watch_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"pm_start": {}, "pm_watch": {"sessions_file": "s.json"}}, f)
            self.assertEqual(W.load_config(p), {"sessions_file": "s.json"})
            self.assertEqual(W.load_config(os.path.join(d, "none.json")), {})

    def test_once_with_no_events_exits_0_and_saves_state(self):
        with tempfile.TemporaryDirectory() as d:
            sessions, state = os.path.join(d, "s.json"), os.path.join(d, "st", "state.json")
            with open(sessions, "w", encoding="utf-8") as f:
                json.dump({}, f)
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.object(
                    W, "usage", return_value={"1": {"five": 1, "week": 2}}):
                rc = cli.main(["--sessions", sessions,
                               "--alerts-dir", os.path.join(d, "none"), "--state", state, "--once",
                               "--notice-dir", ""],
                              sleep=lambda s: self.fail("slept"), launcher_set=True, plugin=False, hub=False, rc=False)
            self.assertEqual((rc, out.getvalue()), (0, ""))
            with open(state, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"branches": {}, "usage": {"1": {"five": 1, "week": 2}},
                                                "bg_alerts": [], "idle": {}, "idle_nolog": []})


if __name__ == "__main__":
    unittest.main()
