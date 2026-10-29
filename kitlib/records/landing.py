"""The landing lock: one per repository, held from a landing's merge to its push or its end, the
same for every repository.

The lock is the file `<git common dir>/kit-landing.lock` (LOCK_NAME): inside the git dir, so the
main checkout and every worktree see the same one, and it is never tracked. It is created with O_CREAT|O_EXCL -
of two takers exactly one wins - and holds one JSON object:

  lane            always `full`, written for older kits' readers (they refuse a record without
                  it); nothing here reads it
  branches        the branches being landed
  pid, pid_start  a process holder (its creation time from kitlib.proc), or both null (a token
                  holder: the command that took it has exited)
  token           the secret a release or a phase change must quote
  started_utc     when it was taken
  main_branch     the main branch it guards
  origin_at_take  refs/remotes/<remote>/<main> when it was taken
  main_at_take    refs/heads/<main> when it was taken
  phase           merge | gate | push | stuck; undo is retired (no landing sets it), and a
                  lock file an older kit left at it still reads

`take` refuses at once while anyone holds it (Busy names the holder); `release` and `set_phase`
refuse any token but the holder's, checked again just before the file is replaced or removed. The
window between that check and the replace or remove is not closed: a break and a new take landing
in it would be overwritten or removed (a file has no compare-and-swap here). `break_lock` is the only way past a holder that is gone, and it
never happens by age (a gate may run an hour). It is refused unless BOTH
  - the holder is gone: a process holder that kitlib.proc says is surely gone (a pid running with
    another start time is a reused pid: gone; one that cannot be inspected: alive), or a token
    holder (or a JSON object of an old format) the caller declares gone with holder_gone=True, AND
  - <main> == <remote>/<main> and the main checkout has no MERGE_HEAD (`<common dir>/MERGE_HEAD`,
    whichever checkout asks): nothing unpushed exists.
A file that cannot be read, is empty, or is not a JSON object is never broken, holder_gone or not:
a taker between its create and its write leaves it so. Such a file is cleared by hand.
Just before removing, a break re-reads the file and refuses when it is no longer the one judged
(released and taken again meanwhile). The window between that read and the remove is not closed.
A take whose write fails after the create removes the file again: no empty lock is left behind.
A PermissionError on a replace or remove (a reader holding the file open) is retried RETRIES
times RETRY_SLEEP apart, then LockError (the replace through kitlib.fs.replace; the removes keep
their own loop, where a file already gone is an error, not success).

Process facts come only from kitlib.proc. Until it is importable here, a process
holder's start time is recorded as null and the holder always counts as alive: fail-closed.
"""
import datetime
import json
import os
import secrets
import time

from kitlib.fs import replace as fsreplace
from kitlib.git import refs, repo

LOCK_NAME = 'kit-landing.lock'
FULL = 'full'                  # the record's constant lane, kept for older kits' readers
PHASES = ('merge', 'gate', 'push', 'undo', 'stuck')
RETRIES = 5                    # tries of a replace or remove while it fails with PermissionError
RETRY_SLEEP = 0.2              # seconds between them
# RETRIES as a wait for kitlib.fs.replace: try k starts at least k-1 pauses in, so the last (at
# 0.8 s or later) is past this bound and the one before it (at 0.6 s) is not; the half pause is
# the margin for a slow try.
REPLACE_WAIT = (RETRIES - 1.5) * RETRY_SLEEP


class LockError(Exception):
    """The lock could not be read or written, or git could not be read."""


class Busy(LockError):
    """Someone holds the lock; `.record` is the holder's record."""

    def __init__(self, record):
        LockError.__init__(self, describe(record))
        self.record = record


class NotHolder(LockError):
    """A release or phase change with a token that is not the holder's."""


def _proc():
    try:
        from kitlib.proc import process
        return process
    except Exception:          # not built yet, or not Windows: no process facts
        return None


def process_start(pid):
    """The creation time kitlib.proc reports for `pid`, or None (no such process, or no proc)."""
    p = _proc()
    if p is None:
        return None
    try:
        return p.proc_ctime(pid)
    except Exception:
        return None


def process_alive(pid, start):
    """False only when kitlib.proc says the process (pid and start time) is surely gone."""
    p = _proc()
    if p is None:
        return True
    try:
        return bool(p.alive(pid, start))
    except Exception:
        return True


def describe(record):
    if not record:
        return 'landing lock: free'
    who = ('pid %s' % record.get('pid')) if record.get('pid') is not None else 'a token holder'
    return ('landing lock: HELD - branches %s, %s, phase %s, since %s, %s at take %s'
            % (' '.join(record.get('branches') or ()) or '-', who,
               record.get('phase'), record.get('started_utc'), record.get('main_branch') or 'main',
               (record.get('origin_at_take') or '?')[:10]))


def _held_open(what, e):
    """The LockError of a replace or remove still refused (PermissionError `e`) after its tries."""
    return LockError('%s failed %d times (%s): the lock file is held open by another process; '
                     'nothing was changed' % (what, RETRIES, e))


def _utc(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')


class LandingLock(object):
    """The landing lock of the repository `root` is in (any of its checkouts).

    `start_of(pid)` and `is_alive(pid, start)` are kitlib.proc's by default; tests pass their
    own."""

    def __init__(self, root, main='main', remote='origin',
                 start_of=process_start, is_alive=process_alive):
        common, why = repo.common_dir(root)
        if not common:
            raise LockError('no git common dir for %s: %s' % (root, why))
        self.root = root
        self.common = common
        self.path = os.path.join(common, LOCK_NAME)
        self.main, self.remote = main, remote
        self.start_of, self.is_alive = start_of, is_alive

    @classmethod
    def for_config(cls, root, cfg, **kw):
        return cls(root, cfg.main_branch, cfg.remote, **kw)

    # ---------------------------------------------------------------- reading

    def _ref(self, name):
        sha, _ = refs.resolve(name, self.root)
        return sha

    def read(self):
        """The holder's record, None when nobody holds the lock; LockError when the file is there
        and is not a record (a lock that cannot be read is held, by nobody we can name)."""
        try:
            with open(self.path, encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:      # not UTF-8: unreadable, so held
            raise LockError('cannot read %s: %s' % (self.path, e))
        try:
            rec = json.loads(text)
        except ValueError as e:
            raise LockError('%s is not a lock record: %s' % (self.path, e))
        if not isinstance(rec, dict) or not rec.get('token'):
            raise LockError('%s is not a lock record (no token)' % self.path)
        return rec

    def holder_state(self, record):
        """'alive', 'dead' or 'token' (no process to look at). A pid that is not a whole number
        cannot be checked: alive."""
        pid = record.get('pid')
        if pid is None:
            return 'token'
        if type(pid) is not int:
            return 'alive'
        return 'alive' if self.is_alive(pid, record.get('pid_start')) else 'dead'

    # ---------------------------------------------------------------- writing

    def take(self, branches, pid=None, now=None):
        """Take the lock for `branches`; the record, token included. Busy while anyone holds it.
        `pid` given: a process holder; None: a token holder."""
        if not branches or not all(isinstance(b, str) and b for b in branches):
            raise LockError('a landing lock names the branches it lands')
        # A lock already there is Busy before the record is built: a waiter's look then asks git
        # nothing and looks up no process. The record is still built before the create, so the
        # file is empty only for the write itself (readers take an empty file as held). The
        # exclusive create below stays the only arbiter: a lock made after this look loses there.
        if os.path.exists(self.path):
            self._busy()
        record = {
            # `lane` is read by no tool here: an older kit refuses a record without it.
            'lane': FULL, 'branches': list(branches), 'pid': pid,
            'pid_start': self.start_of(pid) if pid is not None else None,
            'token': secrets.token_hex(8), 'started_utc': _utc(now), 'main_branch': self.main,
            'origin_at_take': self._ref('refs/remotes/%s/%s' % (self.remote, self.main)),
            'main_at_take': self._ref('refs/heads/%s' % self.main), 'phase': 'merge',
        }
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            self._busy()
        except OSError as e:
            raise LockError('cannot create %s: %s' % (self.path, e))
        data = (json.dumps(record, sort_keys=True) + '\n').encode('utf-8')
        try:
            try:
                while data:
                    data = data[os.write(fd, data):]
            finally:
                os.close(fd)
        except OSError as e:
            try:
                os.remove(self.path)
            except OSError:
                pass
            raise LockError('cannot write %s: %s' % (self.path, e))
        return record

    def _busy(self):
        """Raise Busy naming the holder: phase '?' and no token when the file is not a record,
        'released while we looked' when it is gone again."""
        try:
            holder = self.read()
        except LockError as e:
            holder = {'phase': '?', 'error': str(e)}
        raise Busy(holder or {'phase': 'released while we looked'})

    def _held_by(self, token):
        rec = self.read()
        if rec is None:
            raise NotHolder('nobody holds the landing lock')
        if not token or rec.get('token') != token:
            raise NotHolder('the landing lock is held by someone else: %s' % describe(rec))
        return rec

    def _retry(self, what, fn, *args):
        """fn(*args), tried RETRIES times RETRY_SLEEP apart while it fails with PermissionError,
        then LockError. Kept for release() and break_lock()'s removes: a lock file already gone
        there (removed by someone else after the check) raises FileNotFoundError, where
        kitlib.fs.replace.remove returns as on success."""
        for i in range(RETRIES):
            try:
                return fn(*args)
            except PermissionError as e:
                if i == RETRIES - 1:
                    raise _held_open(what, e)
                time.sleep(RETRY_SLEEP)

    def set_phase(self, token, phase):
        if phase not in PHASES:
            raise LockError('phase must be one of %s' % ', '.join(PHASES))
        """Set the holder's phase. NotHolder unless `token` holds the lock both when it is read and
        just before the replace (the window after that second check is not closed)."""
        rec = self._held_by(token)
        rec['phase'] = phase
        tmp = '%s.%s.tmp' % (self.path, secrets.token_hex(4))
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(json.dumps(rec, sort_keys=True) + '\n')
            self._held_by(token)
            try:
                fsreplace.replace(tmp, self.path, wait=REPLACE_WAIT, pause=RETRY_SLEEP)
            except PermissionError as e:
                raise _held_open('replacing %s' % self.path, e)
        finally:
            try:
                os.remove(tmp)
            except OSError:      # gone after the replace; a leftover names no lock: read() skips it
                pass
        return rec

    def release(self, token):
        """Remove the lock. NotHolder unless `token` holds it, checked just before the remove."""
        rec = self._held_by(token)
        self._retry('removing %s' % self.path, os.remove, self.path)
        return rec

    # ---------------------------------------------------------------- breaking

    def unpushed(self):
        """None when <main> == <remote>/<main> and the main checkout has no MERGE_HEAD, else why."""
        m = self._ref('refs/heads/%s' % self.main)
        o = self._ref('refs/remotes/%s/%s' % (self.remote, self.main))
        if not m or not o:
            return '%s or %s/%s does not resolve' % (self.main, self.remote, self.main)
        if m != o:
            return '%s (%s) != %s/%s (%s): an unpushed merge or commit' % (
                self.main, m[:10], self.remote, self.main, o[:10])
        if os.path.exists(os.path.join(self.common, 'MERGE_HEAD')):
            return 'a merge is in progress in the main checkout (MERGE_HEAD)'
        return None

    def _text(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                return f.read()
        except (OSError, UnicodeDecodeError):      # break_lock refuses a file it cannot read
            return None

    def break_lock(self, holder_gone=False):
        """`(True, record removed)` or `(False, why not)`. Never by age.

        Fail closed: a file that cannot be read, is empty, or is not a JSON object is never
        broken, holder_gone or not - a taker between its exclusive create and its write leaves
        exactly that."""
        judged = self._text()
        if judged is None:
            if not os.path.exists(self.path):
                return False, 'nobody holds the landing lock: nothing to break'
            return False, ('%s cannot be read: not broken. Cleared by hand only once no landing '
                           'runs' % self.path)
        try:
            raw = json.loads(judged)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            return False, ('%s is empty or not a JSON record (as a taker leaves it between its '
                           'create and its write): not broken, holder_gone or not. Cleared by '
                           'hand only once no landing runs' % self.path)
        try:
            rec = self.read()
        except LockError as e:
            rec = {'phase': '?', 'unreadable': str(e), 'pid': None}
            # an old-format record (no token) still names its process: check it
            if raw.get('pid') is not None:
                rec.update(phase=raw.get('phase', '?'), pid=raw['pid'],
                           pid_start=raw.get('pid_start'))
        if rec is None:
            return False, 'nobody holds the landing lock: nothing to break'
        state = self.holder_state(rec)
        if state == 'alive':
            return False, 'the holder is alive: %s' % describe(rec)
        if state == 'token' and not holder_gone:
            return False, ('a token holder (or an old-format record) cannot be seen from here: %s. '
                           'Break it only when that lander is gone (holder_gone)' % describe(rec))
        why = self.unpushed()
        if why:
            return False, ('%s, and %s. Not broken: the unpushed work is undone first, on the '
                           'coordinator\'s word' % (describe(rec), why))
        if self._text() != judged:
            return False, 'the landing lock changed while it was judged (released and taken again?): not broken'
        self._retry('removing %s' % self.path, os.remove, self.path)
        return True, rec

    def gate_refusal(self):
        """None when the lock lets a merged-tree gate run, else the refusal: held, not stuck,
        and <remote>/<main> unmoved since the take."""
        try:
            rec = self.read()
        except LockError as e:
            return 'the landing lock cannot be read (%s)' % e
        if rec is None:
            return 'nobody holds the landing lock: take it before a merged-tree gate'
        if rec.get('phase') == 'stuck':
            return 'the landing lock is stuck (a landing stopped half-way): %s' % describe(rec)
        origin = self._ref('refs/remotes/%s/%s' % (self.remote, self.main))
        if not origin or origin != rec.get('origin_at_take'):
            return ('%s/%s is %s, and the lock was taken at %s: it moved under this landing'
                    % (self.remote, self.main, (origin or '?')[:10], (rec.get('origin_at_take') or '?')[:10]))
        return None
