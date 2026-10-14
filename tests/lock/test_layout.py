"""kitlib.lock.layout on fake hardware (any Windows machine, past 64 logical processors,
any gpu vendor, the owner's division, a change adopted only with nothing live) and
kitlib.lock.game.domain_slots (the preferred L3 domain, or half the cores with one L3)."""
import os
import unittest

from .helpers import StoreCase

if os.name == "nt":
    from kitlib.lock import game
    from kitlib.lock import layout as lay
    from kitlib.lock.kinds import KindsError
    from kitlib.proc import job


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class Derive(unittest.TestCase):
    def test_defaults_on_a_12_core_smt_machine_with_one_card(self):
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1, vram_mb=12288))
        self.assertEqual(L["cpu_slots"], 12)
        self.assertEqual(L["cpu_slot_threads"], 2)
        self.assertEqual(L["slots_cpu"][0], [0, 1])
        self.assertEqual(L["agent_cap"], 6)
        self.assertEqual(L["medium_cores"], 2)
        self.assertEqual(L["half"], {"cpu": 6, "gpu": 1})
        self.assertEqual(L["gpu_slots"], 2)
        self.assertEqual(L["gpu_cards"], [0, 0])
        self.assertEqual(L["gpus"], 1)
        self.assertTrue(lay.valid_layout(L))

    # The older kit's installed machine_lock.py key for this machine (24 threads, 12 SMT-2 cores,
    # one card), copied from its refusal on 2026-10-10. It refuses
    # every run while a live record carries another string.
    LIVE_TOOL_KEY = "t24 g1 cpu 12 x 2 gpu 2 m9b5a6751"

    def test_slot_key_is_the_installed_tools_key(self):
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1, vram_mb=12288))
        self.assertEqual(lay.slot_key(L), self.LIVE_TOOL_KEY)

    def test_slot_key_follows_slot_numbering_only(self):
        hw = lay.fake_hardware(threads=24, smt=2, gpus=1)
        base = lay.slot_key(lay.derive(hw))
        for d in ({"agent_cap": 2}, {"medium_cores": 1}, {"half_cpu_slots": 3}):
            with self.subTest(division=d):
                self.assertEqual(lay.slot_key(lay.derive(hw, d)), base)
        for d in ({"cpu_slot_threads": 4}, {"cpu_slots": 6}, {"gpu_share": 1}, {"threads": 16}):
            with self.subTest(division=d):
                self.assertNotEqual(lay.slot_key(lay.derive(hw, d)), base)

    # Pins of today's text: they pass before and after any change that keeps it. slot_key goes
    # into every run record, where the older kit's tool compares it; layout_key is layout.json's
    # key, which every kit version recomputes (valid_layout). A change to either text has to be
    # made on purpose.
    def test_slot_key_text_for_two_cards_and_for_none(self):
        # g is the count of shared cards and gpu the count of gpu slots (two per card; one
        # placeholder slot with no card). The m part is LIVE_TOOL_KEY's with any count of
        # cards: the hash takes the cpu slots only.
        two = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=2, vram_mb=12288))
        self.assertEqual(lay.slot_key(two), "t24 g2 cpu 12 x 2 gpu 4 m9b5a6751")
        none = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=0))
        self.assertEqual(lay.slot_key(none), "t24 g0 cpu 12 x 2 gpu 1 m9b5a6751")

    def test_layout_key_text_is_pinned(self):
        # layout.json's key: no g part, and a hash that does take the gpu slots' cards.
        for gpus, want in ((1, "t24 cpu 12 x 2 gpu 2 m90bcd92d"),
                           (2, "t24 cpu 12 x 2 gpu 4 m6c673e2a"),
                           (0, "t24 cpu 12 x 2 gpu 1 m9a7f63d7")):
            with self.subTest(gpus=gpus):
                L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=gpus, vram_mb=12288))
                self.assertEqual(L["key"], want)
                self.assertEqual(lay.layout_key(L), want)

    def test_other_labels_names_each_foreign_label_once(self):
        # Every writer in this kit labels its record with slot_key of the one layout in force
        # (layout.json), so another label on a live record means an older writer that counts
        # this machine its own way: other_labels names each such label once, sorted. A record
        # with no label, or one that is not text, names nothing.
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1, vram_mb=12288))
        own = lay.slot_key(L)
        old, two = "t24 cpu 12 x 2 gpu 2 m90bcd92d", "t24 g2 cpu 12 x 2 gpu 4 m0123abcd"
        recs = [{"layout": own}, {"layout": two}, {"layout": old}, {"layout": two}, {},
                {"layout": None}, {"layout": ""}, {"layout": 5}]
        self.assertEqual(lay.other_labels(recs, L), [old, two])
        self.assertEqual(lay.other_labels([{"layout": own}, {}], L), [])
        self.assertEqual(lay.other_labels([], L), [])

    def test_a_small_machine_keeps_one_slot_out_of_half(self):
        L = lay.derive(lay.fake_hardware(threads=4, smt=2, gpus=0))
        self.assertEqual((L["cpu_slots"], L["agent_cap"], L["medium_cores"], L["half"]["cpu"]),
                         (2, 1, 1, 1))

    def test_slot_width_from_the_division(self):
        L = lay.derive(lay.fake_hardware(threads=24, smt=2), {"cpu_slot_threads": 4})
        self.assertEqual(L["cpu_slots"], 6)
        self.assertEqual(L["cpu_slot_threads"], 4)
        self.assertEqual(L["slots_cpu"][1], [4, 5, 6, 7])
        self.assertEqual(L["medium_cores"], 1)
        self.assertEqual(L["agent_cap"], 3)

    def test_division_keys_narrow_the_layout(self):
        L = lay.derive(lay.fake_hardware(threads=24, smt=2),
                       {"threads": 16, "cpu_slots": 6, "agent_cap": 2, "gpu_share": 1})
        self.assertEqual(L["threads"], 16)
        self.assertEqual(L["cpu_slots"], 6)
        self.assertEqual(L["agent_cap"], 2)
        self.assertEqual(L["gpu_slots"], 1)
        self.assertEqual(L["division"]["cpu_slots"], 6)

    def test_a_division_it_cannot_use_raises(self):
        hw = lay.fake_hardware(threads=24, smt=2, gpus=1)
        for d in ({"bogus": 1}, {"cpu_slots": 0}, {"cpu_slots": 13}, {"cpu_slots": "4"},
                  {"gpu_share": [1, 2]}, {"gpu_share": 17}, {"medium_cores": 7}, [1]):
            with self.subTest(division=d), self.assertRaises(ValueError):
                lay.derive(hw, d)

    def test_past_64_logical_processors(self):
        # 80 threads in two processor groups of 40: thread ids 0-39 and 64-103.
        hw = lay.fake_hardware(threads=80, smt=2, group_size=40)
        self.assertEqual([g["group"] for g in hw["groups"]], [0, 1])
        self.assertEqual(hw["threads"][40], 64)
        L = lay.derive(hw)
        self.assertEqual(L["cpu_slots"], 40)
        self.assertEqual(L["slots_cpu"][-1], [102, 103])
        self.assertGreaterEqual(L["cpu_masks"][-1], 1 << 64)
        kind, parts = job.affinity_plan(L["every"])
        self.assertEqual(kind, "groups")
        self.assertEqual([g for g, _m in parts], [0, 1])
        self.assertEqual(job.affinity_plan(lay.cores_mask(L, [0, 1])), ("basic", 0b1111))

    def test_no_gpu_leaves_one_placeholder_slot(self):
        L = lay.derive(lay.fake_hardware(gpus=0))
        self.assertEqual(L["gpu_cards"], [None])
        self.assertEqual((L["gpu_slots"], L["gpus"], L["half"]["gpu"]), (1, 0, 0))
        self.assertEqual(lay.gpu_cards_of(L, [0]), [])

    def test_a_card_under_1_gib_is_listed_but_not_shared(self):
        hw = lay.fake_hardware(gpus=1, vram_mb=512)
        self.assertEqual(hw["gpus"][0]["default_share"], 0)
        self.assertEqual(lay.derive(hw)["gpu_cards"], [None])

    def test_any_gpu_vendor(self):
        hw = lay.fake_hardware(gpus=1, gpu_vendor=0x1002)
        self.assertEqual(hw["gpus"][0]["vendor"], "AMD")
        self.assertEqual(lay.derive(hw)["gpus"], 1)
        intel = lay.fake_hardware(gpus=1, gpu_vendor=0x8086)
        self.assertEqual(intel["gpus"][0]["vendor"], "Intel")

    def test_microsoft_render_adapters_are_left_out(self):
        self.assertEqual(lay.fake_hardware(gpus=1, gpu_vendor=0x1414)["gpus"], [])

    def test_cim_parse(self):
        got = lay.parse_cim('[{"Name": "Radeon", "PNPDeviceID": "PCI\\\\VEN_1002&DEV_73BF\\\\x",'
                            ' "AdapterRAM": 4293918720}]')
        self.assertEqual((got[0]["vendor_id"], got[0]["device_id"]), (0x1002, 0x73BF))
        self.assertEqual(got[0]["vram_mb"], 4095)

    def test_need_for(self):
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1))
        med = {"cpu": "medium", "gpu": 0, "share": False}
        heavy_gpu = {"cpu": "heavy", "gpu": "pool", "share": True}
        self.assertEqual(lay.need_for(L, med), {"cpu": 2, "gpu": 0})
        self.assertEqual(lay.need_for(L, heavy_gpu), {"cpu": 6, "gpu": 2})
        self.assertEqual(lay.need_for(L, heavy_gpu, gpu_share="training"), {"cpu": 6, "gpu": 1})
        self.assertEqual(lay.need_for(L, heavy_gpu, whole=True)["cpu"], 12)
        self.assertEqual(lay.need_for(L, med, cores=3)["cpu"], 3)

    def test_an_integer_kind_books_its_size_clamped_to_the_agent_cap(self):
        # Cpu N books N cpu slots, never past agent_cap, an owner whole hold included
        # (only the heavy alias widens there).
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1))
        self.assertEqual(L["agent_cap"], 6)
        three = {"cpu": 3, "gpu": 0, "share": False}
        self.assertEqual(lay.need_for(L, three), {"cpu": 3, "gpu": 0})
        self.assertEqual(lay.need_for(L, three, whole=True)["cpu"], 3)
        self.assertEqual(lay.kind_cores(L, three), 3)
        self.assertEqual(lay.kind_cores(L, {"cpu": 9}), 6)
        self.assertEqual(lay.kind_cores(L, {"cpu": 9}, whole=True), 6)
        self.assertEqual(lay.need_for(L, {"cpu": 1, "gpu": "pool", "share": True},
                                      gpu_share="training"), {"cpu": 1, "gpu": 1})

    def test_a_bad_cpu_value_fails_closed(self):
        # kind_cores does not trust a table it was handed: a non-int, a bool or a value below 1
        # is a KindsError (exit 10 at the lock), never an int() traceback or a clamp to 1.
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1))
        for bad in (0, -2, True, False, "3", 2.5, None, "huge"):
            with self.assertRaises(KindsError, msg=repr(bad)):
                lay.kind_cores(L, {"cpu": bad})

    def test_the_aliases_keep_their_sizes(self):
        L = lay.derive(lay.fake_hardware(threads=24, smt=2, gpus=1))
        self.assertEqual(lay.kind_cores(L, {"cpu": "medium"}), L["medium_cores"])
        self.assertEqual(lay.kind_cores(L, {"cpu": "medium"}, whole=True), L["medium_cores"])
        self.assertEqual(lay.kind_cores(L, {"cpu": "heavy"}), 6)
        self.assertEqual(lay.kind_cores(L, {"cpu": "heavy"}, whole=True), 12)


@unittest.skipUnless(os.name == "nt", "the lock is Windows only")
class DomainSlots(unittest.TestCase):
    def test_two_l3_domains_take_the_preferred_one(self):
        hw = lay.fake_hardware(threads=24, smt=2, l3=2, sched={t: 1 for t in range(12, 24)})
        L = lay.derive(hw)
        self.assertEqual(L["l3_slots"], [list(range(6)), list(range(6, 12))])
        self.assertEqual(game.domain_slots(L, hw), list(range(6, 12)))

    def test_equal_domains_take_the_first(self):
        hw = lay.fake_hardware(threads=24, smt=2, l3=2)
        self.assertEqual(game.domain_slots(lay.derive(hw), hw), list(range(6)))

    def test_efficiency_class_outranks_scheduling_class(self):
        hw = lay.fake_hardware(threads=24, smt=2, l3=2, efficiency={t: 1 for t in range(12)},
                               sched={t: 1 for t in range(12, 24)})
        self.assertEqual(game.domain_slots(lay.derive(hw), hw), list(range(6)))

    def test_one_l3_takes_half_the_cores_preferred_first(self):
        hw = lay.fake_hardware(threads=24, smt=2, l3=1, sched={t: 1 for t in range(20, 24)})
        self.assertEqual(game.domain_slots(lay.derive(hw), hw), [0, 1, 2, 3, 10, 11])


class InForce(StoreCase):
    def write_division(self, d):
        self.store.write("machine.json", d)

    def test_a_fresh_store_adopts_the_default(self):
        got = lay.in_force(self.store)
        self.assertIsNone(got["pending"])
        self.assertEqual(got["source"], "default")
        self.assertEqual(self.store.read("layout.json")["key"], got["layout"]["key"])

    def test_a_change_waits_for_nothing_live_then_applies(self):
        self.assertEqual(self.layout()["cpu_slots"], 12)
        self.write_division({"cpu_slots": 6})
        self.live_record("r-live1")
        got = lay.in_force(self.store)
        self.assertEqual(got["layout"]["cpu_slots"], 12)
        self.assertEqual(got["wanted"]["cpu_slots"], 6)
        self.assertIn("no run is live or queued", got["pending"])
        self.store.drop("active/r-live1.json")
        got = lay.in_force(self.store)
        self.assertIsNone(got["pending"])
        self.assertEqual(got["layout"]["cpu_slots"], 6)
        self.assertEqual(got["source"], "config")

    def test_a_hardware_change_is_held_back_the_same_way(self):
        self.layout()
        self.live_record("r-live2")
        self.hw = lay.fake_hardware(threads=16, smt=2, l3=2)
        self.store = self.make_store()
        got = lay.in_force(self.store)
        self.assertEqual(got["layout"]["threads"], 24)
        self.assertEqual(got["wanted"]["threads"], 16)
        self.assertTrue(got["pending"])

    def test_read_only_look_never_writes(self):
        got = lay.in_force(self.store, adopt=False)
        self.assertEqual(got["layout"]["cpu_slots"], 12)
        self.assertIsNone(self.store.read("layout.json"))

    def test_a_bad_division_falls_back_with_a_warning(self):
        self.write_division({"cpu_slots": 99})
        got = lay.in_force(self.store)
        self.assertEqual(got["source"], "default")
        self.assertIn("is not used", got["warning"])
        self.assertEqual(got["layout"]["cpu_slots"], 12)


if __name__ == "__main__":
    unittest.main()
