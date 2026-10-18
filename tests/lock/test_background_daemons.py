"""kitlib.lock.background.run_background ends its child's tree (Ctrl+C, a stop whose grace ran
out) the way a foreground run's kill path does (kitlib.lock.run end_sparing): a build daemon
that serves a client outside the run's job is left running with its tree, the run's other
leftovers are ended, and the judgement is in the ended record's `daemons`. A daemon that serves
only the run's own client is ended, and recorded so. The fake daemon, client and run command are
test_release_daemons' (SHARED, DAEMON, CLIENT); every process is the test's own and is ended, and
waited for, at its cleanup."""
import os
import sys
import time
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env
from . import test_release_daemons as trd

if os.name == "nt":
    from kitlib.lock import background as bg
    from kitlib.lock import run as rm

REAL_SLEEP = time.sleep
LOOP_CAP = 90           # background's one-second loop sleeps a test waits before it gives up


class BackgroundLeavesASharedDaemon(StoreCase):
    """A background run whose command started a Gradle daemon that another job's client is
    building on: its tree is ended but for that daemon's."""

    # test_release_daemons' helpers, taken as plain functions (its TestCase classes are not
    # imported, so their tests are not run again here).
    outside_client = trd.ReleaseLeavesASharedDaemon.outside_client
    recorded = trd.ReleaseLeavesASharedDaemon.recorded
    end_recorded = trd.ReleaseLeavesASharedDaemon.end_recorded
    shared_pids = trd.ReleaseLeavesASharedDaemon.shared_pids
    connected = trd.ReleaseLeavesASharedDaemon.connected
    wait_gone = trd.ReleaseCase.wait_gone

    def setUp(self):
        super().setUp()
        self.store = self.make_store(bg_stop_grace_s=0)
        self.said = []
        self.store.say = self.said.append
        self.confine(self.layout()["medium_cores"])
        self.ids = os.path.join(self.tmp, "ids.txt")
        self.stop = os.path.join(self.tmp, "stop")
        self.out = os.path.join(self.tmp, "shared.txt")

    def ended(self):
        names = [n for n in os.listdir(self.store.path("ended")) if n.startswith("r-")]
        self.assertEqual(len(names), 1)
        return self.store.read("ended/" + names[0])

    def results(self):
        return [(d["pid"], d["what"], d["result"]) for d in self.ended().get("daemons", ())]

    def run_id(self):
        names = [n for n in os.listdir(self.store.path("active"))
                 if n.startswith("r-") and n.endswith(".json")]
        return names[0][:-len(".json")] if len(names) == 1 else None

    def run_shared(self, mode, act, *extra):
        """run_background of SHARED in `mode`; act() is called at the first of its one-second
        loop sleeps after SHARED wrote its pids (a client is connected). Its exit code."""
        self.addCleanup(self.end_recorded)
        loops, done = [0], [False]

        def fake(s):
            if s == 1:
                loops[0] += 1
                if not done[0] and os.path.exists(self.out):
                    done[0] = True
                    act()
                elif loops[0] > LOOP_CAP:
                    raise KeyboardInterrupt     # never hang: the test's asserts then fail
            REAL_SLEEP(s)

        def interrupt():
            raise KeyboardInterrupt
        self.interrupt = interrupt
        with mock.patch.object(bg.time, "sleep", fake):
            return bg.run_background(
                [sys.executable, "-c", trd.SHARED, os.path.join(self.tmp, "port.txt"),
                 os.path.join(self.tmp, "connected"), self.out, mode, trd.ROOT, trd.DAEMON,
                 trd.CLIENT, self.ids, self.stop] + list(extra),
                "kit", "bg", store=self.store, env=clean_env())

    def spared(self, daemon, plain, c):
        """The daemon the outside client c uses, and its worker, still run; the plain leftover
        is ended; the record and the lines say the daemon was left."""
        self.connected(c)
        cts = self.recorded()
        self.assertTrue(self.wait_gone(plain, cts[plain]), "a plain leftover outlived the kill")
        self.assertTrue(trd.process.alive(daemon, cts[daemon]),
                        "the background run's kill ended the daemon another job's client uses")
        kids = [p for p in cts if p not in (daemon, plain)]
        self.assertEqual(len(kids), 1, cts)
        self.assertTrue(trd.process.alive(kids[0], cts[kids[0]]),
                        "the background run's kill ended a child of the daemon it left running")
        why = "left: pid %d serves pid %d, which is outside the run's job" % (daemon, c.pid)
        self.assertEqual(self.results(), [(daemon, "gradle-daemon", why)])
        self.assertTrue(any("left the gradle-daemon (pid %d) running" % daemon in s
                            for s in self.said), self.said)

    def test_ctrl_c_leaves_a_daemon_another_jobs_client_uses(self):
        c = self.outside_client()
        code = self.run_shared("outside", lambda: self.interrupt(), "worker", "plain", "stay")
        daemon, _, plain = self.shared_pids()
        self.assertEqual(code, 130)
        self.assertEqual(self.ended()["how"], "interrupted")
        self.spared(daemon, plain, c)

    def test_a_stop_past_its_grace_leaves_a_daemon_another_jobs_client_uses(self):
        """The stop path: the child did not pause within bg_stop_grace_s (0 here), so its tree
        is ended, the shared daemon's spared."""
        c = self.outside_client()
        code = self.run_shared("outside", lambda: self.assertTrue(
            rm.request_stop(self.store, self.run_id(), "test")), "worker", "plain", "stay")
        daemon, _, plain = self.shared_pids()
        self.assertEqual(self.ended()["how"], "stopped")
        self.assertEqual(code, 1)
        self.assertTrue(any("did not pause in time" in s for s in self.said), self.said)
        self.spared(daemon, plain, c)

    def test_ctrl_c_ends_a_daemon_only_its_own_client_uses(self):
        """Control: a daemon serving only the run's own client is judged and ended, as the
        foreground kill path ends it."""
        code = self.run_shared("inside", lambda: self.interrupt(), "stay")
        daemon, client, _ = self.shared_pids()
        self.assertEqual(code, 130)
        self.assertNotEqual(client, 0)
        cts = self.recorded()
        self.assertEqual(self.results(), [(daemon, "gradle-daemon", "killed")])
        self.assertTrue(self.wait_gone(daemon, cts[daemon]), "the daemon outlived the kill")
        self.assertTrue(self.wait_gone(client, cts[client]), "the client outlived the kill")


if __name__ == "__main__":
    unittest.main()
