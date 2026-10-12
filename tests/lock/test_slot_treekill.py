"""kitlib.lock.run.slot(): a run that is stopped, raises, is interrupted (Ctrl+C) or is killed
through its launcher ends its command's whole process tree - child and grandchild, and a subtree
whose top has already exited - because every process the launcher starts is in the run's job
(kitlib.proc.job) and the slot's kill path (Slot.end_tree) ends the job. These trees hold no
build daemon, so end_tree ends the whole job (TerminateJobObject); a job with a shared daemon in
it has that daemon's tree spared and the rest ended process by process, which
tests/lock/test_release_daemons.py covers (docs/gate.md "Build daemons"). This is why the gate has
no `kill_retry` step key (docs/gate.md "Not ported").

Real processes, nothing mocked: a three-level python tree on a scratch store
(helpers.StoreCase). Each descendant is checked by a handle the test opens while it is alive and
holds to the end: Windows never gives the pid of a process to another while a handle to it is
open, so a signalled handle means exactly that process ended. At opening, each pid is checked to
be in the slot's job (Launcher.pids) and to be its level's parent's child. The control test kills
only the top process of the same tree and sees the rest live on, so "every descendant gone" is
not something killing the top alone gives."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import run as rm
    from kitlib.proc import process, win32

# Level 0 starts level 1, which starts level 2; each writes <level>.pid (whole, by rename) and
# sleeps. "orphan": level 0 exits once level 2 is up, leaving levels 1 and 2 without a live parent.
TREE = r'''
import os, subprocess, sys, time
d, level, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3]
tmp = os.path.join(d, "%d.tmp" % level)
with open(tmp, "w") as f:
    f.write(str(os.getpid()))
os.replace(tmp, os.path.join(d, "%d.pid" % level))
if level < 2:
    subprocess.Popen([sys.executable, "-I", __file__, d, str(level + 1), mode])
if level == 0 and mode == "orphan":
    end = time.time() + 30
    while not os.path.exists(os.path.join(d, "2.pid")) and time.time() < end:
        time.sleep(0.02)
    sys.exit(0)
time.sleep(60)
'''
LEVELS = (0, 1, 2)
GONE_MS = 10000
PID_READ_S = 5


def read_pid(path, timeout=PID_READ_S):
    """The pid in the pid file `path`, waited for up to `timeout` seconds; past that it fails,
    naming the file and the last error. A pid file whose name is there may still refuse to open:
    one read at once failed with PermissionError (errno 13), seen once. Inferred cause: the
    handle os.replace renamed the file through is still open for an instant after the name
    appears, and open() does not share a file with a handle that may delete it."""
    end = time.monotonic() + timeout
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                return int(f.read())
        except (OSError, ValueError) as e:
            last = e
        if time.monotonic() >= end:
            raise AssertionError("pid file %s was not readable as a pid within %g s: %r"
                                 % (path, timeout, last))
        time.sleep(0.02)


class SlotTreeKill(StoreCase):
    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.tmp, "tree.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(TREE)

    def tree(self, mode="stay"):
        """(argv, folder) of one tree whose pid files go to a fresh folder."""
        d = tempfile.mkdtemp(dir=self.tmp)
        return [sys.executable, "-I", self.script, d, "0", mode], d

    def reap(self, p):
        if p.poll() is None:
            p.kill()
        p.wait(30)

    def pids(self, d, hold=LEVELS, timeout=30, read_s=PID_READ_S):
        """({level: pid}, {level: handle}) once every level has written its pid file. A file
        that is there gets `read_s` seconds to open and hold a pid (read_pid). Each level
        in `hold` is opened the moment its pid is read (it sleeps 60 s, so the pid is its own)
        and its handle registered for cleanup at once, before anything is asserted: a later
        failure - this wait timing out, a check in hold() - leaves no held level running."""
        out, handles, end = {}, {}, time.monotonic() + timeout
        while len(out) < len(LEVELS) and time.monotonic() < end:
            for lv in LEVELS:
                path = os.path.join(d, "%d.pid" % lv)
                if lv not in out and os.path.exists(path):
                    out[lv] = read_pid(path, read_s)
                    if lv in hold:
                        h = process.open_process(out[lv], process.SYNCHRONIZE
                                                 | process.PROCESS_TERMINATE
                                                 | process.PROCESS_QUERY_LIMITED_INFORMATION)
                        if h:
                            self.addCleanup(self.drop, h)
                            handles[lv] = h
            time.sleep(0.02)
        self.assertEqual(sorted(out), list(LEVELS), "the tree never came up")
        for lv in hold:
            self.assertIn(lv, handles, "level %d (pid %d) could not be opened" % (lv, out[lv]))
        return out, handles

    def drop(self, h):
        if win32.K32.WaitForSingleObject(h, 0) == win32.WAIT_TIMEOUT:
            win32.K32.TerminateProcess(h, 1)
        process.close(h)

    def hold(self, got, in_job=None):
        """The handles of pids(), each checked: alive, its parent level's child and, given in_job
        (the launcher's pids), in the slot's job. Every handle is already held for cleanup."""
        pids, handles = got
        parents = process.process_parents()
        for lv, h in sorted(handles.items()):
            pid = pids[lv]
            self.assertEqual(win32.K32.WaitForSingleObject(h, 0), win32.WAIT_TIMEOUT,
                             "level %d had ended before it was held" % lv)
            if lv:
                self.assertEqual(parents.get(pid), pids[lv - 1],
                                 "pid %d is not level %d's child" % (pid, lv - 1))
            if in_job is not None:
                self.assertIn(pid, in_job, "level %d is not in the slot's job" % lv)
        return handles

    def assert_gone(self, handles):
        for lv, h in sorted(handles.items()):
            self.assertEqual(win32.K32.WaitForSingleObject(h, GONE_MS), win32.WAIT_OBJECT_0,
                             "level %d outlived the slot's kill" % lv)

    def ended(self, rid):
        e = self.store.read("ended/%s.json" % rid)
        return e["how"], e["code"]

    def slot(self):
        self.confine(1)
        return rm.slot("engine-test", "kit", "t", cores=1, store=self.store, env=clean_env())

    def test_a_stop_ends_the_whole_tree(self):
        argv, d = self.tree()
        with self.slot() as launch:
            rid = launch.id
            p = launch.start(argv)
            self.addCleanup(self.reap, p)
            handles = self.hold(self.pids(d), launch.pids())
            self.assertTrue(rm.request_stop(self.store, rid, "test"))
            self.assertEqual(launch.wait(p), 1)
            self.assert_gone(handles)
        self.assertEqual(self.ended(rid), ("cancelled", 1))

    def test_an_error_in_the_block_ends_the_whole_tree(self):
        argv, d = self.tree()
        with self.assertRaises(RuntimeError):
            with self.slot() as launch:
                rid = launch.id
                p = launch.start(argv)
                self.addCleanup(self.reap, p)
                handles = self.hold(self.pids(d), launch.pids())
                raise RuntimeError("boom")
        self.assert_gone(handles)
        self.assertEqual(self.ended(rid), ("error", 1))

    def test_ctrl_c_ends_a_tree_whose_top_already_exited(self):
        # Levels 1 and 2 have no live parent: a walk down from the command's pid cannot find
        # them; the job still holds them.
        argv, d = self.tree("orphan")
        with self.assertRaises(KeyboardInterrupt):
            with self.slot() as launch:
                rid = launch.id
                p = launch.start(argv)
                self.addCleanup(self.reap, p)
                # Level 0 exits on its own, so it is never opened (its pid may be reused).
                got = self.pids(d, hold=(1, 2))
                self.assertEqual(p.wait(30), 0)
                handles = self.hold(got, launch.pids())
                raise KeyboardInterrupt
        self.assert_gone(handles)
        self.assertEqual(self.ended(rid), ("interrupted", 130))

    def test_the_launchers_kill_ends_the_whole_tree(self):
        argv, d = self.tree()
        with self.slot() as launch:
            p = launch.start(argv)
            self.addCleanup(self.reap, p)
            handles = self.hold(self.pids(d), launch.pids())
            launch.kill()
            self.assertEqual(launch.wait(p), 1)
            self.assert_gone(handles)

    def test_control_killing_only_the_top_leaves_its_descendants(self):
        # The same tree outside any slot: ending its top process alone ends nothing below it.
        argv, d = self.tree()
        p = subprocess.Popen(argv, env=clean_env())
        self.addCleanup(self.reap, p)
        handles = self.hold(self.pids(d))
        p.kill()
        p.wait(30)
        self.assertEqual(win32.K32.WaitForSingleObject(handles[0], GONE_MS), win32.WAIT_OBJECT_0)
        time.sleep(0.5)
        for lv in (1, 2):
            self.assertEqual(win32.K32.WaitForSingleObject(handles[lv], 0), win32.WAIT_TIMEOUT,
                             "level %d ended with its parent alone" % lv)

    def pid_files(self, late):
        """A folder holding every level's pid file but `late`, whose name is a folder: there,
        and open() on it fails with PermissionError, as a file not yet shared does."""
        d = tempfile.mkdtemp(dir=self.tmp)
        for lv in LEVELS:
            if lv == late:
                os.mkdir(os.path.join(d, "%d.pid" % lv))
            else:
                with open(os.path.join(d, "%d.pid" % lv), "w", encoding="utf-8") as f:
                    f.write(str(100 + lv))
        return d

    def test_pids_waits_out_a_pid_file_that_cannot_be_opened_yet(self):
        # Red on the read at once: PermissionError from open(), the failure seen.
        d = self.pid_files(late=2)
        path = os.path.join(d, "2.pid")
        with self.assertRaises(PermissionError):
            open(path, encoding="utf-8")

        def write():
            os.rmdir(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write("102")
        t = threading.Timer(0.3, write)
        self.addCleanup(t.join)
        t.start()
        self.assertEqual(self.pids(d, hold=()), ({0: 100, 1: 101, 2: 102}, {}))

    def test_pids_fails_naming_a_pid_file_that_never_opens(self):
        d = self.pid_files(late=1)
        path = os.path.join(d, "1.pid")
        t0 = time.monotonic()
        with self.assertRaises(AssertionError) as caught:
            self.pids(d, hold=(), read_s=0.5)
        self.assertLess(time.monotonic() - t0, 5)
        said = str(caught.exception)
        self.assertIn("pid file %s was not readable as a pid within 0.5 s" % path, said)
        self.assertIn("PermissionError", said)

    def test_pids_waits_out_a_pid_file_with_no_pid_in_it_yet(self):
        d = self.pid_files(late=None)
        path = os.path.join(d, "0.pid")
        with open(path, "w", encoding="utf-8"):
            pass

        def write():
            with open(path, "w", encoding="utf-8") as f:
                f.write("100")
        t = threading.Timer(0.3, write)
        self.addCleanup(t.join)
        t.start()
        self.assertEqual(self.pids(d, hold=())[0], {0: 100, 1: 101, 2: 102})


if __name__ == "__main__":
    unittest.main()
