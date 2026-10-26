"""The machine as Linux shows it in /sys and /proc (docs/linux-support-plan.md section 2.4), for
the lock's hardware look (kitlib.lock.layout.detect_linux) and the seam's hardware primitives.

Every reader takes `root`, the folder /sys and /proc hang from ("/" on a live machine), so a
test points it at a synthetic tree. A reader returns facts and imports nothing from kitlib.lock;
an unreadable or malformed file raises OSError or ValueError for the caller to report.

  allowed      the ids this process may run on: sched_getaffinity(0), intersected with
               cpu/online and with the cpuset.cpus.effective of its own cgroup (v2). A container
               sees the host's CPUs in /sys while its cgroup allows fewer, so /sys alone is never
               the answer. Ids keep their gaps.
  cores        SMT siblings grouped from core_cpus_list (thread_siblings_list before Linux 5.6),
               cut to the allowed ids, each id in exactly one core. Efficiency class: 1 for
               cpu_core and 0 for cpu_atom on a hybrid part; else cpu_capacity ranked into
               classes; else 0 everywhere.
  l3           each L3 domain (cache/indexK with level 3, its shared_cpu_list), cut and unique.
  memory       {total_mb, free_mb} from /proc/meminfo, capped by the smallest memory.max of the
               process's cgroup and its parents.
  adapters     the drm cards (the shape of kitlib.lock.layout.dxgi_adapters): PCI vendor and
               device, amdgpu's mem_info_vram_total, NVIDIA's name and VRAM from nvidia-smi by
               bus id, else its name from /proc/driver/nvidia with VRAM unknown (0).
"""
import os
import re
import subprocess

ROOT = "/"
NVIDIA = 0x10DE
SMI = ["nvidia-smi", "--query-gpu=pci.bus_id,name,memory.total", "--format=csv,noheader"]
_CARD = re.compile(r"card(\d+)$")


def _path(root, rel):
    return os.path.join(root, *rel.strip("/").split("/"))


def _read(root, rel):
    with open(_path(root, rel), encoding="ascii", errors="replace") as f:
        return f.read()


def _maybe(root, rel):
    """The file's text, or None when it is not there."""
    try:
        return _read(root, rel)
    except FileNotFoundError:
        return None


def parse_cpulist(text):
    """The ids of a kernel cpu list ("0-3,8,10-11"), ascending. Raises ValueError."""
    out = set()
    for part in text.strip().split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        lo, hi = int(lo), int(hi or lo)
        if hi < lo:
            raise ValueError("cpu list %r" % text)
        out.update(range(lo, hi + 1))
    return sorted(out)


def _affinity():
    get = getattr(os, "sched_getaffinity", None)
    return set(get(0)) if get else None


def own_cgroup(root):
    """The process's cgroup v2 path ("/" for the root), or None without a unified hierarchy."""
    text = _maybe(root, "proc/self/cgroup")
    for line in (text or "").splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    return None


def _cgroups_up(root):
    """The folder of the process's cgroup, then each parent up to the hierarchy's root."""
    path = own_cgroup(root)
    if path is None:
        return []
    parts = [p for p in path.split("/") if p]
    return ["sys/fs/cgroup/" + "/".join(parts[:n]) for n in range(len(parts), -1, -1)]


def cpuset(root):
    """The ids the process's cgroup allows (cpuset.cpus.effective), or None when not given."""
    up = _cgroups_up(root)
    text = _maybe(root, up[0] + "/cpuset.cpus.effective") if up else None
    return set(parse_cpulist(text)) if text and text.strip() else None


def allowed(root=ROOT, affinity=None):
    """The ids this process may run on, ascending. affinity None: sched_getaffinity(0) where the
    platform has it. Raises OSError or ValueError when cpu/online cannot be read."""
    ids = set(parse_cpulist(_read(root, "sys/devices/system/cpu/online")))
    for cut in (affinity if affinity is not None else _affinity(), cpuset(root)):
        if cut is not None:
            ids &= set(cut)
    return sorted(ids)


def standin(affinity=None):
    """The ids when cpu/online cannot be read: the affinity, else os.cpu_count()'s first ids."""
    a = affinity if affinity is not None else _affinity()
    return sorted(a) if a else list(range(os.cpu_count() or 1))


def _siblings(root, n):
    base = "sys/devices/system/cpu/cpu%d/topology/" % n
    text = _maybe(root, base + "core_cpus_list")
    if text is None:
        text = _maybe(root, base + "thread_siblings_list")
    return None if text is None else parse_cpulist(text)


def _classes(root, ids):
    """{id: efficiency class}: hybrid cpu_core 1 / cpu_atom 0, else ranked cpu_capacity, else 0."""
    big, little = _maybe(root, "sys/devices/cpu_core/cpus"), _maybe(root, "sys/devices/cpu_atom/cpus")
    if big is not None and little is not None:
        fast = set(parse_cpulist(big))
        return {i: 1 if i in fast else 0 for i in ids}
    caps = {}
    for i in ids:
        text = _maybe(root, "sys/devices/system/cpu/cpu%d/cpu_capacity" % i)
        if text is None:
            return {i: 0 for i in ids}
        caps[i] = int(text)
    rank = {c: k for k, c in enumerate(sorted(set(caps.values())))}
    return {i: rank[caps[i]] for i in ids}


def cores(root, ids):
    """([{threads, smt, efficiency, package}], missing): each allowed id in exactly one core (the
    lowest id's sibling list wins where two disagree), ordered by package then first id; missing
    are the ids whose topology could not be read, each counted as a core of its own."""
    have = set(ids)
    done, out, missing = set(), [], []
    eff = _classes(root, ids)
    for i in ids:
        if i in done:
            continue
        sib = _siblings(root, i)
        if sib is None:
            missing.append(i)
            sib = [i]
        group = sorted(({i} | (set(sib) & have)) - done)
        done.update(group)
        pkg = _maybe(root, "sys/devices/system/cpu/cpu%d/topology/physical_package_id" % i)
        out.append({"threads": group, "smt": len(sib) > 1,
                    "efficiency": max(eff.get(t, 0) for t in group),
                    "package": int(pkg) if pkg and pkg.strip() else 0})
    out.sort(key=lambda c: (c["package"], c["threads"][0]))
    return out, missing


def l3(root, ids):
    """[[ids]] of each L3 domain, cut to `ids`, unique, ordered by first id."""
    have, seen = set(ids), set()
    for i in ids:
        base = "sys/devices/system/cpu/cpu%d/cache" % i
        try:
            names = sorted(os.listdir(_path(root, base)))
        except FileNotFoundError:
            continue
        for name in names:
            if not name.startswith("index"):
                continue
            level = _maybe(root, "%s/%s/level" % (base, name))
            shared = _maybe(root, "%s/%s/shared_cpu_list" % (base, name))
            if level and level.strip() == "3" and shared:
                dom = tuple(sorted(set(parse_cpulist(shared)) & have))
                if dom:
                    seen.add(dom)
    return [list(d) for d in sorted(seen)]


def _meminfo(text):
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            out[key.strip()] = int(fields[0])
    if "MemTotal" not in out:
        raise ValueError("/proc/meminfo names no MemTotal")
    return out


def memory(root=ROOT):
    """{total_mb, free_mb}, capped by the cgroup's memory.max. Raises OSError or ValueError."""
    info = _meminfo(_read(root, "proc/meminfo"))
    total = info["MemTotal"] // 1024
    free = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
    up = _cgroups_up(root)
    caps = []
    for where in up:
        text = (_maybe(root, where + "/memory.max") or "").strip()
        if text.isdigit():
            caps.append(int(text) // 2 ** 20)
    if caps:
        cap = min(caps)
        total = min(total, cap)
        used = (_maybe(root, up[0] + "/memory.current") or "").strip()
        if used.isdigit():
            free = min(free, max(0, cap - int(used) // 2 ** 20))
    return {"total_mb": total, "free_mb": min(free, total)}


def _bus(text):
    """A PCI bus id in one form: 'dddd:bb:dd.f', lower case (nvidia-smi gives 8 domain digits)."""
    m = re.match(r"\s*([0-9A-Fa-f]+):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\.([0-7])\s*$", text or "")
    if not m:
        return None
    return "%04x:%s:%s.%s" % (int(m.group(1), 16), m.group(2).lower(), m.group(3).lower(), m.group(4))


def _uevent_slot(root, card):
    for line in (_maybe(root, "sys/class/drm/%s/device/uevent" % card) or "").splitlines():
        if line.startswith("PCI_SLOT_NAME="):
            return _bus(line.split("=", 1)[1])
    try:
        return _bus(os.path.basename(os.path.realpath(_path(root, "sys/class/drm/%s/device" % card))))
    except OSError:
        return None


def nvidia_smi(run=None):
    """{bus id: (name, vram_mb)} from nvidia-smi. Raises OSError when it cannot answer."""
    run = run or subprocess.run
    try:
        r = run(SMI, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError("nvidia-smi: %s" % e)
    if r.returncode:
        raise OSError("nvidia-smi: exit %d" % r.returncode)
    out = {}
    for line in (r.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        mem = re.match(r"(\d+)\s*MiB", parts[2])
        bus = _bus(parts[0])
        if bus:
            out[bus] = (parts[1], int(mem.group(1)) if mem else 0)
    return out


def proc_nvidia(root):
    """{bus id: name} from /proc/driver/nvidia/gpus/*/information (no VRAM is given there)."""
    base = "proc/driver/nvidia/gpus"
    try:
        names = sorted(os.listdir(_path(root, base)))
    except FileNotFoundError:
        return {}
    out = {}
    for d in names:
        info = _maybe(root, "%s/%s/information" % (base, d)) or ""
        model = re.search(r"^Model:\s*(.+?)\s*$", info, re.M)
        bus = re.search(r"^Bus Location:\s*(\S+)", info, re.M)
        key = _bus(bus.group(1) if bus else d)
        if key and model:
            out[key] = model.group(1)
    return out


def adapters(root=ROOT, run=None):
    """([{name, vendor_id, device_id, vram_mb, shared_mb, luid, software}], problems) of each
    drm card in card order; name is "" where nothing names it. No drm class is no card."""
    try:
        names = os.listdir(_path(root, "sys/class/drm"))
    except FileNotFoundError:
        return [], []
    cards = sorted((int(m.group(1)), n) for n in names for m in [_CARD.match(n)] if m)
    out, problems, smi = [], [], None
    for _, name in cards:
        vendor = _maybe(root, "sys/class/drm/%s/device/vendor" % name)
        if vendor is None:
            continue                    # not a PCI device (a virtual or platform display)
        device = _maybe(root, "sys/class/drm/%s/device/device" % name) or "0"
        vram = (_maybe(root, "sys/class/drm/%s/device/mem_info_vram_total" % name) or "").strip()
        bus = _uevent_slot(root, name)
        a = {"name": "", "vendor_id": int(vendor, 16), "device_id": int(device, 16),
             "vram_mb": int(vram) // 2 ** 20 if vram.isdigit() else 0, "shared_mb": 0,
             "luid": "pci:%s" % bus if bus else None, "software": False}
        if a["vendor_id"] == NVIDIA:
            if smi is None:
                try:
                    smi = nvidia_smi(run)
                except OSError as e:
                    smi = {}
                    problems.append("%s; NVIDIA memory unknown, its cards are not shared" % e)
            if bus in smi:
                a["name"], a["vram_mb"] = smi[bus]
            else:
                a["name"] = proc_nvidia(root).get(bus, "")
        out.append(a)
    return out, problems


# ------------------------------------------------------------------ the seam's primitives
def _bits_mask(ids):
    m = 0
    for i in ids:
        m |= 1 << i
    return m


def threads():
    """Every logical processor this process may run on, ascending (the allowed set)."""
    return allowed(ROOT, _affinity())


def all_mask():
    return _bits_mask(threads())


def l3_domains():
    """[mask] of each L3 domain of the allowed ids; [] when not known."""
    return [_bits_mask(d) for d in l3(ROOT, threads())]


def memory_mb():
    """{total_mb, free_mb} of physical memory now, or None."""
    try:
        return memory(ROOT)
    except (OSError, ValueError):
        return None
