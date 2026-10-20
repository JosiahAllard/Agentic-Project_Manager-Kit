"""kitlib.lock.api.history: the history lines of runs that ended in a range, one per run (the
schedule window draws past runs from them; api.stats only aggregates)."""
import datetime
import os
import unittest

from tests.lock.helpers import StoreCase, agent, claude_of

try:
    from kitlib.lock import api
    from kitlib.lock.store import iso, utc_now
except ImportError:                 # not Windows: StoreCase skips
    pass


class History(StoreCase):
    def ended(self, rid, hours_ago, held_h=1.0, **kw):
        end = utc_now() - datetime.timedelta(hours=hours_ago)
        d = {"id": rid, "repo": "kit", "slug": "g", "kind": "engine-test",
             "since": iso(end - datetime.timedelta(hours=held_h + 0.25)),
             "held_since": iso(end - datetime.timedelta(hours=held_h)), "ended": iso(end),
             "code": 0, "how": "done", "cores": [0, 1], "gpus": []}
        d.update(kw)
        self.store.history_append(d)

    def test_empty_store(self):
        self.assertEqual(api.history(self.store), [])

    def test_lines_in_range_oldest_first(self):
        self.ended("r-20261001T000000Z-aaaa", 24 * 10)         # outside 7 days
        self.ended("r-20261007T000000Z-bbbb", 30)
        self.ended("r-20261008T000000Z-cccc", 2, held_h=0.5)
        rows = api.history(self.store)
        self.assertEqual([r["id"] for r in rows], ["r-20261007T000000Z-bbbb", "r-20261008T000000Z-cccc"])
        self.assertEqual(rows[1]["held_s"], 1800)
        self.assertEqual(rows[1]["wait_s"], 900)
        self.assertEqual(len(api.history(self.store, days=1)), 1)
        self.assertEqual(len(api.history(self.store, days=30)), 3)

    def test_since_and_until(self):
        self.ended("r-20261007T000000Z-bbbb", 30)
        self.ended("r-20261008T000000Z-cccc", 2)
        cut = iso(utc_now() - datetime.timedelta(hours=10))
        self.assertEqual([r["id"] for r in api.history(self.store, until=cut)],
                         ["r-20261007T000000Z-bbbb"])
        self.assertEqual([r["id"] for r in api.history(self.store, since=cut)],
                         ["r-20261008T000000Z-cccc"])

    def test_a_bad_time_raises_the_invalid_error(self):
        with self.assertRaises(api.ApiError) as e:
            api.history(self.store, since="yesterday")
        self.assertEqual(e.exception.code, "invalid")

    def test_a_bad_days_raises_the_invalid_error(self):
        for bad in ("week", None, [7]):
            with self.assertRaises(api.ApiError) as e:
                api.history(self.store, days=bad)
            self.assertEqual(e.exception.code, "invalid")

    def put(self, rid, ended):
        self.store.history_append({"id": rid, "repo": "kit", "slug": "g", "kind": "engine-test",
                                   "since": ended, "held_since": ended, "ended": ended, "code": 0,
                                   "how": "done", "cores": [], "gpus": []})

    def test_until_is_exclusive(self):
        self.put("r-20261008T100000Z-aaaa", "2026-10-08T10:00:00Z")
        self.put("r-20261008T110000Z-bbbb", "2026-10-08T11:00:00Z")
        got = api.history(self.store, since="2026-10-08T10:00:00Z", until="2026-10-08T11:00:00Z")
        self.assertEqual([r["id"] for r in got], ["r-20261008T100000Z-aaaa"])

    def test_a_range_over_a_month_boundary_reads_both_months(self):
        self.put("r-20260930T233000Z-aaaa", "2026-09-30T23:30:00Z")
        self.put("r-20261001T003000Z-bbbb", "2026-10-01T00:30:00Z")
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "history"))),
                         ["2026-09.jsonl", "2026-10.jsonl"])
        got = api.history(self.store, since="2026-09-30T23:00:00Z", until="2026-10-01T01:00:00Z")
        self.assertEqual([r["id"] for r in got], ["r-20260930T233000Z-aaaa", "r-20261001T003000Z-bbbb"])

    def test_a_history_line_names_who_started_and_who_stopped_the_run(self):
        from kitlib.lock import run as rm
        r = rm.Run(self.store, self.layout(), "kit", "g", "engine-test", "n", ["x"], "running",
                   env={"CLAUDE_CODE_SESSION_ID": "sess-start"})
        r.save(held_since=iso(utc_now()))
        self.assertTrue(rm.request_stop(self.store, r.id, "agent-b", "sess-stop"))
        self.store.history_append(r.finish(0, "stopped"))
        row = api.history(self.store)[0]
        self.assertEqual(row["session"], "sess-start")
        self.assertIn(row["role"], ("owner", "agent"))
        self.assertEqual(row["stopped_by"], {"by": "agent-b", "session": "sess-stop"})

    def test_a_run_nobody_stopped_has_a_null_stopped_by_and_a_missing_session_is_null(self):
        from kitlib.lock import run as rm
        r = rm.Run(self.store, self.layout(), "kit", "g", "engine-test", "n", ["x"], "running",
                   env={})
        r.save(held_since=iso(utc_now()))
        self.store.history_append(r.finish(0, "done"))
        row = api.history(self.store)[0]
        self.assertIsNone(row["stopped_by"])
        self.assertIsNone(row["session"])

    def test_end_run_carries_the_askers_session_to_the_stop_file_and_the_history_line(self):
        from kitlib.lock import run as rm
        r = rm.Run(self.store, self.layout(), "kit", "g", "engine-test", "n", ["x"], "running",
                   env={})
        r.save(held_since=iso(utc_now()), ancestors=[claude_of("sess-A")])
        self.assertTrue(api.end_run(agent("sess-A"), r.id, self.store)["ok"])
        self.assertEqual(self.store.read("active/%s.stop" % r.id)["session"], "sess-A")
        self.store.history_append(r.finish(0, "stopped"))
        self.assertEqual(api.history(self.store)[0]["stopped_by"],
                         {"by": "test-agent", "session": "sess-A"})


if __name__ == "__main__":
    unittest.main()
