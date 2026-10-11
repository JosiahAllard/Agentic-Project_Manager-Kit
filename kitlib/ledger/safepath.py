"""The rulings ledger's path checks, run before every read or write of it (docs/ledger.md "Path
checks"). Defence in depth beside the guard, which refuses only a direct edit of the ledger file.

`refusal(path)` is None when the path may be used, else one sentence saying why not and the fix.
The checks and the open that follows are separate calls (no lock), so a swap in between is not
caught. Three checks:

- reparse: the file and every existing folder above it, except the drive root, must not be a
  reparse point (a junction, a symlink or any other tag), read with os.lstat's
  st_file_attributes. A swapped-in junction or link would point the ledger somewhere else.
- hard links: the file must have one link (st_nlink); another name would be an unguarded path.
- owner: the file and its own folder, where they exist, must be owned by this process's user,
  Administrators or SYSTEM (GetNamedSecurityInfoW). An owner that cannot be read is a refusal.

A part that does not exist yet is skipped: the writer creates the file, and its folder. Nothing
here prints or exits.
"""
import os
import stat

REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
ADMINISTRATORS = 'S-1-5-32-544'
SYSTEM = 'S-1-5-18'

_MISSING = (FileNotFoundError, NotADirectoryError)


REPARSE_FIX = ('point ledger_path at the real target, or move the ledger off OneDrive (a cloud '
               'placeholder)')
OWNER_FIX = 'check the owner (takeown, or icacls /setowner)'
LINK_FIX = 'remove the other links (fsutil hardlink list), or copy the file to a new one'


def refusal(path):
    """None when `path` may be read or written as the rulings ledger; else why not, with the fix."""
    full = os.path.abspath(path)
    head = 'the rulings ledger path %s is refused: ' % full
    p = full
    while os.path.dirname(p) != p:              # the drive root is not checked
        try:
            st = os.lstat(p)
        except _MISSING:
            st = None
        except OSError as exc:
            return head + 'cannot check %s (%s): check that this user may read it' % (p, exc)
        if st is not None and getattr(st, 'st_file_attributes', 0) & REPARSE_POINT:
            return head + ('%s is a reparse point (a junction, symlink or other tag); the ledger '
                           'file and every folder above it must be plain: %s' % (p, REPARSE_FIX))
        if st is not None and p == full and stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            return head + ('the file has %d hard links, so an edit through another name reaches '
                           'it: %s' % (st.st_nlink, LINK_FIX))
        p = os.path.dirname(p)
    for p in (full, os.path.dirname(full)):
        if not os.path.lexists(p):
            continue
        sid, why = owner_sid(p)
        if sid is None:
            return head + 'cannot read the owner of %s (%s): %s' % (p, why, OWNER_FIX)
        me, why = user_sid()
        if me is None:
            return head + 'cannot read this process\'s user (%s)' % why
        if sid not in (me, ADMINISTRATORS, SYSTEM):
            return head + ('%s is owned by %s, not by this user (%s), Administrators or SYSTEM: '
                           '%s' % (p, sid, me, OWNER_FIX))
    return None


# ------------------------------------------------------------------ Windows security (ctypes)

_API = []
_ME = []


def _win():
    """advapi32 and kernel32 with every signature this module calls declared."""
    if _API:
        return _API[0]
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    vp, dw, h = ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE
    kernel.GetCurrentProcess.restype = h
    kernel.GetCurrentProcess.argtypes = []
    kernel.CloseHandle.argtypes = [h]
    kernel.LocalFree.argtypes = [vp]
    kernel.LocalFree.restype = vp
    advapi.OpenProcessToken.argtypes = [h, dw, ctypes.POINTER(h)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [h, ctypes.c_int, vp, dw, ctypes.POINTER(dw)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, dw, ctypes.POINTER(vp),
                                             vp, vp, vp, ctypes.POINTER(vp)]
    advapi.GetNamedSecurityInfoW.restype = dw
    advapi.ConvertSidToStringSidW.argtypes = [vp, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL

    class Api(object):
        pass
    api = Api()
    api.ctypes, api.wintypes, api.advapi, api.kernel = ctypes, wintypes, advapi, kernel
    _API.append(api)
    return api


def _sid_text(api, sid):
    """The `S-1-...` form of a SID pointer, or None."""
    out = api.wintypes.LPWSTR()
    if not api.advapi.ConvertSidToStringSidW(sid, api.ctypes.byref(out)):
        return None
    try:
        return out.value
    finally:
        api.kernel.LocalFree(api.ctypes.cast(out, api.ctypes.c_void_p))


def owner_sid(path):
    """`(owner SID text, None)` or `(None, why)` for a file or folder. The tests' seam."""
    if os.name != 'nt':
        return None, 'the owner check is Windows only'
    api = _win()
    ctypes = api.ctypes
    owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
    rc = api.advapi.GetNamedSecurityInfoW(path, 1, 1, ctypes.byref(owner), None, None, None,
                                          ctypes.byref(sd))     # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION
    if rc != 0:
        return None, 'GetNamedSecurityInfoW returned %d' % rc
    try:
        text = _sid_text(api, owner) if owner.value else None
        return (text, None) if text else (None, 'no owner SID')
    finally:
        api.kernel.LocalFree(sd)


def user_sid():
    """`(this process's user SID text, None)` or `(None, why)`; cached once read."""
    if _ME:
        return _ME[0], None
    if os.name != 'nt':
        return None, 'the owner check is Windows only'
    api = _win()
    ctypes, wintypes = api.ctypes, api.wintypes
    token = wintypes.HANDLE()
    if not api.advapi.OpenProcessToken(api.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None, 'OpenProcessToken failed (%d)' % ctypes.get_last_error()
    try:
        need = wintypes.DWORD()
        api.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(need))   # TokenUser
        buf = ctypes.create_string_buffer(need.value)
        if not api.advapi.GetTokenInformation(token, 1, buf, need, ctypes.byref(need)):
            return None, 'GetTokenInformation failed (%d)' % ctypes.get_last_error()
        text = _sid_text(api, ctypes.c_void_p.from_buffer(buf).value)   # TOKEN_USER: the SID pointer first
    finally:
        api.kernel.CloseHandle(token)
    if not text:
        return None, 'no user SID'
    _ME.append(text)
    return text, None
