"""pm_watch steps aside for the hub's session-watch job (docs/pm-watch.md "Hub session-watch job"):
its session checks keep their state but print nothing while the job is up; BRANCH stays."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import pmwatch as W

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools"))
import pm_watch as cli                                              # noqa: E402
from tests.home import TempHomeCase                                 # noqa: E402


class Client(object):
    def __init__(self, health):
        self.health = health

    def get(self, path, timeout=None):
        if isinstance(self.health, Exception):
            raise self.health
        return self.health


def jobs_of(health):
    return dict(load=lambda: mock.Mock(port=1), make_client=lambda port: Client(health))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.alerts = os.path.join(self.tmp.name, "alerts")
        os.makedirs(self.alerts)
        self.sessions = os.path.join(self.tmp.name, "sessions.json")
        with open(self.sessions, "w", encoding="utf-8") as f:
            json.dump({"r": {"name": "n7"}}, f)
        self.up = [True]
        self.tipv = ["t1"]

    def watcher(self, usage_reader=lambda: {}):
        w = W.Watcher({}, [("repo", "main")], usage_reader, self.alerts, self.sessions,
                      lambda n: 1000.0, tip=lambda r, ref: self.tipv[0], branch_poll=0,
                      yield_to=lambda: self.up[0])
        w.start(1000.0)
        return w

    def alert(self, run_id):
        with open(os.path.join(self.alerts, run_id + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": run_id, "reason": "exit 1"}, f)


class StepsAside(Base):
    def test_yielded_session_lines_are_dropped_but_the_state_advances(self):
        w = self.watcher()
        self.alert("r1")
        self.assertEqual(w.poll(1060.0), [])
        self.assertTrue(w.yielded)
        self.assertIn("r1.json", w.s["bg_alerts"])
        self.up[0] = False                          # the hub went down: pm_watch takes over
        self.alert("r2")
        self.assertEqual(w.poll(1120.0), ["BGFAIL run r2 exit 1"], "r1 is never repeated")
        self.assertFalse(w.yielded)

    def test_branch_stays_with_pm_watch_while_yielded(self):
        w = self.watcher()
        self.tipv[0] = "t2"
        self.alert("r1")
        self.assertEqual(w.poll(1060.0), ["BRANCH repo main t2"])

    def test_a_session_checks_error_is_dropped_while_yielded_a_branch_error_is_not(self):
        def bad_usage():
            raise RuntimeError("no usage")
        w = self.watcher()
        w.usage_reader, w.usage_poll = bad_usage, 0
        got = [w.poll(1060.0 + i) for i in range(3)]
        self.assertEqual(got, [[], [], []])
        w2 = self.watcher()

        def bad_tip(r, ref):
            raise RuntimeError("ls-remote exit 128")
        w2.tip = bad_tip
        got = [w2.poll(1060.0 + i) for i in range(3)]
        self.assertEqual(got[2], ["ERROR branch check: ls-remote exit 128"])

    def test_a_yield_test_that_raises_is_not_yielded(self):
        w = self.watcher()

        def boom():
            raise OSError("pipe")
        w.yield_to = boom
        self.alert("r1")
        self.assertEqual(w.poll(1060.0), ["BGFAIL run r1 exit 1"])
        self.assertFalse(w.yielded)

    def test_no_yield_test_never_yields(self):
        w = self.watcher()
        w.yield_to = None
        self.alert("r1")
        self.assertEqual(w.poll(1060.0), ["BGFAIL run r1 exit 1"])


class HubSessionJobUp(unittest.TestCase):
    def test_reads_health_jobs(self):
        up = lambda health: W.hub_session_job_up(**jobs_of(health))
        self.assertTrue(up({"jobs": {"session-watch": {"failures": 0, "ok_at": None}}}))
        self.assertTrue(up({"jobs": {"session-watch": {"failures": 2}}}))
        self.assertFalse(up({"jobs": {"session-watch": {"failures": 3}}}), "a failing job covers nothing")
        self.assertFalse(up({"jobs": {"session-watch": {}}}))
        self.assertFalse(up({"jobs": {"plugin-refresh": {"failures": 0}}}))
        self.assertFalse(up({"jobs": None}))
        self.assertFalse(up({}))
        self.assertFalse(up(OSError("down")))

    def test_plugin_job_up_still_reads_its_own_name(self):
        self.assertTrue(W.hub_plugin_job_up(**jobs_of({"jobs": {"plugin-refresh": {}}})))
        self.assertFalse(W.hub_plugin_job_up(**jobs_of({"jobs": {"session-watch": {}}})))


class SessionJobHealthy(unittest.TestCase):
    def test_rule(self):
        now = 10_000.0
        ok = lambda **b: W.session_job_healthy(dict({"failures": 0, "ok_at": now - 60}, **b), now)
        self.assertTrue(ok())
        self.assertTrue(ok(ok_at=now - W.SESSION_JOB_STALE_S))
        self.assertFalse(ok(ok_at=now - W.SESSION_JOB_STALE_S - 1))
        self.assertFalse(ok(ok_at=None), "a job that only skipped has no success")
        self.assertFalse(ok(failures=3))
        self.assertFalse(ok(failures=True))
        self.assertFalse(W.session_job_healthy(None, now))
        self.assertFalse(W.session_job_healthy("up", now))


class SessionYield(unittest.TestCase):
    def test_only_for_the_job_s_own_sessions_file(self):
        up = lambda: True
        here = os.path.abspath("sessions.json")
        self.assertIs(W.session_yield(here, {"sessions_file": here.upper()}, up), up)
        self.assertIsNone(W.session_yield(here, {"sessions_file": here + ".other"}, up))
        self.assertIsNone(W.session_yield(here, {}, up), "no key: the job skips every run")
        self.assertIs(W.session_yield(here, {"sessions_file": here}), W.hub_session_job_up)


class Heartbeat(unittest.TestCase):
    def test_yielded_is_written_and_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json.beat")
            W.write_heartbeat(p, 4, 100.0, False, yielded=True)
            self.assertIs(W.read_heartbeat(p)["yielded"], True)
            W.write_heartbeat(p, 4, 100.0, False)
            self.assertIs(W.read_heartbeat(p)["yielded"], False)


class Stop(Exception):
    pass


class CliBeat(TempHomeCase):
    """tools/pm_watch.py writes the latest poll's yielded answer into its heartbeat."""

    def run_main(self, yielded):
        d = tempfile.mkdtemp(dir=self.home)
        sessions, state = os.path.join(d, "s.json"), os.path.join(d, "state.json")
        with open(sessions, "w", encoding="utf-8") as f:
            json.dump({}, f)

        def sleep(s):
            raise Stop()
        with mock.patch.object(W, "usage", return_value={}), self.assertRaises(Stop):
            cli.main(["--sessions", sessions, "--alerts-dir", os.path.join(d, "none"),
                      "--state", state, "--notice-dir", ""], sleep=sleep, launcher_set=True,
                     plugin=False, hub=False, rc=False, yield_to=lambda: yielded)
        return W.read_heartbeat(W.heartbeat_file(state))

    def test_the_heartbeat_carries_yielded(self):
        self.assertIs(self.run_main(True)["yielded"], True)
        self.assertIs(self.run_main(False)["yielded"], False)


if __name__ == "__main__":
    unittest.main()
