"""kitlib.fs.writelock: one writer of a file at a time, across processes and threads.

A caller holds `locked(path, prefix)` from its read of the file to its write, so two writers never
both read the same text and neither replace loses the other's change. The lock is
kitlib.fs.treelock's OS byte-range lock on a lock file (lock_path):

- in a git checkout (kitlib.git.repo.classify of the file's folder, MAIN or LINKED):
  `<prefix>-<name>-<hash>.lock` in the repository's git common dir, never in the work tree, so a
  write leaves no untracked file in the checkout; the hash is of the file's real, case-folded path,
  so every spelling of one file names one lock;
- outside any checkout (NONE): `.<name>.lock` beside the file;
- git cannot tell (UNKNOWN): NoPlace.

The system drops the lock when its holder exits, so a dead holder never blocks. A busy lock is
tried every `pause` seconds for `wait` seconds, then Busy. The lock nests within one thread (the
same file's lock taken again by its holder just counts) and excludes every other thread. The lock
file is never removed (a remove and a re-create would give two files, each with a holder).

Callers word their own refusals: every error here carries what they need (`.lock`, `.detail`).
"""
import contextlib
import hashlib
import os
import threading
import time

from kitlib.git import repo as gitrepo

from .treelock import TreeBusy, TreeLock, TreeLockError

PAUSE = 0.05    # seconds between tries of a busy lock

_held = threading.local()    # .locks: {lock file identity: [TreeLock, depth]} of this thread


class WriteLockError(Exception):
    """The write lock could not be found, made or taken; `.lock` is its file (None: not known)."""

    def __init__(self, message, lock=None):
        Exception.__init__(self, message)
        self.lock = lock


class NoPlace(WriteLockError):
    """git cannot tell whether the file is in a checkout; `.detail` is what git said."""

    def __init__(self, path, detail):
        WriteLockError.__init__(self, 'cannot tell whether %s is in a git checkout: %s' % (path, detail))
        self.detail = detail


class CannotMake(WriteLockError):
    """The lock file (or its folder) could not be created; `.detail` is the OSError."""

    def __init__(self, lock, detail):
        WriteLockError.__init__(self, 'cannot make %s: %s' % (lock, detail), lock)
        self.detail = detail


class CannotTake(WriteLockError):
    """The OS lock failed for a reason other than another holder; `.detail` is the TreeLockError."""

    def __init__(self, lock, detail):
        WriteLockError.__init__(self, str(detail), lock)
        self.detail = detail


class Busy(WriteLockError):
    """Another holder kept the lock for the whole wait; `.holder` is `held by pid N since T`,
    `.wait` the seconds waited."""

    def __init__(self, lock, holder, wait):
        WriteLockError.__init__(self, '%s is %s; waited %s s' % (lock, holder, wait), lock)
        self.holder = holder
        self.wait = wait


def lock_path(path, prefix):
    """The write lock's file for the file at `path` (the module docstring says where); `prefix`
    names the feature (`kit-ledger`, `kit-pending`). NoPlace when git cannot tell."""
    folder, name = os.path.split(os.path.abspath(path))
    place = gitrepo.classify(folder)
    if place.kind == gitrepo.NONE:
        return os.path.join(folder, '.%s.lock' % name)
    if place.kind not in (gitrepo.MAIN, gitrepo.LINKED) or not place.common:
        raise NoPlace(path, place.detail)
    name = os.path.normcase(name)
    key = os.path.join(gitrepo.real(folder), name)
    tag = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name) or 'file'
    digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]
    return os.path.join(place.common, '%s-%s-%s.lock' % (prefix, tag, digest))


def _identity(lock):
    """The lock file's identity (volume, file index), so every spelling of one path is one key;
    None when the file does not exist."""
    try:
        st = os.stat(lock)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _locks():
    if not hasattr(_held, 'locks'):
        _held.locks = {}
    return _held.locks


def held(path, prefix):
    """True when this thread holds the write lock of the file at `path`. False when it does not,
    when the lock file does not exist yet, and when git cannot say where the lock is (then nobody
    could have taken it)."""
    try:
        key = _identity(lock_path(path, prefix))
    except NoPlace:
        return False
    return key is not None and key in _locks()


@contextlib.contextmanager
def locked(path, prefix, wait, what='write', pause=None):
    """Hold the write lock of the file at `path` for the block; yields the lock file's path.
    Taken already by this thread: nests. Busy: tried every `pause` (default PAUSE) for `wait`
    seconds, then Busy. `what` is the holder's record's `what`, for another taker's message."""
    lock = lock_path(path, prefix)
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, 'ab'):          # exists before its identity is read; a holder's record stays
            pass
    except OSError as e:
        raise CannotMake(lock, e)
    key = _identity(lock)
    mine = _locks()
    if key in mine:
        mine[key][1] += 1
        try:
            yield lock
        finally:
            mine[key][1] -= 1
        return
    pause = PAUSE if pause is None else pause
    deadline = time.monotonic() + wait
    tl = TreeLock(lock, what=what)
    while True:
        try:
            tl.acquire()
            break
        except TreeBusy as e:
            if time.monotonic() >= deadline:
                raise Busy(lock, str(e), wait)
        except TreeLockError as e:
            raise CannotTake(lock, e)
        time.sleep(pause)
    mine[key] = [tl, 1]
    try:
        yield lock
    finally:
        del mine[key]
        tl.release()
