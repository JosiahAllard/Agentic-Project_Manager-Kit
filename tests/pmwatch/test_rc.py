"""The REMOTE check: a running session whose Remote Control link dropped (docs/pm-watch.md
"Remote Control link"). rc_events over fake records, the Claude reader over a real registry folder,
and the check inside a Watcher."""
import json
import os
import tempfile
import unittest

from kitlib import pmwatch as W
from hosts.claude.adapter import claude_host as H

NOW = 2_000_000_000.0
OLD_MS = (NOW - 3600) * 1000           # started an hour ago
YOUNG_MS = (NOW - 60) * 1000           # started a minute ago: inside the grace period
PM_KEY = "pm-session 11 900"           # rc_lost's entry for rec(): name, pid, creation time


def rec(name="pm-session", pid=11, linked=False, started=OLD_MS, proc_start="900"):
    return {"name": name, "pid": pid, "proc_start": proc_start, "started_ms": started,
            "linked": linked}


def live(r):
    return r["pid"] != 99                # pid 99 stands for a dead or reused process


def rc_pids_of(*pairs):
    calls = []

    def rc_pids():
        calls.append(1)
        return dict(pairs)
    return rc_pids, calls


class RcEvents(unittest.TestCase):
    def events(self, records, lost=(), pids=((11, "pm-session"),), names=("pm-session",)):
        rc_pids, self.calls = rc_pids_of(*pids)
        return W.rc_events(set(names), records, live, rc_pids, list(lost), NOW)

    def test_unlinked_live_session_with_remote_control_is_reported_once(self):
        ev, lost = self.events([rec()])
        self.assertEqual(ev, ["REMOTE pm-session link lost: the session runs, its Remote Control "
                              "link is down (pid 11)"])
        self.assertEqual(lost, [PM_KEY])
        self.assertEqual(self.events([rec()], lost=lost), ([], [PM_KEY]))

    def test_link_back_is_reported_once_and_rearms(self):
        self.assertEqual(self.events([rec(linked=True)], lost=[PM_KEY]),
                         (["REMOTE pm-session link back"], []))
        self.assertEqual(self.events([rec(linked=True)]), ([], []))
        self.assertEqual(self.events([rec()])[1], [PM_KEY])

    def test_linked_session_reads_no_process_list(self):
        self.assertEqual(self.events([rec(linked=True)]), ([], []))
        self.assertEqual(self.calls, [])

    def test_young_session_is_inside_the_grace_period(self):
        self.assertEqual(self.events([rec(started=YOUNG_MS)]), ([], []))
        self.assertEqual(self.events([rec(started=YOUNG_MS)], lost=[PM_KEY]),
                         ([], [PM_KEY]))

    def test_record_without_the_link_field_says_nothing(self):
        self.assertEqual(self.events([rec(linked=None)]), ([], []))

    def test_dead_or_reused_pid_is_no_session(self):
        self.assertEqual(self.events([rec(pid=99)], pids=((99, "pm-session"),)), ([], []))

    def test_session_started_without_remote_control_is_skipped(self):
        self.assertEqual(self.events([rec()], pids=((11, None),)), ([], []))
        self.assertEqual(self.events([rec()], pids=((12, "pm-session"),)), ([], []))

    def test_unwatched_name_is_ignored(self):
        self.assertEqual(self.events([rec(name="other")], pids=((11, "other"),)), ([], []))

    def test_newest_live_record_of_a_name_wins(self):
        stale = rec(pid=99, linked=False, started=OLD_MS + 1000)    # newer but dead
        old_unlinked = rec(pid=7, linked=False, started=OLD_MS - 1000)
        current = rec(pid=11, linked=True, started=OLD_MS)
        self.assertEqual(self.events([stale, old_unlinked, current]), ([], []))

    def test_session_gone_drops_out(self):
        self.assertEqual(self.events([], lost=[PM_KEY]), ([], []))

    def test_relaunch_under_the_same_name_starts_clean(self):
        relaunched_linked = rec(pid=12, proc_start="901", linked=True)
        self.assertEqual(self.events([relaunched_linked], lost=[PM_KEY]), ([], []))
        relaunched_unlinked = rec(pid=12, proc_start="901")
        ev, lost = self.events([relaunched_unlinked], lost=[PM_KEY], pids=((12, "pm-session"),))
        self.assertEqual(ev, ["REMOTE pm-session link lost: the session runs, its Remote Control "
                              "link is down (pid 12)"])
        self.assertEqual(lost, ["pm-session 12 901"])

    def test_record_whose_name_is_not_text_is_skipped(self):
        for bad in (["pm-session"], {"n": 1}, 7, None):
            with self.subTest(name=bad):
                self.assertEqual(self.events([rec(name=bad), rec()])[1], [PM_KEY])


class ClaudeReader(unittest.TestCase):
    def test_reads_the_bridge_field_of_each_registry_record(self):
        with tempfile.TemporaryDirectory() as d:
            for pid, extra in ((1, {"bridgeSessionId": None}),
                               (2, {"bridgeSessionId": "session_x"}),
                               (3, {})):
                with open(os.path.join(d, "%d.json" % pid), "w", encoding="utf-8") as f:
                    json.dump(dict({"pid": pid, "name": "n%d" % pid, "startedAt": 5,
                                    "procStart": "77"}, **extra), f)
            got = sorted(H.rc_records([d]), key=lambda r: r["pid"])
        self.assertEqual([(r["name"], r["pid"], r["proc_start"], r["started_ms"], r["linked"])
                          for r in got],
                         [("n1", 1, "77", 5, False), ("n2", 2, "77", 5, True), ("n3", 3, "77", 5, None)])


class InWatcher(unittest.TestCase):
    def watcher(self, d, records, ctime):
        sessions = os.path.join(d, "sessions.json")
        with open(sessions, "w", encoding="utf-8") as f:
            json.dump({"k": {"name": "kit-a1"}}, f)
        rc = W.rc_check(sessions, lambda dirs: records(), dirs=lambda: [], ctime=ctime,
                        rc_pids=lambda: {11: "pm-session", 12: "kit-a1"})
        return W.Watcher({}, [], lambda: {}, os.path.join(d, "none"), sessions, lambda n: NOW,
                         idle_poll=300, rc=rc)

    def test_pm_and_sessions_json_names_are_watched_on_the_idle_cadence(self):
        recs = [{"name": "pm-session", "pid": 11, "proc_start": "5", "started_ms": 1000, "linked": False},
                {"name": "kit-a1", "pid": 12, "proc_start": "5", "started_ms": 1000, "linked": False}]
        with tempfile.TemporaryDirectory() as d:
            w = self.watcher(d, lambda: recs, ctime=lambda pid: 5)
            ev = w.poll(NOW)
            self.assertEqual([e.split(":")[0] for e in ev],
                             ["REMOTE kit-a1 link lost", "REMOTE pm-session link lost"])
            self.assertEqual(w.s["rc_lost"], ["kit-a1 12 5", "pm-session 11 5"])
            recs[0]["linked"] = True
            self.assertEqual(w.poll(NOW + 60), [])               # not due yet
            self.assertEqual(w.poll(NOW + 300), ["REMOTE pm-session link back"])

    def test_creation_time_other_than_the_records_is_no_session(self):
        def raises(pid):
            raise OSError("access denied")
        recs = [{"name": "pm-session", "pid": 11, "proc_start": "5", "started_ms": 1000, "linked": False}]
        for label, ctime in (("differs", lambda pid: 6), ("unknown", lambda pid: None),
                             ("raises", raises)):
            with self.subTest(ctime=label), tempfile.TemporaryDirectory() as d:
                w = self.watcher(d, lambda: recs, ctime=ctime)
                self.assertEqual([e for e in w.poll(NOW) if e.startswith("REMOTE")], [])
                self.assertEqual(w.s["rc_lost"], [])

    def test_third_failure_in_a_row_is_an_error_line(self):
        def boom():
            raise OSError("registry unreadable")
        with tempfile.TemporaryDirectory() as d:
            w = self.watcher(d, boom, ctime=lambda pid: 5)
            self.assertEqual(w.poll(NOW) + w.poll(NOW + 300), [])
            self.assertEqual(w.poll(NOW + 600), ["ERROR rc check: registry unreadable"])


if __name__ == "__main__":
    unittest.main()
