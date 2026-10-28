r"""What holds a worktree, and the walks that read one without leaving it (docs/worktree-remove.md).

Why this exists: on Windows `git worktree remove` is DESTRUCTIVE ON FAILURE. Measured 2026-09-22 (git 2.38.1.windows.1): git deletes the
worktree's `.git` file and its `.git/worktrees/<id>` entry, then fails partway through the tree, so the path leaves `git worktree
list` while a stump stays on disk, and a retry answers `not a working tree`. Two holder kinds were
measured to do it: a process whose current directory is in the tree, and a file held open without
FILE_SHARE_DELETE (an ordinary CPython `open(path, 'r')` is one). So everything is checked BEFORE
git runs.

`blockers(root)` is the authority: it asks Windows for DELETE access on every entry, sharing
everything, and closes the handle again; a path another handle denies delete on answers
ERROR_SHARING_VIOLATION (32). `cwd_holders(root)` only NAMES processes for the cwd case and says how
many it could not read, so "named nobody" is never "nothing holds it". Every walk here stops past
WALK_MAX entries and never descends a link or junction (`prune`). Nothing here deletes anything.

On Linux (docs/linux-support-plan.md slice 9) nothing denies a delete, so `blockers` and
`cwd_holders` read /proc instead (kitlib.platform.linux.paths.process_paths): every path inside
`root` that a process of this user uses (its current folder, root, program, an open file or a
mapped file) is HELD. A process of this user that cannot be read in full makes the root UNREAD
(but for this login's own: the user manager, its (sd-pam) and sshd-session, paths._login_process;
describe names each unread process), as
does a tree the scan's mount table shows at another path too: its device mounted again where the
roots overlap (a bind mount), a mount inside it or at or above the one holding it, or no mount
holding it (paths.aliased), since a process there uses the tree under a path not under `root`.
A /proc that cannot be listed, hides processes of this user by a mount option, or belongs to a
PID namespace other than the first (a container, a WSL2 distro) makes it NOT_SEARCHED. Each is a
refusal: the answer is empty only when every process of this user was read and none uses the tree.
Another user's processes cannot be seen.
"""
import ctypes
import os
import re
import stat

import kitlib.platform
from kitlib.platform.linux import paths as linux_paths

WINDOWS = kitlib.platform.NAME == 'windows'

DELETE = 0x00010000
SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004      # read | write | delete
OPEN_EXISTING = 3
BACKUP_SEMANTICS = 0x02000000
OPEN_REPARSE_POINT = 0x00200000     # open a link or junction itself, never what it points at
INVALID_HANDLE = ctypes.c_void_p(-1).value
SHARING_VIOLATION = 32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x2
# Every walk of a tree stops past this many entries, and a tree not walked in full is never called
# free, idle or clean.
WALK_MAX = 200000
NOT_SEARCHED = -1                   # `blockers`: the tree was past WALK_MAX, not probed in full
#                                     (on Linux: /proc could not be listed, hides processes of
#                                     this user by a mount option, or belongs to a PID
#                                     namespace other than the first)
HELD = -2                           # `blockers` on Linux: a process of this user uses this path
UNREAD = -3                         # `blockers` on Linux: a process of this user could not be read,
#                                     its device is mounted again where the roots overlap, a mount
#                                     stands inside it or over the one holding it, or no mount
#                                     holds it (paths.aliased)
SHOWN = 20                          # a refusal names this many files or links, then counts the rest
LINK_ADVICE = "remove the link by hand first; git's handling of it is untested"


def canon(path):
    """The one comparable form of a path: absolute, junctions and links resolved, case folded."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def linked(path):
    """True for a symbolic link, an NTFS junction or any other reparse point (the attribute is read
    itself: `os.walk` descends a junction). A path that cannot be read answers False."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def prune(where, dirs):
    """Drop every link and junction from a topdown `os.walk`'s `dirs`, so the walk stays in the
    tree. GOTCHA: a junction to an ancestor is a cycle (one repo's walk ran to WinError 1921)."""
    dirs[:] = [d for d in dirs if not linked(os.path.join(where, d))]


def extended(path):
    r"""`\\?\`-prefixed absolute path, so a tree past the Windows path limit is still measurable."""
    path = os.path.abspath(path)
    if not WINDOWS:
        return path
    return path if path.startswith('\\\\?\\') else '\\\\?\\' + path


def plain(path):
    return path[4:] if path.startswith('\\\\?\\') else path


def kernel32():
    lib = ctypes.WinDLL('kernel32', use_last_error=True)
    # GOTCHA: a HANDLE through the default restype (c_int) is truncated, and a FAILED open then
    # compares unequal to INVALID_HANDLE_VALUE, so every locked path reads as deletable.
    lib.CreateFileW.restype = ctypes.c_void_p
    lib.OpenProcess.restype = ctypes.c_void_p
    lib.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    return lib


def _linux():
    return kitlib.platform.NAME == 'linux'


def _under(path, target):
    """True when the /proc path `path` is the folder `target` (a canon form) or under it."""
    flat = os.path.normcase(path)
    if target.endswith(os.sep):                 # the root of the file system
        return flat.startswith(target)
    return flat == target or flat.startswith(target + os.sep)


def blockers(root):
    """Every path under `root` that cannot be deleted right now, as (path, windows error); on
    Linux, as (path, HELD), with (root, UNREAD) and (root, NOT_SEARCHED) (module text); [] on any
    other platform. A walk error is reported too, and past WALK_MAX entries the root is reported
    with NOT_SEARCHED. A link or junction is probed as itself and never descended."""
    if _linux():
        return _linux_blockers(root)
    if not WINDOWS:
        return []
    lib = kernel32()
    found, walk_errors = [], []

    def undeletable(path):
        return _delete_error(lib, path)

    seen = 0
    for where, dirs, files in os.walk(extended(root), onerror=walk_errors.append):
        seen += len(dirs) + len(files)
        if seen > WALK_MAX:
            found.append((plain(os.path.abspath(root)), NOT_SEARCHED))
            break
        for name in files + dirs:
            path = os.path.join(where, name)
            err = undeletable(path)
            if err:
                found.append((plain(path), err))
        prune(where, dirs)
    err = undeletable(extended(root))
    if err:
        found.append((plain(os.path.abspath(root)), err))
    for failure in walk_errors:
        found.append((plain(str(getattr(failure, 'filename', root))),
                      getattr(failure, 'winerror', None) or 0))
    return found


def _linux_blockers(root):
    whole = os.path.abspath(root)
    try:
        scan = linux_paths.process_paths()
    except OSError:                       # no /proc, an empty one, or not this system's
        return [(whole, NOT_SEARCHED)]
    if not scan.total:
        return [(whole, NOT_SEARCHED)]
    target = canon(root)
    found, named = [], set()
    for _pid, _name, _kind, path in scan.entries:
        if path not in named and _under(path, target):
            named.add(path)
            found.append((path, HELD))
    if scan.unreadable or linux_paths.aliased(scan.mounts, os.path.realpath(whole)):
        found.append((whole, UNREAD))         # unread, or reachable by a path /proc names apart
    return found


def _delete_error(lib, path):
    """0 when `path` opens for DELETE (every share mode granted; the handle is closed again), else
    the Windows error: SHARING_VIOLATION when another handle denies delete."""
    handle = lib.CreateFileW(path, DELETE, SHARE_ALL, None, OPEN_EXISTING,
                             BACKUP_SEMANTICS | OPEN_REPARSE_POINT, None)
    if handle is None or handle == INVALID_HANDLE:
        return ctypes.get_last_error()
    lib.CloseHandle(ctypes.c_void_p(handle))
    return 0


def held_folder(root):
    """The first folder under `root` (itself included) that cannot be opened for DELETE, as
    (path, windows error), or None when every folder opened: `blockers` for folders only, so a
    holder is found without naming it. Windows holds a process's current directory open without
    FILE_SHARE_DELETE, so that folder answers SHARING_VIOLATION and its parents do not (measured
    2026-10-19); a file held open is not looked for. Fails closed: a walk error is
    (its path, its error or 0), a tree past WALK_MAX entries is (root, NOT_SEARCHED), and off
    Windows the answer is (root, NOT_SEARCHED). A link or junction is never descended."""
    whole = plain(os.path.abspath(root))
    if not WINDOWS:
        return whole, NOT_SEARCHED
    lib = kernel32()
    walk_errors = []
    err = _delete_error(lib, extended(root))
    if err:
        return whole, err
    seen = 0
    for where, dirs, files in os.walk(extended(root), onerror=walk_errors.append):
        seen += len(dirs) + len(files)
        if seen > WALK_MAX:
            return whole, NOT_SEARCHED
        prune(where, dirs)
        for name in dirs:
            path = os.path.join(where, name)
            err = _delete_error(lib, path)
            if err:
                return plain(path), err
    for failure in walk_errors:
        return (plain(str(getattr(failure, 'filename', root))),
                getattr(failure, 'winerror', None) or 0)
    return None


def processes():
    """(pid, image name) for every process in a toolhelp snapshot."""
    lib = kernel32()

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong),
                    ('th32ProcessID', ctypes.c_ulong), ('th32DefaultHeapID', ctypes.c_void_p),
                    ('th32ModuleID', ctypes.c_ulong), ('cntThreads', ctypes.c_ulong),
                    ('th32ParentProcessID', ctypes.c_ulong), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', ctypes.c_ulong), ('szExeFile', ctypes.c_char * 260)]

    snapshot = lib.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == INVALID_HANDLE:
        return []
    entry = Entry()
    entry.dwSize = ctypes.sizeof(entry)
    out = []
    try:
        if lib.Process32First(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
            while True:
                out.append((entry.th32ProcessID, entry.szExeFile.decode('mbcs', 'replace')))
                if not lib.Process32Next(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                    break
    finally:
        lib.CloseHandle(ctypes.c_void_p(snapshot))
    return out


def current_directory(pid):
    """One process's current directory, read out of its PEB (64-bit offsets). OSError when it
    cannot be read."""
    lib = kernel32()
    ntdll = ctypes.WinDLL('ntdll')
    handle = lib.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if handle is None or handle == INVALID_HANDLE or handle == 0:
        raise OSError('OpenProcess %d' % ctypes.get_last_error())
    handle = ctypes.c_void_p(handle)

    def read(address, size):
        buf = ctypes.create_string_buffer(size)
        if not lib.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, size, None):
            raise OSError('ReadProcessMemory %d' % ctypes.get_last_error())
        return buf.raw

    class BasicInformation(ctypes.Structure):
        _fields_ = [('Reserved1', ctypes.c_void_p), ('PebBaseAddress', ctypes.c_void_p),
                    ('Reserved2', ctypes.c_void_p * 2), ('UniqueProcessId', ctypes.c_void_p),
                    ('Reserved3', ctypes.c_void_p)]

    try:
        info = BasicInformation()
        status = ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info),
                                                 ctypes.sizeof(info), None)
        if status != 0 or not info.PebBaseAddress:
            raise OSError('NtQueryInformationProcess 0x%x' % (status & 0xffffffff))
        parameters = int.from_bytes(read(info.PebBaseAddress + 0x20, 8), 'little')
        raw = read(parameters + 0x38, 16)                     # CurrentDirectory.DosPath
        length, buffer = int.from_bytes(raw[0:2], 'little'), int.from_bytes(raw[8:16], 'little')
        if not length or not buffer:
            raise OSError('empty current directory')
        return read(buffer, length).decode('utf-16-le', 'replace')
    finally:
        lib.CloseHandle(handle)


def cwd_holders(root):
    """(holders, unreadable, total): processes whose current directory is at or under `root`, as
    (pid, image, cwd). `unreadable` is how many this scan could not open. On Linux, from /proc:
    processes of this user only, `unreadable` counting those of this user; ([], 0, 0) when /proc
    cannot be listed, and on any other platform."""
    if _linux():
        try:
            scan = linux_paths.process_paths(('cwd',))
        except OSError:
            return [], 0, 0
        target = canon(root)
        return ([(pid, name, where) for pid, name, _kind, where in scan.entries
                 if _under(where, target)], scan.unreadable, scan.total)
    if not WINDOWS:
        return [], 0, 0
    target = canon(root).rstrip('\\')
    found, unreadable, seen = [], 0, processes()
    for pid, name in seen:
        try:
            where = current_directory(pid)
        except OSError:
            unreadable += 1
            continue
        flat = os.path.normcase(where).rstrip('\\')
        if not (flat == target or flat.startswith(target + '\\')):
            try:                             # a cwd taken through a junction alias
                flat = canon(where).rstrip('\\')
            except (OSError, ValueError):
                continue
        if flat == target or flat.startswith(target + '\\'):
            found.append((pid, name, where))
    return found, unreadable, len(seen)


def _linux_cwds(root):
    """(holders, unread, total) on Linux from one full /proc scan: holders as cwd_holders gives
    them, unread the processes of this user it could not read in full, as (pid, name);
    ([], [], 0) when /proc cannot be read."""
    try:
        scan = linux_paths.process_paths()
    except OSError:
        return [], [], 0
    target = canon(root)
    return ([(pid, name, where) for pid, name, kind, where in scan.entries
             if kind == 'cwd' and _under(where, target)], list(scan.unread), scan.total)


def inside(path, root):
    """True when `path` is `root` or under it (canon forms); False when either cannot be read."""
    try:
        where, target = canon(path).rstrip('\\/'), canon(root).rstrip('\\/')
    except (OSError, ValueError):
        return False
    return where == target or where.startswith(target + os.sep)


def describe(root, why, cwd=None):
    """The lines saying what holds `root`, as precisely as this machine can answer. Three states
    kept apart: nothing holds it; named current-directory holders; blocked with no cwd holder (a
    file handle, or a process the scan could not open). `cwd`: the caller's directory (default
    this process's), for the "THIS command is a holder" line."""
    linux = _linux()
    if not why:
        first = ('  free     nothing is holding it: no process of this user uses anything in it'
                 if linux else
                 '  free     nothing is holding it: every entry answered a DELETE-access open just')
        return [first,
                '           now. No process to chase and nothing to wait for - a human can delete',
                '           the directory, and this tool never will.']
    out = []
    for path, err in why:
        note = ('held open (ERROR_SHARING_VIOLATION)' if err == SHARING_VIOLATION else
                'in use by a process of this user (its current folder, root, program, an open'
                ' file or a mapped file)' if err == HELD else
                'not known to be free: a process of this user could not be read in full, or the'
                ' tree shows at another path too (its device mounted again where the roots'
                ' overlap, a mount inside it or over it, or no mount holding it)' if err == UNREAD
                else 'not searched: the process list (%s) cannot be read, hides processes of this'
                ' user (a mount option) or belongs to a PID namespace other than the first, so'
                ' not known to be free'
                % linux_paths.PROC if err == NOT_SEARCHED and linux else
                'not probed in full: more than %d entries, so not known to be free' % WALK_MAX
                if err == NOT_SEARCHED else 'error %d' % err)
        out.append('  blocked  %s  %s' % (path, note))
    if linux:
        holders, unread, total = _linux_cwds(root)
        unreadable = len(unread)
    else:
        holders, unreadable, total = cwd_holders(root)
        unread = []
    mine = os.getpid()
    for pid, name, where in holders:
        out.append('  holder   pid %d  %s  cwd %s%s'
                   % (pid, name, where, '   <- THIS process' if pid == mine else ''))
    for pid, name in unread:
        out.append('  unread   pid %d  %s  a process of this user this scan could not read in full,'
                   ' so it may hold the tree' % (pid, name))
    if not holders:
        out += ['  holder   no process has its current directory in here, so the block is an open',
                '           file handle (an ordinary read handle is enough), or a process this scan',
                '           could not open. Nothing here is proof of absence.']
    if linux:
        out.append("  scanned  %d process(es), %d of this user unreadable; another user's"
                   " processes are not seen" % (total, unreadable))
    else:
        out.append('  scanned  %d process(es), %d unreadable (not this user, or protected)'
                   % (total, unreadable))
    if any(pid == mine for pid, _, _ in holders) or inside(cwd or os.getcwd(), root):
        return out + ['  do       THIS command is one of the holders: it was started with its working',
                      '           directory inside the tree, so it is standing on what it was asked to',
                      '           remove. Run it again from anywhere else; nothing else need change.']
    return out + ['  do       release it at the source - end the process, or close the file and move that',
                  '           shell out with an absolute `cd` elsewhere - then run this again. Do not',
                  '           force or rename it: a forced delete on a directory something is using is',
                  '           worse than the leak.']


def path_glob(glob, flags=0):
    """`glob` as a compiled whole-string match in which `*` and `?` never cross `/` (fnmatch's do)
    and a `[!...]` class never matches `/`. Case-sensitive unless `flags` says otherwise."""
    out, i, n = [], 0, len(glob)
    while i < n:
        c = glob[i]
        i += 1
        if c == '*':
            out.append('[^/]*')
        elif c == '?':
            out.append('[^/]')
        elif c == '[':
            j = i + 1 if i < n and glob[i] in '!^' else i
            j = j + 1 if j < n and glob[j] == ']' else j
            while j < n and glob[j] != ']':
                j += 1
            if j >= n:
                out.append(re.escape(c))
                continue
            body, i = glob[i:j], j + 1
            if body[:1] in ('!', '^'):
                out.append('[^/%s]' % body[1:].replace('\\', '\\\\'))
            else:
                out.append('[%s]' % body.replace('\\', '\\\\'))
        else:
            out.append(re.escape(c))
    return re.compile('(?:%s)\\Z' % ''.join(out), flags)


def listed(items, most=SHOWN):
    """The first `most` of `items` joined by '; ', and a count of the rest."""
    return '%s%s' % ('; '.join(items[:most]),
                     ' ... and %d more' % (len(items) - most) if len(items) > most else '')


def links_inside(root):
    """`(links, None)`: every symbolic link and junction under `root` as `/`-separated paths from
    it, none descended; `(None, why)` when the tree cannot be walked in full."""
    found, errors, seen = [], [], 0
    top = os.path.abspath(root)
    for where, dirs, files in os.walk(extended(top), onerror=errors.append):
        seen += len(dirs) + len(files)
        if seen > WALK_MAX:
            return None, 'the tree could not be searched for links: more than %d entries' % WALK_MAX
        for name in dirs + files:
            full = os.path.join(where, name)
            if linked(full):
                found.append(os.path.relpath(plain(full), top).replace(os.sep, '/'))
        prune(where, dirs)
    if errors:
        return None, 'the tree could not be searched for links (%d error(s), first: %s)' % (
            len(errors), errors[0])
    return sorted(found), None


def link_reason(path):
    """Why a link or junction stops the removal of `path`, or None."""
    links, why = links_inside(path)
    if why:
        return why
    if links:
        return ('symbolic link(s) or junction(s) inside (%d): %s - %s (delete the link only, never'
                ' what it points at), then run this again' % (len(links), listed(links), LINK_ADVICE))
    return None


CASE_SENSITIVE_KEEPER = '*.local.*'


def keepers_in(root, rel, keepers, budget=None):
    """The keepers inside the build-output entry `rel` of the tree at `root`, as `!! ` lines: each
    file whose own name, or path from `root`, matches a `keepers` glob (case-insensitive, except `*.local.*`). A walk
    error, or a walk past `budget[0]` entries (shared across one tree), is a line too: what could
    not be read in full is not known to be safe."""
    # `*.local.*` is the one case-sensitive glob: a .NET framework DLL such as System.Transactions.Local.dll
    # is not a user's local-settings file. Every other glob keeps matching case-insensitively.
    patterns = [path_glob(g, 0 if g == CASE_SENSITIVE_KEEPER else re.IGNORECASE) for g in keepers]
    hits = []
    budget = [WALK_MAX] if budget is None else budget

    def check(sub):
        name = sub.rsplit('/', 1)[-1]
        if any(p.match(name) or p.match(sub) for p in patterns):
            hits.append('!! %s (a keeper inside build output)' % sub)

    top = rel.rstrip('/')
    full = os.path.join(root, *top.split('/'))
    if linked(full) or not os.path.isdir(full):
        check(top)
        return hits
    errors = []
    for here, dirs, files in os.walk(full, onerror=errors.append):
        budget[0] -= len(dirs) + len(files)
        if budget[0] < 0:
            hits.append('!! %s/ (could not be searched in full for keepers: more than %d entries)'
                        % (top, WALK_MAX))
            return hits
        prune(here, dirs)
        base = os.path.relpath(here, root).replace(os.sep, '/')
        for f in files:
            check('%s/%s' % (base, f))
    for e in errors:
        hits.append('!! %s/ (could not be walked for keepers: %s)' % (top, e))
    return hits


def is_build_output(rel, globs, dirs=()):
    """True when the `!! ` entry `rel` (as `git status --ignored=matching` prints it, a directory
    ending in `/`) matches a build_output glob on its full path or its own final component - never
    a leading directory of a deeper entry - or is one of `dirs` (literal repo-relative folders, no
    trailing `/`; a `{piece, path}` entry resolved through the pieces table) or lies under one."""
    rel = rel.rstrip('/')
    if any(rel == d or rel.startswith(d + '/') for d in dirs):
        return True
    name = rel.rsplit('/', 1)[-1]
    for g in globs:
        pattern = path_glob(g.rstrip('/'))
        if pattern.match(rel) or pattern.match(name):
            return True
    return False
