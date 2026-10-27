"""kitlib.lock.jobset: where a job starts one command (Linux), a held run's job is a JobSet, a job
of its own per command behind the one Job interface. Unit tests on fake jobs, on any platform:
which job each start uses, the reads over the jobs (pids, contains, usage), the closing of a
finished job, the pin fanned out with its undo, and the ends. Through a Slot:
tests/lock/test_slot_jobset.py."""
import unittest
from unittest import mock

import kitlib.platform
from kitlib.lock import jobset as js
from kitlib.proc.job import JobError


class FakePopen:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    def poll(self):
        return self.returncode

    def exit(self, code=0):
        self.returncode = code


class FakeJob:
    """A one-command job, as a Linux one: a second start raises JobError. `procs` is what it
    holds; `cpu`, `peak`, `mem` what usage says once started."""
    pids_next = [100]

    def __init__(self, mask=None):
        self.mask = self.effective = mask
        self.pin = "pinned" if mask is not None else None
        self.note, self.p, self.closed, self.started = None, None, False, False
        self.procs, self.cpu, self.peak, self.mem = [], 0.0, 0, 0
        self.refuse, self.ended, self.moved, self.move_error = False, None, [], None
        self.fail_start = None

    def start(self, cmd, before_resume=None, **kw):
        if self.started or self.closed:
            raise JobError("a job on Linux starts one command")
        self.started = True
        if self.fail_start:
            raise self.fail_start
        FakeJob.pids_next[0] += 1
        self.p = FakePopen(FakeJob.pids_next[0])
        self.procs = [self.p.pid]
        self.cmd, self.kw = cmd, kw
        return self.p

    def pids(self):
        return [] if self.closed else list(self.procs)

    def contains(self, pid):
        return "in" if pid in self.pids() else "out"

    def usage(self):
        if self.closed or not self.started:
            return None
        return (self.cpu, self.peak, self.mem)

    def narrow(self, mask):
        if self.closed or self.refuse or self.mask is None or mask & ~self.mask:
            return False
        self.mask = self.effective = mask
        return True

    def repin(self, mask):
        if self.closed or self.refuse:
            return False
        self.mask = self.effective = mask
        return True

    def unpin(self):
        self.mask = self.effective = self.pin = None

    def terminate(self, code=1):
        self.procs = []
        return True

    def leftovers(self, pids=None):
        if self.move_error:
            raise JobError(self.move_error)
        got = list(self.procs if pids is None else pids)
        self.moved += got
        self.procs = [p for p in self.procs if p not in got]
        return got

    def end(self, kill):
        self.ended = kill
        self.closed = True

    def close(self):
        self.closed = True


def jobset(mask=0b1111):
    made = []

    def make(m):
        made.append(FakeJob(m))
        return made[-1]
    return js.JobSet(mask, make=make), made


class Starts(unittest.TestCase):
    def test_the_first_job_is_made_at_once_and_gives_the_pin(self):
        s, made = jobset(0b11)
        self.assertEqual(len(made), 1)
        self.assertEqual((s.mask, s.pin, s.effective, s.note), (0b11, "pinned", 0b11, None))

    def test_each_command_starts_in_a_job_of_its_own(self):
        s, made = jobset()
        p1 = s.start(["a"], cwd="x")
        p2 = s.start(["b"])
        self.assertEqual(len(made), 2)
        self.assertIs(made[0].p, p1)
        self.assertIs(made[1].p, p2)
        self.assertIs(s.p, p2)
        self.assertEqual(made[0].kw, {"cwd": "x"})

    def test_a_start_that_failed_leaves_its_job_and_the_next_gets_a_new_one(self):
        s, made = jobset()
        made[0].fail_start = OSError("not found")
        with self.assertRaises(OSError):
            s.start(["a"])
        s.start(["b"])
        self.assertEqual(len(made), 2)
        self.assertTrue(made[1].started)
        self.assertTrue(made[0].closed, "the failed job holds nothing: closed at the next start")

    def test_a_job_that_cannot_be_made_raises_job_error_from_start(self):
        made = []

        def make(m):
            if made:
                raise JobError("no scope")
            made.append(FakeJob(m))
            return made[-1]
        s = js.JobSet(None, make=make)
        s.start(["a"])
        with self.assertRaises(JobError):
            s.start(["b"])

    def test_after_the_end_a_start_is_refused(self):
        s, _made = jobset()
        s.end(kill=True)
        with self.assertRaises(JobError):
            s.start(["a"])


class Reads(unittest.TestCase):
    def test_pids_are_every_open_job_s(self):
        s, made = jobset()
        s.start(["a"])
        s.start(["b"])
        made[0].procs.append(7)
        self.assertEqual(sorted(s.pids()), sorted([made[0].p.pid, 7, made[1].p.pid]))

    def test_contains_is_in_when_any_job_holds_the_pid(self):
        s, made = jobset()
        s.start(["a"])
        s.start(["b"])
        self.assertEqual(s.contains(made[1].p.pid), "in")
        self.assertEqual(s.contains(99999), "out")
        made[0].contains = lambda pid: "unknown"
        self.assertEqual(s.contains(99999), "unknown")
        self.assertEqual(s.contains(made[1].p.pid), "in")

    def test_contains_with_no_job_open_is_unknown(self):
        s, _made = jobset()
        s.close()
        self.assertEqual(s.contains(1), "unknown")

    def test_usage_is_none_until_a_job_gives_numbers(self):
        s, _made = jobset()
        self.assertIsNone(s.usage())

    def test_usage_sums_cpu_over_every_job_and_memory_over_the_open_ones(self):
        s, made = jobset()
        s.start(["a"])
        made[0].cpu, made[0].peak, made[0].mem = 2.0, 100, 10
        s.start(["b"])
        made[1].cpu, made[1].peak, made[1].mem = 3.0, 50, 5
        self.assertEqual(s.usage(), (5.0, 150, 15))

    def test_a_finished_job_is_closed_and_its_cpu_kept(self):
        s, made = jobset()
        s.start(["a"])
        made[0].cpu, made[0].peak, made[0].mem = 2.0, 100, 10
        made[0].p.exit(0)
        made[0].procs = []
        s.start(["b"])
        made[1].cpu, made[1].peak, made[1].mem = 3.0, 50, 5
        self.assertTrue(made[0].closed)
        self.assertEqual(s.jobs, [made[1]])
        self.assertEqual(s.usage(), (5.0, 50, 5))
        made[1].p.exit(0)
        made[1].procs = []
        self.assertEqual(s.usage(), (5.0, 0, 0), "every job closed: the cpu stays")
        self.assertTrue(made[1].closed)

    def test_a_job_whose_command_exited_but_left_a_process_stays_open(self):
        s, made = jobset()
        s.start(["a"])
        made[0].p.exit(0)
        made[0].procs = [4242]
        s.start(["b"])
        self.assertFalse(made[0].closed)
        self.assertIn(4242, s.pids())

    def test_a_running_command_s_job_is_never_closed(self):
        s, made = jobset()
        s.start(["a"])
        made[0].procs = []
        s.usage()
        self.assertFalse(made[0].closed)

    def test_the_newest_job_never_started_is_not_closed_by_a_read(self):
        s, made = jobset()
        s.usage()
        self.assertFalse(made[0].closed)
        self.assertEqual(s.pids(), [])


class Pin(unittest.TestCase):
    def test_narrow_goes_to_every_open_job_and_to_the_next(self):
        s, made = jobset(0b1111)
        s.start(["a"])
        s.start(["b"])
        self.assertTrue(s.narrow(0b0011))
        self.assertEqual([j.mask for j in made], [0b0011, 0b0011])
        s.start(["c"])
        self.assertEqual(made[2].mask, 0b0011)
        self.assertEqual(s.mask, 0b0011)

    def test_narrow_refused_by_one_job_puts_the_others_back(self):
        s, made = jobset(0b1111)
        s.start(["a"])
        s.start(["b"])
        made[1].refuse = True
        self.assertFalse(s.narrow(0b0011))
        self.assertEqual([j.mask for j in made], [0b1111, 0b1111])
        s.start(["c"])
        self.assertEqual(made[2].mask, 0b1111)

    def test_repin_undoes_a_narrow_on_every_job(self):
        s, made = jobset(0b1111)
        s.start(["a"])
        s.start(["b"])
        self.assertTrue(s.narrow(0b0001))
        self.assertTrue(s.repin(0b1111))
        self.assertEqual([j.mask for j in made], [0b1111, 0b1111])

    def test_narrow_before_any_command_sets_the_first_job_s_mask(self):
        s, made = jobset(0b1111)
        self.assertTrue(s.narrow(0b0100))
        self.assertEqual(made[0].mask, 0b0100)

    def test_unpin_frees_every_job_and_the_next(self):
        s, made = jobset(0b1111)
        s.start(["a"])
        s.unpin()
        s.start(["b"])
        self.assertEqual([j.mask for j in made], [None, None])

    def test_no_job_open_refuses_a_narrow(self):
        s, _made = jobset(0b1111)
        s.close()
        self.assertFalse(s.narrow(0b1))


class Ends(unittest.TestCase):
    def test_terminate_and_end_reach_every_job(self):
        s, made = jobset()
        s.start(["a"])
        s.start(["b"])
        self.assertTrue(s.terminate(1))
        self.assertEqual(s.pids(), [])
        s.end(kill=False)
        self.assertEqual([j.ended for j in made], [False, False])
        self.assertTrue(s.closed)
        self.assertFalse(js.is_open(s))

    def test_leftovers_asks_every_job_and_names_each_that_could_not(self):
        s, made = jobset()
        s.start(["a"])
        s.start(["b"])
        s.start(["c"])
        made[0].move_error = "pid 5 is still in the job"
        with self.assertRaises(JobError) as cm:
            s.leftovers()
        self.assertIn("pid 5", str(cm.exception))
        self.assertEqual(made[1].moved, [made[1].p.pid])
        self.assertEqual(made[2].moved, [made[2].p.pid])

    def test_leftovers_of_named_pids_goes_to_the_job_that_holds_each(self):
        s, made = jobset()
        s.start(["a"])
        s.start(["b"])
        got = s.leftovers([made[1].p.pid])
        self.assertEqual(got, [made[1].p.pid])
        self.assertEqual((made[0].moved, made[1].moved), ([], [made[1].p.pid]))

    def test_a_job_without_leftovers_moves_nothing(self):
        class NoMove(FakeJob):
            leftovers = None
        s = js.JobSet(None, make=NoMove)
        s.start(["a"])
        self.assertEqual(s.leftovers(), [])


class Helpers(unittest.TestCase):
    def test_one_command_is_linux_s(self):
        with mock.patch.object(kitlib.platform, "NAME", "linux"):
            self.assertTrue(js.one_command())
        with mock.patch.object(kitlib.platform, "NAME", "windows"):
            self.assertFalse(js.one_command())

    def test_is_open_reads_a_handle_or_closed(self):
        class Win:
            h = 5
        w = Win()
        self.assertTrue(js.is_open(w))
        w.h = None
        self.assertFalse(js.is_open(w))
        lin = FakeJob()
        self.assertTrue(js.is_open(lin))
        lin.closed = True
        self.assertFalse(js.is_open(lin))
        self.assertFalse(js.is_open(None))


if __name__ == "__main__":
    unittest.main()
