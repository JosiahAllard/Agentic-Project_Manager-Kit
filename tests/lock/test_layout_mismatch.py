"""KIT-LOCK-RUN-LAYOUT-MISMATCH regression guards (kitlib.lock.run.book).

A run record's `layout` is a label (kitlib.lock.layout.slot_key); every booking counts slots from
the store's layout.json (kitlib.lock.layout.in_force), and the queue plans only from queue.json's
entries (kitlib.lock.queue.plan), never from the active records' `layout` strings. So:

- a run waits in queue.json behind a live gate run that holds its slot, then takes it;
- a gate booking (gate=True, as kitlib.gate.runner books) and a plain `run` booking record the
  same slot_key: one layout source;
- a live record whose layout is in the old format with no `g<gpus>` part, or names another gpu
  count, never makes book() refuse.

The refusal these guard against was the older kit's own check, which compared the strings."""
import datetime
import json
import os
import subprocess
import sys
import time

from .helpers import FAST, OWNER, REPO, StoreCase, clean_env, own_affinity

if os.name == "nt":
    from kitlib.lock import holds as hl
    from kitlib.lock import layout as lay
    from kitlib.lock import run as rm
    from kitlib.lock.store import utc_now

# The two record strings of the incident's kind: the old format (layout_key's, no g<gpus>) and a
# key in today's format from a tool that saw two cards (g2, four gpu slots).
OLD_FORMAT = "t24 cpu 12 x 2 gpu 2 m90bcd92d"
OTHER_GPUS = "t24 g2 cpu 12 x 2 gpu 4 m0123abcd"

# A second process booking a plain run (gate False, as `machine_lock.py run` books) on the test's
# store; it prints when it held its slot.
BOOK_RUN = """
import json, os, sys, time
from kitlib.lock import run as rm
from kitlib.lock.store import Settings, Store
a = json.loads(sys.argv[1])
s = Store(root=a["root"], settings=Settings(**a["fast"]), hardware=a["hw"], kinds_path=a["kinds"])
with rm.slot("engine-test", "kit", "run", cores=1, store=s, wait_s=60,
             env=dict(os.environ)) as launch:
    t0 = time.time()
    rec = s.read("active/%s.json" % launch.id)
    print(json.dumps({"id": launch.id, "cores": launch.cores, "t0": t0, "layout": rec["layout"]}))
"""


def one_usable_slot(case):
    """Leaves runs on case's store one cpu slot, one this process may run on (an owner slots
    hold over every other); returns it."""
    L = case.layout()
    own = own_affinity()
    ok = [i for i, ts in enumerate(L["slots_cpu"]) if set(ts) <= own]
    if not ok:
        case.skipTest("this process may run on none of the fake cpu slots")
    now = utc_now()
    hl.create(case.store, L, {"owner": "owner", "scope": "slots", "start": now,
                              "end": now + datetime.timedelta(hours=1),
                              "slots": {"cpu": [i for i in range(L["cpu_slots"]) if i != ok[0]],
                                        "gpu": []}}, OWNER, None)
    return ok[0]


class LayoutMismatch(StoreCase):
    def entry(self, rid=None, slug=None):
        for e in (self.store.read("queue.json") or {}).get("entries", []):
            if e["id"] == rid or e.get("slug") == slug:
                return e
        return None

    def waiting(self, slug, p):
        end = time.monotonic() + 60
        while time.monotonic() < end:
            e = self.entry(slug=slug)
            if e is not None and e.get("state") == "waiting":
                return e
            if p.poll() is not None:
                self.fail("the run exited %s instead of waiting: %s" % (p.returncode,
                                                                         p.communicate()[1]))
            time.sleep(0.05)
        self.fail("run %s never waited in the queue" % slug)

    def test_a_run_waits_behind_a_live_gate_run_then_takes_its_slot(self):
        # The gate run's record says the old format, as the incident's live run did; the queue
        # plans from queue.json, so the run only waits for the slot the gate's entry holds.
        s = one_usable_slot(self)
        arg = json.dumps({"root": self.root, "fast": FAST, "hw": self.hw,
                          "kinds": self.kinds_path})
        with rm.slot("engine-test", "kit", "gate", cores=1, store=self.store, env=clean_env(),
                     gate=True) as gate:
            rec = self.store.read("active/%s.json" % gate.id)
            self.assertTrue(rec["gate"])
            rec["layout"] = OLD_FORMAT
            self.store.write("active/%s.json" % gate.id, rec)
            self.assertEqual(self.entry(rid=gate.id)["cores"], [s])
            p = subprocess.Popen([sys.executable, "-c", BOOK_RUN, arg], cwd=REPO,
                                 env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
            self.addCleanup(lambda: p.poll() is None and p.kill())
            waiter = self.waiting("run", p)
            self.assertEqual(waiter["cores"], [])
            self.assertIsNone(p.poll())
            released = time.time()
        out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, err)
        got = json.loads(out)
        self.assertEqual(got["id"], waiter["id"])
        self.assertEqual(got["cores"], [s])
        self.assertGreaterEqual(got["t0"], released)
        self.assertEqual(got["layout"], lay.slot_key(self.layout()))
        self.assertNotIn("refused", err)

    def test_a_gate_booking_and_a_run_booking_record_the_same_slot_key(self):
        self.confine(1)
        want = lay.slot_key(self.layout())
        seen = {}
        for gate in (True, False):
            with rm.slot("engine-test", "kit", "t", cores=1, store=self.store, env=clean_env(),
                         gate=gate) as launch:
                rec = self.store.read("active/%s.json" % launch.id)
            self.assertEqual(bool(rec.get("gate")), gate)
            seen[gate] = rec["layout"]
        self.assertEqual(seen, {True: want, False: want})

    def test_a_live_record_in_another_format_or_gpu_count_does_not_refuse_a_booking(self):
        self.confine(1)
        for other in (OLD_FORMAT, OTHER_GPUS):
            with self.subTest(layout=other):
                live = self.live_record("r-20261010T141322Z-3ac42c", layout=other, gate=True)
                self.assertNotEqual(live["layout"], lay.slot_key(self.layout()))
                # no_wait: any refusal or wait would raise LockError here.
                with rm.slot("engine-test", "kit", "t", cores=1, store=self.store,
                             env=clean_env(), no_wait=True) as launch:
                    rec = self.store.read("active/%s.json" % launch.id)
                self.assertEqual(rec["layout"], lay.slot_key(self.layout()))
                self.assertEqual(self.store.read("active/%s.json" % live["id"])["layout"], other)
