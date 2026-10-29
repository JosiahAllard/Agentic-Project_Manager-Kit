"""kitlib.pmwatch.run (stream and relaunch modes) and the heartbeat pm_watch writes for the PM
session-start check (docs/pm-watch.md)."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr

from kitlib import pmwatch as W

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools"))
import pm_watch as cli                                              # noqa: E402
from tests.home import TempHomeCase                                 # noqa: E402


class Stop(Exception):
    pass


class FakeWatcher:
    """Hands out one list of events per poll from `batches`; [] once they run out."""

    def __init__(self, batches):
        self.batches, self.s, self.started = list(batches), {"n": 0}, None

    def start(self, now):
        self.started = now

    def poll(self, now):
        self.s = {"n": self.s["n"] + 1}
        return self.batches.pop(0) if self.batches else []


class RunTest(unittest.TestCase):

    def go(self, batches, passes=10, **kw):
        w, emitted, beats, saved, slept = FakeWatcher(batches), [], [], [], []

        def sleep(s):
            slept.append(s)
            if len(slept) >= passes:
                raise Stop()
        clock = iter(range(100, 10000)).__next__
        try:
            rc = W.run(w, clock, sleep, saved.append, emitted.append, beats.append, **kw)
        except Stop:
            rc = "stopped"
        return rc, emitted, beats, saved, slept

    def test_stream_emits_every_batch_and_keeps_polling(self):
        rc, emitted, beats, _saved, slept = self.go([["A 1"], [], ["B 1", "B 2"]], passes=4, stream=True)
        self.assertEqual(rc, "stopped")                  # never returned on an event
        self.assertEqual(emitted, ["A 1", "B 1\nB 2"])
        self.assertEqual(len(beats), 4)                  # a heartbeat every pass
        self.assertEqual(slept, [W.POLL_SLEEP] * 4)

    def test_without_stream_the_first_events_end_the_run(self):
        rc, emitted, beats, saved, slept = self.go([[], ["A 1"], ["B 1"]])
        self.assertEqual((rc, emitted, slept), (0, ["A 1"], [W.POLL_SLEEP]))
        self.assertEqual(len(beats), 2)
        self.assertEqual(saved[-1], {"n": 2})            # the state is saved before the exit

    def test_a_pass_sleeps_a_minute_inside_the_heartbeat_stale_limit(self):
        from kitlib import pmstart
        self.assertEqual(W.POLL_SLEEP, 60)               # machine load: half the passes of the old 30 s
        self.assertLess(W.POLL_SLEEP * 2, pmstart.BEAT_STALE)

    def test_a_failed_heartbeat_write_never_stops_the_stream(self):
        w, emitted, slept, tries = FakeWatcher([["A 1"], ["B 1"]]), [], [], []

        def beat(now):
            tries.append(now)
            raise PermissionError(13, "Access is denied")   # os.replace onto a beat file held open

        def sleep(s):
            slept.append(s)
            if len(slept) >= 3:
                raise Stop()
        with self.assertRaises(Stop):
            W.run(w, iter(range(100, 10000)).__next__, sleep, lambda s: None, emitted.append, beat,
                  stream=True)
        self.assertEqual(emitted, ["A 1", "B 1"])
        self.assertEqual(len(tries), 3)                  # tried again every pass

    def test_once_polls_once_and_writes_no_heartbeat(self):
        rc, emitted, beats, _saved, slept = self.go([["A 1"]], once=True)
        self.assertEqual((rc, emitted, beats, slept), (0, ["A 1"], [], []))


class HeartbeatTest(unittest.TestCase):

    def test_file_sits_beside_the_state_file(self):
        self.assertEqual(W.heartbeat_file(os.path.join("d", "state.json")), os.path.join("d", "state.json.beat"))

    def test_write_then_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "state.json.beat")
            self.assertIsNone(W.read_heartbeat(p))
            W.write_heartbeat(p, 42, 12.5, True)
            self.assertEqual(W.read_heartbeat(p), {"pid": 42, "beat": 12.5, "stream": True, "started": None,
                                                    "yielded": False})
            W.write_heartbeat(p, 42, 12.5, False, 1700000000.25)
            self.assertEqual(W.read_heartbeat(p), {"pid": 42, "beat": 12.5, "stream": False,
                                                   "started": 1700000000.25, "yielded": False})
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"pid": 42, "beat": 1}, f)     # an older watcher's heartbeat
            self.assertIsNone(W.read_heartbeat(p)["started"])

    def test_own_start_time_is_this_process_start(self):
        t = W.own_start_time()
        self.assertIsInstance(t, float)
        self.assertLessEqual(t, time.time())
        self.assertGreater(t, time.time() - 2 * 86400)   # this test process began within the run

    def test_malformed_reads_as_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b")
            for bad in ("{not json", "[1]", json.dumps({"pid": "42", "beat": 1}),
                        json.dumps({"pid": 42}), json.dumps({"pid": 42, "beat": True}),
                        json.dumps({"pid": True, "beat": 1}),
                        json.dumps({"pid": 42, "beat": 1, "started": "noon"}),
                        json.dumps({"pid": 42, "beat": 1, "started": True})):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(bad)
                self.assertIsNone(W.read_heartbeat(p), bad)


class CliTest(TempHomeCase):
    # TempHomeCase (tests/home.py): parse's --notice-dir default reads an empty machine config.

    def test_stream_flag_parses(self):
        a = cli.parse(["--sessions", "s.json", "--stream"], {}, launcher_set=True)
        self.assertTrue(a.stream)
        self.assertFalse(a.once)

    def test_stream_and_once_together_exit_2(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as c:
            cli.parse(["--sessions", "s.json", "--stream", "--once"], {}, launcher_set=True)
        self.assertEqual(c.exception.code, 2)
        self.assertIn("not allowed with", err.getvalue())


if __name__ == "__main__":
    unittest.main()
