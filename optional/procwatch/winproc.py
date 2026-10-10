"""Windows process and job-object helpers for procwatch.py and jobrun.py (standard library only).

Everything here reads processes except terminate_verified() (one pid, only after its creation
time is re-checked, so a reused pid is never hit) and the job helpers jobrun.py uses on its own
job. Times are FILETIMEs (100 ns units since 1601).
"""
import ctypes
import os
import time
from ctypes import wintypes

K32 = ctypes.WinDLL("kernel32", use_last_error=True)
NTDLL = ctypes.WinDLL("ntdll")

PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
WAIT_OBJECT_0, WAIT_TIMEOUT, INFINITE = 0, 0x102, 0xFFFFFFFF

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectBasicProcessIdList = 3
JobObjectExtendedLimitInformation = 9


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    def value(self):
        return (self.hi << 32) | self.lo


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                               "OtherOperationCount", "ReadTransferCount",
                                               "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


H = wintypes.HANDLE
K32.CreateToolhelp32Snapshot.restype = H
K32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
K32.Process32FirstW.argtypes = (H, ctypes.POINTER(PROCESSENTRY32W))
K32.Process32NextW.argtypes = (H, ctypes.POINTER(PROCESSENTRY32W))
K32.OpenProcess.restype = H
K32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
K32.CloseHandle.argtypes = (H,)
K32.GetProcessTimes.argtypes = (H,) + (ctypes.POINTER(FILETIME),) * 4
K32.QueryFullProcessImageNameW.argtypes = (H, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD))
K32.GetProcessHandleCount.argtypes = (H, ctypes.POINTER(wintypes.DWORD))
K32.K32GetProcessMemoryInfo.argtypes = (H, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD)
K32.TerminateProcess.argtypes = (H, wintypes.UINT)
K32.GetExitCodeProcess.argtypes = (H, ctypes.POINTER(wintypes.DWORD))
K32.WaitForSingleObject.argtypes = (H, wintypes.DWORD)
K32.WaitForSingleObject.restype = wintypes.DWORD
K32.WaitForMultipleObjects.argtypes = (wintypes.DWORD, ctypes.POINTER(H), wintypes.BOOL, wintypes.DWORD)
K32.WaitForMultipleObjects.restype = wintypes.DWORD
K32.GetCurrentProcess.restype = H
K32.CreateJobObjectW.restype = H
K32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
K32.SetInformationJobObject.argtypes = (H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
K32.QueryInformationJobObject.argtypes = (H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD))
K32.AssignProcessToJobObject.argtypes = (H, H)
K32.TerminateJobObject.argtypes = (H, wintypes.UINT)
NTDLL.NtQueryInformationProcess.argtypes = (H, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                            ctypes.POINTER(wintypes.ULONG))
NTDLL.NtQueryInformationProcess.restype = ctypes.c_long


def filetime_now():
    ft = FILETIME()
    K32.GetSystemTimeAsFileTime(ctypes.byref(ft))
    return ft.value()


def snapshot():
    """[(pid, ppid, exe name)] from one Toolhelp snapshot."""
    h = K32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not h or h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        out = []
        ok = K32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
            ok = K32.Process32NextW(h, ctypes.byref(e))
        return out
    finally:
        K32.CloseHandle(h)


def open_process(pid, access=PROCESS_QUERY_LIMITED_INFORMATION):
    return K32.OpenProcess(access, False, int(pid)) or None


def times(h):
    """(creation FILETIME, cpu seconds kernel+user) or (None, None)."""
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not K32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return None, None
    return c.value(), (k.value() + u.value()) / 1e7


def image_path(h):
    n = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(32768)
    return buf.value if K32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None


def command_line(h):
    """The process's command line (NtQueryInformationProcess class 60, Windows 8.1+), or None."""
    size = wintypes.ULONG(0)
    buf = ctypes.create_string_buffer(4096)
    for _ in range(3):
        st = NTDLL.NtQueryInformationProcess(h, 60, buf, len(buf), ctypes.byref(size))
        if st == 0:
            us = UNICODE_STRING.from_buffer(buf)
            if not us.Buffer or not us.Length:
                return ""
            return ctypes.wstring_at(us.Buffer, us.Length // 2)
        if size.value and size.value > len(buf):
            buf = ctypes.create_string_buffer(size.value + 64)
            continue
        return None
    return None


def details(pid):
    """dict of ctime, cpu, image, cmd, handles, ws, priv for one pid; fields None when unreadable
    (other users' and protected processes)."""
    d = {"ctime": None, "cpu": None, "image": None, "cmd": None, "handles": None, "ws": None,
         "priv": None}
    h = open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ) or open_process(pid)
    if not h:
        return d
    try:
        d["ctime"], d["cpu"] = times(h)
        d["image"] = image_path(h)
        d["cmd"] = command_line(h)
        n = wintypes.DWORD(0)
        if K32.GetProcessHandleCount(h, ctypes.byref(n)):
            d["handles"] = int(n.value)
        m = PROCESS_MEMORY_COUNTERS_EX()
        m.cb = ctypes.sizeof(m)
        if K32.K32GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
            d["ws"], d["priv"] = int(m.WorkingSetSize), int(m.PrivateUsage)
    finally:
        K32.CloseHandle(h)
    return d


GIT_PS_DEFAULT = [r"C:\Program Files\Git\usr\bin\ps.exe",          # Git for Windows' defaults
                  r"C:\Program Files (x86)\Git\usr\bin\ps.exe"]


def git_ps(env=os.environ):
    """Where to look for Git's ps.exe: GIT_PS_EXE alone when set, else Git for Windows' defaults."""
    return [env["GIT_PS_EXE"]] if env.get("GIT_PS_EXE") else list(GIT_PS_DEFAULT)


GIT_PS = git_ps()


def msys_table(ps_exe=None):
    """{windows pid: (msys pid, msys parent pid)} for every Git-Bash (MSYS) process, from Git's
    `ps -l`, or None when Git's ps is not there. Needed because Git-Bash breaks the Windows
    parent chain: a program bash runs usually has a Windows parent that has already exited, while
    its MSYS parent is alive. MSYS parent 1 = started from a Windows program."""
    import re
    import subprocess
    exe = ps_exe or next((p for p in GIT_PS if os.path.exists(p)), None)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=15,
                             creationflags=0x08000000).stdout       # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError):
        return None
    table = {}
    for line in out.splitlines()[1:]:
        m = re.match(r"^\s*[A-Z]?\s*(\d+)\s+(\d+)\s+\d+\s+(\d+)\s", line)
        if m:
            table[int(m.group(3))] = (int(m.group(1)), int(m.group(2)))
    return table


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
        ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]


def perf_info():
    """Machine totals in bytes / counts: kernel paged and nonpaged pool, commit, handles."""
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not K32.K32GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return None
    pg = pi.PageSize
    return {"paged_pool": pi.KernelPaged * pg, "nonpaged_pool": pi.KernelNonpaged * pg,
            "commit": pi.CommitTotal * pg, "commit_limit": pi.CommitLimit * pg,
            "handles": pi.HandleCount, "processes": pi.ProcessCount}


def cpu_sample(pids):
    """{pid: (ctime, cpu seconds)} for the pids that can be opened."""
    out = {}
    for pid in pids:
        h = open_process(pid)
        if h:
            try:
                ct, cpu = times(h)
                if ct is not None:
                    out[pid] = (ct, cpu)
            finally:
                K32.CloseHandle(h)
    return out


def terminate_verified(pid, ctime):
    """Terminate pid only if it is still the process created at ctime. Returns 'killed', 'gone',
    'reused' (pid now belongs to another process) or 'denied'."""
    h = open_process(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return "gone" if ctypes.get_last_error() == 87 else "denied"
    try:
        ct, _ = times(h)
        if ct != ctime:
            return "reused"
        return "killed" if K32.TerminateProcess(h, 1) else "denied"
    finally:
        K32.CloseHandle(h)


def is_alive(h):
    return K32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT


def wait_any(handles, timeout_ms=INFINITE):
    """Index of the first signalled handle, or None on timeout."""
    arr = (H * len(handles))(*handles)
    r = K32.WaitForMultipleObjects(len(handles), arr, False, timeout_ms)
    if r == WAIT_TIMEOUT:
        return None
    if WAIT_OBJECT_0 <= r < WAIT_OBJECT_0 + len(handles):
        return r - WAIT_OBJECT_0
    raise ctypes.WinError(ctypes.get_last_error())


# ------------------------------------------------------------------ job objects (jobrun.py)
def create_job(kill_on_close=True):
    job = K32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    set_kill_on_close(job, kill_on_close)
    return job


def set_kill_on_close(job, on):
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    # No JOB_OBJECT_LIMIT_BREAKAWAY_OK: Git-Bash (Cygwin) starts its children with
    # CREATE_BREAKAWAY_FROM_JOB whenever the job allows it, which would let them all escape.
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if on else 0
    if not K32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                       ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())


def assign_self(job):
    if not K32.AssignProcessToJobObject(job, K32.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())


def job_pids(job):
    n = 4096

    class LIST(ctypes.Structure):
        _fields_ = [("assigned", wintypes.DWORD), ("listed", wintypes.DWORD),
                    ("ids", ctypes.c_size_t * n)]
    lst = LIST()
    if not K32.QueryInformationJobObject(job, JobObjectBasicProcessIdList, ctypes.byref(lst),
                                         ctypes.sizeof(lst), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return [int(lst.ids[i]) for i in range(lst.listed)]


def terminate_job(job, code=1):
    return bool(K32.TerminateJobObject(job, code))


def close(h):
    if h:
        K32.CloseHandle(h)


def own_pid():
    return os.getpid()


def sleep(s):
    time.sleep(s)
