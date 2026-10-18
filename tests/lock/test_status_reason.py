"""kitlib.lock.status.pending_reason: the reason for every pending text layout.in_force gives,
driven through each of its branches, so a text changed there without status knowing fails here;
and waits_for_idle, which status and `division --set` read, on top of it."""
import os
import unittest

from .helpers import StoreCase
from .test_layout import no_cores_answer, no_gpu_answer

if os.name == "nt":
    from kitlib.lock import layout as lay
    from kitlib.lock import status as st


class PendingReason(StoreCase):
    def on(self, hw):
        self.hw = hw
        self.store = self.make_store()

    def reason(self, adopt=True):
        got = lay.in_force(self.store, adopt=adopt)
        return st.pending_reason(got["pending"]), got["pending"]

    def test_nothing_pending_is_none(self):
        self.layout()
        self.assertEqual(self.reason(), (None, None))
        self.assertIsNone(st.pending_reason(""))
        self.assertIsNone(st.pending_reason(True))      # not in_force's: no text to read

    def test_a_division_change_under_a_live_run_waits_for_idle(self):
        self.layout()
        self.live_record("r-20261017T120000Z-0000d1", cores=[0])
        self.store.write("machine.json", {"cpu_slots": 6})
        reason, text = self.reason()
        self.assertEqual(reason, st.IDLE, text)
        self.assertTrue(st.waits_for_idle(text))
        reason, text = self.reason(adopt=False)          # the snapshot's look
        self.assertEqual(reason, st.IDLE, text)
        self.assertTrue(st.waits_for_idle(text))

    def test_an_invalid_layout_under_a_live_run_is_written_once_idle(self):
        self.store.write("layout.json", {"key": "not a layout"})
        self.live_record("r-20261017T120000Z-0000d2", cores=[0])
        reason, text = self.reason()
        self.assertEqual(reason, st.UNWRITTEN, text)
        self.assertTrue(st.waits_for_idle(text))

    def test_a_failed_look_over_a_good_layout(self):
        self.layout()
        self.on(no_gpu_answer(self.hw))
        for adopt in (True, False):
            reason, text = self.reason(adopt)
            self.assertEqual(reason, st.FAILED_LOOK, text)
            self.assertFalse(st.waits_for_idle(text))

    def test_a_provisional_layout_written_found_and_seen_read_only(self):
        self.on(no_gpu_answer(self.hw))
        reason, text = self.reason(adopt=False)          # nothing written yet
        self.assertEqual(reason, st.PROVISIONAL, text)
        self.assertIsNone(self.store.read("layout.json"))
        for _ in range(2):                               # writes it, then finds it
            reason, text = self.reason()
            self.assertEqual(reason, st.PROVISIONAL, text)
            self.assertFalse(st.waits_for_idle(text))
        self.assertTrue(self.store.read("layout.json")["provisional"])

    def test_a_provisional_layout_whose_look_keeps_failing_is_still_provisional(self):
        # The case the schedule window called a division wait: nothing changed, nothing waits
        # for idle; the layout in use is from a failed look and stays provisional.
        self.on(no_cores_answer(self.hw))
        self.layout()
        self.live_record("r-20261017T120000Z-0000d3", cores=[0])
        reason, text = self.reason(adopt=False)
        self.assertEqual(reason, st.PROVISIONAL, text)

    def test_an_unreadable_queue_leaves_it_unchecked(self):
        self.layout()
        self.store.write("machine.json", {"cpu_slots": 6})
        with open(self.store.path("queue.json"), "w", encoding="utf-8") as f:
            f.write("not a queue")
        reason, text = self.reason()
        self.assertEqual(reason, st.UNCHECKED, text)
        self.assertFalse(st.waits_for_idle(text))

    def test_a_text_this_kit_does_not_give_is_other(self):
        self.assertEqual(st.pending_reason("an older kit's words; once no run is live or queued"),
                         st.OTHER)
        self.assertFalse(st.waits_for_idle("an older kit's words; once no run is live or queued"))

    def test_three_failed_parts_read_as_a_list(self):
        self.layout()
        self.on(dict(no_gpu_answer(self.hw), failed=["groups", "cores", "gpus"]))
        reason, text = self.reason()
        self.assertTrue(text.startswith("the groups, cores and gpus could not be read ("), text)
        self.assertEqual(reason, st.FAILED_LOOK)

    def test_status_still_prints_a_failed_look_as_a_layout_line(self):
        from kitlib.lock import api
        self.layout()
        self.on(no_gpu_answer(self.hw))
        lines = st.render(api.snapshot(self.store))
        why = [ln for ln in lines if ln.startswith("layout: ")]
        self.assertEqual(len(why), 1, lines)
        self.assertIn("no layout is adopted from that look until one succeeds", why[0])
        self.assertFalse(any(ln.startswith("hardware or division changed") for ln in lines))


if __name__ == "__main__":
    unittest.main()
