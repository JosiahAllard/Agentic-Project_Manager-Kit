"""A background child that ignores the pause it is asked for (a deaf child) is ended, never the
foreground run that needs its slots: bg-yield-late ends its tree, records the run and frees the
slot, and the foreground run then starts and ends normally, with or without --no-wait. Both runs
are this process's own, on a scratch store (two Store objects on one root, as two callers)."""
import os
import sys
import threading
import time
import unittest

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import background as bg
    from kitlib.lock import run as rm
    from kitlib.proc import process

DEAF = "import os,sys,time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(300)"


class DeafChild(StoreCase):
    SETTINGS = {"bg_yield_s": 3.0, "bg_yield_margin_s": 2.0}

    def setUp(self):
        super().setUp()
        self.said, self.bg_said = [], []
        self.store.say = self.said.append
        self.confine(self.layout()["medium_cores"])
        self.bg_store = self.make_store()
        self.bg_store.say = self.bg_said.append

    def wait_for(self, test, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if test():
                return True
            time.sleep(0.05)
        return test()

    def against(self, **fg):
        """A foreground run needing the gpu (local-ai) with fg's booking options against a deaf
        background child. Returns what the foreground run said."""
        marker = os.path.join(self.tmp, "deaf.pid")
        out = {}
        t = threading.Thread(target=lambda: out.update(code=bg.run_background(
            [sys.executable, "-c", DEAF, marker], "kit", "deaf", store=self.bg_store,
            env=clean_env())), daemon=True)
        t.start()
        self.assertTrue(self.wait_for(lambda: os.path.exists(marker) and os.path.getsize(marker),
                                      60), "the deaf child never started: %s" % self.bg_said)
        with open(marker) as f:
            child = int(f.read())
        child_ct = process.proc_ctime(child)
        if child_ct is not None:
            self.addCleanup(process.terminate_verified, child, child_ct)
        t0 = time.monotonic()
        code = rm.run_command([sys.executable, "-c", "pass"], "local-ai", "kit", "fg",
                              store=self.store, env=clean_env(), **fg)
        waited = time.monotonic() - t0
        self.assertEqual(code, 0, self.said)
        self.assertGreaterEqual(waited, 2.5, "it never waited for the yield")
        t.join(30)
        self.assertFalse(t.is_alive(), "the background run never ended")
        self.assertEqual(out.get("code"), 1)
        self.assertTrue(self.wait_for(lambda: not process.alive(child, child_ct), 10),
                        "the deaf child lives on")
        self.assertTrue(any("bg-yield-late" in s for s in self.bg_said), self.bg_said)
        ended = {}
        for n in os.listdir(self.store.path("ended")):
            if n.startswith("r-"):
                e = self.store.read("ended/" + n)
                ended[e["slug"]] = (e["how"], e["code"])
        self.assertEqual(ended, {"deaf": ("yield-late", 1), "fg": ("done", 0)})
        alerts = os.listdir(self.store.path("alerts"))
        self.assertEqual(len(alerts), 1, alerts)
        a = self.store.read("alerts/" + alerts[0])
        self.assertEqual((a["slug"], a["how"], a["code"]), ("deaf", "yield-late", 1))
        self.assertEqual((self.store.read("queue.json") or {}).get("entries", []), [])
        return self.said

    def test_a_deaf_child_is_ended_not_the_waiting_foreground_run(self):
        self.against(wait_s=60)

    def test_a_no_wait_run_waits_out_a_deaf_child(self):
        said = self.against(no_wait=True)
        self.assertTrue(any("asks it to pause" in s for s in said), said)


if __name__ == "__main__":
    unittest.main()
