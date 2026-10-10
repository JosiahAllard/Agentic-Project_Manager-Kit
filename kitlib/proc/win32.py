"""The Win32 bindings kitlib.proc and kitlib.lock share: one kernel32, ntdll and advapi32 instance
each, with every prototype set once (two modules setting different argtypes on one function was a
hazard of the four copies this replaces: machine_lock, winproc, pid_shield, pytests).
Windows only, standard library only.
"""
import ctypes
from ctypes import wintypes

K32 = ctypes.WinDLL("kernel32", use_last_error=True)
NTDLL = ctypes.WinDLL("ntdll")
ADV = ctypes.WinDLL("advapi32", use_last_error=True)

H, DW, BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL


class FILETIME(ctypes.Structure):
    """wintypes.FILETIME's layout and field names, plus value(): the 64-bit count."""
    _fields_ = [("dwLowDateTime", DW), ("dwHighDateTime", DW)]

    def value(self):
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


PROCESS_TERMINATE = 0x0001
PROCESS_SET_INFORMATION = 0x0200            # SetProcessAffinityMask
PROCESS_SET_LIMITED_INFORMATION = 0x2000    # SetProcessDefaultCpuSets
QUERY_LIMITED = PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
MUTEX_MODIFY_STATE = 0x0001
CREATE_SUSPENDED = 0x4
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x10
DETACHED_PROCESS = 0x8
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
WAIT_OBJECT_0, WAIT_ABANDONED_0, WAIT_TIMEOUT, INFINITE = 0, 0x80, 0x102, 0xFFFFFFFF
ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER = 5, 87
ERROR_INSUFFICIENT_BUFFER, ERROR_ALREADY_EXISTS, ERROR_MORE_DATA = 122, 183, 234
SC_MANAGER_CONNECT, SERVICE_QUERY_STATUS = 0x0001, 0x0004
ERROR_SERVICE_DOES_NOT_EXIST = 1060

# Job objects
JOB_KILL_ON_CLOSE = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_AFFINITY = JOB_OBJECT_LIMIT_AFFINITY = 0x10
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800      # never set by the kit (Git-Bash breaks away: job.py)
JOB_ACCOUNTING_INFO = 1                     # JobObjectBasicAccountingInformation
JOB_PID_LIST = JobObjectBasicProcessIdList = 3
JOB_EXT_LIMITS = JobObjectExtendedLimitInformation = 9
JOB_GROUP_INFO_EX = 14                      # JobObjectGroupInformationEx: GROUP_AFFINITY[]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", DW), ("cntUsage", DW), ("th32ProcessID", DW),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", DW), ("cntThreads", DW),
                ("th32ParentProcessID", DW), ("pcPriClassBase", wintypes.LONG), ("dwFlags", DW),
                ("szExeFile", wintypes.WCHAR * 260)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", DW), ("PageFaultCount", DW)] + [
        (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize",
                                       "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                                       "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                       "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class BASIC_LIMITS(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION"""
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", DW),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", DW),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", DW), ("SchedulingClass", DW)]


class EXT_LIMITS(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION"""
    _fields_ = [("Basic", BASIC_LIMITS), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOB_ACCOUNTING(ctypes.Structure):
    """JOBOBJECT_BASIC_ACCOUNTING_INFORMATION (times in 100 ns units)."""
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong), ("TotalPageFaultCount", DW),
                ("TotalProcesses", DW), ("ActiveProcesses", DW), ("TotalTerminatedProcesses", DW)]


class GROUP_AFFINITY(ctypes.Structure):
    _fields_ = [("Mask", ctypes.c_size_t), ("Group", wintypes.WORD),
                ("Reserved", wintypes.WORD * 3)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", DW), ("dwMemoryLoad", DW)] + [
        (n, ctypes.c_ulonglong) for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                                          "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual",
                                          "ullAvailExtendedVirtual")]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int),
                ("ptstrName", ctypes.c_void_p)]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", DW), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", DW), ("Trustee", TRUSTEE_W)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", DW), ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", BOOL)]


def pid_list(n):
    """JOBOBJECT_BASIC_PROCESS_ID_LIST with room for n ids."""
    return type("PID_LIST_%d" % n, (ctypes.Structure,), {
        "_fields_": [("assigned", DW), ("listed", DW), ("ids", ctypes.c_size_t * n)]})()


for _n, _r, _a in (
        ("CreateMutexW", H, (wintypes.LPVOID, BOOL, wintypes.LPCWSTR)),
        ("OpenMutexW", H, (DW, BOOL, wintypes.LPCWSTR)),
        ("ReleaseMutex", BOOL, (H,)),
        ("CloseHandle", BOOL, (H,)),
        ("LocalFree", ctypes.c_void_p, (ctypes.c_void_p,)),
        ("CreateEventW", H, (wintypes.LPVOID, BOOL, BOOL, wintypes.LPCWSTR)),
        ("OpenEventW", H, (DW, BOOL, wintypes.LPCWSTR)),
        ("K32EnumProcesses", BOOL, (ctypes.POINTER(DW), DW, ctypes.POINTER(DW))),
        ("WaitForSingleObject", DW, (H, DW)),
        ("OpenProcess", H, (DW, BOOL, DW)), ("GetCurrentProcess", H, ()),
        ("GetProcessTimes", BOOL, (H,) + (ctypes.POINTER(FILETIME),) * 4),
        ("GetProcessAffinityMask", BOOL, (H, ctypes.POINTER(ctypes.c_size_t),
                                          ctypes.POINTER(ctypes.c_size_t))),
        ("CreateJobObjectW", H, (wintypes.LPVOID, wintypes.LPCWSTR)),
        ("SetInformationJobObject", BOOL, (H, ctypes.c_int, wintypes.LPVOID, DW)),
        ("QueryInformationJobObject", BOOL, (H, ctypes.c_int, wintypes.LPVOID, DW,
                                             ctypes.POINTER(DW))),
        ("AssignProcessToJobObject", BOOL, (H, H)),
        ("TerminateJobObject", BOOL, (H, wintypes.UINT)),
        ("TerminateProcess", BOOL, (H, wintypes.UINT)),
        ("CreateToolhelp32Snapshot", H, (DW, DW)),
        ("Process32FirstW", BOOL, (H, ctypes.POINTER(PROCESSENTRY32W))),
        ("Process32NextW", BOOL, (H, ctypes.POINTER(PROCESSENTRY32W))),
        ("GetLogicalProcessorInformationEx", BOOL, (ctypes.c_int, wintypes.LPVOID,
                                                    ctypes.POINTER(DW))),
        ("GlobalMemoryStatusEx", BOOL, (wintypes.LPVOID,)),
        ("GetConsoleProcessList", DW, (ctypes.POINTER(DW), DW)),
        ("K32GetProcessMemoryInfo", BOOL, (H, wintypes.LPVOID, DW)),
        ("GetSystemCpuSetInformation", BOOL, (wintypes.LPVOID, wintypes.ULONG,
                                              ctypes.POINTER(wintypes.ULONG), H, wintypes.ULONG)),
        ("SetProcessAffinityMask", BOOL, (H, ctypes.c_size_t)),
        ("SetProcessDefaultCpuSets", BOOL, (H, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG))):
    getattr(K32, _n).restype, getattr(K32, _n).argtypes = _r, _a

NTDLL.NtResumeProcess.restype, NTDLL.NtResumeProcess.argtypes = ctypes.c_long, (H,)
NTDLL.NtQueryInformationProcess.restype = ctypes.c_long
NTDLL.NtQueryInformationProcess.argtypes = (H, ctypes.c_int, wintypes.LPVOID, wintypes.ULONG,
                                            ctypes.POINTER(wintypes.ULONG))
PROCESS_COMMAND_LINE_INFORMATION = 60       # Windows 8.1+: a UNICODE_STRING and its text


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]
ADV.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL
ADV.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
    wintypes.LPCWSTR, DW, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID)
ADV.OpenSCManagerW.restype, ADV.OpenSCManagerW.argtypes = H, (wintypes.LPCWSTR, wintypes.LPCWSTR, DW)
ADV.OpenServiceW.restype, ADV.OpenServiceW.argtypes = H, (H, wintypes.LPCWSTR, DW)
ADV.CloseServiceHandle.restype, ADV.CloseServiceHandle.argtypes = BOOL, (H,)
ADV.GetSecurityInfo.restype, ADV.GetSecurityInfo.argtypes = DW, (
    H, ctypes.c_int, DW, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
ADV.SetSecurityInfo.restype, ADV.SetSecurityInfo.argtypes = DW, (
    H, ctypes.c_int, DW, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
ADV.CreateWellKnownSid.restype, ADV.CreateWellKnownSid.argtypes = BOOL, (
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(DW))
ADV.SetEntriesInAclW.restype, ADV.SetEntriesInAclW.argtypes = DW, (
    wintypes.ULONG, ctypes.POINTER(EXPLICIT_ACCESS_W), ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p))


def last_error():
    return ctypes.get_last_error()


def close(h):
    if h:
        K32.CloseHandle(h)


def resume(handle):
    """NtResumeProcess on a process handle (an int, or a Popen); 0 on success, else the NTSTATUS.
    Popen closes the thread handle of a CREATE_SUSPENDED child, so the process is resumed."""
    h = getattr(handle, "_handle", handle)
    return NTDLL.NtResumeProcess(int(h))
