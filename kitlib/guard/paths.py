"""The guard's one path normaliser and placer (multi-host plan section 6, rule 2;
docs/linux-support-plan.md section 3, "Guard path semantics").

Every place the guard folds a filesystem path to compare it, key a cache on it or build a canonical
form calls norm(). On Windows the rules are unchanged: os.path.normpath folds `.`, `..`, doubled
and mixed separators, os.path.normcase folds case (and `/` to `\\`).

  norm(path)                  the comparison form: normpath, then the case fold
  norm(path, absolute=True)   the same of os.path.abspath(path) (relative to this process's folder)
  norm(path, real=True)       the same of os.path.realpath(path): links and junctions resolved; a
                              path realpath cannot read falls back to abspath
  norm(path, fold=False)      any of the above without the case fold: a path to show or join to
  norm(path, strict=True)     the fold for what the guard allows (Linux; the same on Windows)

On Linux (kitlib.platform.NAME 'linux', posix()) a path is absolute when it starts with `/`; there
are no drives and `\\` is a name's character. Case folds where the filesystem folds it: each part
of a path is folded when the mount that holds it (the deepest mount point at or above its folder,
read once from /proc/self/mountinfo, _mounts) has an fstype that folds (FOLDING_FS). One whose
fstype is neither folding nor known not to fold (CASE_FS), and every part when the table cannot
be read, is in doubt: the fold for what the guard refuses or judges as protected (the default)
folds it, which refuses more; the strict fold, for what it allows (kitlib.guard.jail's allowed
roots), does not, so doubt never widens what an agent may write. A folder with ext4's per-folder
casefold attribute on a mount that does not fold is not read (docs/guard.md "Known limits").

Not paths here: the text of a command line (drive letters, quoting, PowerShell provider paths) is
read by kitlib.guard.shell and commands, and a URL by kitlib.guard.prepush; those are a shell's
grammar, not the filesystem's.
"""
import os
import re

import kitlib.platform

# fstypes whose names compare without case (as Linux mounts them by default)
FOLDING_FS = frozenset(('vfat', 'msdos', 'exfat', 'cifs', 'smb3', 'smbfs', 'hfs', 'hfsplus'))
# fstypes whose names compare with case
CASE_FS = frozenset((
    'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'f2fs', 'jfs', 'reiserfs', 'nilfs2', 'bcachefs',
    'tmpfs', 'ramfs', 'overlay', 'squashfs', 'erofs', 'nfs', 'nfs4', 'proc', 'sysfs', 'devtmpfs',
    'devpts', 'cgroup2', 'securityfs', 'debugfs', 'tracefs', 'mqueue', 'hugetlbfs', 'fusectl',
    'configfs', 'pstore', 'bpf', 'efivarfs', 'binfmt_misc'))
MOUNTINFO = '/proc/self/mountinfo'


def posix():
    """True when the guard reads paths as Linux does (kitlib.platform.NAME, read now)."""
    return kitlib.platform.NAME == 'linux'


def norm(path, absolute=False, real=False, fold=True, strict=False):
    if real:
        try:
            p = os.path.realpath(path)
        except (OSError, ValueError):
            p = os.path.abspath(path)
    elif absolute:
        p = os.path.abspath(path)
    else:
        p = os.path.normpath(path)
    if not fold:
        return p
    if posix():
        return _fold_posix(p, strict)
    return os.path.normcase(p)


def fold(path, strict=False):
    """The case fold of norm() alone, on `path` as written (no `.` or `..` folded)."""
    return _fold_posix(path, strict) if posix() else os.path.normcase(path)


def _fold_posix(p, strict):
    """`p` (normalised) with each part folded whose mount folds (in doubt: unless `strict`)."""
    mounts = _mounts()
    if mounts is None or not p.startswith('/'):
        return p if strict else p.lower()
    parts = p.split('/')
    out = [parts[0]]
    for k in range(1, len(parts)):
        folder = '/'.join(parts[:k]) or '/'
        out.append(parts[k].lower() if _folds(_mount_of(mounts, folder), strict) else parts[k])
    return '/'.join(out)


def _folds(fstype, strict):
    if fstype in FOLDING_FS:
        return True
    if fstype in CASE_FS:
        return False
    return not strict


def _mount_of(mounts, folder):
    """The fstype of the deepest mount point at or above `folder` (None when none is)."""
    best, fstype = -1, None
    for point, kind in mounts:
        if folder == point or point == '/' or folder.startswith(point.rstrip('/') + '/'):
            if len(point) > best:
                best, fstype = len(point), kind
    return fstype


_TABLE = []


def _mounts():
    """[(mount point, fstype)] from /proc/self/mountinfo, a later line over an earlier one at the
    same point, read once per process (a hook is one process per call); None when it cannot be
    read. Deviation from the plan's seam (section 2.1): this reader belongs in
    kitlib/platform/linux/paths.py, which slice 9 builds; it moves there then."""
    if not _TABLE:
        _TABLE.append(_read_mounts())
    return _TABLE[0]


def _read_mounts():
    try:
        with open(MOUNTINFO, encoding='utf-8', errors='surrogateescape') as f:
            text = f.read()
    except OSError:
        return None
    table = {}
    for line in text.splitlines():
        head, sep, tail = line.partition(' - ')
        fields, rest = head.split(), tail.split()
        if not sep or len(fields) < 5 or not rest:
            continue
        table[_unescape(fields[4])] = rest[0]
    return sorted(table.items()) or None


def _unescape(text):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), text)
