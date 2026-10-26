"""The affinity primitives on Linux (docs/linux-support-plan.md section 8, slice 5), with the
meanings kitlib.proc.process gives them on Windows. A mask is the seam's: bit i is cpu id i
(kitlib.platform.linux.hardware).

  affinity_of(pid, ctime)          the process's affinity mask (its main thread's), or None
  set_affinity(pid, ctime, mask)   pin every thread of the process to `mask`

Linux affinity is a hard mask, so set_affinity never answers 'soft'; it is cooperative all the
same: the process may set its own back (the S2 and W2 containment tiers of section 2.3 say so).
A cpuset the process runs under wins over the mask: the kernel keeps only the cpus both allow, so
set_affinity reads the mask back and answers 'overridden' when it is not the one asked for, as
Windows does when a job's limit wins. The process is held by a pidfd from the first ctime check
to the last, and the ctime is read again after the set: a pid that names another process by then
answers 'reused' (the set may have reached that process's threads; no kill is involved).
This module imports on any platform; its calls need Linux."""
import errno
import os

from kitlib.platform import PlatformUnsupported
from kitlib.platform.linux import process

PROC = '/proc'


def mask_cpus(mask):
    """The cpu ids of mask, ascending."""
    out, i = [], 0
    mask = int(mask or 0)
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out


def cpus_mask(ids):
    m = 0
    for i in ids:
        m |= 1 << int(i)
    return m


def affinity_of(pid, ctime):
    """The affinity mask of process (pid, ctime), or None when it is gone, reused or cannot be
    read."""
    try:
        if process.proc_ctime(pid) != ctime or ctime is None:
            return None
        got = cpus_mask(os.sched_getaffinity(int(pid)))
        if process.proc_ctime(pid) != ctime:
            return None
        return got
    except (OSError, ValueError):
        return None


def tids(pid):
    """The thread ids of pid now ([pid] alone when its task list cannot be read)."""
    try:
        return sorted(int(t) for t in os.listdir('%s/%d/task' % (PROC, int(pid))) if t.isdigit())
    except OSError:
        return [int(pid)]


def set_threads(pid, ids):
    """sched_setaffinity on every thread of pid; 'pinned', 'gone', 'denied' or 'failed'. A thread
    that ends meanwhile is skipped."""
    done = 0
    for tid in tids(pid):
        try:
            os.sched_setaffinity(tid, ids)
            done += 1
        except ProcessLookupError:
            continue
        except PermissionError:
            return 'denied'
        except OSError as e:
            return 'denied' if e.errno == errno.EPERM else 'failed'
    return 'pinned' if done else 'gone'


def set_affinity(pid, ctime, mask):
    """Best effort: confine every thread of (pid, ctime) to mask. Returns 'pinned', 'overridden'
    (set, but a cpuset keeps it elsewhere), 'gone', 'reused', 'denied' (another user's process) or
    'failed' (an empty mask, or one the kernel refuses: none of its cpus allowed)."""
    ids = mask_cpus(mask)
    if not ids:
        return 'failed'
    pid = int(pid)
    try:
        fd = process._pidfd_open(pid)
    except ProcessLookupError:
        return 'gone'
    except PlatformUnsupported:
        raise
    except OSError as e:
        return 'denied' if e.errno == errno.EPERM else 'failed'
    try:
        seen = process.identity(pid)
        if seen is None or seen[1]:
            return 'gone'
        if seen[0] != ctime:
            return 'reused'
        result = set_threads(pid, ids)
        if result != 'pinned':
            return result
        try:
            got = set(os.sched_getaffinity(pid))
        except ProcessLookupError:
            return 'gone'
        except OSError:
            return 'failed'
        if process.proc_ctime(pid) != ctime:
            return 'reused'
        return 'pinned' if got == set(ids) else 'overridden'
    finally:
        os.close(fd)
