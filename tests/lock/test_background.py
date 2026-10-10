"""kitlib.lock.background.run_background: however it ends (its child exits, Ctrl+C), it settles
(queue entry gone, record ended with how and code, the child's process tree ended), and holds
that cannot be read are never read as none: it starts nothing until they read."""
import gc
import os
import sys
import time
import unittest
import warnings
from unittest import mock

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import background as bg
    from kitlib.lock import run as rm
    from kitlib.proc import process

REAL_SLEEP = time.sleep
PID_THEN_WAIT = "import os,sys,time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"


class Background(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(self.layout()["medium_cores"])

    def ended(self):
        names = [n for n in os.listdir(self.store.path("ended")) if n.startswith("r-")]
        self.assertEqual(len(names), 1)
        e = self.store.read("ended/" + names[0])
        self.assertEqual([n for n in os.listdir(self.store.path("active"))
                          if n.startswith("r-")], [])
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])
        return e

    def sleeps(self, until):
        """A time.sleep that raises KeyboardInterrupt (Ctrl+C) at the first of background's
        one-second loop sleeps where until(loops) is true. time is one module, so the patch
        reaches every sleep in the lock (the holds' read retries among them): only the loop's
        count."""
        loops = [0]

        def fake(s):
            if s == 1:
                loops[0] += 1
                if until(loops[0]):
                    raise KeyboardInterrupt
            REAL_SLEEP(s)
        return fake

    def test_a_child_that_exits_settles_the_run(self):
        code = bg.run_background([sys.executable, "-c", "import sys; sys.exit(0)"], "kit", "bg",
                                 store=self.store, env=clean_env())
        self.assertEqual(code, 0)
        e = self.ended()
        self.assertEqual((e["how"], e["code"]), ("done", 0))
        self.assertEqual([d["id"] for d in self.store.history_records()], [e["id"]])

    def test_its_command_goes_through_r1415_holding_its_gpu(self):
        # A background run always holds a gpu slot, so a docker exec it runs is left alone.
        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
        with mock.patch.object(bg, "docker_cpu_env", wraps=rm.docker_cpu_env) as f:
            self.assertEqual(bg.run_background(cmd, "kit", "bg", store=self.store,
                                               env=clean_env()), 0)
        f.assert_called_once_with(cmd, True)
        self.assertEqual(rm.docker_cpu_env(["docker", "exec", "box", "sh"], True),
                         ["docker", "exec", "box", "sh"])

    def test_ctrl_c_ends_the_child_and_settles(self):
        pid_file = os.path.join(self.tmp, "child.pid")

        def started(_n):
            for _ in range(200):
                if os.path.exists(pid_file) and os.path.getsize(pid_file):
                    return True
                REAL_SLEEP(0.05)
            return True
        with mock.patch.object(bg.time, "sleep", self.sleeps(started)),                 warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            code = bg.run_background([sys.executable, "-c", PID_THEN_WAIT, pid_file], "kit", "bg",
                                     store=self.store, env=clean_env())
            gc.collect()
        self.assertEqual(code, 130)
        # The killed child is waited on: an unwaited Popen warns when it is dropped.
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)
                          and "still running" in str(w.message)], [])
        e = self.ended()
        self.assertEqual((e["how"], e["code"]), ("interrupted", 130))
        with open(pid_file, encoding="utf-8") as f:
            pid = int(f.read())
        deadline = time.monotonic() + 10
        while process.proc_ctime(pid) is not None and time.monotonic() < deadline:
            REAL_SLEEP(0.1)
        self.assertIsNone(process.proc_ctime(pid), "the child outlived its background run")

    def test_unreadable_holds_start_nothing(self):
        # The old loop read a hold file it could not parse as no hold (red there: the child ran
        # and exited 0 before the third look).
        os.makedirs(self.store.path("holds"), exist_ok=True)
        with open(self.store.path("holds", "h-20261006T120000Z-abcd.json"), "w") as f:
            f.write("{")
        with mock.patch.object(bg.time, "sleep", self.sleeps(lambda n: n >= 3)):
            code = bg.run_background([sys.executable, "-c", "import sys; sys.exit(0)"], "kit",
                                     "bg", store=self.store, env=clean_env())
        self.assertEqual(code, 130)
        e = self.ended()
        self.assertEqual((e["how"], e["child"]), ("interrupted", "waiting"))
        self.assertTrue(any("starts nothing until they read" in s for s in self.said), self.said)


if __name__ == "__main__":
    unittest.main()
