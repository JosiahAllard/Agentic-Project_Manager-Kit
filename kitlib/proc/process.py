"""Process information and verified kills: one implementation of what machine_lock.py, winproc.py
and pytests.py each carried. Times are FILETIMEs (100 ns units since 1601); a process is
named by (pid, creation time) wherever a reused pid could otherwise be hit.
"""
import ctypes
import os
from ctypes import wintypes

from . import win32
from .win32 import K32, FILETIME

PROCESS_TERMINATE = win32.PROCESS_TERMINATE
PROCESS_QUERY_LIMITED_INFORMATION = win32.QUERY_LIMITED
SYNCHRONIZE = win32.SYNCHRONIZE
WAIT_TIMEOUT = win32.WAIT_TIMEOUT
close = win32.close


def snapshot():
    """[(pid, ppid, exe name)] from one Toolhelp snapshot. Raises OSError when none can be made."""
    h = K32.CreateToolhelp32Snapshot(win32.TH32CS_SNAPPROCESS, 0)
    if not h or h == win32.INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        e = win32.PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        out = []
        ok = K32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
            ok = K32.Process32NextW(h, ctypes.byref(e))
        return out
    finally:
        K32.CloseHandle(h)


def process_parents():
    """{pid: parent pid} from one snapshot."""
    return {pid: ppid for pid, ppid, _name in snapshot()}


def open_process(pid, access=PROCESS_QUERY_LIMITED_INFORMATION):
    return K32.OpenProcess(access, False, int(pid)) or None


def times(h):
    """(creation FILETIME, cpu seconds kernel+user) of a process handle, or (None, None)."""
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not K32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k),
                               ctypes.byref(u)):
        return None, None
    return c.value(), (k.value() + u.value()) / 1e7


def handle_ctime(h):
    return times(h)[0]


def own_ctime():
    return handle_ctime(K32.GetCurrentProcess())


def proc_ctime(pid):
    h = open_process(pid)
    if not h:
        return None
    try:
        return handle_ctime(h)
    finally:
        K32.CloseHandle(h)


def alive(pid, ctime):
    """False only when the process (pid and creation time) is surely gone; True when alive or
    when it cannot be opened for another reason (another user's, say)."""
    h = K32.OpenProcess(win32.QUERY_LIMITED | SYNCHRONIZE, False, int(pid))
    if not h:
        return ctypes.get_last_error() != win32.ERROR_INVALID_PARAMETER
    try:
        if ctime is not None and handle_ctime(h) not in (None, int(ctime)):
            return False
        return K32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        K32.CloseHandle(h)


def ancestors_of(pid, ctime, parents=None, limit=64):
    """[(pid, ctime)] of a process's ancestors, nearest first; the walk stops at a parent created
    after its child (the parent's pid was reused) or one that cannot be opened."""
    parents = process_parents() if parents is None else parents
    out, cur, cur_ct = [], int(pid), ctime
    for _ in range(limit):
        pp = parents.get(cur)
        if not pp or pp == cur:
            break
        pct = proc_ctime(pp)
        if pct is None or (cur_ct is not None and pct > cur_ct):
            break
        out.append((pp, pct))
        cur, cur_ct = pp, pct
    return out


def own_ancestors():
    """{(pid, ctime)} of this process's ancestors (see ancestors_of)."""
    return set(ancestors_of(os.getpid(), own_ctime()))


def descendants(pid, ctime, snap=None):
    """[(pid, ctime)] of every live descendant of (pid, ctime), parents before children. A child
    whose creation time is before its parent's holds a reused parent pid and is skipped."""
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
    """The command line of pid (of (pid, ctime) when ctime is given), or None when it is gone,
    reused or cannot be read. Query access only (NtQueryInformationProcess class 60)."""
    h = open_process(pid)
    if not h:
        return None
    try:
        if ctime is not None and handle_ctime(h) != ctime:
            return None
        size = wintypes.ULONG(0)
        buf = ctypes.create_string_buffer(4096)
        for _ in range(3):
            st = win32.NTDLL.NtQueryInformationProcess(
                h, win32.PROCESS_COMMAND_LINE_INFORMATION, buf, len(buf), ctypes.byref(size))
            if st == 0:
                us = win32.UNICODE_STRING.from_buffer(buf)
                return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer and us.Length \
                    else ""
            if size.value > len(buf):
                buf = ctypes.create_string_buffer(size.value + 64)
                continue
            return None
        return None
    finally:
        K32.CloseHandle(h)


def memory_counters(h):
    """PROCESS_MEMORY_COUNTERS_EX of a handle, or None. An exited process's handle still gives
    its peak."""
    m = win32.PROCESS_MEMORY_COUNTERS_EX()
    m.cb = ctypes.sizeof(m)
    return m if K32.K32GetProcessMemoryInfo(h, ctypes.byref(m), m.cb) else None


def process_working_set(h):
    """(peak, now) working set in bytes of the process behind handle h, or (0, 0)."""
    m = memory_counters(h)
    return (int(m.PeakWorkingSetSize), int(m.WorkingSetSize)) if m else (0, 0)


def memory_mb():
    """{total_mb, free_mb} of this machine's physical memory now, or None."""
    m = win32.MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not K32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return {"total_mb": int(m.ullTotalPhys // 2**20), "free_mb": int(m.ullAvailPhys // 2**20)}


def system_mask():
    """The system affinity mask of this process's processor group (at most 64 threads); see
    kitlib.proc.cpu for every group."""
    pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
    K32.GetProcessAffinityMask(K32.GetCurrentProcess(), ctypes.byref(pm), ctypes.byref(sm))
    return sm.value


def terminate_verified(pid, ctime, code=1):
    """Terminate pid only if it is still the process created at ctime. Returns 'killed', 'gone',
    'reused' (pid now belongs to another process) or 'denied'."""
    h = open_process(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return "gone" if ctypes.get_last_error() == win32.ERROR_INVALID_PARAMETER else "denied"
    try:
        ct, _ = times(h)
        if ct != ctime:
            return "reused"
        return "killed" if K32.TerminateProcess(h, code) else "denied"
    finally:
        K32.CloseHandle(h)


def kill_tree(pid, ctime, code=1, snap=None):
    """Terminate (pid, ctime) and every live descendant, children first, each by terminate_verified
    (no reused pid is hit). Returns {pid: result}. A job object (kitlib.proc.job) is the sure way to
    end a tree; this is for one that was started outside any."""
    tree = [(int(pid), ctime)] + descendants(pid, ctime, snap)
    return {p: terminate_verified(p, ct, code) for p, ct in reversed(tree)}


def affinity_of(pid, ctime):
    """The affinity mask (its processor group's) of process (pid, ctime), or None when it is gone,
    reused or cannot be read. Opens it for query only."""
    h = open_process(pid)
    if not h:
        return None
    try:
        if times(h)[0] != ctime:
            return None
        pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
        if not K32.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm)):
            return None
        return pm.value
    finally:
        K32.CloseHandle(h)


def restore_affinity(pid, ctime, mask, soft):
    """Best effort: undoes set_affinity on (pid, ctime). soft: its default CPU sets are cleared;
    else its affinity is set back to mask (what affinity_of read before the pin). It checks the
    creation time with query access before it opens the process for write. Returns 'restored',
    'gone', 'reused', 'denied' or 'failed' (nothing to set back to)."""
    if ctime is None or (not soft and not mask):
        return "failed"
    ct = proc_ctime(pid)
    if ct is None:
        return "gone"
    if ct != ctime:
        return "reused"
    access = PROCESS_QUERY_LIMITED_INFORMATION | (
        win32.PROCESS_SET_LIMITED_INFORMATION if soft else win32.PROCESS_SET_INFORMATION)
    h = open_process(pid, access)
    if not h:
        err = ctypes.get_last_error()
        return "gone" if err == win32.ERROR_INVALID_PARAMETER else "denied"
    try:
        if times(h)[0] != ctime:
            return "reused"
        ok = (K32.SetProcessDefaultCpuSets(h, None, 0) if soft
              else K32.SetProcessAffinityMask(h, mask))
        if ok:
            return "restored"
        return "denied" if ctypes.get_last_error() == win32.ERROR_ACCESS_DENIED else "failed"
    finally:
        K32.CloseHandle(h)


def set_affinity(pid, ctime, mask):
    """Best effort: confine (pid, ctime) to global thread mask `mask`. Opens the process with
    write access (set information), so a caller that must not touch a process (a game under kernel
    anti-cheat) never calls it. A mask all in group 0 is a hard affinity
    (SetProcessAffinityMask); any other is the process's default CPU sets (SetProcessDefaultCpuSets,
    Windows 10+: soft, its threads may still be moved by their own code). Returns 'pinned', 'soft',
    'overridden' (the call succeeded but the process runs elsewhere: a job's affinity limit wins
    over its own mask), 'gone', 'reused', 'denied' (anti-cheat or another user's process) or
    'failed'."""
    from . import cpu
    parts = cpu.split(mask)
    soft = list(parts) != [0]
    access = PROCESS_QUERY_LIMITED_INFORMATION | (
        win32.PROCESS_SET_LIMITED_INFORMATION if soft else win32.PROCESS_SET_INFORMATION)
    h = open_process(pid, access)
    if not h:
        err = ctypes.get_last_error()
        return "gone" if err == win32.ERROR_INVALID_PARAMETER else "denied"
    try:
        if times(h)[0] != ctime:
            return "reused"
        if not soft:
            ok = K32.SetProcessAffinityMask(h, parts[0])
        else:
            ids = [c["id"] for c in cpu.cpu_sets() if mask >> c["thread"] & 1]
            if not ids:
                return "failed"
            arr = (wintypes.ULONG * len(ids))(*ids)
            ok = K32.SetProcessDefaultCpuSets(h, arr, len(ids))
        if ok and not soft:
            pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
            got = K32.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm))
            if got and pm.value != parts[0]:
                return "overridden"
        if ok:
            return "soft" if soft else "pinned"
        return "denied" if ctypes.get_last_error() == win32.ERROR_ACCESS_DENIED else "failed"
    finally:
        K32.CloseHandle(h)


