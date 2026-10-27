"""A held run whose job is a kitlib.lock.jobset JobSet, as on Linux, where a job starts one
command: here on Windows, a JobSet of real job objects (jobset.one_command patched on), so a
Slot's starts, reads, give-back and tree end run through the set. The set on fakes:
tests/lock/test_jobset.py."""
import json
import os
import sys
import unittest
from unittest import mock

import kitlib.platform
from .helpers import StoreCase, clean_env
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.lock import jobset
    from kitlib.lock import layout as lay
    from kitlib.lock import run as rm
    from kitlib.proc import process
    from .test_giveback import affinity, gb, usable_slots

SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


@windows_only("the lock is Windows only; real job objects stand in for Linux's one-command jobs")
class SlotJobSet(StoreCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(jobset, "one_command", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def held(self, n=2):
        usable = usable_slots(self, n)
        s = rm.book("engine-test", "kit", "t", cores=n, store=self.store, env=clean_env())
        self.addCleanup(s.release)
        return s, usable

    def sleeper(self, s, **kw):
        p = s.start(SLEEP, **kw)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        return p

    def test_each_command_runs_in_a_job_of_its_own_and_the_run_measures_them_all(self):
        s, _usable = self.held()
        self.assertIsInstance(s.job, jobset.JobSet)
        burn = [sys.executable, "-c", "import time\nt=time.process_time()+0.3\n"
                "while time.process_time()<t: pass"]
        self.assertEqual(s.launcher.run(burn), 0)
        self.assertEqual(s.launcher.run(burn), 0)
        self.assertGreaterEqual(s.job.usage()[0], 0.5, "the cpu of both commands")
        self.assertEqual(s.job.jobs[:-1], [], "a finished command's job is closed")
        rec = s.release()
        self.assertEqual(rec["how"], "done")
        self.assertGreaterEqual(rec["cpu_s"], 0.5)

    def test_two_live_commands_are_both_the_run_s_and_its_tree_end_ends_both(self):
        s, _usable = self.held()
        a, b = self.sleeper(s), self.sleeper(s)
        self.assertEqual(len(s.job.jobs), 2)
        self.assertEqual(sorted(s.launcher.pids()), sorted([a.pid, b.pid]))
        self.assertEqual((s.job.contains(a.pid), s.job.contains(b.pid)), ("in", "in"))
        s.launcher.kill()
        self.assertIsNotNone(a.wait(10))
        self.assertIsNotNone(b.wait(10))
        self.assertEqual(s.release(how="cancelled")["how"], "cancelled")

    def test_a_give_back_narrows_every_live_command_and_the_next(self):
        s, usable = self.held(2)
        a = self.sleeper(s)
        b = self.sleeper(s, give_back=True)
        req = {"id": "x1", "cpu": [usable[1]], "by_pid": b.pid,
               "by_ctime": process.proc_ctime(b.pid), "at": "now"}
        with open(gb().request_path(self.store, s.id), "w", encoding="utf-8") as f:
            json.dump(req, f)
        self.assertIsNone(s.serve_give_back())
        keep = lay.cores_mask(self.layout(), [usable[0]])
        self.assertEqual((affinity(a), affinity(b)), (keep, keep))
        c = self.sleeper(s)
        self.assertEqual(affinity(c), keep)
        self.assertEqual(s.job.mask, keep)

    def test_the_window_monitor_is_not_started_off_windows(self):
        s, _usable = self.held()
        with mock.patch.object(kitlib.platform, "NAME", "linux"):
            self.sleeper(s)
        self.assertIsNone(s.watch)
        self.assertIsNone(s.windows)
        self.assertEqual(s.window_lines(), [])


if __name__ == "__main__":
    unittest.main()
