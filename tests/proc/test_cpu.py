"""Tests for kitlib.proc.cpu: topology records of every processor group, parsed from synthetic
buffers laid out as Windows lays them out (this machine has one group, so the multi-group paths
are proven only here), and the live machine's answer."""
import os
import struct
import unittest
from unittest import mock
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.proc import cpu


def core_record(groups, smt=True, efficiency=0):
    """A RelationProcessorCore record: Relationship, Size, Flags @8, EfficiencyClass @9,
    GroupCount @30, GROUP_AFFINITY[] @32 (Mask @0, Group @8, 16 bytes each)."""
    size = 32 + 16 * max(1, len(groups))
    b = bytearray(size)
    struct.pack_into("<II", b, 0, 0, size)
    struct.pack_into("<BB", b, 8, 1 if smt else 0, efficiency)
    struct.pack_into("<H", b, 30, len(groups))
    for i, (g, mask) in enumerate(groups):
        struct.pack_into("<QH", b, 32 + 16 * i, mask, g)
    return bytes(b)


def group_record(masks):
    """A RelationGroup record: MaximumGroupCount @8, ActiveGroupCount @10, then
    PROCESSOR_GROUP_INFO[] @32 (48 bytes each; ActiveProcessorMask @40)."""
    size = 32 + 48 * len(masks)
    b = bytearray(size)
    struct.pack_into("<II", b, 0, 4, size)
    struct.pack_into("<HH", b, 8, len(masks), len(masks))
    for i, m in enumerate(masks):
        n = bin(m).count("1")
        struct.pack_into("<BB", b, 32 + 48 * i, min(n, 255), min(n, 255))
        struct.pack_into("<Q", b, 32 + 48 * i + 40, m)
    return bytes(b)


def cache_record(level, groups):
    """A RelationCache record: Level BYTE @8, Type DWORD @16 (0 unified), GroupCount WORD @38,
    GroupMasks[] @40 (Mask @0, Group @8, 16 bytes each)."""
    size = 40 + 16 * max(1, len(groups))
    b = bytearray(size)
    struct.pack_into("<II", b, 0, 2, size)
    struct.pack_into("<B", b, 8, level)
    struct.pack_into("<H", b, 38, len(groups))
    for i, (g, mask) in enumerate(groups):
        struct.pack_into("<QH", b, 40 + 16 * i, mask, g)
    return bytes(b)


def cpu_set(sid, group, lp, core, llc, eff=0, sched=0, parked=False):
    """A SYSTEM_CPU_SET_INFORMATION of 32 bytes (layout: kitlib.proc.cpu.parse_cpu_sets)."""
    b = bytearray(32)
    struct.pack_into("<IIIHBBBBBBB", b, 0, 32, 0, sid, group, lp, core, llc, 0, eff,
                     1 if parked else 0, sched)
    return bytes(b)


@windows_only("Windows only")
class Synthetic(unittest.TestCase):
    def test_cores_in_two_groups_get_global_ids(self):
        raw = (core_record([(0, 0b11)]) + core_record([(0, 0b1100)])
               + core_record([(1, 0b11)]) + core_record([(1, 1 << 63)], smt=False))
        got = cpu.parse_cores(raw)
        self.assertEqual([c["threads"] for c in got], [[0, 1], [2, 3], [64, 65], [127]])
        self.assertEqual([c["smt"] for c in got], [True, True, True, False])

    def test_a_record_of_another_relation_is_skipped(self):
        other = bytearray(48)
        struct.pack_into("<II", other, 0, 2, 48)          # RelationCache
        raw = bytes(other) + core_record([(0, 0b11)])
        self.assertEqual([c["threads"] for c in cpu.parse_cores(raw)], [[0, 1]])

    def test_a_truncated_record_ends_the_walk(self):
        raw = core_record([(0, 0b11)]) + struct.pack("<II", 0, 400)
        self.assertEqual([c["threads"] for c in cpu.parse_cores(raw)], [[0, 1]])

    def test_efficiency_class_is_kept(self):
        got = cpu.parse_cores(core_record([(0, 1)], smt=False, efficiency=1))
        self.assertEqual(got[0]["efficiency"], 1)

    def test_groups_give_their_active_masks(self):
        raw = group_record([(1 << 64) - 1, 0xFF])
        self.assertEqual(cpu.parse_groups(raw), [(0, (1 << 64) - 1), (1, 0xFF)])

    def test_a_mask_splits_into_groups_and_back(self):
        mask = 0b1010 | (0b11 << 64) | (1 << 130)
        self.assertEqual(cpu.split(mask), {0: 0b1010, 1: 0b11, 2: 1 << 2})
        self.assertEqual(cpu.bits(mask), [1, 3, 64, 65, 130])
        self.assertEqual(cpu.bits_mask(cpu.bits(mask)), mask)

    def test_mask_text_runs_past_64(self):
        self.assertEqual(cpu.mask_text(0xF0), "4-7")
        self.assertEqual(cpu.mask_text(0xF00F), "0-3,12-15")
        self.assertEqual(cpu.mask_text((1 << 63) | (1 << 64) | (1 << 65)), "63-65")
        self.assertEqual(cpu.mask_text(0), "")

    def test_l3_domains_are_the_ccds(self):
        # A 5900X: two CCDs, threads 0-11 and 12-23; L2 records are ignored.
        raw = (cache_record(2, [(0, 0b11)]) + cache_record(3, [(0, 0xFFF000)])
               + cache_record(3, [(0, 0xFFF)]) + cache_record(3, [(0, 0xFFF)]))
        self.assertEqual(cpu.parse_l3(raw), [0xFFF, 0xFFF000])

    def test_an_l3_past_group_zero_gets_global_ids(self):
        self.assertEqual(cpu.parse_l3(cache_record(3, [(1, 0xF)])), [0xF << 64])


    def test_cpu_sets_give_thread_cache_and_class(self):
        raw = cpu_set(256, 0, 0, 0, 0, sched=1) + cpu_set(257, 1, 3, 40, 1, eff=1, parked=True)
        got = cpu.parse_cpu_sets(raw)
        self.assertEqual([(c["id"], c["thread"], c["llc"], c["efficiency"], c["sched"], c["parked"])
                          for c in got], [(256, 0, 0, 0, 1, False), (257, 67, 1, 1, 0, True)])


@windows_only("Windows only")
class GroupFallback(unittest.TestCase):
    """groups() stands in group 0 alone when Windows will not list the groups; listed_groups()
    says so (None), so a caller can tell the stand-in from Windows' answer."""

    def look(self, raw):
        from kitlib.proc import process
        with mock.patch.object(cpu, "_query", return_value=raw), \
                mock.patch.object(process, "system_mask", return_value=0xF) as sm:
            return cpu.listed_groups(), cpu.groups(), sm.called

    def test_no_answer_from_windows_is_reported_and_group_zero_stands_in(self):
        listed, got, stood_in = self.look(None)
        self.assertIsNone(listed)
        self.assertEqual(got, [(0, 0xF)])
        self.assertTrue(stood_in)

    def test_an_answer_with_no_group_record_is_reported(self):
        listed, got, _ = self.look(core_record([(0, 0b11)]))
        self.assertIsNone(listed)
        self.assertEqual(got, [(0, 0xF)])

    def test_listed_groups_are_windows_own_and_groups_uses_them(self):
        listed, got, stood_in = self.look(group_record([(1 << 64) - 1, 0xFF]))
        self.assertEqual(listed, [(0, (1 << 64) - 1), (1, 0xFF)])
        self.assertEqual(got, listed)
        self.assertFalse(stood_in)


@windows_only("Windows only")
class Live(unittest.TestCase):
    def test_windows_lists_this_machines_groups(self):
        self.assertIsNotNone(cpu.listed_groups())
        self.assertEqual(cpu.listed_groups(), cpu.groups())

    def test_every_logical_processor_is_named_once(self):
        ids = cpu.threads()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), os.cpu_count())

    def test_cores_cover_the_threads(self):
        cores = cpu.cores()
        self.assertIsNotNone(cores)
        named = sorted(i for c in cores for i in c["threads"])
        self.assertEqual(named, cpu.threads())

    def test_l3_domains_partition_the_threads(self):
        doms = cpu.l3_domains()
        self.assertTrue(doms)
        self.assertEqual(sorted(i for m in doms for i in cpu.bits(m)), cpu.threads())

    def test_cpu_sets_name_every_thread(self):
        self.assertEqual([c["thread"] for c in cpu.cpu_sets()], cpu.threads())


if __name__ == "__main__":
    unittest.main()
