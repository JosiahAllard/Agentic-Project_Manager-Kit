"""kitlib.dailyfull.treelock: one daily full run at a time per worktree (docs/daily-full.md).

A run takes the tree's lock before it fetches or moves the tree and holds it until its result is
written, so a second run (a manual one during the scheduled one, or a double trigger) can never
move the tree under a run that is mid-gate.

The lock is an operating-system byte-range lock (msvcrt.locking, LockFile) on one byte far past
the end of the file `<git common dir>/kit-tree-<name>-<hash>.lock`. The common dir exists before
the tree is first added, every checkout of the repository sees it, `git worktree prune` never
removes it, and it is never tracked. The OS lock is the exclusion: the system drops it when the
holder exits, however it exits, so a dead holder never blocks a run, and a pid that was reused
cannot fake one. The file's text is only the holder's record (pid, the process's creation time,
since), for the busy message. A release empties it; a holder that died leaves its record, and the
next taker reports that stale record as taken over. The file itself is never removed: a remove
and a re-create would give two files, each with a holder.
"""
import datetime
import errno
import hashlib
import json
import os
import time

try:
    import msvcrt
except ImportError:          # not Windows: TreeLock refuses (the kit is Windows only)
    msvcrt = None

LOCK_AT = 1 << 30            # the locked byte: past any record, so readers are never blocked
READ_TRIES = 10              # a busy reader re-reads an empty record (a take writing it) this often
READ_SLEEP = 0.05
# The errnos of a lock another handle holds (msvcrt LK_NBLCK sets EACCES; EDEADLOCK is LK_LOCK's
# give-up). Any other error is a failure to lock, never busy.
BUSY_ERRNOS = (errno.EACCES, getattr(errno, 'EDEADLOCK', errno.EDEADLK))


class TreeLockError(Exception):
    """The lock file could not be opened, locked or written."""


class TreeBusy(TreeLockError):
    """Another run holds the tree; `.record` is its record ({} when it could not be read)."""

    def __init__(self, path, record):
        TreeLockError.__init__(self, describe(record))
        self.path = path
        self.record = record


def lock_name(wt):
    """The lock's file name for the worktree path `wt`: its folder name and a hash of its real,
    case-folded path (two trees with one folder name get two locks)."""
    key = os.path.normcase(os.path.realpath(wt))
    tag = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in os.path.basename(key))
    return 'kit-tree-%s-%s.lock' % (tag or 'tree', hashlib.sha1(key.encode('utf-8')).hexdigest()[:12])


def describe(record):
    """`held by pid N since T` for a holder's record."""
    record = record or {}
    return 'held by pid %s since %s' % (record.get('pid', '?'), record.get('since', '?'))


def _own_start():
    """This process's creation time (a FILETIME) from kitlib.proc, or None."""
    try:
        from kitlib.proc import process
        return process.own_ctime()
    except Exception:
        return None


def _parse(text):
    try:
        rec = json.loads(text)
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


def read_record(path):
    """The record in the lock file at `path`: {} when the file is missing, empty (free, or a take
    still writing it) or unreadable (not JSON, not UTF-8). It says nothing about whether the lock is held: only a lock attempt does."""
    try:
        with open(path, encoding='utf-8') as f:
            return _parse(f.read())
    except (OSError, ValueError):       # ValueError: not UTF-8 (UnicodeDecodeError): unreadable
        return {}


class TreeLock(object):
    """The lock file at `path`. acquire() takes it or raises TreeBusy; release() lets it go. As a
    context manager it acquires on entry and releases on exit, an exception included.

    `took_over`: after acquire, the record a dead holder left ({} when the lock was free)."""

    def __init__(self, path, what=None):
        self.path = path
        self.what = what
        self.fd = None
        self.took_over = {}

    def _lock_byte(self, mode):
        os.lseek(self.fd, LOCK_AT, os.SEEK_SET)
        msvcrt.locking(self.fd, mode, 1)

    def acquire(self):
        if msvcrt is None:
            raise TreeLockError('the tree lock needs Windows (msvcrt)')
        if self.fd is not None:
            raise TreeLockError('%s is already held by this lock object' % self.path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0)
        try:
            fd = os.open(self.path, flags, 0o644)
        except OSError as e:
            raise TreeLockError('cannot open %s: %s' % (self.path, e))
        self.fd = fd
        try:
            self._lock_byte(msvcrt.LK_NBLCK)
        except OSError as e:
            self.fd = None
            os.close(fd)
            if e.errno in BUSY_ERRNOS:
                raise TreeBusy(self.path, self._holder())
            raise TreeLockError('cannot lock %s: %s' % (self.path, e))
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            self.took_over = _parse(os.read(fd, 1 << 16).decode('utf-8', 'replace'))
            record = {'pid': os.getpid(), 'pid_start': _own_start(),
                      'since': datetime.datetime.now().isoformat(timespec='seconds'),
                      'what': self.what}
            data = (json.dumps(record, sort_keys=True) + '\n').encode('utf-8')
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        except OSError as e:
            self.release()
            raise TreeLockError('cannot write %s: %s' % (self.path, e))
        return self

    def _holder(self):
        """The holder's record as a busy taker reads it; a take that has the lock but has not
        written yet shows an empty file for a moment, so an empty read is retried."""
        for _ in range(READ_TRIES):
            rec = read_record(self.path)
            if rec:
                return rec
            time.sleep(READ_SLEEP)
        return {}

    def release(self):
        """Empty the record and drop the lock. Safe to call when not held."""
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
            except OSError:
                pass             # the record stays; the lock is still dropped below
            try:
                os.lseek(fd, LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass             # closing the handle drops it too
        finally:
            os.close(fd)

    @property
    def held(self):
        return self.fd is not None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
