"""Layout-mismatch regression guards (kitlib.lock.run.book).

A run record's `layout` is a label (kitlib.lock.layout.slot_key); every booking counts slots from
the store's layout.json (kitlib.lock.layout.in_force), and the queue plans only from queue.json's
entries (kitlib.lock.queue.plan), never from the active records' `layout` strings. So:

- a run waits in queue.json behind a live gate run that holds its slot, then takes it;
- a gate booking (gate=True, as kitlib.gate.runner books) and a plain `run` booking record the
  same slot_key: one layout source;
- a live record whose layout is in the old format with no `g<gpus>` part, or names another gpu
  count, never makes book() refuse;
- a layout.json that is missing, unreadable or invalid to this kit version is not rewritten while
  a run is live, only once none is (kitlib.lock.layout.in_force).

The refusal these guard against is the installed machine_lock.py's own check, which compares the
strings."""
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
with rm.slot("engine-test", "kit", a["slug"], cores=1, store=s, wait_s=60,
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


def end_process(p):
    """Cleanup: a child still running is killed, then waited on and its pipes closed (no
    ResourceWarning); one already done only has its pipes closed."""
    if p.poll() is None:
        p.kill()
    p.communicate(timeout=60)


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
        # The gate run holds the one usable slot and its record says another layout: the old
        # format, as the incident's live run did, or another gpu count. The queue plans from
        # queue.json, so the run only waits for the slot the gate's entry holds, then takes it.
        s = one_usable_slot(self)
        for n, other in enumerate((OLD_FORMAT, OTHER_GPUS)):
            with self.subTest(layout=other):
                slug = "run%d" % n
                arg = json.dumps({"root": self.root, "fast": FAST, "hw": self.hw,
                                  "kinds": self.kinds_path, "slug": slug})
                with rm.slot("engine-test", "kit", "gate", cores=1, store=self.store,
                             env=clean_env(), gate=True) as gate:
                    rec = self.store.read("active/%s.json" % gate.id)
                    self.assertTrue(rec["gate"])
                    rec["layout"] = other
                    self.store.write("active/%s.json" % gate.id, rec)
                    self.assertEqual(self.entry(rid=gate.id)["cores"], [s])
                    # The premise, read back just before the run books.
                    self.assertEqual(self.store.read("active/%s.json" % gate.id)["layout"], other)
                    self.assertNotEqual(other, lay.slot_key(self.layout()))
                    p = subprocess.Popen([sys.executable, "-c", BOOK_RUN, arg], cwd=REPO,
                                         env=clean_env(), stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                    self.addCleanup(end_process, p)
                    waiter = self.waiting(slug, p)
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

    def test_a_slotless_live_record_in_another_format_or_gpu_count_does_not_refuse_a_booking(self):
        # A live record that holds no slot and has no queue entry; test 1 covers one that holds
        # the slot.
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


class InvalidLayoutUnderLiveRuns(StoreCase):
    """layout.json as another kit version may leave it (missing, unreadable, or valid only to that
    version's layout_key) is the layout of the runs live now: rewriting it under them renumbers
    their slots, so it waits for nothing live or queued, as a changed layout does."""

    def raw(self):
        try:
            with open(self.store.path("layout.json"), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def spoil(self, how, good):
        path = self.store.path("layout.json")
        if how == "missing":
            self.store.drop("layout.json")
        elif how == "unreadable":
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"key": "t24 cpu 12 x 2 gpu 2 m')
        else:                       # another version's key formula: invalid to this one
            self.store.write("layout.json", dict(good, key="v2 " + good["key"]))
        self.assertFalse(lay.valid_layout(self.store.read("layout.json")))

    def test_it_is_pending_while_a_run_is_live_then_adopted(self):
        good = self.layout()
        for n, how in enumerate(("missing", "unreadable", "another version")):
            with self.subTest(layout_json=how):
                rid = "r-20261017T120000Z-00000%d" % n
                self.live_record(rid)
                self.spoil(how, good)
                before = self.raw()
                got = lay.in_force(self.store)
                self.assertEqual(self.raw(), before, "layout.json was rewritten under a live run")
                self.assertIn("no run is live or queued (1 now)", got["pending"])
                self.assertEqual(got["layout"]["key"], got["wanted"]["key"])
                self.store.drop("active/%s.json" % rid)
                got = lay.in_force(self.store)
                self.assertIsNone(got["pending"])
                self.assertEqual(self.store.read("layout.json")["key"], good["key"])

    def test_an_unreadable_queue_leaves_an_invalid_layout_unwritten_and_says_so(self):
        # Whether a run is queued is not known, so layout.json is not rewritten, and pending says
        # why the layout in use is the wanted one, unwritten.
        good = self.layout()
        with open(self.store.path("queue.json"), "w", encoding="utf-8") as f:
            f.write("not a queue")
        for how in ("missing", "unreadable", "another version"):
            with self.subTest(layout_json=how):
                self.spoil(how, good)
                before = self.raw()
                got = lay.in_force(self.store)
                self.assertEqual(self.raw(), before)
                self.assertEqual(got["layout"]["key"], good["key"])
                self.assertIn("the layout could not be checked (queue unreadable", got["pending"])
