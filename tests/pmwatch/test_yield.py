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


NOW = 50_000.0


def block(**kw):
    return dict({"every_s": 60, "runs": 4, "failures": 0, "running": False,
                 "last_at": NOW - 30, "ok_at": None}, **kw)


class HubSessionJobUp(unittest.TestCase):
    def test_reads_health_jobs(self):
        up = lambda health: W.hub_session_job_up(now=NOW, **jobs_of(health))
        self.assertTrue(up({"jobs": {"session-watch": block()}}), "a skipping job (no ok_at) is up")
        self.assertFalse(up({"jobs": {"plugin-refresh": block()}}))
        self.assertFalse(up({"jobs": {"session-watch": {}}}))
        self.assertFalse(up({"jobs": None}))
        self.assertFalse(up({}))
        self.assertFalse(up(OSError("down")))

    def test_one_failure_ends_the_step_aside(self):
        live = lambda **kw: W.session_job_live(block(**kw), NOW)
        self.assertFalse(live(failures=1), "a job failing before it polls would lose events")
        self.assertFalse(live(failures=True))

    def test_a_hung_job_with_no_failure_is_not_up(self):
        live = lambda **kw: W.session_job_live(block(**kw), NOW)
        self.assertTrue(live(last_at=NOW - 3 * 60))
        self.assertFalse(live(last_at=NOW - 3 * 60 - 1), "a stale last_at: the job thread is hung")
        self.assertFalse(live(last_at=NOW - 3600, running=True))
        self.assertFalse(live(last_at=None, runs=0), "not run yet: pm_watch keeps its checks")
        self.assertFalse(live(every_s=0))
        self.assertFalse(live(every_s="60"))

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
    def test_only_for_the_job_s_own_inputs(self):
        up = lambda: True
        s, a, n = (os.path.abspath(x) for x in ("sessions.json", "alerts", "notices"))
        job = W.job_inputs({"sessions_file": s}, a, n)
        self.assertEqual(job, (s, a, n), "the default alerts folder stands in for an unset key")
        self.assertIs(W.session_yield((s.upper(), a, n), job, up), up)
        self.assertIs(W.session_yield((s, a, n), job), W.hub_session_job_up)
        self.assertIsNone(W.session_yield((s + ".other", a, n), job, up))
        self.assertIsNone(W.session_yield((s, a + "2", n), job, up), "--alerts-dir elsewhere")
        self.assertIsNone(W.session_yield((s, a, n + "2"), job, up), "--notice-dir elsewhere")
        self.assertIsNone(W.session_yield((s, a, None), job, up), "no notice check here, the job has one")
        self.assertIs(W.session_yield((s, a, ""), (s, a, None), up), up, "neither has a notice folder")
        self.assertIsNone(W.session_yield((s, a, n), W.job_inputs({}, a, n), up),
                          "no key: the job skips every run")
        self.assertEqual(W.job_inputs({"sessions_file": s, "alerts_dir": "X:/al"}, a, None),
                         (s, "X:/al", None))


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

    def run_default(self, cfg_of, args_of=lambda d: []):
        """main with no yield_to: the default wiring (W.session_yield over job_inputs) decides,
        with the hub's job always up and the machine config `cfg_of(folder)`."""
        d = tempfile.mkdtemp(dir=self.home)
        sessions, state = os.path.join(d, "s.json"), os.path.join(d, "state.json")
        with open(sessions, "w", encoding="utf-8") as f:
            json.dump({}, f)
        cfg = dict(cfg_of(d), sessions_file=sessions, state_file=state)

        def sleep(s):
            raise Stop()
        with mock.patch.object(W, "usage", return_value={}),                 mock.patch.object(W, "load_config", return_value=cfg),                 mock.patch.object(W, "configured_notice_dir", return_value=None),                 mock.patch.object(W, "hub_session_job_up", lambda: True), self.assertRaises(Stop):
            cli.main(args_of(d), sleep=sleep, launcher_set=True, plugin=False, hub=False, rc=False)
        return W.read_heartbeat(W.heartbeat_file(state))

    def test_main_yields_by_default_only_on_the_job_s_inputs(self):
        alerts = lambda d: {"alerts_dir": os.path.join(d, "alerts")}
        self.assertIs(self.run_default(alerts)["yielded"], True)
        self.assertIs(self.run_default(alerts, lambda d: ["--alerts-dir", os.path.join(d, "x")])["yielded"],
                      False, "alerts nobody else watches are not dropped")


if __name__ == "__main__":
    unittest.main()
