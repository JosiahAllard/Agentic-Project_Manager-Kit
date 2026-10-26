"""Four synthetic Linux /sys and /proc trees for the lock's hardware reader
(docs/linux-support-plan.md section 2.4), written by hand from the kernel's documented layout
(Documentation/ABI/testing/sysfs-devices-system-cpu, Documentation/admin-guide/cgroup-v2.rst,
proc(5), the drm sysfs class). No real machine was recorded for them.

TREES maps a name to {files: {path under the tree root: text}, affinity: the ids
sched_getaffinity gives the process, smi: nvidia-smi's answer or None, expect: what the reader
must find}. write(files, folder) lays a tree out under folder.

  desktop    8 cores of 2 SMT threads, siblings (n, n+8), one L3, an NVIDIA card
  hybrid     6 P-cores of 2 threads (0-11) and 8 E-cores (12-19): cpu_core and cpu_atom; an
             Intel iGPU and an AMD card with its own VRAM
  gaps       ids with gaps (online 0-3,6-7,12-15), CPU 15 isolated (out of the affinity), an
             older kernel (thread_siblings_list only), cpu_capacity classes, two L3 domains, no
             drm class at all
  container  /sys lists 32 CPUs, the cgroup's cpuset.cpus.effective allows 8 (4-7,20-23) while the
             affinity still names all 32, a memory.max on a parent cgroup caps the memory
"""
import os

CPU = "sys/devices/system/cpu"
GIB = 2 ** 30


def cpu(n, siblings, caches, package="0", capacity=None, old_kernel=False):
    """The files of an online cpuN: its topology and caches [(level, type, shared_cpu_list)]."""
    d = "%s/cpu%d" % (CPU, n)
    files = {d + "/topology/physical_package_id": package + "\n",
             d + "/topology/thread_siblings_list": siblings + "\n"}
    if not old_kernel:                  # core_cpus_list is new in Linux 5.6
        files[d + "/topology/core_cpus_list"] = siblings + "\n"
    for k, (level, kind, shared) in enumerate(caches):
        files[d + "/cache/index%d/level" % k] = "%d\n" % level
        files[d + "/cache/index%d/type" % k] = kind + "\n"
        files[d + "/cache/index%d/shared_cpu_list" % k] = shared + "\n"
    if capacity is not None:
        files[d + "/cpu_capacity"] = "%d\n" % capacity
    return files


def offline(n):
    """An offline cpuN: its folder and `online` file stay, its topology and caches are gone."""
    return {"%s/cpu%d/online" % (CPU, n): "0\n"}


def meminfo(total_kb, available_kb):
    return {"proc/meminfo": ("MemTotal:       %d kB\nMemFree:        %d kB\nMemAvailable:   %d kB\n"
                             "Buffers:          123456 kB\n" % (total_kb, available_kb // 2, available_kb))}


def cgroup(path, **files):
    """/proc/self/cgroup naming the v2 cgroup `path`, and files in cgroups (path: {name: text})."""
    out = {"proc/self/cgroup": "0::%s\n" % path}
    for where, names in files.items():
        for name, text in names.items():
            out["sys/fs/cgroup%s/%s" % (where, name)] = text
    return out


def card(n, vendor, device, slot, vram=None):
    """A drm cardN on PCI slot `slot`; vram: mem_info_vram_total in bytes (amdgpu only)."""
    d = "sys/class/drm/card%d" % n
    files = {d + "/dev": "226:%d\n" % n,
             d + "/device/vendor": "0x%04x\n" % vendor,
             d + "/device/device": "0x%04x\n" % device,
             d + "/device/uevent": "DRIVER=x\nPCI_CLASS=30000\nPCI_SLOT_NAME=%s\n" % slot}
    if vram is not None:
        files[d + "/device/mem_info_vram_total"] = "%d\n" % vram
    return files


def merge(*parts):
    out = {}
    for p in parts:
        out.update(p)
    return out


def _desktop():
    caches = lambda n: [(1, "Data", "%d,%d" % (n, n + 8)), (1, "Instruction", "%d,%d" % (n, n + 8)),
                        (2, "Unified", "%d,%d" % (n, n + 8)), (3, "Unified", "0-15")]
    files = merge(
        {CPU + "/online": "0-15\n", CPU + "/present": "0-15\n", CPU + "/possible": "0-15\n"},
        *[cpu(n, "%d,%d" % (n % 8, n % 8 + 8), caches(n % 8)) for n in range(16)],
        meminfo(32768000, 30000000),
        cgroup("/user.slice/user-1000.slice/session-2.scope",
               **{"/user.slice/user-1000.slice/session-2.scope": {"cpuset.cpus.effective": "0-15\n",
                                                                   "memory.max": "max\n"}}),
        {"sys/class/drm/version": "drm 1.1.0 20060810\n",
         "sys/class/drm/card0-DP-1/status": "connected\n",
         "sys/class/drm/renderD128/dev": "226:128\n"},
        card(0, 0x10DE, 0x2684, "0000:01:00.0"))
    return {"files": files, "affinity": set(range(16)),
            "smi": "00000000:01:00.0, NVIDIA Example GPU, 24564 MiB\n",
            "expect": {"threads": list(range(16)),
                       "cores": [[n, n + 8] for n in range(8)],
                       "efficiency": [0] * 8,
                       "l3": [list(range(16))],
                       "memory_mb": 32000,
                       "gpus": [("NVIDIA Example GPU", 0x10DE, 24564, 2)]}}


def _hybrid():
    p = [cpu(n, "%d-%d" % (n - n % 2, n - n % 2 + 1),
             [(1, "Data", "%d-%d" % (n - n % 2, n - n % 2 + 1)), (2, "Unified", "%d-%d" % (n - n % 2, n - n % 2 + 1)),
              (3, "Unified", "0-19")]) for n in range(12)]
    e = [cpu(n, "%d" % n, [(1, "Data", "%d" % n), (2, "Unified", "12-15" if n < 16 else "16-19"),
                           (3, "Unified", "0-19")]) for n in range(12, 20)]
    files = merge(
        {CPU + "/online": "0-19\n", CPU + "/present": "0-19\n",
         "sys/devices/cpu_core/cpus": "0-11\n", "sys/devices/cpu_atom/cpus": "12-19\n"},
        *p, *e,
        meminfo(65536000, 60000000),
        cgroup("/user.slice", **{"/user.slice": {"cpuset.cpus.effective": "0-19\n"}}),
        card(0, 0x8086, 0xA780, "0000:00:02.0"),
        card(1, 0x1002, 0x744C, "0000:03:00.0", vram=16368 * 2 ** 20))
    return {"files": files, "affinity": set(range(20)), "smi": None,
            "expect": {"threads": list(range(20)),
                       "cores": [[n, n + 1] for n in range(0, 12, 2)] + [[n] for n in range(12, 20)],
                       "efficiency": [1] * 6 + [0] * 8,
                       "l3": [list(range(20))],
                       "memory_mb": 64000,
                       "gpus": [("Intel 0xA780", 0x8086, 0, 0), ("AMD 0x744C", 0x1002, 16368, 2)]}}


def _gaps():
    # SMT pairs (n, n+8); 4, 5 and 8-11 offline; 15 isolated, so the affinity leaves it out.
    l3 = lambda n: "0-3,8-11" if n % 8 < 4 else "4-7,12-15"
    online = [0, 1, 2, 3, 6, 7, 12, 13, 14, 15]
    files = merge(
        {CPU + "/online": "0-3,6-7,12-15\n", CPU + "/present": "0-15\n", CPU + "/isolated": "15\n"},
        *[cpu(n, "%d,%d" % (n % 8, n % 8 + 8), [(1, "Data", "%d,%d" % (n % 8, n % 8 + 8)), (3, "Unified", l3(n))],
              capacity=1024 if n < 8 else 446, old_kernel=True) for n in online],
        *[offline(n) for n in (4, 5, 8, 9, 10, 11)],
        meminfo(16384000, 12000000))
    return {"files": files, "affinity": set(online) - {15}, "smi": None,
            "expect": {"threads": [0, 1, 2, 3, 6, 7, 12, 13, 14],
                       "cores": [[0], [1], [2], [3], [6, 14], [7], [12], [13]],
                       "efficiency": [1, 1, 1, 1, 1, 1, 0, 0],
                       "l3": [[0, 1, 2, 3], [6, 7, 12, 13, 14]],
                       "memory_mb": 16000,
                       "gpus": []}}


def _container():
    scope = "/kit.slice/run.scope"
    files = merge(
        {CPU + "/online": "0-31\n", CPU + "/present": "0-31\n"},
        *[cpu(n, "%d,%d" % (n % 16, n % 16 + 16), [(2, "Unified", "%d,%d" % (n % 16, n % 16 + 16)),
                                                   (3, "Unified", "0-31")]) for n in range(32)],
        meminfo(131072000, 100000000),
        cgroup(scope, **{"/kit.slice": {"memory.max": "%d\n" % (8 * GIB)},
                         scope: {"cpuset.cpus.effective": "4-7,20-23\n", "memory.max": "max\n",
                                 "memory.current": "%d\n" % GIB}}),
        {"sys/class/drm/version": "drm 1.1.0 20060810\n"})
    return {"files": files, "affinity": set(range(32)), "smi": None,
            "expect": {"threads": [4, 5, 6, 7, 20, 21, 22, 23],
                       "cores": [[4, 20], [5, 21], [6, 22], [7, 23]],
                       "efficiency": [0] * 4,
                       "l3": [[4, 5, 6, 7, 20, 21, 22, 23]],
                       "memory_mb": 8192,
                       "free_mb": 7168,
                       "gpus": []}}


TREES = {"desktop": _desktop(), "hybrid": _hybrid(), "gaps": _gaps(), "container": _container()}


def write(files, folder):
    """Lay `files` out under folder; returns folder."""
    for rel, text in files.items():
        path = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii", newline="\n") as f:
            f.write(text)
    return folder
