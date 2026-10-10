"""A gate run (book(gate=True), as kitlib.gate.runner books) that ends "done" stops the build
daemons it left in its job (kitlib.proc.daemons), with their children; any other leftover runs
on, unpinned. Any other run leaves them. machine_lock.daemons "stop" stops them after every run,
"leave" after none. The daemon here is a python child whose command line names Gradle's daemon
class; every process is the test's own."""
import os
import sys
import time
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import run as rm
    from kitlib.proc import process

# Starts a "daemon" and a plain leftover that outlive it, writes their pids, then exits at once:
# the run ends "done" with both still in its job.
LEAVE = "\n".join([
    "import subprocess, sys",
    "sleep = 'import time; time.sleep(120)'",
    "d = subprocess.Popen([sys.executable, '-c', sleep,",
    "                      'org.gradle.launcher.daemon.bootstrap.GradleDaemon'])",
    "p = subprocess.Popen([sys.executable, '-c', sleep, 'plain-leftover'])",
    "open(sys.argv[1], 'w').write('%d %d' % (d.pid, p.pid))"])


class ReleaseStopsDaemons(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(1)

    def run_leaving(self, env=None, **kw):
        marker = os.path.join(self.tmp, "pids.txt")
        code = rm.run_command([sys.executable, "-c", LEAVE, marker], "engine-test", "kit",
                              "daemon", cores=1, store=self.store, env=env or clean_env(), **kw)
        self.assertEqual(code, 0)
        with open(marker) as f:
            pids = [int(x) for x in f.read().split()]
        return pids

    def own(self, pid):
        """(pid, ctime) of a live leftover, ended at cleanup if the run left it running."""
        ct = process.proc_ctime(pid)
        if ct is not None:
            self.addCleanup(process.terminate_verified, pid, ct)
        return ct

    def ended(self):
        names = os.listdir(self.store.path("ended"))
        self.assertEqual(len(names), 1)
        return self.store.read("ended/" + names[0])

    def wait_gone(self, pid, ct, timeout=10):
        end = time.time() + timeout
        while time.time() < end and process.alive(pid, ct):
            time.sleep(0.1)
        return not process.alive(pid, ct)

    def stopped(self, daemon, plain, rec):
        killed = [(d["pid"], d["what"], d["result"]) for d in rec.get("daemons", ())]
        self.assertEqual(killed, [(daemon, "gradle-daemon", "killed")])
        self.assertTrue(self.wait_gone(daemon, None), "the gradle daemon outlived the run")
        self.assertIsNotNone(self.own(plain), "a plain leftover was ended too")

    def test_a_done_gate_run_stops_the_daemon_and_leaves_the_rest(self):
        daemon, plain = self.run_leaving(gate=True)
        plain_ct = self.own(plain)
        rec = self.ended()
        killed = [(d["pid"], d["what"], d["result"]) for d in rec.get("daemons", ())]
        self.assertEqual(killed, [(daemon, "gradle-daemon", "killed")])
        self.assertTrue(self.wait_gone(daemon, None), "the gradle daemon outlived the run")
        self.assertIsNotNone(plain_ct, "a plain leftover was ended too")
        self.assertEqual(rec["how"], "done")
        self.assertTrue(any("stopped the gradle-daemon (pid %d)" % daemon in s for s in self.said),
                        self.said)

    def test_leave_keeps_it_running_unpinned(self):
        self.store.site = {"daemons": "leave"}
        daemon, plain = self.run_leaving()
        self.own(plain)
        self.assertIsNotNone(self.own(daemon), "the daemon was stopped under 'leave'")
        self.assertNotIn("daemons", self.ended())

    def test_a_run_that_is_not_a_gate_leaves_it_by_default(self):
        daemon, plain = self.run_leaving()
        self.own(plain)
        self.assertIsNotNone(self.own(daemon), "a run that is not a gate stopped the daemon")
        rec = self.ended()
        self.assertNotIn("daemons", rec)
        self.assertNotIn("gate", rec)
        self.assertFalse(any("stopped the" in s for s in self.said), self.said)

    def test_the_gate_is_in_the_record(self):
        self.run_leaving(gate=True)
        self.assertIs(self.ended()["gate"], True)

    def test_the_default_can_be_named(self):
        self.store.site = {"daemons": "gates"}
        daemon, plain = self.run_leaving()
        self.own(plain)
        self.assertIsNotNone(self.own(daemon), "'gates' stopped the daemon of a run not a gate")

    def test_stop_stops_it_after_any_run(self):
        self.store.site = {"daemons": "stop"}
        daemon, plain = self.run_leaving()
        self.stopped(daemon, plain, self.ended())

    def test_leave_keeps_it_after_a_gate_too(self):
        self.store.site = {"daemons": "leave"}
        daemon, plain = self.run_leaving(gate=True)
        self.own(plain)
        self.assertIsNotNone(self.own(daemon), "the daemon of a gate was stopped under 'leave'")
        self.assertNotIn("daemons", self.ended())

    def test_a_nested_gate_stops_the_daemons_in_its_own_job(self):
        """A gate booked inside a run that already holds what it needs (gate.py run under
        `machine_lock.py run`) has no record of its own; it still stops what its job left, since
        the run around it is not a gate and leaves them."""
        parent = {"id": "r-outer", "pid": os.getpid(), "ctime": 0, "kind": "engine-test",
                  "shares": {}, "has": {"cpu": 1}, "cores": [0], "gpus": []}
        env = clean_env(**{rm.NEST_ENV: "r-outer:%d:0" % os.getpid()})
        with mock.patch.object(rm, "nest_check", return_value=(parent, None)):
            daemon, plain = self.run_leaving(env=env, gate=True)
        self.own(plain)
        self.assertTrue(self.wait_gone(daemon, None), "a nested gate left the gradle daemon")
        self.assertTrue(any("stopped the gradle-daemon (pid %d)" % daemon in s for s in self.said),
                        self.said)
        self.assertFalse(os.listdir(self.store.path("ended")) if os.path.isdir(
            self.store.path("ended")) else [], "a nested slot wrote a record")


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class CommandLine(unittest.TestCase):
    def test_reads_a_live_process_and_checks_its_ctime(self):
        cl = process.command_line(os.getpid())
        self.assertIn("python", (cl or "").lower())
        own = process.own_ctime()
        self.assertEqual(process.command_line(os.getpid(), own), cl)
        self.assertIsNone(process.command_line(os.getpid(), own + 1))


if __name__ == "__main__":
    unittest.main()
