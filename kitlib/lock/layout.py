"""The machine and its division into slots.

Hardware (detect, or fake_hardware in tests) is what Windows reports, on any machine: logical
processors in every processor group (past 64), physical cores with SMT and efficiency class, L3
domains (a CPU may have several), CPU sets (scheduling class), memory, and display adapters of any vendor
through DXGI (CIM as the fallback). nvidia-smi is never needed to size the pools; it only adds
live use (kitlib.lock.api).

The division is the owner's (machine.json); any key missing takes its default:

    cpu_slot_threads  0: one cpu slot per physical core, its SMT threads together;
                      N: N consecutive threads to a slot
    cpu_slots         the first N slots (default: all)
    threads           the first N logical processors (default: all)
    gpu_share         slots per card, a list in hardware order (default 2 for a card with at
                      least 1 GiB of its own memory, else 0; 0 leaves a card out); or one number
                      for every card
    half_cpu_slots    an owner half hold's cpu slots (default half, rounded up, leaving one)
    half_gpu_slots    its gpu slots (default half, rounded up, at least 1 with a gpu, leaving one)
    agent_cap         the most cpu slots an agent run takes (default half, rounded down, >= 1)
    medium_cores      a medium kind's cpu slots (default 4 threads' worth, within agent_cap)

Every process must count slots the same way, so the layout in force is the store's layout.json.
A changed machine or division is adopted only while nothing is live or queued (in_force), under
the queue mutex; until then the old layout stays and the change is reported as pending. A
layout.json that is missing or invalid is rewritten under the same rule. A look at the hardware
that failed (detect's `failed`) never replaces a layout.json from a good look; with no valid
layout.json its layout is written marked provisional, and replaced under the same rule.
"""
import ctypes
import hashlib
import json
import re
import subprocess
import time
from ctypes import wintypes

from kitlib.lock import kinds
from kitlib.proc import cpu, process

POOLS = ("cpu", "gpu")
DEFAULT_GPU_SHARE = 2           # two trainers share one card
MEDIUM_THREADS = 4              # a medium run: 2 cores of 2 SMT threads (the old medium slot)
COUNT_VRAM_MB = 1024            # a card with less of its own memory is left out by default
DIVISION_KEYS = ("threads", "cpu_slot_threads", "cpu_slots", "gpu_share", "half_cpu_slots",
                 "half_gpu_slots", "agent_cap", "medium_cores")
VENDORS = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x1022: "AMD", 0x8086: "Intel", 0x5143: "Qualcomm",
           0x13B5: "ARM", 0x1414: "Microsoft"}
CACHE_S = 10.0


# ------------------------------------------------------------------ gpus: DXGI, then CIM
class GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                ("d4", ctypes.c_ubyte * 8)]


IID_IDXGIFactory1 = GUID(0x770AAE78, 0xF26F, 0x4DBA,
                         (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", wintypes.WCHAR * 128), ("VendorId", wintypes.UINT),
                ("DeviceId", wintypes.UINT), ("SubSysId", wintypes.UINT),
                ("Revision", wintypes.UINT), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("LuidLow", wintypes.DWORD),
                ("LuidHigh", wintypes.LONG), ("Flags", wintypes.UINT)]


DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ADAPTER_FLAG_SOFTWARE = 2
# vtable slots: IUnknown 0-2 (Release 2); IDXGIFactory1.EnumAdapters1 12; IDXGIAdapter1.GetDesc1 10
_RELEASE, _ENUM_ADAPTERS1, _GET_DESC1 = 2, 12, 10


def _vcall(obj, index, restype, argtypes, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def dxgi_adapters():
    """[{name, vendor_id, device_id, vram_mb, shared_mb, luid, software}] of every adapter DXGI
    lists, in its order (the one driving the main display first). Raises OSError."""
    try:
        create = ctypes.WinDLL("dxgi").CreateDXGIFactory1
    except (OSError, AttributeError) as e:
        raise OSError("dxgi: %s" % e)
    create.restype = ctypes.c_long
    create.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    factory = ctypes.c_void_p()
    hr = create(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory))
    if hr < 0 or not factory.value:
        raise OSError("CreateDXGIFactory1: 0x%08X" % (hr & 0xFFFFFFFF))
    out = []
    try:
        for i in range(64):
            adapter = ctypes.c_void_p()
            hr = _vcall(factory, _ENUM_ADAPTERS1, ctypes.c_long,
                        [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(adapter))
            if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                break
            if hr < 0 or not adapter.value:
                raise OSError("EnumAdapters1(%d): 0x%08X" % (i, hr & 0xFFFFFFFF))
            try:
                d = DXGI_ADAPTER_DESC1()
                hr = _vcall(adapter, _GET_DESC1, ctypes.c_long,
                            [ctypes.POINTER(DXGI_ADAPTER_DESC1)], ctypes.byref(d))
                if hr < 0:
                    raise OSError("GetDesc1(%d): 0x%08X" % (i, hr & 0xFFFFFFFF))
                out.append({"name": d.Description, "vendor_id": d.VendorId,
                            "device_id": d.DeviceId, "vram_mb": d.DedicatedVideoMemory // 2**20,
                            "shared_mb": d.SharedSystemMemory // 2**20,
                            "luid": "%08x:%08x" % (d.LuidHigh & 0xFFFFFFFF, d.LuidLow),
                            "software": bool(d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)})
            finally:
                _vcall(adapter, _RELEASE, ctypes.c_ulong, [])
    finally:
        _vcall(factory, _RELEASE, ctypes.c_ulong, [])
    return out


CIM_PS = ("Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID,AdapterRAM "
          "| ConvertTo-Json -Compress")


def parse_cim(text):
    """Adapters (the dxgi_adapters shape) from Win32_VideoController JSON. AdapterRAM is a 32-bit
    count, so a card past 4 GiB reads as at most 4 GiB here."""
    data = json.loads(text or "[]")
    out = []
    for c in data if isinstance(data, list) else [data]:
        if not isinstance(c, dict):
            continue
        pnp = str(c.get("PNPDeviceID") or "")
        ven = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp)
        dev = re.search(r"DEV_([0-9A-Fa-f]{4})", pnp)
        ram = c.get("AdapterRAM")
        vid = int(ven.group(1), 16) if ven else 0
        out.append({"name": str(c.get("Name") or ""), "vendor_id": vid,
                    "device_id": int(dev.group(1), 16) if dev else 0,
                    "vram_mb": int(ram) // 2**20 if isinstance(ram, int) and ram > 0 else 0,
                    "shared_mb": 0, "luid": None, "software": vid == 0x1414})
    return out


def cim_adapters():
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", CIM_PS],
                           capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError("Win32_VideoController: %s" % e)
    if r.returncode:
        raise OSError("Win32_VideoController: exit %d" % r.returncode)
    try:
        return parse_cim(r.stdout)
    except ValueError as e:
        raise OSError("Win32_VideoController: %s" % e)


def gpu_entries(adapters, source):
    """The cards of `adapters` the lock can divide: software and Microsoft's render adapters are
    left out. Each gets its index here, vendor, and default_share (DEFAULT_GPU_SHARE with at least
    COUNT_VRAM_MB of its own memory, else 0: an integrated gpu is listed but not shared)."""
    out = []
    for a in adapters:
        if a.get("software") or a.get("vendor_id") == 0x1414:
            continue
        out.append(dict(a, index=len(out), source=source,
                        vendor=VENDORS.get(a.get("vendor_id"), "0x%04X" % (a.get("vendor_id") or 0)),
                        default_share=DEFAULT_GPU_SHARE if a.get("vram_mb", 0) >= COUNT_VRAM_MB else 0))
        out[-1].pop("software", None)
    return out


# ------------------------------------------------------------------ hardware
def detect():
    """This machine, as a JSON-ready dict (thread ids, never masks: a mask past 53 bits does not
    survive a JavaScript reader). Never raises: what cannot be read is in `problems`, and `failed`
    names the parts read as a stand-in rather than from Windows ("groups": the processor groups
    were not listed (cpu.listed_groups), so group 0 alone stands in, as cpu.groups() would, and the
    threads are that group's; "cores": each thread counted as one; "gpus": neither DXGI nor CIM answered, so
    no card). A layout from such a look never replaces a layout.json from a good look, and is
    otherwise written provisional (in_force). CIM answering for DXGI is not a failed look. One
    listing of the groups gives both `groups` and `threads`, so the two never disagree, and a
    failed listing is not asked again: a second answer would mark "groups" failed over every group."""
    problems, failed = [], []
    listed = cpu.listed_groups()
    if listed is None:
        problems.append("Windows did not list the processor groups; group 0 alone stands in")
        failed.append("groups")
    grps = listed or [(0, process.system_mask())]  # cpu.groups()'s stand-in, without asking again
    ids = [cpu.gid(g, i) for g, m in grps for i in range(cpu.GROUP_BITS) if m >> i & 1]
    cores = cpu.cores()
    if cores is None:
        problems.append("Windows did not list the physical cores; each thread counts as one")
        failed.append("cores")
        cores = [{"threads": [i], "smt": False, "efficiency": 0} for i in ids]
    try:
        gpus, gsrc = gpu_entries(dxgi_adapters(), "dxgi"), "dxgi"
    except OSError as e:
        problems.append("DXGI: %s; CIM used" % e)
        try:
            gpus, gsrc = gpu_entries(cim_adapters(), "cim"), "cim"
        except OSError as e2:
            problems.append(str(e2))
            failed.append("gpus")
            gpus, gsrc = [], "none"
    mem = process.memory_mb() or {}
    return {"threads": ids,
            "groups": [{"group": g, "threads": [cpu.gid(g, i) for i in range(64) if m >> i & 1]}
                       for g, m in grps],
            "cores": [{"threads": sorted(c["threads"]), "smt": bool(c["smt"]),
                       "efficiency": c["efficiency"]} for c in cores],
            "l3": [cpu.bits(m) for m in cpu.l3_domains()], "cpu_sets": cpu.cpu_sets(),
            "memory_mb": mem.get("total_mb"), "gpus": gpus,
            "source": {"cpu": "windows", "gpu": gsrc}, "problems": problems, "failed": failed}


def fake_hardware(threads=24, smt=2, l3=1, gpus=1, vram_mb=12288, group_size=64,
                  efficiency=None, sched=None, memory_mb=65536, gpu_vendor=0x10DE):
    """A machine for tests: `threads` threads in groups of group_size, smt threads to a core,
    l3 equal L3 domains, `gpus` cards (an int, or a list of {name, vendor_id, vram_mb}),
    efficiency/sched: {thread: class} for the CPU sets."""
    ids = []
    for t in range(threads):
        g, b = divmod(t, group_size)
        ids.append(cpu.gid(g, b))
    smt = max(1, smt)
    cores = [{"threads": ids[i:i + smt], "smt": smt > 1, "efficiency": 0}
             for i in range(0, len(ids), smt)]
    for c in cores:
        c["efficiency"] = max((efficiency or {}).get(t, 0) for t in c["threads"])
    per = -(-len(cores) // max(1, l3))
    l3s = [[t for c in cores[i:i + per] for t in c["threads"]] for i in range(0, len(cores), per)]
    sets = []
    for ci, c in enumerate(cores):
        for t in c["threads"]:
            sets.append({"id": 256 + len(sets), "thread": t, "core": ci,
                         "llc": next(k for k, d in enumerate(l3s) if t in d),
                         "efficiency": (efficiency or {}).get(t, 0),
                         "sched": (sched or {}).get(t, 0), "parked": False})
    if isinstance(gpus, int):
        gpus = [{"name": "Fake GPU %d" % i, "vendor_id": gpu_vendor, "vram_mb": vram_mb}
                for i in range(gpus)]
    adapters = [dict({"device_id": 0, "shared_mb": 0, "luid": "fake:%d" % i, "software": False},
                     **g) for i, g in enumerate(gpus)]
    groups = {}
    for t in ids:
        groups.setdefault(t // 64, []).append(t)
    return {"threads": ids, "groups": [{"group": g, "threads": ts} for g, ts in sorted(groups.items())],
            "cores": cores, "l3": l3s, "cpu_sets": sets, "memory_mb": memory_mb,
            "gpus": gpu_entries(adapters, "fake"), "source": {"cpu": "fake", "gpu": "fake"},
            "problems": [], "failed": []}


def hardware(store):
    """store.hardware when the store was given one (tests), else detect(), cached CACHE_S."""
    if store.hardware is not None:
        return store.hardware
    c = getattr(store, "_hw_cache", None)
    if c is None or time.monotonic() - c[0] > CACHE_S:
        c = store._hw_cache = (time.monotonic(), detect())
    return c[1]


def preferred_threads(hw):
    """Threads ranked best first: higher efficiency class, then higher scheduling class (where
    Windows publishes a preferred-core order), then lower id."""
    score = {c["thread"]: (c.get("efficiency", 0), c.get("sched", 0)) for c in hw.get("cpu_sets") or ()}
    return sorted(hw["threads"], key=lambda t: (-score.get(t, (0, 0))[0], -score.get(t, (0, 0))[1], t))


# ------------------------------------------------------------------ the division
def _int(d, k, lo, hi, default):
    x = d.get(k, default)
    if type(x) is not int:
        raise ValueError("%s must be a whole number, not %r" % (k, x))
    if not lo <= x <= hi:
        raise ValueError("%s %d is outside %d-%d on this machine" % (k, x, lo, hi))
    return x


def derive(hw, division=None):
    """The layout for hardware hw and division (machine.json; None or {} for every default).
    Raises ValueError for a division it cannot use."""
    d = {} if division is None else division
    if not isinstance(d, dict):
        raise ValueError("the division must be a JSON object")
    bad = sorted(k for k in d if k not in DIVISION_KEYS)
    if bad:
        raise ValueError("unknown key %s (known: %s)" % (", ".join(bad), ", ".join(DIVISION_KEYS)))
    ids = list(hw["threads"])
    if not ids:
        raise ValueError("no threads")
    t = _int(d, "threads", 1, len(ids), len(ids))
    ids = ids[:t]
    have = set(ids)
    cst = _int(d, "cpu_slot_threads", 0, t, 0)
    if cst:
        slots = [ids[i:i + cst] for i in range(0, len(ids), cst)]
    else:
        slots = [sorted(i for i in c["threads"] if i in have) for c in hw["cores"]]
        slots = [s for s in slots if s]
        named = {i for s in slots for i in s}
        slots += [[i] for i in ids if i not in named]
        slots.sort()
    n = _int(d, "cpu_slots", 1, len(slots), len(slots))
    slots = slots[:n]
    cards = hw.get("gpus") or []
    share = d.get("gpu_share", [c["default_share"] for c in cards])
    if type(share) is int:
        share = [share] * len(cards)
    if (not isinstance(share, list) or len(share) != len(cards)
            or any(type(x) is not int or not 0 <= x <= 16 for x in share)):
        raise ValueError("gpu_share must be one number 0-16 or a list of %d (one per card)"
                         % len(cards))
    gpu_cards = [c["index"] for c, k in zip(cards, share) for _ in range(k)]
    real = bool(gpu_cards)
    if not gpu_cards:
        gpu_cards = [None]              # one slot, so gpu kinds still queue on a gpu-less machine
    gs = len(gpu_cards)
    hc = -(-n // 2)
    hc = min(hc, n - 1) if n >= 2 else n
    hg = 0 if not real else max(1, -(-gs // 2))
    hg = min(hg, gs - 1) if gs >= 2 else hg
    hc = _int(d, "half_cpu_slots", 1, n, hc)
    hg = _int(d, "half_gpu_slots", 0, gs, hg)
    cap = _int(d, "agent_cap", 1, n, max(1, n // 2))
    width = max(len(s) for s in slots)
    med = _int(d, "medium_cores", 1, cap, min(cap, max(1, MEDIUM_THREADS // width)))
    masks = [cpu.bits_mask(s) for s in slots]
    l3 = [sorted(i for i, s in enumerate(slots) if set(s) & set(dom)) for dom in hw.get("l3") or ()]
    used = {"threads": t, "cpu_slot_threads": cst, "cpu_slots": n, "gpu_share": share,
            "half_cpu_slots": hc, "half_gpu_slots": hg, "agent_cap": cap, "medium_cores": med}
    out = {"threads": t, "cpu_slots": n, "cpu_slot_threads": width, "slots_cpu": slots,
           "cpu_masks": masks, "every": cpu.bits_mask(i for s in slots for i in s),
           "gpu_slots": gs, "gpu_cards": gpu_cards, "gpus": len({c for c in gpu_cards if c is not None}),
           "half": {"cpu": hc, "gpu": hg}, "agent_cap": cap, "medium_cores": med,
           "l3_slots": [x for x in l3 if x], "division": used}
    out["key"] = layout_key(out)
    return out


def default_division(hw):
    return derive(hw)["division"]


def layout_key(L):
    """A short fingerprint: every run and queue entry counts slots by index, so two processes
    may only share a store with the same key."""
    return "t%d cpu %d x %d gpu %d m%s" % (
        L["threads"], L["cpu_slots"], L["cpu_slot_threads"], L["gpu_slots"],
        hashlib.sha1(json.dumps([L["slots_cpu"], L["gpu_cards"], L["half"], L["agent_cap"],
                                 L["medium_cores"]]).encode()).hexdigest()[:8])


def slot_key(L):
    """The run record's `layout`: a label of the slot numbering (threads, gpus, cpu slots and
    their threads, gpu slots). It keeps the installed machine_lock.py's format and hash inputs,
    for the records. Choices that renumber no slot (half, agent_cap, medium_cores) are left out,
    as there; layout_key, layout.json's adoption key, covers them. Nothing here compares it to
    decide anything: other_labels reports a live record that carries another one."""
    every = L["every"]
    return "t%d g%d cpu %d x %d gpu %d m%s" % (
        L["threads"], L["gpus"], L["cpu_slots"], L["cpu_slot_threads"], L["gpu_slots"],
        hashlib.sha1(json.dumps([L["cpu_masks"], every, every, L["slots_cpu"]])
                     .encode()).hexdigest()[:8])


def other_labels(records, L):
    """The slot labels (records' `layout`) other than slot_key(L), each once, sorted. Every
    writer in this kit labels a record from the one layout in force (layout.json, from DXGI or
    CIM detection), so another label means an older writer that counts this machine's cpu or
    gpu slots its own way (the installed machine_lock.py counts gpus with nvidia-smi). A
    record with no label, or one that is not text, names nothing."""
    own = slot_key(L)
    labels = (r.get("layout") for r in records)
    return sorted({x for x in labels if isinstance(x, str) and x and x != own})


def read_division(store):
    """(division or None, warning or None) from the store's machine.json."""
    path = store.path("machine.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, "%s is not used (%s); the default division applies" % (path, e)
    if not isinstance(d, dict):
        return None, "%s is not used (it must hold a JSON object); the default division applies" % path
    return {k: v for k, v in d.items() if not k.startswith("_")}, None


def division_notes(store):
    """The keys starting with _ (notes such as _comment) in the store's machine.json; {} when it
    is missing or unreadable."""
    try:
        with open(store.path("machine.json"), encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if k.startswith("_")} if isinstance(d, dict) else {}


def wanted(store, hw=None):
    """The layout this machine and the owner's division give now: (layout, source, warning). A
    machine.json holding only notes gives the default."""
    hw = hw or hardware(store)
    div, warning = read_division(store)
    if div:
        try:
            return derive(hw, div), "config", warning
        except ValueError as e:
            warning = "%s is not used (%s); the default division applies" % (
                store.path("machine.json"), e)
    return derive(hw), "default", warning


def valid_layout(L):
    try:
        return (isinstance(L, dict) and L.get("key") == layout_key(L)
                and len(L["cpu_masks"]) == L["cpu_slots"] and len(L["gpu_cards"]) == L["gpu_slots"])
    except (KeyError, TypeError):
        return False


# How in_force's `pending` texts start or end, one per reason (kitlib.lock.status.pending_reason
# reads the reason from these, so the words and the reason never drift apart).
PENDING_IDLE = "the machine or division changed"                    # starts: waits for idle
PENDING_UNWRITTEN = "layout.json is missing, unreadable or not valid to this kit version"  # starts
PENDING_FAILED_LOOK = "; no layout is adopted from that look until one succeeds"          # ends
PENDING_PROVISIONAL = ("; the layout in use came from such a look and is provisional: the first "
                       "look that succeeds replaces it while no run is live or queued")   # ends
PENDING_UNCHECKED = "the layout could not be checked"                                    # starts


def in_force(store, adopt=True):
    """{layout, wanted, source, warning, pending}: the layout every process uses now (the store's
    layout.json) and the one this machine and division ask for. A different wanted layout is
    adopted only while no run is live or queued (never under live runs), under the queue
    mutex, so a run in the queue never sees its slots renumbered; until then `pending` says why
    it waits. A layout.json that is missing, unreadable or invalid to this version waits the same
    way: the runs live now count their slots from it, so it is rewritten only once none is, and
    until then the wanted layout is used without being written (and recomputed on each call, so
    a division change applies to it at once). A look at the hardware that failed (detect's
    `failed`) never replaces a layout.json written from a good look. With no valid layout.json it
    is written as before, marked `provisional`, so the runs booked on it keep their numbering; a
    provisional layout.json is replaced under the same rule as any other (by a good look, or by
    another failed look such as a division change), and a good look with its key makes it final.
    adopt False: never write (a read-only look)."""
    hw = hardware(store)
    want, source, warning = wanted(store, hw)
    failed = list(hw.get("failed") or [])
    why = None
    if failed:
        parts = ", ".join(failed[:-1]) + " and " + failed[-1] if len(failed) > 1 else failed[0]
        why = "the %s could not be read (%s)" % (parts,
                                                 "; ".join(hw.get("problems") or ()) or "no detail")
    provisional = why and why + PENDING_PROVISIONAL

    def settled(c):
        """Nothing to write: c is the wanted layout, final unless this look failed too."""
        return valid_layout(c) and c["key"] == want["key"] and (failed or not c.get("provisional"))

    def kept(c):
        """A failed look does not replace c, a layout.json from a good look."""
        return failed and valid_layout(c) and not c.get("provisional") and c["key"] != want["key"]

    cur = store.read("layout.json")
    out = {"layout": want, "wanted": want, "source": source, "warning": warning, "pending": None}
    if settled(cur):
        out.update(pending=provisional if cur.get("provisional") else None)
        return out
    if kept(cur):
        out.update(layout=cur, pending=why + PENDING_FAILED_LOOK)
        return out
    if not adopt:
        if valid_layout(cur) and cur["key"] != want["key"]:
            out.update(layout=cur, pending=PENDING_IDLE + "; it is used once no run is live or "
                                                          "queued")
        elif not valid_layout(cur):
            out.update(pending=provisional)
        return out
    from kitlib.lock import queue as q_mod
    try:
        with store.lock("queue", store.settings.queue_lock_s):
            cur = store.read("layout.json")
            if settled(cur):
                out.update(pending=provisional if cur.get("provisional") else None)
                return out
            if kept(cur):
                out.update(layout=cur, pending=why + PENDING_FAILED_LOOK)
                return out
            same = valid_layout(cur) and cur["key"] == want["key"]  # only the mark goes: no renumbering
            busy = [] if same else [e["id"] for e in q_mod.load(store)["entries"]
                                    if not store.run_dead(e)]
            busy += [] if same else [r["id"] for r in store.live_runs() if r["id"] not in busy]
            if busy and valid_layout(cur):
                out.update(layout=cur, pending=PENDING_IDLE + " (%s -> %s); it is used once no "
                           "run is live or queued (%d now)" % (cur["key"], want["key"], len(busy)))
                return out
            if busy:
                out.update(pending=PENDING_UNWRITTEN + "; %s is written once no run is live or "
                                   "queued (%d now)" % (want["key"], len(busy)))
                return out
            store.write("layout.json", dict(want, adopted_at=time.time(),
                                            **({"provisional": True} if failed else {})))
            out.update(pending=provisional)
    except (OSError, q_mod.QueueUnreadable) as e:
        out.update(layout=cur if valid_layout(cur) else want,
                   pending=PENDING_UNCHECKED + " (%s)" % e)
    return out


# ------------------------------------------------------------------ needs and masks
def kind_cores(L, kind, whole=False):
    """The cpu slots kind entry `kind` books, its size: a whole number N is N, never
    past agent_cap (whole or not); the aliases keep their old sizes: medium is medium_cores,
    heavy agent_cap, or every slot inside an owner whole hold (whole). Any other value is a
    KindsError (fail closed, as kinds.load)."""
    cpu = kind["cpu"]
    if cpu == "medium":
        return L["medium_cores"]
    if cpu == "heavy":
        return L["cpu_slots"] if whole else L["agent_cap"]
    if not kinds.is_size(cpu):      # a table that skipped kinds.load: fail closed, not int()
        raise kinds.KindsError("cpu must be medium or heavy, or a whole number of cores from 1, "
                               "not %r" % (cpu,))
    return min(cpu, L["agent_cap"])


def need_for(L, kind, gpu_share=None, cores=None, whole=False):
    """{'cpu': cpu slots, 'gpu': gpu slots} a run of kind entry `kind` (kinds.load()[name]) asks
    for. cpu: kind_cores; gpu pool: every gpu slot, or one with gpu_share 'training' on a
    shareable kind. `cores` sets the cpu count instead (book() has checked it is 1..the kind's
    size)."""
    need = {"cpu": kind_cores(L, kind, whole), "gpu": 0}
    if kind["gpu"] == "pool":
        need["gpu"] = 1 if gpu_share == "training" and kind["share"] else L["gpu_slots"]
    if cores is not None:
        need["cpu"] = cores
    return need


def size(L, pool):
    return L["cpu_slots" if pool == "cpu" else "gpu_slots"]


def cores_mask(L, slots):
    m = 0
    for i in slots:
        if 0 <= i < len(L["cpu_masks"]):
            m |= L["cpu_masks"][i]
    return m


def gpu_cards_of(L, slots):
    return sorted({L["gpu_cards"][i] for i in slots
                   if 0 <= i < len(L["gpu_cards"]) and L["gpu_cards"][i] is not None})


def need_text(need):
    out = []
    if need.get("cpu"):
        out.append("%d core%s" % (need["cpu"], "" if need["cpu"] == 1 else "s"))
    if need.get("gpu"):
        out.append("%d gpu slot%s" % (need["gpu"], "" if need["gpu"] == 1 else "s"))
    return " + ".join(out) or "nothing"


def slots_text(pool, slots):
    slots = sorted(slots)
    if not slots:
        return "no %s slot" % pool
    if len(slots) == 1:
        return "%s slot %d" % (pool, slots[0])
    return "%s slots %s" % (pool, cpu.mask_text(cpu.bits_mask(slots)))


THREADS_RE = re.compile(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?")
MAX_THREAD = 64 * 64 - 1        # Windows: at most 64 processor groups of 64


def parse_threads(text):
    """A Linux cpuset list of global thread ids ('8-15,20', docker's --cpuset-cpus) -> the sorted
    ids. ValueError for anything else: an empty list, an open or backwards range, a stray comma,
    an id past MAX_THREAD."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a thread list is like 8-15,20, not %r" % (text,))
    got = set()
    for part in text.split(","):
        m = THREADS_RE.fullmatch(part)
        if not m:
            raise ValueError("a thread list is like 8-15,20, not %r" % text)
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if hi < lo:
            raise ValueError("%s: the range runs backwards" % part.strip())
        if hi > MAX_THREAD:
            raise ValueError("%d: past the last thread id Windows has (%d)" % (hi, MAX_THREAD))
        got.update(range(lo, hi + 1))
    return sorted(got)


def within_slots(L, threads):
    """The cpu slots of layout L whose threads all lie in `threads` (a slot only partly inside is
    left out)."""
    have = set(threads)
    return [i for i, ts in enumerate(L["slots_cpu"]) if ts and set(ts) <= have]
