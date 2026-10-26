"""kitlib.lock.run nesting: MACHINE_LOCK_RUN names a run by id, wrapper pid and ctime;
nest_check accepts it only when that wrapper is an ancestor, alive, and its record holds;
nest_need inherits what the parent holds enough of and refuses a run that needs more."""
import contextlib
import os
import time
import types
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env, unopenable
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.lock import run as rm
    from kitlib.lock import store as store_mod
    from kitlib.proc import process

RID = "r-20261006T120000Z-abcdef"


class NestCheck(StoreCase):
    def parent_or_skip(self):
        """(pid, ctime) of this process's parent, which is one of its ancestors."""
        anc = process.own_ancestors()
        pp = os.getppid()
        ct = process.proc_ctime(pp)
        if ct is None or (pp, ct) not in anc:
            self.skipTest("this process's parent cannot be read as an ancestor")
        return pp, ct

    def value(self, pid, ct, rid=RID):
        return "%s:%d:%d" % (rid, pid, ct)

    def test_malformed(self):
        for v in (None, "", "r-1:2:3", RID, "%s:12:x" % RID, "%s:1:2:3" % RID):
            with self.subTest(v=v):
                self.assertEqual(rm.nest_check(self.store, v), (None, "malformed value"))

    def test_this_process_is_not_its_own_ancestor(self):
        got, why = rm.nest_check(self.store, self.value(os.getpid(), process.own_ctime()))
        self.assertIsNone(got)
        self.assertIn("not an ancestor", why)

    def test_an_ancestor_with_no_record_holds_nothing(self):
        pp, ct = self.parent_or_skip()
        got, why = rm.nest_check(self.store, self.value(pp, ct))
        self.assertIsNone(got)
        self.assertEqual(why, "the named run holds no lock")

    def test_a_waiting_record_holds_nothing(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, state="waiting")
        self.assertEqual(rm.nest_check(self.store, self.value(pp, ct))[1],
                         "the named run holds no lock")

    def test_a_record_naming_another_wrapper(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID)                       # its wrapper: this process, not the parent
        got, why = rm.nest_check(self.store, self.value(pp, ct))
        self.assertIsNone(got)
        self.assertEqual(why, "the record names another wrapper")

    def test_a_held_ancestor_is_the_parent(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, has={"cpu": 2, "gpu": 0, "disk": 3},
                         cores=[0, 1], shares={"cpu": "medium", "gpu": None})
        got, why = rm.nest_check(self.store, self.value(pp, ct))
        self.assertIsNone(why)
        self.assertEqual((got["id"], got["pid"], got["ctime"]), (RID, pp, ct))
        self.assertEqual(got["has"], {"cpu": 2})
        self.assertEqual(got["cores"], [0, 1])
        self.assertEqual(got["kind"], "engine-test")

    def test_book_inside_a_parent_that_holds_enough_takes_nothing(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, has={"cpu": 2}, cores=[0, 1])
        s = rm.book("engine-test", "kit", "t", store=self.store,
                    env=clean_env(**{rm.NEST_ENV: self.value(pp, ct)}))
        try:
            self.assertTrue(s.nested)
            self.assertEqual(s.id, RID)
            self.assertEqual(s.cores, [0, 1])
            self.assertIsNone(s.launcher.mask)
            self.assertIsNone(self.store.read("queue.json"))
        finally:
            self.assertIsNone(s.release())
        self.assertEqual(self.store.read("active/%s.json" % RID)["state"], "held")

    def test_book_inside_a_parent_that_holds_too_little_is_refused(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, has={"cpu": 1}, cores=[0])
        with self.assertRaises(rm.LockError) as cm:
            rm.book("engine-test", "kit", "t", store=self.store,
                    env=clean_env(**{rm.NEST_ENV: self.value(pp, ct)}))
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("nested run refused", cm.exception.message)
        self.assertIsNone(self.store.read("queue.json"))

    def test_a_record_that_cannot_be_opened_now_is_read_again(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, has={"cpu": 2}, cores=[0, 1])
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(unopenable(self.store.path("active", RID + ".json")))
        sleeps = []

        def sleep(s):
            # The wrapper's os.replace is done: the next read opens it.
            sleeps.append(s)
            stack.close()
        with mock.patch.object(store_mod, "time",
                               types.SimpleNamespace(monotonic=time.monotonic, sleep=sleep)):
            got, why = rm.nest_check(self.store, self.value(pp, ct))
        self.assertIsNone(why)
        self.assertEqual(got["cores"], [0, 1])
        self.assertEqual(len(sleeps), 1)

    def test_a_record_that_stays_unreadable_is_an_error_never_no_lock(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, has={"cpu": 2}, cores=[0, 1])
        with unopenable(self.store.path("active", RID + ".json")):
            with self.assertRaises(rm.LockError) as cm:
                rm.nest_check(self.store, self.value(pp, ct))
        self.assertEqual(cm.exception.code, rm.EXIT_REQUIRE)
        self.assertIn("could not be read", cm.exception.message)

    def test_book_inside_a_parent_whose_record_stays_unreadable_takes_no_lock(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, has={"cpu": 2}, cores=[0, 1])
        with unopenable(self.store.path("active", RID + ".json")):
            with self.assertRaises(rm.LockError) as cm:
                rm.book("engine-test", "kit", "t", store=self.store,
                        env=clean_env(**{rm.NEST_ENV: self.value(pp, ct)}))
        self.assertIn("could not be read", cm.exception.message)
        self.assertIsNone(self.store.read("queue.json"))        # it never queued for its own


@windows_only("the lock is Windows only")
class NestNeed(unittest.TestCase):
    PARENT = {"id": RID, "kind": "cpu-eval", "has": {"cpu": 6}}

    def test_not_nested_takes_everything(self):
        need = {"cpu": 2, "gpu": 1}
        self.assertEqual(rm.nest_need(None, "k", need), (need, need, None))

    def test_inherits_a_pool_the_parent_holds_enough_of(self):
        own, has, why = rm.nest_need(self.PARENT, "engine-test", {"cpu": 2, "gpu": 0})
        self.assertIsNone(why)
        self.assertEqual(own, {"cpu": 0, "gpu": 0})
        self.assertEqual(has, {"cpu": 6, "gpu": 0})

    def test_takes_a_pool_the_parent_has_none_of(self):
        own, has, _ = rm.nest_need(self.PARENT, "local-ai", {"cpu": 2, "gpu": 2})
        self.assertEqual(own, {"cpu": 0, "gpu": 2})
        self.assertEqual(has, {"cpu": 6, "gpu": 2})

    def test_needs_more_than_the_parent_holds(self):
        own, has, why = rm.nest_need(dict(self.PARENT, has={"cpu": 2}), "cpu-eval",
                                     {"cpu": 6, "gpu": 0})
        self.assertIsNone(own)
        self.assertIsNone(has)
        self.assertIn("start it outside that run", why)
        self.assertIn(RID, why)


if __name__ == "__main__":
    unittest.main()
