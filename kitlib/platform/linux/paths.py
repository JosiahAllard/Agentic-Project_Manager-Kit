"""The kit's folders and process reads on Linux (docs/linux-support-plan.md section 2.2 and
section 8, slice 9), with the meanings the Windows lookups give them.

  profile()        the user's profile folder from the password database
                   (pwd.getpwuid(os.getuid()).pw_dir), never from $HOME: a session can change
                   that. kitlib.config.user.real_profile and kitlib.lock.store.profile_dir on
                   Linux; OSError when there is no entry or it is not absolute.
  read_pointer()   the PM-repo pointer (kitlib.config.pointer.registry on Linux) as
                   (value, why): MACHINE_POINTER when that file is there, else the user's
                   <profile>/.config/pm-kit/pointer (USER_POINTER); never $XDG_CONFIG_HOME, so
                   the environment cannot move it. A machine file that is there but bad is the
                   answer (BAD), never a fall back to the user's. One line, the PM repository path.
  temp()           the system temp folder, /tmp, whatever TMPDIR this process inherited
                   (kitlib.config.user.system_temp on Linux): every process agrees.
  process_paths()  every path this user's processes use, from /proc (kitlib.worktree.holders and
                   classify on Linux): Scan(entries, unreadable, total, mounts, unread).
  aliased()        whether a tree can be reached by another path too (a bind mount), from the
                   mount table the scan read (kitlib.worktree.holders on Linux).
  read_mountinfo() a mount table's text, this process's (<PROC>/self/mountinfo) by default, and
  parse_mountinfo() its mounts as Mount(device, root, point, fstype), the kernel's octal escapes
                   in root and point undone (kitlib.guard.paths reads each mount's fstype).

The process scan reads, for each process of this user, its current folder (cwd), root, program
(exe), every open file (fd/*) and every mapped file (maps). A process is this user's when any of
the four users of its status `Uid:` line (real, effective, saved, file system), or the owner of its
/proc/<pid> folder, is this user: the folder's owner is the effective user only (on Linux 7.0 it
stays so for a process the kernel marks not dumpable; only the entries inside go to root), so the
owner alone would miss a setuid program this user ran. A
process whose user cannot be told (no status, and a folder of this user's or root's that cannot be
read) counts as unreadable, as does a process of this user that cannot be read in full, so a caller
can refuse rather than call a tree free. Two kinds of process of this user whose links the kernel
refuses to this user (EACCES or EPERM on its ns/mnt) are this login's own and are counted in
`total` only, as another user's are (_login_process): one in this user's manager's own scope (its
cgroup v2 path ends /user@<uid>.service/init.scope: systemd --user and its (sd-pam)), and one that
has not exec'd since its fork (PF_FORKNOEXEC in its stat flags) whose parent, not PID 1, is another
user's (a root daemon's child that dropped to this user, still running the daemon's code:
sshd-session). Every other process of this user that cannot be read stays unreadable: ssh-agent,
gpg-agent, or an agent host that marks itself not dumpable. `unread` names them, as (pid, name).
A process that ends while it is read (its folder gone) is
skipped. A live leader's other threads (/proc/<pid>/task) give their cwd and root as well, since
one that called unshare(CLONE_FS) has its own, and one in another mount namespace (or whose
namespace cannot be read) leaves the process not read in full; a zombie leader whose other threads
still run has every kind read from them instead. A process of this user in another mount namespace (a
devcontainer, rootless podman) names paths of another tree, so it counts as unreadable, as does
one whose namespace cannot be read. A process of another user is counted in `total` only: this
user cannot see what it uses. A /proc that lists no process, or not this one, is mounted with an
option that hides processes of this user (hidepid=2 or 4, invisible, ptraceable, subset=pid; on
any proc mount there, not only the last), or whose mount options or whose own mount namespace
cannot be read, gives OSError, as when it cannot be listed; so does a scan run in any PID
namespace but the first (a container or a WSL2 distro: its ns/pid link is not INIT_PID) or whose
PID namespace cannot be read, whose /proc lists none of the processes outside it.
Not seen: a thread's own open-file table (CLONE_FILES unshared). PROC, _uid, _pid, _owner,
_status_uid, _link, _pw_dir and is_absolute are the seams a test points at a folder tree of its
own (this process's mountinfo and status are read under PROC too). This module imports on any
platform; its reads need Linux.
"""
import collections
import errno
import os
import posixpath
import re

PROC = '/proc'
MACHINE_POINTER = '/etc/pm-kit/pointer'
USER_POINTER = ('.config', 'pm-kit', 'pointer')     # under profile()
TEMP = '/tmp'
KINDS = ('cwd', 'root', 'exe', 'fd', 'map')
DELETED = ' (deleted)'                              # /proc's suffix on a removed file's path
GONE = (errno.ENOENT, errno.ESRCH)
EXITED = ('Z', 'X')                                  # stat states of an ended process

PF_FORKNOEXEC = 0x40                                 # stat flags: forked and not exec'd since
REFUSED = (errno.EACCES, errno.EPERM)                # the kernel will not show this user a link

# entries: (pid, name, kind, path) for each path a process of this user uses, kind one of KINDS;
# unreadable: how many processes of this user could not be read in full; total: every process
# /proc listed; mounts: this process's mount table, the mountinfo text (for aliased); unread:
# those processes, as (pid, name).
Scan = collections.namedtuple('Scan', 'entries unreadable total mounts unread')
# One line of a mountinfo table: device (major:minor), root (the mount's folder inside its file
# system: not / for a bind mount of a folder), point (where it is mounted), fstype (None when the
# line gives none after ` - `).
Mount = collections.namedtuple('Mount', 'device root point fstype')


def _pw_dir():
    import pwd                        # Linux only: never imported at load
    return pwd.getpwuid(os.getuid()).pw_dir


def is_absolute(path):
    """Whether `path` is absolute as Linux spells one (a leading /; a path with a drive is not)."""
    return posixpath.isabs(path)


def profile():
    """The user's profile folder from the password database. OSError when this user has no entry
    or its folder is not an absolute path."""
    try:
        path = _pw_dir()
    except KeyError as e:
        raise OSError('the password database has no entry for this user: %s' % e) from None
    if not isinstance(path, str) or not is_absolute(path):
        raise OSError('the password database gives this user no absolute home folder (%r)' % (path,))
    return path


def pointer_files():
    """(the machine pointer file, the user's), the user's from profile() (OSError as profile)."""
    return MACHINE_POINTER, os.path.join(profile(), *USER_POINTER)


def _read_one(path):
    """`(value, None)`, `(None, why)`, or None when no file is at `path`."""
    try:
        with open(path, 'rb') as f:
            data = f.read(65537)
    except FileNotFoundError:
        return None
    except OSError as e:
        return None, 'cannot read the PM repo pointer %s: %s' % (path, e)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None, 'the PM repo pointer %s is not UTF-8 text' % path
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None, 'the PM repo pointer %s is empty' % path
    if len(lines) > 1:
        return None, ('the PM repo pointer %s holds %d lines: it is one line, the PM repository '
                      'path' % (path, len(lines)))
    return lines[0], None


def read_pointer():
    """`(value, None)`; `(None, None)` when neither pointer file is there; `(None, why)` when the
    one in force cannot be read or is not one line of UTF-8 text."""
    got = _read_one(MACHINE_POINTER)
    if got is not None:
        return got
    try:
        user_file = pointer_files()[1]
    except OSError as e:
        return None, 'cannot find the user PM repo pointer file: %s' % e
    got = _read_one(user_file)
    return got if got is not None else (None, None)


def temp():
    """The system temp folder."""
    return TEMP


def _uid():
    return os.getuid()


def _pid():
    return os.getpid()


def _owner(folder):
    """The owner of /proc/<pid> `folder`: the process's effective user (on Linux 7.0 also for one
    the kernel marks not dumpable, such as ssh-agent: only the entries inside go to root), so
    never the answer alone: a setuid program this user ran has another effective user."""
    return os.stat(folder).st_uid


def _status_uid(folder):
    """The users of the process of `folder`, the `Uid:` line of its status: (real, effective,
    saved, file system). OSError when status cannot be read; ValueError when it has no such line
    or the line has not those four."""
    with open(os.path.join(folder, 'status'), 'rb') as f:
        for line in f:
            if line.startswith(b'Uid:'):
                uids = tuple(int(n) for n in line.split()[1:])
                if len(uids) != 4:
                    raise ValueError('not four users on the Uid line of %s'
                                     % os.path.join(folder, 'status'))
                return uids
    raise ValueError('no Uid line in %s' % os.path.join(folder, 'status'))


MINE, OTHERS, UNKNOWN = 'mine', 'others', 'unknown'


def _whose(folder, me):
    """MINE, OTHERS, UNKNOWN, or None when the process ended. Any of its four users (status: real,
    effective, saved, file system) or the folder's owner being `me` makes it this user's. Status
    naming other users only decides OTHERS; with no status, a folder owned by another user but root
    does (a root folder may be a setuid program's this user ran); anything else is UNKNOWN."""
    uids = owner = None
    try:
        uids = _status_uid(folder)
    except OSError as e:
        if e.errno in GONE:
            return None
    except (ValueError, IndexError):
        pass
    try:
        owner = _owner(folder)
    except OSError as e:
        if e.errno in GONE:
            return None
    if owner == me or me in (uids or ()):
        return MINE
    if uids is not None or owner not in (None, 0):
        return OTHERS
    return UNKNOWN


def _link(path):
    return os.readlink(path)


def _state(folder):
    """The stat state letter of the process of `folder`, None when the folder is gone; OSError
    when stat cannot be read for another reason."""
    try:
        with open(os.path.join(folder, 'stat'), 'rb') as f:
            data = f.read()
    except OSError as e:
        if e.errno in GONE:
            return None
        raise
    state = data.rsplit(b')', 1)[-1].split()[:1]
    return state[0].decode('ascii', 'replace') if state else '?'


def _name(folder):
    try:
        with open(os.path.join(folder, 'comm'), 'rb') as f:
            return os.fsdecode(f.read()).strip() or '?'
    except OSError:
        return '?'


def _paths(folder, kinds):
    """Each path the process of `folder` uses, as (kind, path). OSError when one cannot be read,
    except an open file closed while the list was read."""
    for kind in kinds:
        if kind == 'fd':
            where = os.path.join(folder, 'fd')
            for fd in os.listdir(where):
                try:
                    yield kind, _link(os.path.join(where, fd))
                except OSError as e:
                    if e.errno not in GONE:
                        raise
        elif kind == 'map':
            seen = set()
            with open(os.path.join(folder, 'maps'), 'rb') as f:
                for line in f:
                    parts = line.rstrip(b'\n').split(None, 5)
                    if len(parts) == 6 and parts[5] not in seen:
                        seen.add(parts[5])
                        yield kind, os.fsdecode(parts[5])
        else:
            yield kind, _link(os.path.join(folder, kind))


def _collect(folder, pid, name, kinds, found):
    """Add the paths of `folder` to `found`. OSError as _paths."""
    for kind, path in _paths(folder, kinds):
        if path.endswith(DELETED):
            path = path[:-len(DELETED)]
        if os.path.isabs(path):                # not socket:[n], pipe:[n], anon_inode:...
            found.append((pid, name, kind, path))


LIVE_TASK_KINDS = ('cwd', 'root')     # what a thread of a live leader can hold apart from it


def _threads(folder, pid, name, kinds, found, mine=None):
    """Read `kinds` of each thread in /proc/<pid>/task but the leader. None when there is no
    other thread or the list is gone (the process ended), else whether every one was read; a
    thread whose folder goes away while read has ended. With `mine`, a thread whose mount namespace
    is not this process's `mine` (another, or one that cannot be read) is not read in full."""
    try:
        tids = [t for t in os.listdir(os.path.join(folder, 'task')) if t.isdigit() and t != str(pid)]
    except OSError as e:
        return None if e.errno in GONE else False
    if not tids:
        return None
    whole = True
    for tid in tids:
        task = os.path.join(folder, 'task', tid)
        try:
            if mine is not None and _link(os.path.join(task, 'ns', 'mnt')) != mine:
                whole = False                  # its paths are another mount table's
                continue
            _collect(task, pid, name, kinds, found)
        except OSError:
            if os.path.isdir(task):
                whole = False
    return whole


def _process(folder, pid, kinds, mine=None):
    """(entries, read in full) for the process of `folder`, or None when it ended while read. A
    live leader's other threads give their cwd and root too (one that called unshare(CLONE_FS)
    has its own), each in this process's mount namespace `mine` (one that called
    unshare(CLONE_NEWNS) has its own); a zombie leader's give every kind, since the leader gives
    none (_same_mounts read their namespaces)."""
    name, found = _name(folder), []
    try:
        _collect(folder, pid, name, kinds, found)
    except OSError:
        try:
            state = _state(folder)
        except OSError:
            return found, False
        if state is None:
            return None
        if state not in EXITED:
            return found, False
        whole = _threads(folder, pid, name, kinds, found)
        if whole is None:
            return None
        return found, whole
    whole = _threads(folder, pid, name, [k for k in kinds if k in LIVE_TASK_KINDS], found, mine)
    return found, whole is not False


HIDING = ('hidepid=2', 'hidepid=invisible', 'hidepid=4', 'hidepid=ptraceable', 'subset=pid')
# the ns/pid link of the first PID namespace: its inode is fixed (PROC_PID_INIT_INO, Linux 3.8 on)
INIT_PID = 'pid:[4026531836]'


_OCTAL = re.compile(r'\\([0-7]{3})')


def _unescape(field):
    """A mountinfo path field with the kernel's octal escapes (\\040 for a space, \\134 for a
    backslash) undone. A backslash before anything but three octal digits (0-7) stays as it is."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def proc_options(text, point):
    """The options (per mount and superblock) of every proc file system mounted at `point` in
    mountinfo `text`, one set; None when there is none. Every one counts: which of them a path
    reaches is not read here."""
    got = None
    for line in text.splitlines():
        head, sep, tail = line.partition(' - ')
        fields, rest = head.split(), tail.split()
        if not sep or len(fields) < 6 or len(rest) < 3:
            continue
        if _unescape(fields[4]) == point and rest[0] == 'proc':
            got = (got or set()) | set(fields[5].split(',')) | set(rest[2].split(','))
    return got


def read_mountinfo(path=None):
    """The text of the mountinfo table at `path`, this process's (<PROC>/self/mountinfo) when None,
    decoded as the file system names paths (os.fsdecode). OSError when it cannot be read."""
    with open(path or os.path.join(PROC, 'self', 'mountinfo'), 'rb') as f:
        return os.fsdecode(f.read())


def parse_mountinfo(text):
    """[Mount] of each line of mountinfo `text`, in table order (a later mount at a point hides an
    earlier one there). A line with no ` - ` or fewer than six fields before it is skipped."""
    out = []
    for line in text.splitlines():
        head, sep, tail = line.partition(' - ')
        fields, rest = head.split(), tail.split()
        if sep and len(fields) >= 6:
            out.append(Mount(fields[2], _unescape(fields[3]), _unescape(fields[4]),
                             rest[0] if rest else None))
    return out


def _mounts(text):
    """(device, root inside its file system, mount point) of each mount in mountinfo `text`, in
    table order."""
    return [(m.device, m.root, m.point) for m in parse_mountinfo(text)]


def _at_or_under(path, top, sep):
    if top.endswith(sep):
        return path.startswith(top) or path == top.rstrip(sep)
    return path == top or path.startswith(top + sep)


def aliased(text, path):
    """True when the tree at `path` (an absolute, resolved path) can be reached by another path in
    mountinfo `text`: its file system is mounted again where the mount's root and the tree's place
    inside that file system overlap (a bind mount of the tree, of a folder above it or of one in
    it), or any other mount stands at or under `path` (a bind of another folder into the tree: a
    process there names that folder's path), or a later mount stands at or above the one holding
    it (which then hides it). True also when no mount holds `path` (this mount
    table does not reach it, a chroot): then where else it shows is not known. A process standing in such another path holds the tree
    under a path /proc does not show under it."""
    flat = os.path.normcase(path)
    table = [(device, root, os.path.normcase(point)) for device, root, point in _mounts(text)]
    best = None
    for n, (_device, _root, point) in enumerate(table):
        if _at_or_under(flat, point, os.sep) and (best is None or len(point) >= len(table[best][2])):
            best = n                          # the deepest mount holds it; a later one hides
    if best is None:
        return True
    device, root, point = table[best]
    if any(_at_or_under(point, later, os.sep) for _d, _r, later in table[best + 1:]):
        return True                           # mounted over it later: which one holds it is unread
    rest = [part for part in path[len(point):].split(os.sep) if part]
    place = posixpath.join(root, *rest)
    for n, (other, other_root, other_point) in enumerate(table):
        if n != best and (_at_or_under(other_point, flat, os.sep)
                          or other == device and (_at_or_under(place, other_root, '/')
                                                  or _at_or_under(other_root, place, '/'))):
            return True
    return False


def _check_proc(me_folder):
    """OSError unless the /proc at PROC shows every process of this user: this process is in the
    first PID namespace (its ns/pid link is INIT_PID; inside another, as in a container, the /proc
    it mounts lists none of the processes outside, which may use a tree bound in, and its status
    NSpid line names one id all the same), and the mount options (from this process's mountinfo)
    are known and hide none (hidepid=2 or 4, invisible, ptraceable, subset=pid hide those this
    user cannot trace; any proc mount there counts). Returns this process's mount namespace and
    its mount table (the mountinfo text)."""
    try:
        pid_ns = _link(os.path.join(me_folder, 'ns', 'pid'))
    except OSError as e:
        raise OSError(e.errno, 'cannot read this process\'s PID namespace: %s' % e.strerror,
                      PROC) from None
    if pid_ns != INIT_PID:
        raise OSError(errno.EACCES, 'is the PID namespace %s\'s, not the first (%s): the processes '
                      'outside it are not listed' % (pid_ns, INIT_PID), PROC)
    try:
        text = read_mountinfo(os.path.join(me_folder, 'mountinfo'))
    except OSError as e:
        raise OSError(e.errno, 'cannot read its mount options: %s' % e.strerror, PROC) from None
    options = proc_options(text, PROC)
    if options is None:
        raise OSError(errno.ENOENT, 'is not a proc file system in this mount table', PROC)
    hiding = sorted(options.intersection(HIDING))
    if hiding:
        raise OSError(errno.EACCES, 'is mounted with %s, which hides processes of this user'
                      % ','.join(hiding), PROC)
    try:
        return _link(os.path.join(me_folder, 'ns', 'mnt')), text
    except OSError as e:
        raise OSError(e.errno, 'cannot read this process\'s mount namespace: %s' % e.strerror,
                      PROC) from None


class _Denied(object):
    """_same_mounts' answer for a live process whose ns/mnt the kernel refuses to this user: false,
    as any namespace not known to be this one is, but told apart for _login_process."""
    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return 'DENIED'


DENIED = _Denied()


def _same_mounts(folder, mine):
    """True when the process of `folder` shares this process's mount namespace `mine`; False when
    it is another (its paths name another tree) or cannot be read; DENIED (false too) when it is
    live and the kernel refuses its namespace to this user (EACCES, EPERM); None when it ended. A
    zombie leader has none of its own: its other threads' decide, and with none it has ended."""
    try:
        return _link(os.path.join(folder, 'ns', 'mnt')) == mine
    except OSError as e:
        refused = e.errno in REFUSED
        try:
            state = _state(folder)
        except OSError:
            return False
    if state is None:
        return None
    if state not in EXITED:
        return DENIED if refused else False
    try:
        tids = [t for t in os.listdir(os.path.join(folder, 'task'))
                if t.isdigit() and t != os.path.basename(folder)]
    except OSError as e:
        return None if e.errno in GONE else False
    seen = False
    for tid in tids:
        task = os.path.join(folder, 'task', tid)
        try:
            if _link(os.path.join(task, 'ns', 'mnt')) != mine:
                return False
            seen = True
        except OSError:
            if os.path.isdir(task):
                return False
    return True if seen else None


def _ppid(folder):
    """The parent's pid from the status `PPid:` line of `folder`; None when it cannot be read."""
    try:
        with open(os.path.join(folder, 'status'), 'rb') as f:
            for line in f:
                if line.startswith(b'PPid:'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _flags(folder):
    """The kernel's flags for the process of `folder` (the ninth stat field); None when they cannot
    be read."""
    try:
        with open(os.path.join(folder, 'stat'), 'rb') as f:
            return int(f.read().rsplit(b')', 1)[-1].split()[6])
    except (OSError, ValueError, IndexError):
        return None


def _cgroup(folder):
    """The cgroup v2 path of the process of `folder` (its `0::` line); None when there is none or
    it cannot be read."""
    try:
        with open(os.path.join(folder, 'cgroup'), 'rb') as f:
            for line in f:
                if line.startswith(b'0::'):
                    return os.fsdecode(line[3:].rstrip(b'\n'))
    except OSError:
        pass
    return None


def _login_process(folder, me):
    """True when the process of `folder` (this user's, its ns/mnt refused: DENIED) is this login's
    own and so holds no tree: in this user's manager's own scope (cgroup v2 path ending
    /user@<me>.service/init.scope: systemd --user, (sd-pam)), or not exec'd since its fork
    (PF_FORKNOEXEC) with a parent, not PID 1, of another user (a root daemon's child that dropped
    to this user, running the daemon's code: sshd-session). False when any of it cannot be read."""
    group = _cgroup(folder)
    if group is not None and group.endswith('/user@%d.service/init.scope' % me):
        return True
    flags, parent = _flags(folder), _ppid(folder)
    if flags is None or not flags & PF_FORKNOEXEC or parent is None or parent <= 1:
        return False
    return _whose(os.path.join(os.path.dirname(folder), str(parent)), me) == OTHERS


def process_paths(kinds=KINDS):
    """Scan of every process /proc lists (see the module's text). OSError when PROC cannot be
    listed, lists no process, does not list this one (not this system's process list), is
    mounted to hide processes, or its mount options or this process's mount namespace cannot be
    read: then nothing is known."""
    me = _uid()
    pids = [n for n in os.listdir(PROC) if n.isdigit()]
    if not pids:
        raise OSError(errno.ENOENT, 'lists no process', PROC)
    if str(_pid()) not in pids:
        raise OSError(errno.ENOENT, 'does not list this process (%d), so it is not this '
                      "system's process list" % _pid(), PROC)
    mine, mounts = _check_proc(os.path.join(PROC, str(_pid())))
    entries, unread = [], []
    for n in pids:
        folder = os.path.join(PROC, n)
        whose = _whose(folder, me)
        if whose is None or whose == OTHERS:
            continue
        if whose == UNKNOWN:                     # not known to be another user's
            unread.append((int(n), _name(folder)))
            continue
        same = _same_mounts(folder, mine)
        if same is None:
            continue
        if same is DENIED and _login_process(folder, me):
            continue                             # this login's own: counted in total only
        if not same:                             # its paths are another mount table's
            unread.append((int(n), _name(folder)))
            continue
        got = _process(folder, int(n), kinds, mine)
        if got is None:
            continue
        found, whole = got
        entries += found
        if not whole:
            unread.append((int(n), _name(folder)))
    return Scan(entries, len(unread), len(pids), mounts, unread)
