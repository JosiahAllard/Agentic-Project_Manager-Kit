"""kitlib.lock.giveback: a held run gives back cpu slots while its command runs on, and a queued
run takes them at once; the run's job is narrowed off them first; what cannot be given back is
refused and changes nothing; the record is written before the queue, and the ack after it."""
import copy
import ctypes
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from .helpers import FAST, OWNER, REPO, StoreCase, clean_env, own_affinity

if os.name == "nt":
    import datetime

    from kitlib.lock import holds as hl
    from kitlib.lock import layout as lay
    from kitlib.lock import run as rm
    from kitlib.lock.store import utc_now
    from kitlib.proc import process, win32


def gb():
    """kitlib.lock.giveback, imported when a test runs (the red-first run loads this file on a
    tree that has no such module)."""
    return importlib.import_module("kitlib.lock.giveback")


# The holder's command: once a run waits in the queue, it gives back its highest cpu slot, then
# waits (up to a["wait"] s) for the queued run to start. Exit 0: it started while this ran.
HOLDER = r"""
import json, os, sys, time
a = json.loads(sys.argv[1])

def waiting():
    try:
        with open(os.path.join(a["root"], "queue.json"), encoding="utf-8") as f:
            q = json.load(f)
    except (OSError, ValueError):
        return False
    return any(e.get("slug") == "waiter" and e.get("state") == "waiting" for e in q["entries"])

end = time.time() + 60
while time.time() < end and not waiting():
    time.sleep(0.05)
try:
    from kitlib.lock import giveback
    offer = giveback.Offer.from_env()
    held = offer.held() if offer else []
    print("offer", offer is not None, "held", held,
          "gave", offer.give([max(held)]) if held else None, flush=True)
except ImportError as e:
    print("no give back:", e, flush=True)
end = time.time() + a["wait"]
while time.time() < end and not os.path.exists(a["started"]):
    time.sleep(0.05)
sys.exit(0 if os.path.exists(a["started"]) else 3)
"""

# The queued run: once the holder holds, it books one cpu slot and says when it started.
WAITER = r"""
import json, os, sys, time
from kitlib.lock import run as rm
from kitlib.lock.store import Settings, Store
a = json.loads(sys.argv[1])
s = Store(root=a["root"], settings=Settings(**a["fast"]), hardware=a["hw"], kinds_path=a["kinds"])
end = time.time() + 60
while time.time() < end and not any(e.get("slug") == "holder" and e.get("state") == "held"
                                    for e in (s.read("queue.json") or {}).get("entries", [])):
    time.sleep(0.05)
with rm.slot("engine-test", "kit", "waiter", cores=1, store=s, wait_s=60,
             env=dict(os.environ)) as launch:
    d = {"cores": launch.cores, "t0": time.time()}
    with open(a["started"], "w", encoding="utf-8") as f:
        json.dump(d, f)
    print(json.dumps(d))
"""


def affinity(p):
    """The affinity mask of live process p (a Popen)."""
    pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
    if not win32.K32.GetProcessAffinityMask(int(p._handle), ctypes.byref(pm), ctypes.byref(sm)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pm.value


def usable_slots(case, n):
    """Leaves runs on case's store exactly n cpu slots, ones this process may run on (an owner
    slots hold over every other); returns them."""
    L = case.layout()
    own = own_affinity()
    ok = [i for i, ts in enumerate(L["slots_cpu"]) if set(ts) <= own]
    if len(ok) < n:
        case.skipTest("this process may run on only %d of the fake cpu slots" % len(ok))
    now = utc_now()
    hl.create(case.store, L, {"owner": "owner", "scope": "slots", "start": now,
                              "end": now + datetime.timedelta(hours=1),
                              "slots": {"cpu": [i for i in range(L["cpu_slots"]) if i not in ok[:n]],
                                        "gpu": []}}, OWNER, None)
    return ok[:n]


class QueuedRunStarts(StoreCase):
    def test_a_queued_run_starts_while_the_holder_runs_after_it_gave_slots_back(self):
        usable = usable_slots(self, 2)
        started = os.path.join(self.tmp, "started.json")
        arg = json.dumps({"root": self.root, "fast": FAST, "hw": self.hw,
                          "kinds": self.kinds_path, "started": started, "wait": 20})
        waiter = subprocess.Popen([sys.executable, "-c", WAITER, arg], cwd=REPO, env=clean_env(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: waiter.poll() is None and waiter.kill())
        out = os.path.join(self.tmp, "holder.out")
        with open(out, "w", encoding="utf-8") as log:
            code = rm.run_command([sys.executable, "-c", HOLDER, arg], "engine-test", "kit",
                                  "holder", cores=2, store=self.store, log=log,
                                  env=clean_env(PYTHONPATH=REPO))
        t_end = time.time()
        w_out, w_err = waiter.communicate(timeout=120)
        with open(out, encoding="utf-8") as f:
            said = f.read()
        self.assertEqual(code, 0, "the queued run did not start while the holder ran: %s" % said)
        self.assertEqual(waiter.returncode, 0, w_err)
        w = json.loads(w_out)
        self.assertLess(w["t0"], t_end)
        self.assertEqual(w["cores"], [max(usable)])
        ended = [d for d in self.store.history_records() if d["slug"] == "holder"][0]
        self.assertEqual(ended["cores"], [min(usable)])
        self.assertEqual(ended["given_back"]["cpu"], [max(usable)])


class Narrowing(StoreCase):
    """What the wrapper writes and pins when it gives slots back, and what it refuses."""

    def held(self, n=2):
        usable = usable_slots(self, n)
        s = rm.book("engine-test", "kit", "t", cores=n, store=self.store, env=clean_env())
        self.addCleanup(s.release)
        self.assertEqual(sorted(s.cores), usable)
        return s, usable

    def ask(self, s, p, cpu, gpu=None):
        req = {"id": "x%d" % time.monotonic_ns(), "cpu": cpu, "by_pid": p.pid,
               "by_ctime": process.proc_ctime(p.pid), "at": "now"}
        if gpu is not None:
            req["gpu"] = gpu
        with open(gb().request_path(self.store, s.id), "w", encoding="utf-8") as f:
            json.dump(req, f)
        s.serve_give_back()
        return req["id"], self.store.read("active/%s.json" % s.id)

    def sleeper(self, s, **kw):
        p = s.start([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        return p

    def test_given_back_slots_leave_the_job_record_queue_and_env(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        rid, rec = self.ask(s, p, [usable[1]])
        L = self.layout()
        keep = lay.cores_mask(L, [usable[0]])
        self.assertEqual(rec["given_back"]["id"], rid)
        self.assertEqual(rec["given_back"]["cpu"], [usable[1]])
        self.assertEqual((rec["cores"], rec["slots"]["cpu"], rec["has"]["cpu"]), ([usable[0]], [usable[0]], 1))
        self.assertEqual(rec["need"]["cpu"], 2)       # what it asked for stays
        self.assertEqual(rec["threads"], rm.cpu.mask_text(keep))
        e = [x for x in self.store.read("queue.json")["entries"] if x["id"] == s.id][0]
        self.assertEqual(e["cores"], [usable[0]])
        self.assertEqual(s.job.mask, keep)
        self.assertEqual(affinity(p), keep)         # the running command moved off it
        self.assertEqual(s.env[rm.CPUMASK_ENV], hex(keep))
        self.assertFalse(os.path.exists(gb().request_path(self.store, s.id)))

    def test_refusals_change_nothing(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        L = self.layout()
        both = lay.cores_mask(L, usable)
        for cpu, gpu, why in (([usable[0], usable[1]], None, "keeps at least one"),
                              ([99], None, "not this run's"),
                              ([usable[1]], [0], "gpu slots cannot")):
            rid, rec = self.ask(s, p, cpu, gpu)
            self.assertEqual(rec["give_back_refused"]["id"], rid)
            self.assertIn(why, rec["give_back_refused"]["why"])
            self.assertEqual(sorted(rec["cores"]), usable)
            self.assertEqual(s.job.mask, both)
        outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: outsider.poll() is None and (outsider.kill(), outsider.wait()))
        rid, rec = self.ask(s, outsider, [usable[1]])
        self.assertIn("not a live process of this run", rec["give_back_refused"]["why"])
        self.assertEqual(sorted(rec["cores"]), usable)

    def test_only_the_offered_command_gets_the_offer(self):
        usable = usable_slots(self, 2)
        env = dict(clean_env(), **{gb().GIVE_ENV: "an offer this run inherited"})
        s = rm.book("engine-test", "kit", "t", cores=2, store=self.store, env=env)
        self.addCleanup(s.release)
        self.assertEqual(sorted(s.cores), usable)
        self.assertNotIn(gb().GIVE_ENV, s.env)
        seen = os.path.join(self.tmp, "env.txt")
        code = """import os,sys; open(sys.argv[1],'w').write(os.environ.get(%r,'-'))""" % gb().GIVE_ENV
        s.launcher.run([sys.executable, "-c", code, seen])
        with open(seen, encoding="utf-8") as f:
            self.assertEqual(f.read(), "-")
        s.launcher.wait(s.launcher.start([sys.executable, "-c", code, seen], give_back=True))
        with open(seen, encoding="utf-8") as f:
            self.assertEqual(f.read(), gb().request_path(self.store, s.id))

    def test_a_failed_queue_write_puts_the_record_back(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        L = self.layout()
        with mock.patch("kitlib.lock.queue.save", side_effect=OSError("disk full")):
            rid, rec = self.ask(s, p, [usable[1]])
        self.assertEqual(rec["give_back_refused"]["id"], rid)
        self.assertIn("pinned to them again", rec["give_back_refused"]["why"])
        self.assertEqual(sorted(rec["cores"]), usable)
        self.assertNotIn("given_back", rec)
        e = [x for x in self.store.read("queue.json")["entries"] if x["id"] == s.id][0]
        self.assertEqual(sorted(e["cores"]), usable)
        self.assertEqual(s.job.mask, lay.cores_mask(L, usable))
        # The job was narrowed, then widened again: the running command is back on both slots.
        self.assertEqual(affinity(p), lay.cores_mask(L, usable))

    def test_a_failed_record_write_changes_nothing_and_the_asker_hears_refused(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        L = self.layout()
        both = lay.cores_mask(L, usable)
        rel = "active/%s.json" % s.id
        disk, mem = self.store.read(rel), copy.deepcopy(s.run.rec)
        self.assertEqual(disk, mem)
        real, failed = type(self.store).write, []

        def write(store, r, data, *a, **kw):
            # The first write of the run record fails; every other write goes through.
            if r == rel and not failed:
                failed.append(r)
                raise OSError("disk full")
            return real(store, r, data, *a, **kw)
        Store = type(self.store)
        with mock.patch.object(Store, "write", write):
            with self.assertRaises(OSError):
                gb().shrink(self.store, s.run, [usable[0]], {}, {"id": "y", "cpu": [usable[1]]})
        self.assertEqual(failed, [rel])
        self.assertEqual(s.run.rec, mem)                    # in memory, exactly as it was
        self.assertEqual(self.store.read(rel), disk)        # on disk too

        # The same failure under a real request: Offer.give hears "refused", never "given".
        offer = gb().Offer(gb().request_path(self.store, s.id))
        failed.clear()
        res = []
        with mock.patch.object(Store, "write", write), \
                mock.patch.object(s.job, "pids", return_value=[os.getpid()]):
            t = threading.Thread(target=lambda: res.append(offer.give([usable[1]], wait_s=20)))
            t.start()
            end = time.monotonic() + 30
            while t.is_alive() and time.monotonic() < end:
                s.serve_give_back()
                time.sleep(0.02)
            t.join(5)
        self.assertEqual(failed, [rel])
        self.assertEqual(len(res), 1, "Offer.give did not answer")
        self.assertEqual(res[0][0], "refused", res[0])
        self.assertIn("could not be written", res[0][1])
        on_disk = self.store.read(rel)
        self.assertNotIn("given_back", on_disk)
        self.assertEqual({k: v for k, v in on_disk.items() if k != "give_back_refused"}, disk)
        self.assertEqual({k: v for k, v in s.run.rec.items() if k != "give_back_refused"}, mem)
        e = [x for x in self.store.read("queue.json")["entries"] if x["id"] == s.id][0]
        self.assertEqual(sorted(e["cores"]), usable)
        self.assertEqual(sorted(s.cores), usable)
        self.assertEqual(s.job.mask, both)
        self.assertEqual(affinity(p), both)

    def test_a_malformed_request_is_looked_at_again_then_refused(self):
        s, usable = self.held(2)
        self.sleeper(s, give_back=True)
        path = gb().request_path(self.store, s.id)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        # Read mid-replace, it may be half written: two more looks before it is refused.
        self.assertIsNone(s.serve_give_back())
        self.assertIsNone(s.serve_give_back())
        self.assertTrue(os.path.exists(path))
        why = s.serve_give_back()
        self.assertIn("malformed (not a JSON object)", why)
        self.assertFalse(os.path.exists(path))
        rec = self.store.read("active/%s.json" % s.id)
        self.assertNotIn("give_back_refused", rec)          # no id to answer
        self.assertEqual(sorted(rec["cores"]), usable)
        # One that names its id is answered at once.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"id": "bad1", "cpu": "all", "by_pid": 1, "by_ctime": 1}, f)
        self.assertIn("cpu is not a list", s.serve_give_back())
        rec = self.store.read("active/%s.json" % s.id)
        self.assertEqual(rec["give_back_refused"]["id"], "bad1")
        self.assertEqual(sorted(rec["cores"]), usable)

    def test_an_asker_whose_creation_time_differs_is_refused(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        req = {"id": "ct1", "cpu": [usable[1]], "by_pid": p.pid,
               "by_ctime": process.proc_ctime(p.pid) + 1, "at": "now"}
        with open(gb().request_path(self.store, s.id), "w", encoding="utf-8") as f:
            json.dump(req, f)
        self.assertIn("not a live process of this run", s.serve_give_back())
        rec = self.store.read("active/%s.json" % s.id)
        self.assertEqual(rec["give_back_refused"]["id"], "ct1")
        self.assertEqual(sorted(rec["cores"]), usable)
        self.assertEqual(s.job.mask, lay.cores_mask(self.layout(), usable))

    def test_the_run_s_end_removes_its_request_file(self):
        s, _usable = self.held(2)
        path = gb().request_path(self.store, s.id)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        s.release()
        self.assertFalse(os.path.exists(path))

    def test_the_record_is_written_before_the_queue(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        seen = []
        real = gb().qm.save

        def save(store, q):
            # At the queue write, the record already says the narrower booking.
            seen.append(self.store.read("active/%s.json" % s.id)["cores"])
            return real(store, q)
        with mock.patch.object(gb().qm, "save", side_effect=save):
            self.ask(s, p, [usable[1]])
        self.assertEqual(seen, [[usable[0]]])

    def queue_cores(self, s):
        return sorted([x for x in self.store.read("queue.json")["entries"] if x["id"] == s.id][0]["cores"])

    def test_the_ack_is_written_after_the_queue(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        seen = []
        real = gb().qm.save

        def save(store, q):
            seen.append(self.store.read("active/%s.json" % s.id))
            return real(store, q)
        with mock.patch.object(gb().qm, "save", side_effect=save):
            rid, rec = self.ask(s, p, [usable[1]])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["cores"], [usable[0]])     # the booking first...
        self.assertNotIn("given_back", seen[0])             # ...but no ack before the queue says so
        self.assertEqual(rec["given_back"]["id"], rid)

    def test_a_polling_asker_never_hears_given_while_the_queue_write_fails(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        both = lay.cores_mask(self.layout(), usable)
        offer = gb().Offer(gb().request_path(self.store, s.id))
        res = []
        t = threading.Thread(target=lambda: res.append(offer.give([usable[1]], wait_s=20)))

        def save(store, q):
            # A queue write that waits on a held queue.json, then fails, while the asker polls
            # (Offer.give looks every POLL_S): it ends early only if the asker has answered.
            end = time.monotonic() + 1.0
            while t.is_alive() and time.monotonic() < end:
                time.sleep(0.02)
            raise OSError("queue.json is held")
        with mock.patch.object(gb().qm, "save", side_effect=save), \
                mock.patch.object(s.job, "pids", return_value=[os.getpid()]):
            t.start()
            end = time.monotonic() + 30
            while t.is_alive() and time.monotonic() < end:
                s.serve_give_back()
                time.sleep(0.02)
            t.join(5)
        self.assertEqual(len(res), 1, "Offer.give did not answer")
        self.assertEqual(res[0][0], "refused", res[0])
        self.assertIn("could not be written", res[0][1])
        rec = self.store.read("active/%s.json" % s.id)
        self.assertNotIn("given_back", rec)
        self.assertEqual(sorted(rec["cores"]), usable)
        self.assertEqual(self.queue_cores(s), usable)
        self.assertEqual(s.job.mask, both)
        self.assertEqual(affinity(p), both)

    def test_a_lost_ack_undoes_nothing_is_written_next_poll_and_a_re_ask_is_refused(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        keep = lay.cores_mask(self.layout(), [usable[0]])
        rel = "active/%s.json" % s.id
        real, failed = type(self.store).write, []

        def write(store, r, data, *a, **kw):
            # The first record write that carries the ack fails; every other write goes through.
            if r == rel and "given_back" in data and not failed:
                failed.append(r)
                raise OSError("disk full")
            return real(store, r, data, *a, **kw)
        with mock.patch.object(type(self.store), "write", write):
            rid, rec = self.ask(s, p, [usable[1]])
        self.assertEqual(failed, [rel])
        # Given back all the same: the queue freed the slot, so nothing is widened again.
        self.assertNotIn("give_back_refused", rec)
        self.assertNotIn("given_back", rec)
        self.assertEqual(rec["cores"], [usable[0]])
        self.assertEqual(self.queue_cores(s), [usable[0]])
        self.assertEqual((s.job.mask, affinity(p), s.cores), (keep, keep, [usable[0]]))
        # The next poll writes the ack.
        self.assertIsNone(s.serve_give_back())
        self.assertEqual(self.store.read(rel)["given_back"]["id"], rid)
        # An asker that timed out and asks again for the same slot is refused; nothing changes.
        rid2, rec2 = self.ask(s, p, [usable[1]])
        self.assertEqual(rec2["give_back_refused"]["id"], rid2)
        self.assertIn("not this run's", rec2["give_back_refused"]["why"])
        self.assertEqual((rec2["cores"], rec2["given_back"]["id"]), ([usable[0]], rid))
        self.assertEqual(self.queue_cores(s), [usable[0]])
        self.assertEqual((s.job.mask, affinity(p)), (keep, keep))

    def test_a_refused_repin_leaves_the_job_narrowed_and_a_later_ask_refused(self):
        s, usable = self.held(2)
        p = self.sleeper(s, give_back=True)
        L = self.layout()
        both, keep = lay.cores_mask(L, usable), lay.cores_mask(L, [usable[0]])
        real = s.job.repin

        def repin(mask):
            # Narrowing goes through (Job.narrow repins); widening back to both is refused.
            return False if mask == both else real(mask)
        with mock.patch.object(s.job, "repin", side_effect=repin), \
                mock.patch("kitlib.lock.queue.save", side_effect=OSError("disk full")):
            rid, rec = self.ask(s, p, [usable[1]])
        self.assertEqual(rec["give_back_refused"]["id"], rid)
        self.assertIn("stays narrowed to", rec["give_back_refused"]["why"])
        self.assertNotIn("given_back", rec)
        self.assertEqual(sorted(rec["cores"]), usable)      # the booking keeps both
        self.assertEqual(self.queue_cores(s), usable)
        self.assertEqual(sorted(s.cores), usable)
        self.assertEqual((s.job.mask, affinity(p)), (keep, keep))
        # The pin cannot widen, so a request that keeps the other slot cannot narrow onto it.
        rid2, rec2 = self.ask(s, p, [usable[0]])
        self.assertEqual(rec2["give_back_refused"]["id"], rid2)
        self.assertIn("could not be narrowed", rec2["give_back_refused"]["why"])
        self.assertEqual(sorted(rec2["cores"]), usable)
        self.assertEqual(self.queue_cores(s), usable)
        self.assertEqual((s.job.mask, affinity(p)), (keep, keep))


class Helpers(unittest.TestCase):
    def test_surplus_gives_back_the_highest_and_keeps_one(self):
        g = gb()
        self.assertEqual(g.surplus([4, 5, 6, 7], 2, 8), [])
        self.assertEqual(g.surplus([4, 5, 6, 7], 2, 5), [7])
        self.assertEqual(g.surplus([7, 4, 6, 5], 2, 2), [5, 6, 7])
        self.assertEqual(g.surplus([4, 5], 2, 0), [5])
        self.assertEqual(g.threads_per_slot([0, 1, 2], "0x3f"), 2)
        self.assertEqual(g.threads_per_slot([], "0x3f"), 1)

    def test_an_offer_needs_the_run_it_names(self):
        g = gb()
        path = os.path.abspath(os.path.join("store", "active", "r-20261010T000000Z-abcdef.giveback"))
        good = {g.GIVE_ENV: path, "MACHINE_LOCK_RUN": "r-20261010T000000Z-abcdef:1:2"}
        self.assertEqual(g.Offer.from_env(good).rid, "r-20261010T000000Z-abcdef")
        self.assertIsNone(g.Offer.from_env(dict(good, MACHINE_LOCK_RUN="r-20261010T000000Z-000000:1:2")))
        self.assertIsNone(g.Offer.from_env({"MACHINE_LOCK_RUN": good["MACHINE_LOCK_RUN"]}))
        self.assertIsNone(g.Offer.from_env(dict(good, **{g.GIVE_ENV: "rel.giveback"})))

    def test_the_drainer_asks_for_the_surplus_and_stops_after_a_refusal(self):
        g = gb()
        path = os.path.abspath(os.path.join("store", "active", "r-20261010T000000Z-abcdef.giveback"))
        env = {g.GIVE_ENV: path, "MACHINE_LOCK_RUN": "r-20261010T000000Z-abcdef:1:2",
               "MACHINE_LOCK_CPUMASK": "0xff"}
        self.assertIsNone(g.drainer(print, {}))
        held, asked, said = [[0, 1, 2, 3]], [], []

        def give(_offer, cpu, wait_s=10.0):
            asked.append(cpu)
            if len(asked) == 1:
                held[0] = [i for i in held[0] if i not in cpu]
                return "given", {"cores": held[0]}
            return "refused", "no"
        with mock.patch.object(g.Offer, "held", lambda _o: list(held[0])), \
                mock.patch.object(g.Offer, "give", give):
            d = g.drainer(said.append, env)
            self.assertIsNone(d(8))                 # 4 slots of 2 threads: nothing spare
            self.assertEqual(d(3), {"cores": [0, 1]})
            self.assertIsNone(d(1))                 # refused: it keeps them, and asks no more
            self.assertIsNone(d(1))
        self.assertEqual(asked, [[2, 3], [1]])
        self.assertIn("gave back cpu slot(s) 2,3, the run holds 0,1", said[0])
        self.assertIn("not given back (refused: no)", said[1])

    def test_a_gpu_that_is_not_a_list_is_refused_not_read_as_none(self):
        g = gb()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "r.giveback")
        base = {"id": "g1", "cpu": [1], "by_pid": 1, "by_ctime": 1}
        for gpu in (0, 1, None, "0", {}, False):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dict(base, gpu=gpu), f)
            req = g.read_request(path)
            self.assertEqual(req.get("bad"), "gpu is not a list of slot ids", gpu)
            self.assertEqual(req.get("id"), "g1")
            self.assertIn("malformed", g.check(req, [0, 1], False, True, "pinned", [1],
                                               lambda *_: True))
        for gpu, want in ((None, []), ([], []), ([0], [0])):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(base if gpu is None else dict(base, gpu=gpu), f)
            self.assertEqual(g.read_request(path)["gpu"], want)

    def test_the_request_file_is_not_a_run_record(self):
        # Both tools list active/*.json as run records; the request must not be one.
        self.assertFalse(gb().SUFFIX.endswith(".json"))


if __name__ == "__main__":
    unittest.main()
