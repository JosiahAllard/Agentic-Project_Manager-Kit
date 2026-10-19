"""kitlib.lock.run: a run records the git tree it tests (book's `tree`, as kitlib.gate.runner.gated
books a gate) on its record, and its history line keeps it (Store.HISTORY_KEYS), so a gate's run
is matched to the exact tree it tested. Slot.note_tree records it on a run already held (a
landing's slot, which runs the merged gate). A run that names no tree has none in its record and
`tree` null in its history line."""
import os
import unittest

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import run as rm

TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
OTHER = "0123456789abcdef0123456789abcdef01234567"


class GateTree(StoreCase):
    def book(self, **kw):
        kw.setdefault("store", self.store)
        kw.setdefault("env", clean_env())
        return rm.book("engine-test", "kit", "t", cores=1, no_wait=True, gate=True, **kw)

    def history(self, rid):
        hist = [d for d in self.store.history_records() if d["id"] == rid]
        self.assertEqual(len(hist), 1, hist)
        return hist[0]

    def test_a_booked_tree_is_on_the_record_and_kept_in_the_history_line(self):
        self.confine(1)
        with self.book(tree=TREE) as slot:
            rid = slot.id
            self.assertEqual(self.store.read("active/%s.json" % rid)["tree"], TREE)
        self.assertEqual(self.store.read("ended/%s.json" % rid)["tree"], TREE)
        self.assertEqual(self.history(rid)["tree"], TREE)

    def test_a_run_with_no_tree_has_a_null_tree_in_its_history_line(self):
        self.confine(1)
        with self.book() as slot:
            rid = slot.id
            self.assertNotIn("tree", self.store.read("active/%s.json" % rid))
        hist = self.history(rid)
        self.assertIn("tree", hist)
        self.assertIsNone(hist["tree"])

    def test_note_tree_records_the_tree_a_held_run_now_tests(self):
        self.confine(1)
        with self.book(tree=OTHER) as slot:
            rid = slot.id
            slot.note_tree(TREE)
            self.assertEqual(self.store.read("active/%s.json" % rid)["tree"], TREE)
            slot.note_tree(None)                    # nothing to note: the tree stays
            self.assertEqual(self.store.read("active/%s.json" % rid)["tree"], TREE)
        self.assertEqual(self.history(rid)["tree"], TREE)


if __name__ == "__main__":
    unittest.main()
