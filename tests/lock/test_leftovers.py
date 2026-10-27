"""kitlib.lock.run move_leftovers: before a run's job is closed without a kill (a run that ended
on its own, a kill that spared a build daemon, a background child that exited), what still runs
in it is moved out of it where the job can do that (a Linux job's leftovers); a move that fails
is said and never fails the release. Here on Windows: a job object with a leftovers stand-in
(a Windows job object has none, and moves nothing)."""
import importlib
import os
import sys
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.lock import background as bg
    from kitlib.lock import jobset
    from kitlib.lock import run as rm
    from kitlib.proc.job import Job, JobError
    from .test_giveback import usable_slots

EXIT0 = [sys.executable, "-c", "import sys; sys.exit(0)"]
SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


class Fake:
    """A job: open until `closed`; unpin logs or raises `unpin_error`; leftovers moves `procs`
    or raises `error`. `log` is the order of the calls."""

    def __init__(self, procs=(), error=None, has=True, unpin_error=None):
        self.procs, self.error, self.closed, self.calls = list(procs), error, False, 0
        self.unpin_error, self.log = unpin_error, []
        if not has:
            self.leftovers = None

    def unpin(self):
        self.log.append("unpin")
        if self.unpin_error:
            raise self.unpin_error

    def leftovers(self, pids=None):
        self.calls += 1
        self.log.append("leftovers")
        if self.error:
            raise self.error
        return list(self.procs)


@windows_only("the lock is Windows only")
class Unit(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.store = mock.Mock(say=self.said.append)

    def test_what_is_moved_is_said_and_returned(self):
        self.assertEqual(rm.move_leftovers(self.store, "r1", Fake([5, 6])), [5, 6])
        self.assertEqual(self.said, ["run r1: left pid 5, pid 6 running, moved out of its job"])

    def test_nothing_left_says_nothing(self):
        self.assertEqual(rm.move_leftovers(self.store, "r1", Fake()), [])
        self.assertEqual(self.said, [])

    def test_the_job_is_unpinned_before_what_runs_on_is_moved(self):
        f = Fake([5])
        self.assertEqual(rm.move_leftovers(self.store, "r1", f), [5])
        self.assertEqual(f.log, ["unpin", "leftovers"])

    def test_an_unpin_that_fails_is_said_and_the_move_goes_on(self):
        f = Fake([5], unpin_error=OSError("affinity refused"))
        self.assertEqual(rm.move_leftovers(self.store, "r1", f), [5])
        self.assertEqual(f.log, ["unpin", "leftovers"])
        self.assertTrue(self.said[0].startswith("WARNING: run r1: its job could not be unpinned "
                                                "before what it left was moved out: "),
                        self.said)
        self.assertIn("affinity refused", self.said[0])
        self.assertEqual(self.said[1:], ["run r1: left pid 5 running, moved out of its job"])

    def test_a_job_that_cannot_move_moves_none(self):
        f = Fake([5], has=False)
        self.assertEqual(rm.move_leftovers(self.store, "r1", f), [])
        self.assertEqual(f.log, [], "a job that cannot move is not unpinned here either")
        self.assertEqual(rm.move_leftovers(self.store, "r1", None), [])
        self.assertEqual(self.said, [])

    def test_a_closed_job_is_not_asked(self):
        f = Fake([5])
        f.closed = True
        self.assertEqual(rm.move_leftovers(self.store, "r1", f), [])
        self.assertEqual(f.calls, 0)
        self.assertEqual(f.log, [])

    def test_a_failed_move_is_said_and_goes_no_further(self):
        for err in (JobError("pid 5 is still in the job"), OSError("busctl failed")):
            self.said.clear()
            self.assertEqual(rm.move_leftovers(self.store, "r1", Fake([5], error=err)), [])
            self.assertEqual(len(self.said), 1)
            self.assertTrue(self.said[0].startswith("WARNING: run r1: what it left could not be "
                                                    "moved out of its job: "), self.said[0])
            self.assertIn(str(err), self.said[0])

    def test_a_say_that_fails_is_dropped(self):
        self.store.say = mock.Mock(side_effect=OSError("stderr gone"))
        self.assertEqual(rm.move_leftovers(self.store, "r1", Fake([5])), [5])
        self.assertEqual(rm.move_leftovers(self.store, "r1", Fake(error=JobError("x"))), [])


ALL = list(range(8))                        # the faked machine's cpus


@windows_only("the lock is Windows only")
class AffinityTiers(unittest.TestCase):
    """The release's own sequence (move_leftovers, then end(kill=False)) on a Linux S2 and W2
    job built without a process: there the pin is each process's affinity, and every moved pid
    must get every cpu back. Reaching them is the point: the close's unpin covers only the
    processes still in the job, and the move takes them all out."""

    def setUp(self):
        self.c = importlib.import_module("kitlib.platform.linux.contain")
        self.set = []                       # (pid, cpus) of each affinity write
        for x in (mock.patch.object(self.c.lcpu, "set_threads",
                                    side_effect=lambda pid, ids: self.set.append(
                                        (pid, list(ids))) or "pinned"),
                  mock.patch.object(self.c.hardware, "threads", return_value=ALL)):
            x.start()
            self.addCleanup(x.stop)
        self.store = mock.Mock(say=lambda t: None)

    def bare(self, cls, **attrs):
        j = object.__new__(cls)
        j.__dict__.update(dict(closed=False, kill_on_close=True, _k=None, _g=None,
                               _child=object(), wd=None, p=None, mask=0b11, pin="pinned",
                               effective=0b11, note=None), **attrs)
        return j

    def freed(self):
        return {pid for pid, ids in self.set if ids == ALL}

    def test_w2_moved_processes_get_every_cpu_back(self):
        j = self.bare(self.c.WeakJob, spared=set(), token="wtest", _ctime=1)
        j.members = lambda: {p: 1 for p in (5, 6) if p not in j.spared}
        self.assertEqual(rm.move_leftovers(self.store, "r1", j), [5, 6])
        j.end(kill=False)
        self.assertEqual(j.spared, {5, 6})
        self.assertEqual(self.freed(), {5, 6})

    def test_s2_moved_processes_get_every_cpu_back(self):
        held = {5, 6}                       # the pids in J; the busctl move takes them out
        j = self.bare(self.c.ScopeJob, cpuset=False, parent=None, name="pmkit-job-1-1-ab.scope",
                      cg="/app.slice/pmkit-job-1-1-ab.scope")
        j.pids = lambda: sorted(held)
        j.contains = lambda pid: "in" if pid in held else "out"
        with mock.patch.object(self.c, "_call",
                               side_effect=lambda argv: held.clear() or (True, "")):
            self.assertEqual(rm.move_leftovers(self.store, "r1", j), [5, 6])
            j.end(kill=False)
        self.assertEqual(self.freed(), {5, 6})


def moving_job(log, error=None):
    """A kitlib.proc.job.Job class whose jobs log ("unpin", id), ("leftovers", id) and
    ("end", id, kill)."""
    class Moving(Job):
        def unpin(self):
            log.append(("unpin", id(self)))
            return Job.unpin(self)

        def leftovers(self, pids=None):
            log.append(("leftovers", id(self)))
            if error:
                raise error
            return []

        def end(self, kill):
            log.append(("end", id(self), kill))
            return Job.end(self, kill)
    return Moving


@windows_only("the lock is Windows only")
class Release(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.log = []

    def book(self, error=None, one_command=False):
        usable_slots(self, 1)
        for x in (mock.patch.object(rm, "Job", moving_job(self.log, error)),
                  mock.patch.object(jobset, "one_command", return_value=one_command)):
            x.start()
            self.addCleanup(x.stop)
        s = rm.book("engine-test", "kit", "t", cores=1, store=self.store, env=clean_env())
        self.addCleanup(s.release)
        return s

    def sleeper(self, s):
        p = s.start(SLEEP)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        return p

    def ends(self):
        return [e for e in self.log if e[0] == "end"]

    def assert_unpinned_before_moved(self, msg=None):
        """Each job moved was unpinned first (a moved process keeps its affinity on S2 and W2;
        the close's own unpin no longer reaches it)."""
        for e in self.log:
            if e[0] == "leftovers":
                before = self.log[:self.log.index(e)]
                self.assertIn(("unpin", e[1]), before, msg)

    def test_a_run_that_ends_on_its_own_moves_what_runs_on_before_the_close(self):
        for one in (False, True):
            self.log.clear()
            s = self.book(one_command=one)
            p = self.sleeper(s)
            self.assertEqual(s.release(how="done")["how"], "done")
            self.assertIsNone(p.poll(), "what a run left runs on")
            kinds = [e[0] for e in self.log]
            self.assertIn("leftovers", kinds, one)
            self.assertLess(kinds.index("leftovers"), kinds.index("end"), one)
            self.assert_unpinned_before_moved(one)
            self.assertEqual({e[2] for e in self.ends()}, {False}, one)
            p.kill()
            p.wait()

    def test_a_finished_command_s_job_is_closed_with_nothing_to_move(self):
        s = self.book(one_command=True)
        self.assertEqual(s.launcher.run(EXIT0), 0)
        self.assertEqual(s.release()["how"], "done")
        self.assertEqual(self.log, [], "reaped, its job closed: nothing ran on to move or end")

    def test_a_kill_that_spares_nothing_moves_nothing(self):
        s = self.book()
        p = self.sleeper(s)
        self.assertEqual(s.release(how="cancelled")["how"], "cancelled")
        p.wait(10)
        self.assertNotIn("leftovers", [e[0] for e in self.log])
        self.assertIn(True, [e[2] for e in self.ends()])

    def test_a_kill_that_spared_a_daemon_moves_what_runs_on(self):
        s = self.book()
        p = self.sleeper(s)
        with mock.patch.object(rm, "end_sparing", return_value=True):
            s.end_tree()
        self.assertEqual(s.release(how="cancelled")["how"], "cancelled")
        kinds = [e[0] for e in self.log]
        self.assertLess(kinds.index("leftovers"), kinds.index("end"))
        self.assert_unpinned_before_moved()
        self.assertEqual([e[2] for e in self.ends()], [False])
        p.kill()
        p.wait()

    def test_a_failed_move_is_said_and_the_run_still_settles(self):
        s = self.book(error=JobError("pid 7 is still in the job"))
        p = self.sleeper(s)
        self.assertEqual(s.release(how="done")["how"], "done")
        self.assertTrue(any(t.startswith("WARNING: run %s: what it left could not be moved"
                                         % s.id) and "pid 7" in t for t in self.said), self.said)
        p.kill()
        p.wait()


@windows_only("the lock is Windows only")
class Background(StoreCase):
    def test_a_child_that_exits_has_its_leftovers_moved_before_the_close(self):
        self.store.say = lambda t: None
        self.confine(self.layout()["medium_cores"])
        log = []
        with mock.patch.object(bg, "Job", moving_job(log)):
            self.assertEqual(bg.run_background(EXIT0, "kit", "bg", store=self.store,
                                               env=clean_env()), 0)
        kinds = [e[0] for e in log]
        self.assertIn("leftovers", kinds)
        self.assertLess(kinds.index("unpin"), kinds.index("leftovers"))
        self.assertLess(kinds.index("leftovers"), kinds.index("end"))
        self.assertEqual(log[kinds.index("end")][2], False)

    def test_end_child_moves_a_spared_tree_and_not_a_killed_one(self):
        log = []
        run = mock.Mock(id="r1", rec={})
        # The spared tree is unpinned before it is moved (the close unpins again: on Windows
        # end(kill=False) is the unpin).
        for spared, moved in ((True, ["unpin", "leftovers", "end", "unpin"]), (False, ["end"])):
            log.clear()
            job = moving_job(log)(None)
            with mock.patch.object(bg, "end_sparing", return_value=spared):
                bg.end_child(job, None, self.store, run)
            self.assertEqual([e[0] for e in log], moved, spared)
            self.assertEqual([e[2] for e in log if e[0] == "end"], [not spared])


if __name__ == "__main__":
    unittest.main()
