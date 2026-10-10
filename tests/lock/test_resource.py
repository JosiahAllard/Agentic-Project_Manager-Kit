"""kitlib.lock.resource: one holder per resource, first come first served, dead callers dropped,
an unreadable queue never written over. On a scratch store (tests/lock/helpers.py)."""
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from tests.lock.helpers import REPO, StoreCase

if os.name == "nt":
    from kitlib.lock import resource as rs
    from kitlib.proc import process

ALPHA, GAMMA = "landing:alpha", "landing:gamma"


class ResourceCase(StoreCase):
    def join(self, label, resource=ALPHA):
        t = rs.join(self.store, resource, label)
        self.addCleanup(t.leave)
        return t

    def file(self):
        with open(self.store.path(rs.FILE), encoding="utf-8") as f:
            return json.load(f)

    def write_file(self, data):
        """resources.json as given: a dict as JSON, a str as it is."""
        os.makedirs(self.store.root, exist_ok=True)
        with open(self.store.path(rs.FILE), "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))


class FirstComeFirstServed(ResourceCase):
    def test_the_first_caller_takes_it_and_the_rest_see_who_is_ahead(self):
        a, b, c = self.join("a"), self.join("b"), self.join("c")
        self.assertEqual(a.try_take(), [])
        self.assertTrue(a.held)
        self.assertEqual([e["label"] for e in b.try_take()], ["a"])
        self.assertEqual([e["label"] for e in c.try_take()], ["a", "b"])
        self.assertEqual(b.try_take()[0]["state"], "held")

    def test_order_is_by_join_not_by_who_asks_first(self):
        a, b, c = self.join("a"), self.join("b"), self.join("c")
        self.assertEqual(a.try_take(), [])
        a.leave()
        self.assertEqual([e["label"] for e in c.try_take()], ["b"])     # c never jumps b
        self.assertEqual(b.try_take(), [])
        b.leave()
        self.assertEqual(c.try_take(), [])

    def test_a_waiter_that_leaves_lets_the_next_one_up(self):
        a, b, c = self.join("a"), self.join("b"), self.join("c")
        a.try_take()
        b.leave()
        self.assertEqual([e["label"] for e in c.try_take()], ["a"])
        a.leave()
        self.assertEqual(c.try_take(), [])

    def test_resources_are_separate(self):
        a = self.join("a", ALPHA)
        g = self.join("g", GAMMA)
        self.assertEqual(a.try_take(), [])
        self.assertEqual(g.try_take(), [])

    def test_the_holder_asking_again_still_holds(self):
        a = self.join("a")
        self.assertEqual(a.try_take(), [])
        held_at = self.file()["entries"][0]["held_at"]
        self.assertEqual(a.try_take(), [])
        self.assertEqual(self.file()["entries"][0]["held_at"], held_at)

    def test_tickets_keep_counting_after_the_queue_empties(self):
        a = self.join("a")
        a.leave()
        b = self.join("b")
        self.assertGreater(b.ticket, a.ticket)

    def test_entries_lists_live_callers_in_ticket_order(self):
        a, b = self.join("a"), self.join("b")
        self.join("g", GAMMA)
        a.try_take()
        self.assertEqual([(e["label"], e["state"]) for e in rs.entries(self.store, ALPHA)],
                         [("a", "held"), ("b", "waiting")])
        self.assertEqual(len(rs.entries(self.store)), 3)
        self.assertIn("pid %d, held since" % os.getpid(), rs.text(rs.entries(self.store, ALPHA)[0]))

    def test_leave_is_idempotent_and_a_left_turn_cannot_take(self):
        a = self.join("a")
        a.leave()
        a.leave()
        self.assertEqual(rs.entries(self.store), [])
        with self.assertRaises(rs.ResourceError):
            a.try_take()

    def test_as_a_context_it_leaves_on_exit(self):
        with rs.join(self.store, ALPHA, "a") as a:
            self.assertEqual(a.try_take(), [])
        self.assertEqual(rs.entries(self.store), [])


class Waiting(ResourceCase):
    def test_wait_zero_answers_at_once(self):
        a, b = self.join("a"), self.join("b")
        a.try_take()
        slept, told = [], []        # never sleeps, however long its look took on a loaded machine
        with mock.patch.object(rs, "time", mock.Mock(monotonic=time.monotonic, sleep=slept.append)):
            self.assertEqual([e["label"] for e in b.wait(0, on_wait=told.append)], ["a"])
        self.assertEqual((slept, told), ([], []))

    def test_wait_times_out_with_the_entries_still_ahead(self):
        a, b = self.join("a"), self.join("b")
        a.try_take()
        told = []
        t0 = time.monotonic()
        self.assertEqual([e["label"] for e in b.wait(0.4, on_wait=told.append)], ["a"])
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)
        self.assertEqual(len(told), 1)                          # told once, not every poll

    def test_wait_takes_it_when_the_holder_leaves(self):
        import threading
        a, b = self.join("a"), self.join("b")
        a.try_take()
        threading.Timer(0.3, a.leave).start()
        self.assertEqual(b.wait(10), [])
        self.assertTrue(b.held)


CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, %(repo)r)
    from kitlib.lock.store import Settings, Store
    from kitlib.lock import resource as rs
    t = rs.join(Store(root=%(root)r, settings=Settings(queue_read_s=0.3)), %(res)r, "child")
    t.try_take()
    print(t.id, flush=True)
    os._exit(0)                     # dies holding it: no leave
""")


class DeadCallers(ResourceCase):
    def test_a_holder_that_died_is_dropped_and_the_next_one_takes_it(self):
        out = subprocess.run([sys.executable, "-c", CHILD % {"repo": REPO, "root": self.root,
                                                               "res": ALPHA}],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([e["label"] for e in self.file()["entries"]], ["child"])
        self.assertEqual(self.file()["entries"][0]["state"], "held")
        b = self.join("b")
        self.assertEqual(b.try_take(), [])
        self.assertEqual([e["label"] for e in self.file()["entries"]], ["b"])

    def test_an_entry_without_its_run_mutex_is_dead(self):
        self.write_file({"ticket": 7, "entries": [
            {"id": "l-20260101T000000Z-dead", "ticket": 7, "resource": ALPHA, "label": "gone",
             "state": "held", "pid": os.getpid(), "ctime": process.own_ctime(),
             "since": "x", "held_at": "x"}]})
        self.assertEqual(rs.entries(self.store), [])
        b = self.join("b")
        self.assertEqual(b.ticket, 8)
        self.assertEqual(b.try_take(), [])

    def test_a_waiter_whose_process_ended_is_dropped(self):
        a = self.join("a")
        a.try_take()
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        d = self.file()
        d["entries"].append({"id": "l-20260101T000000Z-gone", "ticket": d["ticket"] + 1,
                             "resource": ALPHA, "label": "gone", "state": "waiting",
                             "pid": p.pid, "ctime": 1, "since": "x", "held_at": None})
        d["ticket"] += 1
        self.write_file(d)
        c = self.join("c")
        self.assertEqual([e["label"] for e in c.try_take()], ["a"])
        self.assertNotIn("gone", [e["label"] for e in self.file()["entries"]])


class Unreadable(ResourceCase):
    def test_an_unreadable_queue_is_never_written_over(self):
        path = self.store.path(rs.FILE)
        self.write_file("{not json")
        with self.assertRaises(rs.ResourceUnreadable):
            rs.join(self.store, ALPHA, "a")
        with self.assertRaises(rs.ResourceUnreadable):
            rs.entries(self.store)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_a_turn_whose_queue_turns_unreadable_takes_nothing(self):
        a = self.join("a")
        self.write_file("[]")
        with self.assertRaises(rs.ResourceError):
            a.try_take()
        self.assertFalse(a.held)


if __name__ == "__main__":
    unittest.main()
