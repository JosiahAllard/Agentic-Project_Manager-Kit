"""Named pipes, pipe and file ACLs, and process SIDs and images: raw Win32 through ctypes.

Promoted from the bridge's Phase 0 probe package (docs/multi-host-plan.md section 2.4), which the
bridge's service, frontend and host halves (optional/bridge) build on. Every refusal raises
kitlib.proc.errors.ProcError with a fixed code. Windows only.

    connect(name, pid, creation)   open a local pipe whose server is that exact live process, run by
                                   this user, and whose owner is that user
    PrivateListener(name)          a one-instance pipe only this user can open, its ACL read back
    protect_file(path)             restrict a new file to this user, read back
    process_sids(pid)              (user SID, owner SID) of a process
    process_image(pid)             the resolved image path of a process
    descriptor(handle)             (owner SID, SDDL) of a kernel object
"""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import time

from kitlib.platform.dll import LazyDLL

from .errors import ProcError
from .process import proc_ctime

# Private kernel32 and advapi32 instances (no prototype here touches kitlib.proc.win32's), loaded
# at first use (kitlib.platform.dll): this module imports on any platform.
K = LazyDLL('kernel32', use_last_error=True)
A = LazyDLL('advapi32', use_last_error=True)
INVALID = ctypes.c_void_p(-1).value


def bind(dll, name, arguments, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = arguments, result
    return function


def _later(dll, name, arguments, result):
    """bind() at the first call rather than now: the module-level functions below, so importing
    this module loads no DLL. A missing export raises at that call, naming the function."""
    bound = []

    def call(*args):
        if not bound:
            bound.append(bind(dll, name, arguments, result))
        return bound[0](*args)
    call.__name__ = call.__qualname__ = name
    return call


close = _later(K, 'CloseHandle', [w.HANDLE], w.BOOL)
local_free = _later(K, 'LocalFree', [w.HANDLE], w.HANDLE)
create_file = _later(K, 'CreateFileW', [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                                      w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
create_pipe = _later(K, 'CreateNamedPipeW', [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                                           w.DWORD, w.DWORD, w.DWORD, w.LPVOID], w.HANDLE)
connect_pipe = _later(K, 'ConnectNamedPipe', [w.HANDLE, w.LPVOID], w.BOOL)
disconnect_pipe = _later(K, 'DisconnectNamedPipe', [w.HANDLE], w.BOOL)
peek = _later(K, 'PeekNamedPipe', [w.HANDLE, w.LPVOID, w.DWORD, w.LPVOID,
                                  ctypes.POINTER(w.DWORD), w.LPVOID], w.BOOL)
read = _later(K, 'ReadFile', [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID], w.BOOL)
write = _later(K, 'WriteFile', [w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID], w.BOOL)
server_pid = _later(K, 'GetNamedPipeServerProcessId', [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL)
client_pid = _later(K, 'GetNamedPipeClientProcessId', [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL)
open_process = _later(K, 'OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
open_token = _later(A, 'OpenProcessToken', [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL)
token_info = _later(A, 'GetTokenInformation', [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD,
                                              ctypes.POINTER(w.DWORD)], w.BOOL)
sid_text = _later(A, 'ConvertSidToStringSidW', [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL)
security_info = _later(A, 'GetSecurityInfo', [w.HANDLE, ctypes.c_int, w.DWORD,
                                            ctypes.POINTER(w.LPVOID), w.LPVOID, w.LPVOID,
                                            w.LPVOID, ctypes.POINTER(w.LPVOID)], w.DWORD)
from_sddl = _later(A, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
                    [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), w.LPVOID], w.BOOL)
to_sddl = _later(A, 'ConvertSecurityDescriptorToStringSecurityDescriptorW',
                  [w.LPVOID, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), w.LPVOID], w.BOOL)


def sid_string(sid):
    text = w.LPWSTR()
    if not sid_text(sid, ctypes.byref(text)):
        raise ProcError('sid-query-failed')
    try:
        return text.value
    finally:
        local_free(text)


def process_sids(pid):
    process = open_process(0x1000, False, pid)
    if not process:
        raise ProcError('process-token-unavailable')
    token = w.HANDLE()
    try:
        if not open_token(process, 8, ctypes.byref(token)):
            raise ProcError('process-token-unavailable')
        output = []
        for kind in (1, 4):  # TokenUser, TokenOwner; both begin with PSID.
            size = w.DWORD()
            token_info(token, kind, None, 0, ctypes.byref(size))
            if not ctypes.sizeof(w.LPVOID) <= size.value <= 65536:
                raise ProcError('token-size-invalid')
            buffer = ctypes.create_string_buffer(size.value)
            if not token_info(token, kind, buffer, size.value, ctypes.byref(size)):
                raise ProcError('token-query-failed')
            output.append(sid_string(ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0]))
        return tuple(output)
    finally:
        if token:
            close(token)
        close(process)


def process_image(pid):
    process = open_process(0x1000, False, pid)
    if not process:
        raise ProcError('process-image-unavailable')
    query = bind(K, 'QueryFullProcessImageNameW',
                 [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL)
    try:
        size = w.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query(process, 0, buffer, ctypes.byref(size)):
            raise ProcError('process-image-unavailable')
        return Path(buffer.value).resolve(strict=True)
    finally:
        close(process)


def descriptor(handle, kind=6):
    owner, security = w.LPVOID(), w.LPVOID()
    if security_info(handle, kind, 5, ctypes.byref(owner), None, None, None,
                     ctypes.byref(security)):
        raise ProcError('pipe-security-unavailable')
    text = w.LPWSTR()
    try:
        if not to_sddl(security, 1, 5, ctypes.byref(text), None):
            raise ProcError('pipe-security-unavailable')
        return sid_string(owner), text.value
    finally:
        if text:
            local_free(text)
        local_free(security)


class Pipe:
    def __init__(self, handle):
        self.handle = handle

    def close(self):
        if self.handle is not None:
            close(self.handle)
            self.handle = None


def connect(name, pid, creation):
    if not name.startswith('\\\\.\\pipe\\') or not creation:
        raise ProcError('nonlocal-pipe-or-missing-identity')
    handle = create_file(name, 0xC0020000, 0, None, 3, 0x00100000, None)
    if handle == INVALID:
        raise ProcError('pipe-connect-failed')
    pipe = Pipe(handle)
    try:
        actual = w.ULONG()
        if not server_pid(handle, ctypes.byref(actual)) or actual.value != pid or proc_ctime(pid) != creation:
            raise ProcError('pipe-process-identity-mismatch')
        user, owner = process_sids(pid)
        if user != process_sids(os.getpid())[0] or descriptor(handle)[0] not in (user, owner):
            raise ProcError('pipe-owner-mismatch')
        return pipe
    except BaseException:
        pipe.close()
        raise


class SecurityAttributes(ctypes.Structure):
    _fields_ = [('length', w.DWORD), ('descriptor', w.LPVOID), ('inherit', w.BOOL)]


class PrivateListener(Pipe):
    def __init__(self, name):
        user = process_sids(os.getpid())[0]
        security = w.LPVOID()
        sddl = 'O:%sD:P(A;;GA;;;%s)' % (user, user)
        if not from_sddl(sddl, 1, ctypes.byref(security), None):
            raise ProcError('private-acl-creation-failed')
        try:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), security, False)
            handle = create_pipe(name, 0x00080003, 1, 1, 4096, 4096, 0, ctypes.byref(attributes))
        finally:
            local_free(security)
        if handle == INVALID:
            raise ProcError('private-pipe-creation-failed')
        super().__init__(handle)
        try:
            owner, actual = descriptor(handle)
            dacl = actual.split('D:', 1)[1]
            if owner != user or dacl not in ('P(A;;GA;;;%s)' % user, 'P(A;;FA;;;%s)' % user):
                raise ProcError('private-acl-readback-mismatch')
        except BaseException:
            self.close()
            raise

    def accept(self, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if connect_pipe(self.handle, None) or ctypes.get_last_error() in (535, 232):
                # A short-lived receipt client may already have closed. Drain
                # its buffered bytes; closure alone never authenticates it.
                return
            if ctypes.get_last_error() != 536:
                raise ProcError('private-pipe-accept-failed')
            time.sleep(.02)
        raise ProcError('private-pipe-accept-timeout')

    def reset(self):
        """Reuse this same authenticated reply address for a later status."""
        if not disconnect_pipe(self.handle):
            raise ProcError('private-pipe-disconnect-failed')
        # Unlike the newly created first instance, a disconnected instance must
        # enter listening state before another client can open it. PIPE_NOWAIT
        # makes this a bounded state change, not a waiting call.
        if not connect_pipe(self.handle, None) and ctypes.get_last_error() not in (536, 535, 232):
            raise ProcError('private-pipe-rearm-failed')

    def verify_client(self, pid, creation):
        """Kernel identity of a still-connected client, never its role."""
        if type(pid) is not int or type(creation) is not int or pid <= 0 or creation <= 0:
            raise ProcError('invalid-client-identity')
        actual = w.ULONG()
        if (not client_pid(self.handle, ctypes.byref(actual)) or actual.value != pid
                or proc_ctime(pid) != creation):
            raise ProcError('pipe-client-process-mismatch')
        if process_sids(pid)[0] != process_sids(os.getpid())[0]:
            raise ProcError('pipe-client-user-mismatch')
        return {'client_process_verified': True, 'client_user_verified': True}


def win32_refusal(code):
    """ProcError(code) carrying this thread's GetLastError as `winerror`, also named in its text."""
    error = ProcError(code)
    error.winerror = ctypes.get_last_error()
    error.args = ('%s (Win32 error %d)' % (error.args[0], error.winerror),)
    return error


def protect_file(path):
    """Restrict a newly created file to this user before advertising it; never a native key.

    The protected DACL goes first, on its own: the file's owner holds WRITE_DAC implicitly, but
    WRITE_OWNER only through an ACE, and a folder whose inherited ACEs give this user none (Modify
    through Authenticated Users, Administrators deny-only in a filtered token) refused the old
    single owner-and-DACL call with ERROR_ACCESS_DENIED. Once the DACL grants this user full
    access, the owner is set to this user too, and both are read back."""
    user = process_sids(os.getpid())[0]
    set_security = bind(A, 'SetFileSecurityW', [w.LPCWSTR, w.DWORD, w.LPVOID], w.BOOL)
    # PROTECTED_DACL|DACL_SECURITY_INFORMATION, then OWNER_SECURITY_INFORMATION.
    for flags, sddl in ((0x80000004, 'D:P(A;;GA;;;%s)' % user), (0x1, 'O:%s' % user)):
        security = w.LPVOID()
        if not from_sddl(sddl, 1, ctypes.byref(security), None):
            raise win32_refusal('private-file-acl-creation-failed')
        try:
            if not set_security(str(path), flags, security):
                raise win32_refusal('private-file-acl-set-failed')
        finally:
            local_free(security)
    handle = create_file(str(path), 0x20000, 7, None, 3, 0, None)
    if handle == INVALID:
        raise win32_refusal('private-file-acl-readback-failed')
    try:
        owner, actual = descriptor(handle, 1)
        # Windows maps generic file access to FILE_ALL_ACCESS in the readback.
        if owner != user or actual.split('D:', 1)[1] not in (
                'P(A;;FA;;;%s)' % user, 'P(A;;GA;;;%s)' % user):
            raise ProcError('private-file-acl-readback-failed')
    finally:
        close(handle)
