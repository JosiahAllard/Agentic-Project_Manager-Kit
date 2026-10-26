"""The process primitives on Linux (docs/linux-support-plan.md section 2.2 and section 8, slice 4),
read from /proc, with the meanings kitlib.proc.process gives them on Windows. On Linux
kitlib.proc.process binds its process names to these, so its callers run unchanged.

A process is (pid, ctime). The ctime is `(boot << 64) | starttime`: boot is
/proc/sys/kernel/random/boot_id read as one 128-bit integer (BOOT_ID, the seam a test points at a
file), starttime is field 22 of /proc/<pid>/stat, clock ticks since boot. Two boots never share a
ctime, so a record or token written before a reboot names no live process after it; within a boot
the order is starttime's. stamp_now() is the same encoding of the clock now (CLOCK_BOOTTIME, the
clock starttime counts on), so a process created after it never has a ctime below it; one started
in the stamp's own tick has the stamp's value (a tie errs safe: kitlib.lock.run _end_others ages
only ctimes below the stamp).

  identity(pid)       (ctime, exited) of pid, None when no process has it (kitlib.lock.store
                      wrapper_state); exited: a zombie, ended but not reaped
  alive, proc_ctime, own_ctime, snapshot, process_parents, listed_ctimes, listed_ctime, _ancestor,
  ancestry, ancestors_of, own_ancestors, descendants, command_line, terminate_verified, kill_tree
                      as kitlib.proc.process documents them
  stamp_now()         the seam's stamp (kitlib.lock.run filetime_now on Linux)

terminate_verified opens a pidfd on the pid first and reads its starttime after: the pidfd names
the process it was opened on, so the signal reaches that process or none, and the starttime read
after the open shows it is the one meant. Without pidfd_open (Linux 5.3, Python 3.9) it raises
PlatformUnsupported rather than signal a pid that may have been reused. This module imports on any
platform; its calls need Linux's /proc."""
import errno
import os
import signal
import time

from kitlib.platform import PlatformUnsupported

PROC = '/proc'
BOOT_ID = '/proc/sys/kernel/random/boot_id'
EXITED = ('Z', 'X')         # stat states of a process that has ended: zombie, dead


def boot():
    """This boot's id (BOOT_ID) as one 128-bit integer. Raises OSError when it cannot be read or
    is not 32 hex digits."""
    with open(BOOT_ID, encoding='ascii') as f:
        text = f.read().strip().replace('-', '')
    if len(text) != 32:
        raise OSError('%s is not a boot id: %r' % (BOOT_ID, text))
    try:
        return int(text, 16)
    except ValueError:
        raise OSError('%s is not a boot id: %r' % (BOOT_ID, text)) from None


def _exists(pid):
    """Does some process have pid, by signal 0? False only on ESRCH; a process this one may not
    signal (EPERM, another user's) exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _stat(pid):
    """(state, ppid, starttime, comm) from /proc/<pid>/stat, or None when no process has pid.
    Raises OSError when it cannot be read otherwise. comm may hold spaces and parentheses, so
    the fields are read after its last ')'. A stat that is not there is no process only when
    signal 0 finds none (ESRCH, _exists): /proc mounted hidepid=2 hides another user's process
    that a signal still finds, and that one raises (it cannot be told)."""
    if pid <= 0:
        return None
    try:
        with open('%s/%d/stat' % (PROC, pid), 'rb') as f:
            data = f.read()
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        if _exists(pid):
            raise OSError(errno.EACCES, '%s/%d/stat is hidden, but a process has the pid'
                          % (PROC, pid)) from None
        return None
    head, close = data.find(b'('), data.rfind(b')')
    fields = data[close + 2:].split()
    if head < 0 or close < head or len(fields) < 20:
        raise OSError('%s/%d/stat cannot be read: %r' % (PROC, pid, data[:200]))
    return (fields[0].decode('ascii', 'replace'), int(fields[1]), int(fields[19]),
            data[head + 1:close].decode('utf-8', 'replace'))


def _ctime(starttime, boot_id=None):
    return ((boot() if boot_id is None else boot_id) << 64) | starttime


def identity(pid):
    """(ctime, exited) of pid, or None when no process has it. Raises OSError when that cannot be
    told (its stat or the boot id cannot be read)."""
    st = _stat(int(pid))
    if st is None:
        return None
    return _ctime(st[2]), st[0] in EXITED


def proc_ctime(pid):
    """The ctime of pid (an ended, unreaped one's too), or None when it is gone or cannot be read."""
    try:
        seen = identity(pid)
    except (OSError, ValueError):
        return None
    return None if seen is None else seen[0]


def own_ctime():
    return proc_ctime(os.getpid())


def alive(pid, ctime):
    """False only when the process (pid and ctime) is surely gone: no process has pid, it has
    ended (a zombie), or pid's ctime is another; True when alive, and when that cannot be told
    (ctime None or not a number, or pid's stat unreadable)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        seen = identity(pid)
    except (OSError, ValueError):
        return True
    if seen is None or seen[1]:
        return False
    try:
        return ctime is None or seen[0] == int(ctime)
    except (TypeError, ValueError):
        return True


def _pids():
    """Every pid /proc lists (threads are not listed). Raises OSError when /proc cannot be read."""
    return [int(n) for n in os.listdir(PROC) if n.isdigit()]


def snapshot():
    """[(pid, ppid, comm)] of every process /proc lists. Raises OSError when /proc cannot be read."""
    out = []
    for pid in _pids():
        try:
            st = _stat(pid)
        except OSError:
            continue
        if st is not None:
            out.append((pid, st[1], st[3]))
    return out


def process_parents():
    """{pid: parent pid} from one snapshot."""
    return {pid: ppid for pid, ppid, _name in snapshot()}


def listed_ctimes():
    """{pid: ctime} of every process /proc lists, or None when that cannot be had."""
    try:
        b = boot()
        out = {}
        for pid in _pids():
            try:
                st = _stat(pid)
            except OSError:
                continue
            if st is not None:
                out[pid] = _ctime(st[2], b)
        return out
    except OSError:
        return None


def listed_ctime(pid):
    return proc_ctime(pid)


def _ancestor(pid):
    """(ctime or None, exited, recorded parent pid or None) of pid from one read of its stat;
    the parent is given for an ended one only, as on Windows."""
    try:
        st = _stat(int(pid))
        if st is None:
            return None, False, None
        ct = _ctime(st[2])
    except (OSError, ValueError):
        return None, False, None
    exited = st[0] in EXITED
    return ct, exited, st[1] if exited else None


def ancestry(pid, ctime, parents=None, limit=64, through_exited=False):
    """(ancestors, exited): [(pid, ctime)] of the process's ancestors, nearest first, and whether
    the last has ended (a zombie). The walk stops after `limit` hops, at a parent that is gone, at
    one created after its child (a reused pid; an equal ctime, the same tick, is still a parent)
    and, unless through_exited, at an ended one; through_exited goes on to the parent its stat
    records."""
    parents = process_parents() if parents is None else parents
    out, cur, cur_ct, exited = [], int(pid), ctime, False
    pp = parents.get(cur)
    for _ in range(limit):
        if not pp or pp == cur:
            break
        pct, gone, recorded = _ancestor(pp)
        if pct is None or (cur_ct is not None and pct > cur_ct):
            break
        out.append((pp, pct))
        exited = gone
        if gone and not through_exited:
            break
        cur, cur_ct = pp, pct
        pp = recorded if gone else parents.get(cur)
    return out, exited


def ancestors_of(pid, ctime, parents=None, limit=64):
    return ancestry(pid, ctime, parents, limit)[0]


def own_ancestors():
    """{(pid, ctime)} of this process's ancestors (ancestors_of)."""
    return set(ancestors_of(os.getpid(), own_ctime()))


def descendants(pid, ctime, snap=None):
    """[(pid, ctime)] of every live descendant of (pid, ctime), parents before children. A child
    whose ctime is before its parent's holds a reused parent pid and is skipped."""
    snap = snapshot() if snap is None else snap
    kids = {}
    for p, pp, _n in snap:
        if p != pp:
            kids.setdefault(pp, []).append(p)
    out, todo, seen = [], [(int(pid), ctime)], {int(pid)}
    while todo:
        cur, cur_ct = todo.pop(0)
        for c in kids.get(cur, ()):
            if c in seen:
                continue
            cct = proc_ctime(c)
            if cct is None or (cur_ct is not None and cct < cur_ct):
                continue
            seen.add(c)
            out.append((c, cct))
            todo.append((c, cct))
    return out


def command_line(pid, ctime=None):
    """The command line of pid (of (pid, ctime) when ctime is given): its /proc cmdline arguments
    joined by spaces, unquoted; "" for a process with none (a kernel thread, a zombie). None when
    it is gone, reused or cannot be read. The ctime is read before and after the arguments, so a
    pid reused between the two is not taken for the process."""
    if ctime is not None and proc_ctime(pid) != ctime:
        return None
    try:
        with open('%s/%d/cmdline' % (PROC, int(pid)), 'rb') as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    if ctime is not None and proc_ctime(pid) != ctime:
        return None
    args = data.split(b'\0')
    if args and args[-1] == b'':
        args.pop()
    return ' '.join(a.decode('utf-8', 'replace') for a in args)


def _pidfd_open(pid):
    try:
        opener = os.pidfd_open
    except AttributeError:
        opener = None
    if opener is None or not hasattr(signal, 'pidfd_send_signal'):
        raise PlatformUnsupported('a verified kill needs pidfd_open and pidfd_send_signal '
                                  '(Linux 5.3, Python 3.9); this Python has none')
    try:
        return opener(pid)
    except OSError as e:
        if e.errno == errno.ENOSYS:
            raise PlatformUnsupported('a verified kill needs pidfd_open (Linux 5.3); this '
                                      'kernel has none') from e
        raise


def terminate_verified(pid, ctime, code=1):
    """SIGKILL pid only if it is still the process created at ctime (`code` is Windows's exit
    code; a signal carries none). Returns 'killed', 'gone' (no such process, or it has ended),
    'reused' (pid now belongs to another process) or 'denied'."""
    try:
        fd = _pidfd_open(int(pid))
    except ProcessLookupError:
        return 'gone'
    except PlatformUnsupported:
        raise
    except (OSError, ValueError):
        return 'denied'
    try:
        try:
            seen = identity(pid)
        except (OSError, ValueError):
            return 'denied'
        if seen is None:
            return 'gone'
        if seen[0] != ctime:
            return 'reused'
        if seen[1]:
            return 'gone'
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            return 'gone'
        except OSError:
            return 'denied'
        return 'killed'
    finally:
        os.close(fd)


def kill_tree(pid, ctime, code=1, snap=None):
    """End (pid, ctime) and every live descendant, children first, each by terminate_verified
    (no reused pid is hit). Returns {pid: result}."""
    tree = [(int(pid), ctime)] + descendants(pid, ctime, snap)
    return {p: terminate_verified(p, ct, code) for p, ct in reversed(tree)}


def stamp_now():
    """The seam's stamp: this boot shifted, or-ed with the clock ticks since boot now
    (CLOCK_BOOTTIME in SC_CLK_TCK units, floored as starttime is)."""
    ticks = time.clock_gettime_ns(time.CLOCK_BOOTTIME) * os.sysconf('SC_CLK_TCK') // 10 ** 9
    return _ctime(ticks)
