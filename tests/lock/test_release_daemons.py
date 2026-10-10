"""A gate run (book(gate=True), as kitlib.gate.runner books) that ends "done" stops the build
daemons it left in its job (kitlib.proc.daemons), with their children; any other leftover runs
on, unpinned. Any other run leaves them. machine_lock.daemons "stop" stops them after every run,
"leave" after none. A daemon serving a client outside the run's job (another gate building on
it) is left running (ReleaseLeavesASharedDaemon). The daemon here is a python child whose command
line names Gradle's daemon class; every process is the test's own."""
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


class ReleaseCase(StoreCase):
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


class ReleaseStopsDaemons(ReleaseCase):
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


# A fake Gradle daemon: listens on a loopback port 0 (written to argv[1]) and holds every
# connection it accepts, as a daemon serving a build does. It ends itself within half a second
# of the stop file argv[2] appearing, or after 120 s.
DAEMON = "\n".join([
    "import os, socket, sys, time",
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(8); s.settimeout(0.5)",
    "open(sys.argv[1] + '.tmp', 'w').write(str(s.getsockname()[1]))",
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])",
    "held, end = [], time.time() + 120",
    "while time.time() < end and not os.path.exists(sys.argv[2]):",
    "    try: held.append(s.accept()[0])",
    "    except socket.timeout: pass"])

# A build client: waits for the daemon's port, connects, says so in argv[2], and stays connected
# until the stop file argv[3] appears (or 120 s).
CLIENT = "\n".join([
    "import os, socket, sys, time",
    "end = time.time() + 30",
    "while not os.path.exists(sys.argv[1]) and time.time() < end: time.sleep(0.05)",
    "c = socket.create_connection(('127.0.0.1', int(open(sys.argv[1]).read())), timeout=10)",
    "open(sys.argv[2], 'w').close()",
    "end = time.time() + 120",
    "while time.time() < end and not os.path.exists(sys.argv[3]): time.sleep(0.2)"])

# The run's command: starts the daemon; "inside" also starts a client as its own child, "nested"
# starts one in a job of its own nested in the run's (kitlib.proc.job), "outside" leaves the
# client to the test. Each child's "<pid> <ctime>" is added to argv[8] as soon as it is started:
# this process holds the child's handle, so the pid is still that child's when its ctime is read,
# and the test's cleanup ends exactly those processes, never a later holder of a reused pid.
# Waits until a client is connected, writes "<daemon pid> <client pid or 0>" and exits, leaving
# both in the run's job.
SHARED = "\n".join([
    "import os, subprocess, sys, time",
    "port, connected, out, mode, root, daemon, client, ids, stop = sys.argv[1:10]",
    "sys.path.insert(0, root)",
    "from kitlib.proc import job, process",
    "def note(p):",
    "    with open(ids, 'a') as f: f.write('%d %d ' % (p.pid, process.proc_ctime(p.pid)))",
    "d = subprocess.Popen([sys.executable, '-c', daemon, port, stop,",
    "                      'org.gradle.launcher.daemon.bootstrap.GradleDaemon'])",
    "note(d)",
    "c = None",
    "if mode == 'inside':",
    "    c = subprocess.Popen([sys.executable, '-c', client, port, connected, stop])",
    "elif mode == 'nested':",
    "    c = job.Job(kill_on_close=False).start([sys.executable, '-c', client, port, connected,",
    "                                            stop])",
    "if c: note(c)",
    "end = time.time() + 30",
    "while not os.path.exists(connected) and time.time() < end: time.sleep(0.05)",
    "open(out, 'w').write('%d %d' % (d.pid, c.pid if c else 0))"])

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ReleaseLeavesASharedDaemon(ReleaseCase):
    """A Gradle daemon a gate's client started sits in that gate's job, yet any client on the
    machine may be building on it. The gate's release stops it only when every client it serves
    is in the gate's own job (a job nested in it counts as in). The daemon policy is set here,
    never read from the machine config."""

    def setUp(self):
        super().setUp()
        self.store.site = {"daemons": "gates"}
        self.ids = os.path.join(self.tmp, "ids.txt")
        self.stop = os.path.join(self.tmp, "stop")

    def recorded(self):
        """{pid: ctime} of each child the run's command recorded while it was alive."""
        try:
            with open(self.ids) as f:
                words = [int(x) for x in f.read().split()]
        except FileNotFoundError:
            return {}
        return dict(zip(words[0::2], words[1::2]))

    def end_recorded(self):
        """Cleanup, registered before the run: every child the run's command started ends
        itself on the stop file, and each recorded one is ended by its own (pid, ctime) whatever
        the run did or raised, so a pid reused since is never hit."""
        open(self.stop, "w").close()
        for pid, ct in self.recorded().items():
            process.terminate_verified(pid, ct)
            self.wait_gone(pid, ct, timeout=5)

    def run_shared(self, mode):
        port = os.path.join(self.tmp, "port.txt")
        connected = os.path.join(self.tmp, "connected")
        out = os.path.join(self.tmp, "shared.txt")
        self.addCleanup(self.end_recorded)
        code = rm.run_command([sys.executable, "-c", SHARED, port, connected, out, mode, ROOT,
                               DAEMON, CLIENT, self.ids, self.stop], "engine-test", "kit",
                              "daemon", cores=1, store=self.store, env=clean_env(), gate=True)
        with open(out) as f:
            pids = [int(x) for x in f.read().split()]
        self.cts = self.recorded()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(connected), "no client connected to the daemon")
        self.assertEqual(sorted(self.cts), sorted(p for p in pids if p))
        return pids

    def results(self):
        return [(d["pid"], d["what"], d["result"]) for d in self.ended().get("daemons", ())]

    def test_a_daemon_serving_a_client_in_another_job_is_left_running(self):
        from kitlib.proc import job
        other = job.Job()                   # another gate's job: not the run's, nor nested in it
        try:
            c = other.start([sys.executable, "-c", CLIENT, os.path.join(self.tmp, "port.txt"),
                             os.path.join(self.tmp, "connected"), self.stop])
        except BaseException:
            other.end(True)
            raise

        def end_client():                   # the test's own job: only its own client is in it
            other.end(True)
            c.wait(30)
        self.addCleanup(end_client)
        daemon, _ = self.run_shared("outside")
        self.assertTrue(process.alive(daemon, self.cts[daemon]),
                        "the daemon another job's client uses was stopped")
        why = "left: pid %d serves pid %d, which is outside the run's job" % (daemon, c.pid)
        self.assertEqual(self.results(), [(daemon, "gradle-daemon", why)])
        self.assertTrue(any("left the gradle-daemon (pid %d) running" % daemon in s
                            for s in self.said), self.said)

    def test_a_daemon_serving_only_the_runs_own_client_is_stopped(self):
        daemon, client = self.run_shared("inside")
        self.assertNotEqual(client, 0)
        self.assertEqual(self.results(), [(daemon, "gradle-daemon", "killed")])
        self.assertTrue(self.wait_gone(daemon, self.cts[daemon]),
                        "the gradle daemon outlived the run")

    def test_a_client_in_a_job_nested_in_the_runs_counts_as_inside(self):
        daemon, client = self.run_shared("nested")
        self.assertNotEqual(client, 0)
        self.assertEqual(self.results(), [(daemon, "gradle-daemon", "killed")])
        self.assertTrue(self.wait_gone(daemon, self.cts[daemon]),
                        "the gradle daemon outlived the run")


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
