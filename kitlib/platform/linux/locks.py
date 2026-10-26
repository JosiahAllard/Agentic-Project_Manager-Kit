"""The lock's exclusion on Linux (docs/linux-support-plan.md section 2.2): fcntl.flock on a file,
where Windows uses a named mutex, a run's liveness mutex and msvcrt.locking. The kernel drops a
flock when the last descriptor of its open file description closes, so a holder that dies, however
it dies, frees the lock: Windows's "an abandoned mutex counts as free". A flock belongs to the open
file description, not to the process, so two descriptors in one process (two threads, two
TreeLocks) exclude each other as two Windows handles do; fcntl.lockf locks belong to the process
and would not. Every descriptor is opened O_CLOEXEC, so a child started with exec never inherits a
lock. Imported only from the Linux branches of kitlib.lock.store and kitlib.fs.treelock.

  hold(path, wait_s, name)   a context manager: LOCK_EX on `path`, recursive for its thread and
                             exclusive of every other thread and process; OSError when it is not
                             had within wait_s (Store.lock)
  RunLock(path)              a run's liveness signal, held until release(); OSError when another
                             holds it (Store.run_mutex)
  run_lock_held(path)        True while a RunLock holds `path`, False when none does, None when it
                             cannot be told (Store.run_mutex_held)
  lock_fd(fd), unlock_fd(fd) LOCK_EX | LOCK_NB and LOCK_UN on a descriptor (kitlib.fs.treelock);
                             a busy lock raises OSError with an errno in BUSY_ERRNOS

Off Linux (no fcntl) every call raises PlatformUnsupported naming what was asked."""
import contextlib
import errno
import os
import threading
import time

from kitlib.platform import PlatformUnsupported

try:
    import fcntl
except ImportError:          # Windows: the module imports, every call refuses
    fcntl = None

POLL_S = 0.01               # between tries of a busy hold(): flock has no timeout of its own
BUSY_ERRNOS = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
CLOEXEC = getattr(os, 'O_CLOEXEC', 0)

_held = threading.local()    # .locks: {path: [fd, depth]} of this thread's hold()s


def _need(what):
    if fcntl is None:
        raise PlatformUnsupported('%s needs fcntl.flock (Linux); this platform has none' % what)


def lock_fd(fd):
    """LOCK_EX | LOCK_NB on fd: OSError (an errno in BUSY_ERRNOS) when another holds it."""
    _need('a file lock')
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_fd(fd):
    _need('a file lock')
    fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path, flags):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    return os.open(path, flags | os.O_CREAT | CLOEXEC, 0o644)


def _locks():
    if not hasattr(_held, 'locks'):
        _held.locks = {}
    return _held.locks


@contextlib.contextmanager
def hold(path, wait_s, name):
    """LOCK_EX on the file at `path` (made when missing, never removed) for the block. Taken again
    by its holding thread: nests. Busy: tried every POLL_S for wait_s seconds, then OSError "the
    <name> mutex was not free within <wait_s> s", Windows's words."""
    _need('the %s lock' % name)
    key = os.path.abspath(path)
    mine = _locks()
    if key in mine:
        mine[key][1] += 1
        try:
            yield
        finally:
            mine[key][1] -= 1
        return
    fd = _open(key, os.O_RDWR)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in BUSY_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise OSError("the %s mutex was not free within %g s" % (name, wait_s))
            time.sleep(POLL_S)
        mine[key] = [fd, 1]
        try:
            yield
        finally:
            del mine[key]
    finally:
        os.close(fd)                    # drops the flock


class RunLock:
    """A run's liveness signal: LOCK_EX on `path` (runs/<id>.alive), held until release(), which
    removes the file and then lets go. The lock is checked to be on the file `path` names now, so a
    take that raced a release onto the removed file takes again. Raises OSError when another holds
    it, or when the file cannot be made or locked."""

    def __init__(self, path):
        _need('the run lock')
        self.name = path
        self.fd = None
        while True:
            fd = _open(path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                same = os.path.samestat(os.fstat(fd), os.stat(path))
            except FileNotFoundError:
                same = False            # released and removed between the open and the stat
            except OSError as e:
                os.close(fd)
                if e.errno in BUSY_ERRNOS:
                    raise OSError("the run lock %s is owned by another process" % path)
                raise
            if same:
                self.fd = fd
                return
            os.close(fd)

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            with contextlib.suppress(OSError):
                os.remove(self.name)
        finally:
            os.close(fd)


def run_lock_held(path):
    """Is the RunLock on `path` held? True when a holder has it; False when no file is there or the
    lock is free (a shared probe is taken and let go at once: two probes never see each other);
    None when it cannot be told (the file cannot be opened, or the probe fails otherwise)."""
    _need('the run lock')
    try:
        fd = os.open(path, os.O_RDONLY | CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as e:
            return True if e.errno in BUSY_ERRNOS else None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
