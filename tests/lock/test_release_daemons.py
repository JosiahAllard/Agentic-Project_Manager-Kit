"""A gate run (book(gate=True), as kitlib.gate.runner books) that ends "done" stops the build
daemons it left in its job (kitlib.proc.daemons), with their children; any other leftover runs
on, unpinned. Any other run leaves them. machine_lock.daemons "stop" stops them after every run,
"leave" after none. A daemon serving a client outside the run's job (another gate building on
it) is left running (ReleaseLeavesASharedDaemon), on the kill path too (a gate that errored or
was cancelled, a launcher's kill: its other processes are ended, the shared daemon's tree is
not). The daemon here is a python child whose command line names Gradle's daemon class; every
process is the test's own and is ended, and waited for, at its cleanup."""
import contextlib
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import run as rm
    from kitlib.proc import process

# Starts a "daemon" and a plain leftover that outlive it, writes "<pid> <ctime>" of each (read
# while it holds their handles, so each pid is still that child's; argv[2] is the kit's root),
# then exits at once: the run ends "done" with both still in its job.
LEAVE = "\n".join([
    "import subprocess, sys",
    "sys.path.insert(0, sys.argv[2])",
    "from kitlib.proc import process",
    "sleep = 'import time; time.sleep(120)'",
    "d = subprocess.Popen([sys.executable, '-c', sleep,",
    "                      'org.gradle.launcher.daemon.bootstrap.GradleDaemon'])",
    "p = subprocess.Popen([sys.executable, '-c', sleep, 'plain-leftover'])",
    "open(sys.argv[1], 'w').write(' '.join('%d %d' % (c.pid, process.proc_ctime(c.pid))",
    "                                      for c in (d, p)))"])


class ReleaseCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(1)

    def run_leaving(self, env=None, **kw):
        """[daemon pid, plain pid] LEAVE left. Each is ended at cleanup by the (pid, ctime) LEAVE
        wrote, and waited for, whatever the test asserts: nothing it started outlives it."""
        marker = os.path.join(self.tmp, "pids.txt")
        code = rm.run_command([sys.executable, "-c", LEAVE, marker, ROOT], "engine-test", "kit",
                              "daemon", cores=1, store=self.store, env=env or clean_env(), **kw)
        words, self.left = [], {}
        with contextlib.suppress(FileNotFoundError), open(marker) as f:
            words = [int(x) for x in f.read().split()]
            self.left = dict(zip(words[0::2], words[1::2]))
            for pid, ct in self.left.items():
                self.addCleanup(self.end_left, pid, ct)
        self.assertEqual(code, 0)
        return words[0::2]

    def end_left(self, pid, ct):
        process.terminate_verified(pid, ct)
        self.assertTrue(self.wait_gone(pid, ct), "pid %d outlived the test's cleanup" % pid)

    def own(self, pid):
        """The creation time of leftover pid (one run_leaving read) while it still runs, else
        None. run_leaving's cleanup ends it."""
        ct = self.left[pid]
        return ct if process.alive(pid, ct) else None

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
        self.assertTrue(self.wait_gone(daemon, self.left[daemon]), "the gradle daemon outlived the run")
        self.assertIsNotNone(self.own(plain), "a plain leftover was ended too")


class ReleaseStopsDaemons(ReleaseCase):
    def test_a_done_gate_run_stops_the_daemon_and_leaves_the_rest(self):
        daemon, plain = self.run_leaving(gate=True)
        plain_ct = self.own(plain)
        rec = self.ended()
        killed = [(d["pid"], d["what"], d["result"]) for d in rec.get("daemons", ())]
        self.assertEqual(killed, [(daemon, "gradle-daemon", "killed")])
        self.assertTrue(self.wait_gone(daemon, self.left[daemon]), "the gradle daemon outlived the run")
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
        self.assertTrue(self.wait_gone(daemon, self.left[daemon]), "a nested gate left the gradle daemon")
        self.assertTrue(any("stopped the gradle-daemon (pid %d)" % daemon in s for s in self.said),
                        self.said)
        self.assertFalse(os.listdir(self.store.path("ended")) if os.path.isdir(
            self.store.path("ended")) else [], "a nested slot wrote a record")


# A process that sleeps until the stop file argv[1] appears, or 120 s.
SLEEPER = ("import os, sys, time; "
           "[time.sleep(0.2) for _ in range(600) if not os.path.exists(sys.argv[1])]")

# A fake Gradle daemon: listens on a loopback port 0 (written to argv[1]) and holds every
# connection it accepts, as a daemon serving a build does. It ends itself within half a second
# of the stop file argv[2] appearing, or after 120 s. Given argv[4] and argv[5] (the kit's root),
# it first starts a worker child (a SLEEPER, as a Gradle daemon starts test executors) and writes
# the worker's "<pid> <ctime>" to argv[4] while it holds the worker's handle. Then, once the file
# argv[4] + ".spawn" appears, it starts a second worker, written the same way to argv[4] + "2".
DAEMON = "\n".join([
    "import os, socket, subprocess, sys, time",
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(8); s.settimeout(0.1)",
    "spawn = None",
    "def worker(path):",
    "    w = subprocess.Popen([sys.executable, '-c', %r, sys.argv[2]])" % SLEEPER,
    "    open(path + '.tmp', 'w').write('%d %d' % (w.pid, process.proc_ctime(w.pid)))",
    "    os.replace(path + '.tmp', path)",
    "if len(sys.argv) > 5:",
    "    sys.path.insert(0, sys.argv[5])",
    "    from kitlib.proc import process",
    "    worker(sys.argv[4])",
    "    spawn = sys.argv[4] + '.spawn'",
    "open(sys.argv[1] + '.tmp', 'w').write(str(s.getsockname()[1]))",
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])",
    "held, end = [], time.time() + 120",
    "while time.time() < end and not os.path.exists(sys.argv[2]):",
    "    if spawn and os.path.exists(spawn) and not os.path.exists(sys.argv[4] + '2'):",
    "        worker(sys.argv[4] + '2')",
    "    try: held.append(s.accept()[0])",
    "    except socket.timeout: pass"])

# A build client: waits for the daemon's port, connects, says so in argv[2], and stays connected
# until the stop file argv[3] appears (or 120 s). The port file is read until it opens: just after
# the daemon renames it into place, Windows may refuse the open for a moment (PermissionError).
CLIENT = "\n".join([
    "import os, socket, sys, time",
    "end = time.time() + 30",
    "while True:",
    "    try:",
    "        with open(sys.argv[1]) as f: port = int(f.read())",
    "        break",
    "    except (FileNotFoundError, PermissionError):",
    "        if time.time() >= end: raise",
    "        time.sleep(0.05)",
    "c = socket.create_connection(('127.0.0.1', port), timeout=10)",
    "open(sys.argv[2], 'w').close()",
    "end = time.time() + 120",
    "while time.time() < end and not os.path.exists(sys.argv[3]): time.sleep(0.2)"])

# The run's command: starts the daemon; "inside" also starts a client as its own child, "nested"
# starts one in a job of its own nested in the run's (kitlib.proc.job), "outside" leaves the
# client to the test. Each child's "<pid> <ctime>" is added to argv[8] as soon as it is started:
# this process holds the child's handle, so the pid is still that child's when its ctime is read,
# and the test's cleanup ends exactly those processes, never a later holder of a reused pid.
# Its own command line carries this source, so it spells the daemon's class in two parts: else
# kitlib.proc.daemons would take it for a Gradle daemon while it runs ("stay").
# Waits until a client is connected, writes "<daemon pid> <client pid or 0> <plain pid or 0>" and
# exits, leaving them in the run's job. Words after argv[9]: "worker" has the daemon start a
# worker child (its "<pid> <ctime>" in argv[8] + ".worker"), "plain" starts a plain leftover
# (a SLEEPER), "stay" keeps this process running until the stop file appears.
SHARED = "\n".join([
    "import os, subprocess, sys, time",
    "port, connected, out, mode, root, daemon, client, ids, stop = sys.argv[1:10]",
    "extra = sys.argv[10:]",
    "sys.path.insert(0, root)",
    "from kitlib.proc import job, process",
    "def note(p):",
    "    with open(ids, 'a') as f: f.write('%d %d ' % (p.pid, process.proc_ctime(p.pid)))",
    "more = [ids + '.worker', root] if 'worker' in extra else []",
    "d = subprocess.Popen([sys.executable, '-c', daemon, port, stop,",
    "                      'org.gradle.launcher.daemon.bootstrap.Gradle' + 'Daemon'] + more)",
    "note(d)",
    "c = pl = None",
    "if mode == 'inside':",
    "    c = subprocess.Popen([sys.executable, '-c', client, port, connected, stop])",
    "elif mode == 'nested':",
    "    c = job.Job(kill_on_close=False).start([sys.executable, '-c', client, port, connected,",
    "                                            stop])",
    "if c: note(c)",
    "if 'plain' in extra:",
    "    pl = subprocess.Popen([sys.executable, '-c', %r, stop])" % SLEEPER,
    "    note(pl)",
    "end = time.time() + 30",
    "while not os.path.exists(connected) and time.time() < end: time.sleep(0.05)",
    "open(out + '.tmp', 'w').write('%d %d %d' % (d.pid, c.pid if c else 0, pl.pid if pl else 0))",
    "os.replace(out + '.tmp', out)",
    "end = time.time() + 120",
    "while 'stay' in extra and time.time() < end and not os.path.exists(stop): time.sleep(0.2)"])

# A run's command with no daemon: starts a plain leftover (a SLEEPER), adds its "<pid> <ctime>"
# to argv[3], writes its pid to argv[4] and exits, leaving it in the run's job.
PLAIN = "\n".join([
    "import subprocess, sys",
    "root, stop, ids, out = sys.argv[1:5]",
    "sys.path.insert(0, root)",
    "from kitlib.proc import process",
    "p = subprocess.Popen([sys.executable, '-c', %r, stop])" % SLEEPER,
    "open(ids, 'a').write('%d %d ' % (p.pid, process.proc_ctime(p.pid)))",
    "open(out, 'w').write(str(p.pid))"])

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
        """{pid: ctime} of each child the run's command, or its daemon, recorded while it was
        alive."""
        words = []
        for path in (self.ids, self.ids + ".worker", self.ids + ".worker2"):
            try:
                with open(path) as f:
                    words += [int(x) for x in f.read().split()]
            except FileNotFoundError:
                pass
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
        return pids[:2]

    def results(self):
        return [(d["pid"], d["what"], d["result"]) for d in self.ended().get("daemons", ())]

    def outside_client(self):
        """A build client in another gate's job (the test's own, not the run's nor nested in
        it), connecting once the daemon writes its port; ended with its job at cleanup."""
        from kitlib.proc import job
        other = job.Job()
        try:
            c = other.start([sys.executable, "-c", CLIENT, os.path.join(self.tmp, "port.txt"),
                             os.path.join(self.tmp, "connected"), self.stop])
        except BaseException:
            other.end(True)
            raise

        def end_client():
            other.end(True)
            c.wait(30)
        self.addCleanup(end_client)
        return c

    def book_shared(self, mode, *extra):
        """A gate's slot (book(gate=True), as kitlib.gate.runner books) with SHARED started in its
        job: (slot, the command's Popen). Every recorded child is ended at cleanup, and the slot
        released ("error") if the test has not."""
        self.addCleanup(self.end_recorded)
        s = rm.book("engine-test", "kit", "daemon", cores=1, store=self.store, env=clean_env(),
                    gate=True)
        self.addCleanup(s.release, 1, "error")
        p = s.start([sys.executable, "-c", SHARED, os.path.join(self.tmp, "port.txt"),
                     os.path.join(self.tmp, "connected"), os.path.join(self.tmp, "shared.txt"),
                     mode, ROOT, DAEMON, CLIENT, self.ids, self.stop] + list(extra))
        return s, p

    def shared_pids(self, timeout=30):
        """(daemon, client or 0, plain or 0) SHARED wrote once a client connected."""
        out = os.path.join(self.tmp, "shared.txt")
        end = time.time() + timeout
        while True:
            try:                            # mid-rename, Windows may refuse the open for a moment
                with open(out) as f:
                    return tuple(int(x) for x in f.read().split())
            except (FileNotFoundError, PermissionError):
                if time.time() >= end:
                    raise
                time.sleep(0.05)

    def errored(self, s, p):
        """The gate's block raises once its command exits, as a gate whose step raised: the slot
        releases with how "error", the kill path."""
        with self.assertRaises(RuntimeError):
            with s:
                s.wait(p)
                raise RuntimeError("a gate step raised")

    def connected(self, c):
        """The outside client c (outside_client) connected to the daemon and still runs. Checked
        before anything the kill path did is judged: a client that never connected, or has
        exited, leaves the daemon serving no other job, and the test, not the kill path, is at
        fault."""
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "connected")),
                        "test setup: the outside build client (pid %d, exit %r) never connected "
                        "to the daemon" % (c.pid, c.poll()))
        self.assertIsNone(c.poll(), "test setup: the outside build client (pid %d) has exited; "
                                    "the daemon may have served no other job" % c.pid)

    def spared(self, daemon, plain, c, workers=1):
        """The kill path left the daemon the outside client c uses, and its `workers` worker
        children, running, and ended the run's other leftover."""
        self.connected(c)
        client = c.pid
        cts = self.recorded()
        # The leftover is waited for first, so a daemon ended by the same kill has had time to go.
        self.assertTrue(self.wait_gone(plain, cts[plain]), "a plain leftover outlived the kill")
        self.assertTrue(process.alive(daemon, cts[daemon]),
                        "the kill path ended the daemon another job's client uses")
        kids = [p for p in cts if p not in (daemon, plain)]
        self.assertEqual(len(kids), workers, cts)
        for p in kids:
            self.assertTrue(process.alive(p, cts[p]),
                            "the kill path ended a child of the daemon it left running")
        why = "left: pid %d serves pid %d, which is outside the run's job" % (daemon, client)
        self.assertEqual(self.results(), [(daemon, "gradle-daemon", why)])
        self.assertTrue(any("left the gradle-daemon (pid %d) running" % daemon in s
                            for s in self.said), self.said)

    def test_an_errored_gate_leaves_a_daemon_another_jobs_client_uses(self):
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        self.errored(s, p)
        daemon, _, plain = self.shared_pids()
        self.assertEqual(self.ended()["how"], "error")
        self.spared(daemon, plain, c)

    def test_a_cancelled_gate_leaves_a_daemon_another_jobs_client_uses(self):
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain", "stay")
        daemon, _, plain = self.shared_pids()
        self.assertTrue(rm.request_stop(self.store, s.id, "test"))
        code, how = s.wait(p)
        s.release(code, how)
        self.assertEqual((code, how), (1, "cancelled"))
        self.assertIsNotNone(p.poll(), "the cancelled command outlived the kill")
        self.assertEqual(self.ended()["how"], "cancelled")
        self.spared(daemon, plain, c)

    def judged(self):
        """True once the release said it leaves the daemon: the kill path's passes come next."""
        return any("left the gradle-daemon" in s for s in self.said)

    def test_a_second_ctrl_c_during_the_kill_still_ends_the_rest(self):
        """Ctrl+C while the cancel's passes end the job's processes: the KeyboardInterrupt goes
        up, and the release that follows (130, "interrupted", as run_command's) ends what the
        passes had not reached, sparing the shared daemon again."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain", "stay")
        daemon, _, plain = self.shared_pids()
        real, calls = process.terminate_verified, []

        def interrupted(pid, ct, code=1):   # the second process the passes end: Ctrl+C
            calls.append(pid)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(pid, ct, code)
        self.assertTrue(rm.request_stop(self.store, s.id, "test"))
        with mock.patch.object(process, "terminate_verified", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                s.wait(p)
        self.assertEqual(calls, [p.pid, plain], "the passes did not end the command, then the "
                                                "leftover")
        s.release(130, "interrupted")
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(10)
        self.assertIsNotNone(p.poll(), "the command outlived the kill")
        self.spared(daemon, plain, c)

    def test_ctrl_c_during_the_releases_own_kill_ends_the_whole_job(self):
        """Ctrl+C inside the end_tree release itself calls (an errored gate; wait ended no tree):
        release ends the whole job, as the kill did before end_tree, and the KeyboardInterrupt
        goes up."""
        self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        self.assertEqual(s.wait(p), (0, "done"))
        daemon, _, plain = self.shared_pids()
        calls = []

        def interrupted(pid, ct, code=1):   # the first process release's passes end: Ctrl+C
            calls.append(pid)
            raise KeyboardInterrupt
        with mock.patch.object(process, "terminate_verified", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                s.release(1, "error")
        self.assertEqual(calls, [plain], "release's passes did not reach the leftover")
        self.assertTrue(self.wait_gone(plain, self.recorded()[plain]),
                        "the leftover outlived an interrupted release")

    def test_a_worker_the_daemon_starts_between_the_reads_is_spared(self):
        """A worker the left daemon starts after the process table is read is in the job's pid
        list read after it, yet not in that table: the job is read first, so it is ended only if
        it was already in the job when the table was read."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        real = process.snapshot
        spawn, made = self.ids + ".worker.spawn", self.ids + ".worker2"

        def then_a_worker():                # the table as read, then the daemon starts a worker
            snap = real()
            if self.judged() and not os.path.exists(spawn):
                open(spawn, "w").close()
                end = time.time() + 10
                while not os.path.exists(made) and time.time() < end:
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(made), "the daemon started no second worker")
            return snap
        with mock.patch.object(process, "snapshot", then_a_worker):
            self.errored(s, p)
        self.assertTrue(os.path.exists(spawn), "the passes never read the process table")
        daemon, _, plain = self.shared_pids()
        self.spared(daemon, plain, c, workers=2)

    def test_a_target_created_after_the_job_was_read_is_not_ended(self):
        """A pid in the job's list whose process now was created after the list was read is
        another process (the listed one ended and its pid was reused): the pass leaves it."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        daemon, _, plain = self.shared_pids()
        real_ct, real_end, later, ended = process.proc_ctime, process.terminate_verified, {}, []

        def reused(pid):                    # once, after the judgement: the leftover's pid now
            ct = real_ct(pid)               # names a process created 100 s on
            if pid == plain and ct is not None and not later and self.judged():
                later[pid] = ct + 10 ** 9
                return later[pid]
            return ct

        def spy(pid, ct, code=1):
            ended.append((pid, ct))
            return real_end(pid, ct, code)
        with mock.patch.object(process, "proc_ctime", reused), \
                mock.patch.object(process, "terminate_verified", spy):
            self.errored(s, p)
        self.assertIn(plain, later, "the passes never read the leftover's creation time")
        self.assertNotIn((plain, later[plain]), ended,
                         "a pass ended a process created after it read the job")
        self.spared(daemon, plain, c)

    def test_a_daemon_whose_judgement_raised_is_spared_and_not_called_left(self):
        """daemons.stop raised (it may have ended some daemons first): each daemon found is
        recorded "not judged", never "left", and spared while it runs; the rest is ended."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        with mock.patch.object(rm.daemons, "stop", side_effect=RuntimeError("boom")):
            self.errored(s, p)
        daemon, _, plain = self.shared_pids()
        self.connected(c)
        cts = self.recorded()
        self.assertTrue(self.wait_gone(plain, cts[plain]), "a plain leftover outlived the kill")
        self.assertTrue(process.alive(daemon, cts[daemon]), "the unjudged daemon was ended")
        self.assertEqual(self.results(),
                         [(daemon, "gradle-daemon", "not judged (RuntimeError: boom)")])

    def test_a_result_that_cannot_be_said_changes_nothing_the_kill_ends_or_spares(self):
        """store.say raises on the line about the left daemon (as a CLI's does once its stderr
        has closed): the kill goes on as judged, the shared daemon's tree spared and the rest
        ended, and the failure does not leave the release."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        self.assertEqual(s.wait(p), (0, "done"))
        daemon, _, plain = self.shared_pids()

        def deaf(text):
            self.said.append(text)
            if "gradle-daemon" in text:
                raise OSError("stderr has closed")
        self.store.say = deaf
        escaped = None
        try:
            s.release(1, "error")
        except OSError as e:
            escaped = e
        self.assertTrue(self.judged(), self.said)       # the say that fails was reached
        self.connected(c)
        cts = self.recorded()
        self.assertTrue(self.wait_gone(plain, cts[plain]), "a plain leftover outlived the kill")
        for pid in cts:
            if pid != plain:
                self.assertTrue(process.alive(pid, cts[pid]),
                                "a result that could not be said ended the tree of the daemon "
                                "another job's client uses (pid %d)" % pid)
        self.assertIsNone(escaped, "the say's failure left the release")
        self.assertEqual(self.ended()["how"], "error")

    def test_a_client_that_exited_fails_as_the_tests_fault_not_the_kill_paths(self):
        """Control on the harness: an outside client that exits once it says it connected leaves
        the daemon serving no other job, so the kill path rightly ends it. spared() says the
        test's setup failed, not that the kill path ended a shared daemon."""
        with mock.patch.object(sys.modules[__name__], "CLIENT",
                               "import sys; open(sys.argv[2], 'w').close()"):
            c = self.outside_client()
        c.wait(30)
        s, p = self.book_shared("outside", "worker", "plain")
        self.errored(s, p)
        daemon, _, plain = self.shared_pids()
        with self.assertRaises(AssertionError) as caught:
            self.spared(daemon, plain, c)
        self.assertIn("test setup", str(caught.exception))

    def test_the_launchers_kill_leaves_a_daemon_another_jobs_client_uses(self):
        """Launcher.kill (what slot() yields) ends the job's processes but the shared daemon's
        tree, as a cancel does; the block then ends "done"."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain", "stay")
        daemon, _, plain = self.shared_pids()
        with s:
            s.launcher.kill()
            self.assertEqual(s.launcher.wait(p), 1, "the command outlived the launcher's kill")
        self.assertEqual(self.ended()["how"], "done")
        self.spared(daemon, plain, c)

    def test_a_listed_process_gone_by_the_last_pass_is_not_said_to_run_on(self):
        """A pid every pass lists whose process has exited (as one does between the last list
        and the warning) is not said to run on. Here it is a process this test started and
        waited for, its handle held so the pid stays its own."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(30)
        real = s.job.pids
        with mock.patch.object(s.job, "pids", lambda: list(real()) + [gone.pid]):
            self.errored(s, p)
        daemon, _, plain = self.shared_pids()
        self.spared(daemon, plain, c)
        self.assertFalse([x for x in self.said if "could not be ended" in x], self.said)

    def test_a_second_kill_keeps_what_the_first_recorded(self):
        """Ctrl+C during the cancel's passes, then the release's end_tree judges again. A daemon
        the first judgement stopped is gone by the second, so only the first says it: the record
        keeps it. (It is stood in for by a result added to the first judgement's; pid 4, the
        System process, is never in a job, so no pass touches it.)"""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain", "stay")
        daemon, _, plain = self.shared_pids()
        real_stop, judged = rm.daemons.stop, []
        first = (4, "kotlin-daemon", "killed")

        def stop(found, contains):
            judged.append(found)
            return real_stop(found, contains) + ([first] if len(judged) == 1 else [])

        def interrupted(pid, ct, code=1):   # the first pass's first kill: Ctrl+C
            raise KeyboardInterrupt
        self.assertTrue(rm.request_stop(self.store, s.id, "test"))
        with mock.patch.object(rm.daemons, "stop", stop):
            with mock.patch.object(process, "terminate_verified", interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    s.wait(p)
            s.release(130, "interrupted")
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(10)
        self.assertIsNotNone(p.poll(), "the command outlived the kill")
        self.assertEqual(len(judged), 2, "the release did not judge the daemons again")
        self.assertIn(first, self.results(), "the second kill dropped what the first recorded")
        self.assertTrue(self.wait_gone(plain, self.recorded()[plain]),
                        "a plain leftover outlived the kill")
        self.connected(c)
        self.assertTrue(process.alive(daemon, self.recorded()[daemon]),
                        "the kill path ended the daemon another job's client uses")
        why = "left: pid %d serves pid %d, which is outside the run's job" % (daemon, c.pid)
        self.assertIn((daemon, "gradle-daemon", why), self.results())

    def test_a_left_daemon_whose_creation_time_cannot_be_read_is_spared_with_its_tree(self):
        """In the passes, the left daemon cannot be opened to read its creation time (as for a
        process this user may not query), yet it is not surely gone: it and its worker are
        spared, erring toward the client building on it."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        daemon, _, plain = self.shared_pids()
        real_ct = process.proc_ctime

        def unreadable(pid):
            return None if pid == daemon and self.judged() else real_ct(pid)
        with mock.patch.object(process, "proc_ctime", unreadable):
            self.errored(s, p)
        self.spared(daemon, plain, c)

    def test_a_process_table_that_cannot_be_read_in_the_passes_ends_the_whole_job(self):
        """process.snapshot raises in the passes: the job is ended whole, fail closed, the daemon
        it had left among them, and that is said."""
        c = self.outside_client()
        s, p = self.book_shared("outside", "worker", "plain")
        daemon, _, plain = self.shared_pids()
        real = process.snapshot

        def unreadable():
            if self.judged():
                raise OSError("no snapshot")
            return real()
        with mock.patch.object(process, "snapshot", unreadable):
            self.errored(s, p)
        self.connected(c)
        cts = self.recorded()
        self.assertTrue(self.wait_gone(plain, cts[plain]), "a plain leftover outlived the kill")
        self.assertTrue(self.wait_gone(daemon, cts[daemon]),
                        "the daemon outlived a kill whose passes could not read the table")
        self.assertTrue(any("could not be read while its tree was ended (no snapshot)" in x
                            for x in self.said), self.said)

    def test_an_errored_gate_ends_a_daemon_only_its_own_client_uses(self):
        """Control: a daemon serving the run's own client dies on the kill path as it always
        did, under machine_lock.daemons "leave" too (the kill path does not read it)."""
        self.store.site = {"daemons": "leave"}
        s, p = self.book_shared("inside")
        self.errored(s, p)
        daemon, client, _ = self.shared_pids()
        cts = self.recorded()
        self.assertNotEqual(client, 0)
        self.assertTrue(self.wait_gone(daemon, cts[daemon]), "the daemon outlived the kill")
        self.assertTrue(self.wait_gone(client, cts[client]), "the client outlived the kill")

    def test_an_errored_gate_with_no_daemon_ends_its_whole_tree(self):
        """Control: with no daemon in its job, the kill path ends every process in it."""
        self.addCleanup(self.end_recorded)
        out = os.path.join(self.tmp, "plain.txt")
        s = rm.book("engine-test", "kit", "daemon", cores=1, store=self.store, env=clean_env(),
                    gate=True)
        self.addCleanup(s.release, 1, "error")
        self.errored(s, s.start([sys.executable, "-c", PLAIN, ROOT, self.stop, self.ids, out]))
        with open(out) as f:
            plain = int(f.read())
        self.assertTrue(self.wait_gone(plain, self.recorded()[plain]),
                        "a plain leftover outlived the kill")

    def test_a_daemon_serving_a_client_in_another_job_is_left_running(self):
        c = self.outside_client()
        daemon, _ = self.run_shared("outside")
        self.connected(c)
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
