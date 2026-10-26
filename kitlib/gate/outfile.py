"""A branch gate's claim on its output file (docs/gate.md "Whose run a log is").

Two gate calls whose stdout is the same file (two shells' `gate.py ... > gate-full.log` in one
shared scratch folder) each write at their own file position: the later call's shell truncates
the file and its header lands at the start, so the earlier run's log begins with the other run's
`gate <branch> at <tip>` and budget lines. So a branch gate whose stdout is a disk file takes an
exclusive OS lock (LockFileEx) on one byte far past any log's end, through that stdout handle,
before it says anything; a second gate on the same file finds the byte locked and is REFUSED
before its header. The lock covers no byte anyone reads or writes, and it ends with the process
(or Claim.release), so nothing is left to go stale. A stdout that is not a disk file (a console,
a pipe, NUL; PowerShell 5.1's `>` writes the file itself, through a pipe) claims nothing.
"""
import ctypes
from ctypes import wintypes

from kitlib.platform.dll import LazyDLL

FILE_TYPE_DISK = 1
LOCKFILE_FAIL_IMMEDIATELY = 0x1
LOCKFILE_EXCLUSIVE_LOCK = 0x2
ERROR_LOCK_VIOLATION = 33
OFFSET_HIGH = 0x7FFFFFF0    # the claimed byte is at OFFSET_HIGH << 32: past any log's end


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p),
                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE)]


# A private kernel32 instance, loaded at first use (kitlib.platform.dll), so this module imports on
# any platform; each function gets its (restype, argtypes) then.
_K32 = LazyDLL('kernel32', {
    'GetFileType': (wintypes.DWORD, [wintypes.HANDLE]),
    'LockFileEx': (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]),
    'UnlockFileEx': (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(_OVERLAPPED)]),
    'GetFinalPathNameByHandleW': (wintypes.DWORD, [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                                   wintypes.DWORD])}, use_last_error=True)


def _range():
    ov = _OVERLAPPED()
    ov.Offset, ov.OffsetHigh = 0, OFFSET_HIGH
    return ov


class Claim:
    """The claim one gate holds on its output file, until release() or the process ends."""

    def __init__(self, handle):
        self.handle = handle

    def release(self):
        if self.handle is not None:
            _K32.UnlockFileEx(self.handle, 0, 1, 0, ctypes.byref(_range()))
            self.handle = None


def path_of(handle):
    """The file's path, or None when Windows does not give one."""
    buf = ctypes.create_unicode_buffer(1024)
    n = _K32.GetFinalPathNameByHandleW(handle, buf, len(buf), 0)
    if not n or n >= len(buf):
        return None
    p = buf.value
    if p.startswith('\\\\?\\UNC\\'):
        return '\\\\' + p[8:]
    return p[4:] if p.startswith('\\\\?\\') else p


def refusal(path):
    where = path or 'its stdout file'
    return ['REFUSED: this gate\'s output file %s is the output of another gate run that is still '
            'running; a second gate writing into it would put its lines among that run\'s; '
            'nothing ran' % where,
            'Instead: give each gate run its own log file, named after the branch and tip '
            '(gate-<branch slug>-<tip>.log), or wait for that run to end; every other line in '
            'that file is the other run\'s']


def claim(stream):
    """(Claim or None, refused, warning) for the gate whose stdout is `stream`: a disk file
    another gate holds is (None, the REFUSED lines, None); a disk file is (Claim, None, None); no
    file descriptor, or not a disk file, is (None, None, None); a lock Windows refuses for another
    reason is (None, None, a WARNING line), and the gate runs (fail open). Windows only: msvcrt is
    imported here, so the module imports on any platform."""
    import msvcrt
    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return None, None, None
    if _K32.GetFileType(handle) != FILE_TYPE_DISK:
        return None, None, None
    if _K32.LockFileEx(handle, LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0,
                       ctypes.byref(_range())):
        return Claim(handle), None, None
    err = ctypes.get_last_error()
    if err == ERROR_LOCK_VIOLATION:
        return None, refusal(path_of(handle)), None
    return None, None, ('WARNING: the gate could not claim its output file (Windows error %d): a '
                        'second gate writing into it is not refused' % err)
