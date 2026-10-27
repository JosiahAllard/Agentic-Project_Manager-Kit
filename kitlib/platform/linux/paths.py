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
                   classify on Linux): Scan(entries, unreadable, total).

The process scan reads, for each process whose /proc/<pid> folder this user owns (the kernel
gives a process of another user, and one it marks not dumpable, to that user or root, and refuses
their links), its current folder (cwd), root, program (exe), every open file (fd/*) and every
mapped file (maps). A process of this user that cannot be read in full counts as unreadable, so
a caller can refuse rather than call a tree free; one that ends while it is read (its folder gone,
or a zombie) is skipped. A process of another user is counted in `total` only: this user cannot
see what it uses. PROC, _uid, _owner, _link and _pw_dir are the seams a test points at a folder
tree of its own. This module imports on any platform; its reads need Linux.
"""
import collections
import errno
import os
import posixpath

PROC = '/proc'
MACHINE_POINTER = '/etc/pm-kit/pointer'
USER_POINTER = ('.config', 'pm-kit', 'pointer')     # under profile()
TEMP = '/tmp'
KINDS = ('cwd', 'root', 'exe', 'fd', 'map')
DELETED = ' (deleted)'                              # /proc's suffix on a removed file's path
GONE = (errno.ENOENT, errno.ESRCH)
EXITED = ('Z', 'X')                                  # stat states of an ended process

# entries: (pid, name, kind, path) for each path a process of this user uses, kind one of KINDS;
# unreadable: how many processes of this user could not be read in full; total: every process
# /proc listed.
Scan = collections.namedtuple('Scan', 'entries unreadable total')


def _pw_dir():
    import pwd                        # Linux only: never imported at load
    return pwd.getpwuid(os.getuid()).pw_dir


def profile():
    """The user's profile folder from the password database. OSError when this user has no entry
    or its folder is not an absolute path."""
    try:
        path = _pw_dir()
    except KeyError as e:
        raise OSError('the password database has no entry for this user: %s' % e) from None
    if not isinstance(path, str) or not (posixpath.isabs(path) or os.path.isabs(path)):
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


def _owner(folder):
    return os.stat(folder).st_uid


def _link(path):
    return os.readlink(path)


def _ended(folder):
    """True when the process of /proc/<pid> `folder` has ended: its folder is gone, or its stat
    state is a zombie's or a dead one's. False when that cannot be read for another reason."""
    try:
        with open(os.path.join(folder, 'stat'), 'rb') as f:
            data = f.read()
    except OSError as e:
        return e.errno in GONE
    state = data.rsplit(b')', 1)[-1].split()[:1]
    return bool(state) and state[0].decode('ascii', 'replace') in EXITED


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


def _process(folder, pid, kinds):
    """(entries, read in full) for the process of `folder`, or None when it ended while read."""
    name, found = _name(folder), []
    try:
        for kind, path in _paths(folder, kinds):
            if path.endswith(DELETED):
                path = path[:-len(DELETED)]
            if os.path.isabs(path):            # not socket:[n], pipe:[n], anon_inode:...
                found.append((pid, name, kind, path))
    except OSError:
        if _ended(folder):
            return None
        return found, False
    return found, True


def process_paths(kinds=KINDS):
    """Scan of every process /proc lists (see the module's text). OSError when PROC cannot be
    listed: then nothing is known."""
    me = _uid()
    pids = [n for n in os.listdir(PROC) if n.isdigit()]
    entries, unreadable = [], 0
    for n in pids:
        folder = os.path.join(PROC, n)
        try:
            owner = _owner(folder)
        except OSError as e:
            if e.errno not in GONE:          # not known to be another user's
                unreadable += 1
            continue
        if owner != me:
            continue
        got = _process(folder, int(n), kinds)
        if got is None:
            continue
        found, whole = got
        entries += found
        if not whole:
            unreadable += 1
    return Scan(entries, unreadable, len(pids))
