"""Processor topology across every processor group (any Windows machine, past 64 logical
processors). A thread is named by one global id, group * 64 + its bit in the group's mask, so a
set of threads is one Python int (a mask of any width) and group 0 keeps the ids it always had.
"""
import ctypes
import struct
from ctypes import wintypes

from . import win32
from .win32 import K32, DW

GROUP_BITS = 64
RELATION_PROCESSOR_CORE, RELATION_GROUP = 0, 4


def gid(group, bit):
    return group * GROUP_BITS + bit


def split(mask):
    """{group: 64-bit mask} of a global mask."""
    out, g = {}, 0
    while mask:
        m = mask & ((1 << GROUP_BITS) - 1)
        if m:
            out[g] = m
        mask >>= GROUP_BITS
        g += 1
    return out


def bits(mask):
    """The global ids set in mask, ascending."""
    out, i = [], 0
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out


def bits_mask(ids):
    m = 0
    for i in ids:
        m |= 1 << i
    return m


def _query(relation):
    """The raw SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX records for relation, or None."""
    n = DW(0)
    K32.GetLogicalProcessorInformationEx(relation, None, ctypes.byref(n))
    if ctypes.get_last_error() != win32.ERROR_INSUFFICIENT_BUFFER or not n.value:
        return None
    buf = ctypes.create_string_buffer(n.value)
    if not K32.GetLogicalProcessorInformationEx(relation, buf, ctypes.byref(n)):
        return None
    return buf.raw[:n.value]


def _records(raw, relation):
    """(offset, size) of each record of `relation` in raw. Each record: Relationship DWORD @0,
    Size DWORD @4, then the relation's body @8."""
    off = 0
    while off + 8 <= len(raw):
        rel, size = struct.unpack_from("<II", raw, off)
        if size < 8 or off + size > len(raw):
            break
        if rel == relation:
            yield off, size
        off += size


def parse_cores(raw):
    """[{"threads": [global ids], "smt": bool, "efficiency": n}] of each physical core in raw
    (RelationProcessorCore records): PROCESSOR_RELATIONSHIP is Flags BYTE @8 (1: SMT),
    EfficiencyClass BYTE @9, GroupCount WORD @30, GroupMask[] @32, each a GROUP_AFFINITY of 16
    bytes (Mask QWORD @0, Group WORD @8). Sorted by lowest thread."""
    out = []
    for off, size in _records(raw, RELATION_PROCESSOR_CORE):
        if size < 48:
            continue
        flags, eff = struct.unpack_from("<BB", raw, off + 8)
        count = struct.unpack_from("<H", raw, off + 30)[0]
        threads = []
        for g in range(count):
            at = off + 32 + 16 * g
            if at + 10 > off + size:
                break
            mask, group = struct.unpack_from("<QH", raw, at)
            threads += [gid(group, i) for i in range(GROUP_BITS) if mask >> i & 1]
        if threads:
            out.append({"threads": sorted(threads), "smt": bool(flags & 1), "efficiency": eff})
    return sorted(out, key=lambda c: c["threads"][0])


def parse_groups(raw):
    """[(group, active mask)] from a RelationGroup record: GROUP_RELATIONSHIP is
    MaximumGroupCount WORD @8, ActiveGroupCount WORD @10, then GroupInfo[] @32, each a
    PROCESSOR_GROUP_INFO of 48 bytes (MaximumProcessorCount BYTE @0, ActiveProcessorCount BYTE @1,
    ActiveProcessorMask QWORD @40)."""
    for off, size in _records(raw, RELATION_GROUP):
        active = struct.unpack_from("<H", raw, off + 10)[0]
        out = []
        for g in range(active):
            at = off + 32 + 48 * g
            if at + 48 > off + size:
                break
            out.append((g, struct.unpack_from("<Q", raw, at + 40)[0]))
        return out
    return []


def cores():
    """The machine's physical cores (parse_cores), every group; None when Windows will not say."""
    raw = _query(RELATION_PROCESSOR_CORE)
    return (parse_cores(raw) or None) if raw else None


def groups():
    """[(group, active mask)] of every active processor group; [(0, system mask)] when Windows
    will not say."""
    raw = _query(RELATION_GROUP)
    out = parse_groups(raw) if raw else []
    if not out:
        from .process import system_mask
        out = [(0, system_mask())]
    return out


def threads():
    """Every active logical processor's global id, ascending."""
    return [gid(g, i) for g, m in groups() for i in range(GROUP_BITS) if m >> i & 1]


def all_mask():
    return bits_mask(threads())


def mask_text(mask):
    """A Linux cpuset list of a global mask: '0-3,8', '' for none."""
    ids, parts, i = bits(mask), [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        parts.append(str(ids[i]) if i == j else "%d-%d" % (ids[i], ids[j]))
        i = j + 1
    return ",".join(parts)


RELATION_CACHE = 2


def parse_l3(raw):
    """[global mask] of each L3 cache domain in raw (RelationCache records), lowest thread first.
    CACHE_RELATIONSHIP is Level BYTE @8, Type DWORD @16, GroupCount WORD @38 (0 before Windows 11:
    one group), GroupMasks[] @40, each a GROUP_AFFINITY of 16 bytes. On a Ryzen each L3 is one
    CCD (game mode books one)."""
    out = set()
    for off, size in _records(raw, RELATION_CACHE):
        if size < 56 or raw[off + 8] != 3:
            continue
        count = max(1, struct.unpack_from("<H", raw, off + 38)[0])
        mask = 0
        for g in range(count):
            at = off + 40 + 16 * g
            if at + 10 > off + size:
                break
            m, group = struct.unpack_from("<QH", raw, at)
            mask |= m << (group * GROUP_BITS)
        if mask:
            out.add(mask)
    return sorted(out, key=lambda m: bits(m)[0])


def l3_domains():
    """The machine's L3 domains (parse_l3); [] when Windows will not say."""
    raw = _query(RELATION_CACHE)
    return parse_l3(raw) if raw else []


def parse_cpu_sets(raw):
    """[{"id", "thread" (global id), "core", "llc", "efficiency", "sched", "parked"}] of each
    SYSTEM_CPU_SET_INFORMATION in raw: Size DWORD @0, Type DWORD @4 (0: CpuSetInformation), Id
    DWORD @8, Group WORD @12, LogicalProcessorIndex BYTE @14, CoreIndex @15, LastLevelCacheIndex
    @16, NumaNodeIndex @17, EfficiencyClass @18, AllFlags @19 (1: parked), SchedulingClass BYTE @20.
    EfficiencyClass is higher on faster cores of a hybrid CPU; SchedulingClass is Windows' relative
    performance class of a core where it publishes one (0 everywhere when it does not)."""
    out, off = [], 0
    while off + 8 <= len(raw):
        size, typ = struct.unpack_from("<II", raw, off)
        if size < 8 or off + size > len(raw):
            break
        if typ == 0 and size >= 24:
            sid, group, lp, core, llc, _numa, eff, flags, sched = struct.unpack_from(
                "<IHBBBBBBB", raw, off + 8)
            out.append({"id": sid, "thread": gid(group, lp), "core": core, "llc": llc,
                        "efficiency": eff, "sched": sched, "parked": bool(flags & 1)})
        off += size
    return sorted(out, key=lambda c: c["thread"])


def cpu_sets():
    """parse_cpu_sets of this machine; [] when Windows will not say (before Windows 10)."""
    n = wintypes.ULONG(0)
    try:
        K32.GetSystemCpuSetInformation(None, 0, ctypes.byref(n), None, 0)
    except AttributeError:
        return []
    if not n.value:
        return []
    buf = ctypes.create_string_buffer(n.value)
    if not K32.GetSystemCpuSetInformation(buf, n.value, ctypes.byref(n), None, 0):
        return []
    return parse_cpu_sets(buf.raw[:n.value])
