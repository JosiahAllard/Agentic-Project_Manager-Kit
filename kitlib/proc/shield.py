"""Keep children safe from kills by a reused pid. Windows only.

GOTCHA: Claude Code ends every finished Bash shell with `taskkill /PID <n> /T /F` twice, about
1.5 s apart. By the second the shell is gone and <n> can belong to a process created in between,
which then dies with exit code 1 having printed nothing. Only a process created after the shell
died can hold <n>, and the second kill comes within ~1.6 s of the first, so the victim is always
a process at most ~1.6 s old, or a descendant of one.

`PidHold` (one per process: `hold()`) is the fix for every process at once: it keeps a handle to
each process this user can open until `linger` seconds after it exits, so no pid is reused while
a kill may still name it. It denies nothing to anyone. A caller warms it (`warm`) before it starts
a child. Its handles close when its process ends, normally (atexit: `close`) or killed, so pids of
processes that exited in the `linger` seconds before are free at once. A process may publish its
warmed hold (`publish`: HOLD_ENV, inherited by every child); a process that inherits a running one
(`inherited`) can skip its own. The kit gate does neither (docs/gate.md refuses the pid shield).
kitlib.pytests' test runs do both only when opted in (runner.PidHoldRun, `--pid-hold` on
tools/pytests.py and tools/kit_tests.py, before the first unit), and shield each unit always.

`shield` is the older, narrower guard, kept for a child's own process: it merges one ACE denying
PROCESS_TERMINATE to Everyone into the child's DACL, through Popen's handle (granted all access at
creation, so the caller's handle keeps every right: `proc.kill()`, TerminateJobObject and
`unshield` still work). Call it while the child is still suspended - kitlib.proc.job.Job.start's
`before_resume` - so not one instruction of it runs unshielded. Its cost: while shielded, the
process cannot be ended by its pid (Task Manager and taskkill get "Access is denied"), and no
process of this user, its own children included, can open it by pid for any access holding
PROCESS_TERMINATE: .NET's Process.EnableRaisingEvents asks for such access (.NET 8, measured), so a
.NET child that watches its shielded parent dies with Win32Exception 5.
kitlib.pytests keeps each unit shielded for life (its job ends it).

Every message says "returned", never "failed": a caller that runs the child unshielded anyway
prints it, and it must not read as a failing line to a gate's fail patterns. (runner.PidHoldRun's
lines do say FAILED: its flag is refused in any gate step, so no gate reads them.)

The Win32 bindings are kitlib.proc.win32's; resume is win32.resume (Job.start calls it).
"""
import atexit
import collections
import ctypes
import itertools
import os
import threading
import time
from ctypes import wintypes

from kitlib.proc import win32

K32, ADV = win32.K32, win32.ADV


def _handle(proc):
    return wintypes.HANDLE(int(getattr(proc, "_handle", proc)))


def process_dacl(handle):
    """(DACL pointer, security descriptor to LocalFree) of a process, via GetSecurityInfo.
    The DACL pointer is NULL (`.value` None) when the process has a NULL DACL."""
    dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
    # 6: SE_KERNEL_OBJECT, 4: DACL_SECURITY_INFORMATION
    err = ADV.GetSecurityInfo(handle, 6, 4, None, None, ctypes.byref(dacl), None,
                                    ctypes.byref(sd))
    if err:
        raise OSError('GetSecurityInfo returned %d' % err)
    return dacl, sd


def shield(proc, read_dacl=process_dacl):
    """Deny PROCESS_TERMINATE to Everyone on `proc`'s own process object; return a token holding
    the DACL it had before, for `unshield` (or `release`, to keep the shield). Raises OSError,
    having written nothing, when it cannot. `read_dacl` is `process_dacl` (a seam for tests)."""

    sid = ctypes.create_string_buffer(68)                   # SECURITY_MAX_SID_SIZE
    size = wintypes.DWORD(len(sid))
    if not ADV.CreateWellKnownSid(1, None, sid, ctypes.byref(size)):     # 1: WinWorldSid
        raise OSError('CreateWellKnownSid returned error %d' % ctypes.get_last_error())
    handle = _handle(proc)
    old, sd = read_dacl(handle)
    kept = False
    try:
        if not old.value:
            # A NULL DACL grants everyone everything; merging a deny entry into it would make
            # an ACL holding only that entry, which refuses every right to everyone.
            raise OSError('its process has a NULL DACL; left as it is')
        # 0x0001: PROCESS_TERMINATE; 3: DENY_ACCESS; trustee form 0: a SID, type 5: a group
        deny = win32.EXPLICIT_ACCESS_W(win32.PROCESS_TERMINATE, 3, 0, win32.TRUSTEE_W(
            None, 0, 0, 5, ctypes.cast(sid, ctypes.c_void_p)))
        new = ctypes.c_void_p()
        err = ADV.SetEntriesInAclW(1, ctypes.byref(deny), old, ctypes.byref(new))
        if err:
            raise OSError('SetEntriesInAclW returned %d' % err)
        try:
            err = ADV.SetSecurityInfo(handle, 6, 4, None, None, new, None)
            if err:
                raise OSError('SetSecurityInfo returned %d' % err)
        finally:
            K32.LocalFree(new)
        kept = True
        return old, sd
    finally:
        if not kept:
            K32.LocalFree(sd)


def unshield(proc, token):
    """Put back the DACL `proc` had before `shield` (its pid can be killed again), and free the
    token. Raises OSError when SetSecurityInfo refuses; the token is freed either way."""
    old, sd = token
    try:
        err = ADV.SetSecurityInfo(_handle(proc), 6, 4, None, None, old, None)
        if err:
            raise OSError('SetSecurityInfo returned %d' % err)
    finally:
        K32.LocalFree(sd)


def release(token):
    """Free a `shield` token without lifting the shield (it stays for the process's life)."""
    K32.LocalFree(token[1])


_HOLD_ACCESS = win32.SYNCHRONIZE | win32.QUERY_LIMITED
_MAX_PIDS = 1 << 20             # EnumProcesses' buffer stops growing here (a corrupt answer)


HOLD_ENV = 'KIT_PID_HOLD'       # the name of a published hold's beacon (PidHold.publish)
_BEACONS = itertools.count(1)


def inherited():
    """True when HOLD_ENV names the beacon of a hold that is running now - published (warmed)
    by an ancestor, and not yet stopped: its handles already keep this process's pids,
    and its children's, unused, so this process need not make or warm a hold of its own. False
    with no HOLD_ENV, or once that hold has stopped or its process has ended (its beacon is gone
    then). LIMIT: true when asked; the ancestor may end a moment later."""
    name = os.environ.get(HOLD_ENV)
    if not name:
        return False
    handle = K32.OpenEventW(win32.SYNCHRONIZE, False, name)
    if not handle:
        return False
    K32.CloseHandle(handle)
    return True


class PidHold(object):
    """Hold a handle to every process this user can open, so that no pid is reused until `linger`
    seconds after its process exited. Windows only.

    WHY: a process object outlives its process while any handle to it is open, and Windows does
    not hand its pid to a new process until the object is gone (measured: taskkill of
    a held, exited pid answers "not found", exit 128, and kills nothing). Held from before a
    shell exits until `linger` (3 s) after, the pid Claude Code's second taskkill names - ~1.6 s
    after the shell ended - belongs to no live process, so it kills nothing.
    Unlike `shield`, it denies no access to anyone: a .NET child may open its parent with
    PROCESS_ALL_ACCESS.

    HOW: a daemon thread, every `interval` seconds: K32EnumProcesses, then OpenProcess(SYNCHRONIZE |
    QUERY_LIMITED_INFORMATION) on each pid not yet held. A held pid no longer listed has exited
    (confirmed on its handle); its handle is closed `linger` seconds later. A pid it cannot open
    (another user's, a protected process) joins a refused set, pruned to pids still listed and
    retried every `retry` seconds. Every `retry` seconds it also asks each held handle whether its
    process has exited, in case an exited process stays listed.

    COST AND LIMITS: one handle per process of this user, and each exited process object kept
    `linger` seconds longer. A process that starts and exits between two passes is never held:
    Git's usr\\bin\\bash.exe running `true` lives at least 15 ms, three passes (measured,
    n=20; bin\\bash.exe at least about twice that). A refused pid freed and reused by a process
    of this user within one pass is held only at the next retry. Pids freed before the first pass
    are not held at all: `warm` waits out that window. The 5 ms `interval` assumes Python 3.11 or
    later, whose time.sleep is high-resolution on Windows; before 3.11 a sleep lasts at least a
    timer tick (15.6 ms by default), a pass comes that much less often, and a process as short as
    that bash can fall between two. Holding stops when this process ends, normally (atexit runs
    `close`) or killed: either way the pids still lingering are freed at once."""

    def __init__(self, linger=3.0, interval=0.005, retry=1.0):
        self.linger, self.interval, self.retry = linger, interval, retry
        self.error = None               # why the watcher stopped, when it failed
        self.passes = 0
        self._k32 = K32
        self._lock = threading.Lock()
        self._held = {}                 # pid -> handle, running or lingering
        self._alive = set()             # held pids not yet seen exited
        self._lingering = collections.deque()   # (monotonic time seen exited, pid), oldest first
        self._refused = set()
        self._events = collections.deque(maxlen=16384)
        self._seq = 0
        self._size = 1024
        self._buf = (wintypes.DWORD * self._size)()
        self._first_at = None
        self._beacon = None             # (handle, name) once published; closed when it stops
        self._first = threading.Event()     # the first full pass is done (or the watcher stopped)
        self._stop = threading.Event()      # close() asked
        self._done = threading.Event()      # the watcher has stopped
        self._thread = threading.Thread(target=self._watch, name='kitlib.proc.shield.PidHold',
                                        daemon=True)
        self._thread.start()

    @property
    def stopped(self):
        """True once the watcher has stopped: closed, or failed (`error` says why)."""
        return self._done.is_set()

    def warm(self, seconds, timeout=60.0):
        """Return once `seconds` have passed since the first full pass, so that no pid freed
        before the hold began can still be named by a pending kill. Raises OSError when the
        watcher has stopped, or makes no first pass within `timeout` seconds.

        It waits until time.monotonic() reaches the target, not for one wait of the time left:
        on Windows an Event.wait can wake a fraction of a millisecond early (0.3997 s of 0.4 s
        seen once)."""
        self._first.wait(timeout)
        if self._first_at is None or self.stopped:
            raise OSError('the pid hold %s' % self._why())
        target = self._first_at + seconds
        while not self.stopped:
            left = target - time.monotonic()
            if left <= 0:
                break
            self._done.wait(left)
        if self.stopped:
            raise OSError('the pid hold %s' % self._why())

    def _why(self):
        if self.error is not None:
            return 'stopped (%s)' % self.error
        return 'is closed' if self.stopped else 'made no pass in time'

    def holds(self, pid):
        """True while the hold has a handle open to `pid`'s process (running, or lingering)."""
        with self._lock:
            return int(pid) in self._held

    def seq(self):
        """The sequence number of the latest event; `record(seq())` is what happens after now."""
        with self._lock:
            return self._seq

    def record(self, since=0):
        """Events after sequence number `since`, oldest first (the last 16384): (seq, pid, what),
        what 'held' (a handle opened), 'exited' (its process seen gone) or 'released' (the
        handle closed: the pid may be reused)."""
        with self._lock:
            return [e for e in self._events if e[0] > since]

    def publish(self):
        """Make this hold findable by every process this one starts from now on: a named event
        (its beacon, open while the watcher runs) and HOLD_ENV naming it in os.environ, which
        children inherit; `inherited` asks for it. Call it once the hold is warmed - a child that
        finds it skips its own hold and warm. Returns the name; a second call returns the same.
        Raises OSError when the watcher has stopped or the event cannot be made."""
        with self._lock:
            if self._beacon is None:
                if self.stopped:
                    raise OSError('the pid hold %s' % self._why())
                name = 'Local\\kit-pid-hold-%d-%d' % (os.getpid(), next(_BEACONS))
                handle = self._k32.CreateEventW(None, True, False, name)
                if not handle:
                    raise OSError('CreateEventW returned error %d' % ctypes.get_last_error())
                if ctypes.get_last_error() == win32.ERROR_ALREADY_EXISTS:
                    self._k32.CloseHandle(handle)
                    raise OSError('CreateEventW: %s is already taken' % name)
                self._beacon = (handle, name)
            name = self._beacon[1]
        os.environ[HOLD_ENV] = name
        return name

    def close(self):
        """Stop the watcher and close every handle at once: pids still lingering are freed, and
        the beacon (`publish`) is gone. atexit runs it when this process ends normally - a process
        that finishes frees, as it exits, the pids of processes that exited in the `linger`
        seconds before, as a hard kill of it would."""
        self._stop.set()
        self._thread.join(10.0)

    def _event(self, pid, what):
        with self._lock:
            self._seq += 1
            self._events.append((self._seq, pid, what))

    def _listed(self):
        got = wintypes.DWORD()
        while True:
            if not self._k32.K32EnumProcesses(self._buf, ctypes.sizeof(self._buf),
                                              ctypes.byref(got)):
                raise OSError('K32EnumProcesses returned error %d' % ctypes.get_last_error())
            count = got.value // ctypes.sizeof(wintypes.DWORD)
            if count < self._size:
                return set(self._buf[:count])
            if self._size >= _MAX_PIDS:
                raise OSError('K32EnumProcesses listed %d pids or more' % self._size)
            self._size *= 2             # a full buffer may have cut the list short
            self._buf = (wintypes.DWORD * self._size)()

    def _exited(self, handle):
        return self._k32.WaitForSingleObject(handle, 0) == win32.WAIT_OBJECT_0

    def _pass(self, sweep):
        # Set operations only, so a quiet pass runs no Python loop over every held pid. Cost,
        # measured as process_time of one warmed hold over 30 s windows on one shared machine:
        # 11.5-14.8% of one core at 149-167 passes a second with 512-683 pids held; it grows with
        # the pids held (3.1% at 228).
        listed = self._listed()
        listed.discard(0)                                           # the System Idle Process
        now = time.monotonic()
        if sweep:
            self._refused.clear()
        for pid in listed - self._held.keys() - self._refused:
            handle = self._k32.OpenProcess(_HOLD_ACCESS, False, pid)
            if handle:
                with self._lock:
                    self._held[pid] = handle
                self._alive.add(pid)
                self._event(pid, 'held')
            else:
                self._refused.add(pid)
        self._refused &= listed
        for pid in (set(self._alive) if sweep else self._alive - listed):
            if self._exited(self._held[pid]):
                self._alive.discard(pid)
                self._lingering.append((now, pid))
                self._event(pid, 'exited')
        while self._lingering and now - self._lingering[0][0] >= self.linger:
            self._release(self._lingering.popleft()[1])

    def _release(self, pid):
        with self._lock:
            handle = self._held.pop(pid)
        self._alive.discard(pid)
        self._k32.CloseHandle(handle)
        self._event(pid, 'released')

    def _watch(self):
        try:
            next_sweep = time.monotonic() + self.retry
            while not self._stop.is_set():
                sweep = time.monotonic() >= next_sweep
                if sweep:
                    next_sweep = time.monotonic() + self.retry
                self._pass(sweep)
                self.passes += 1
                if self._first_at is None:
                    self._first_at = time.monotonic()
                    self._first.set()
                time.sleep(self.interval)
        except BaseException as e:      # noqa: BLE001  (a note for its users, never a crash)
            self.error = '%s: %s' % (type(e).__name__, e)
        finally:
            with self._lock:                # publish makes no beacon after this
                if self._beacon is not None:
                    self._k32.CloseHandle(self._beacon[0])  # a stopped hold is found by no child
                    self._beacon = None
                self._done.set()            # `stopped` is True only once the beacon is gone
            self._first.set()
            if self.error is not None:
                # Failed, not closed: keep what exited lingering out its time before freeing it.
                self._stop.wait(self.linger)
            for pid in list(self._held):
                self._release(pid)


_HOLD = None
_HOLD_LOCK = threading.Lock()


def hold():
    """This process's PidHold, made once on first use under a lock (two first calls at once
    would make two and leak one) and closed at interpreter exit (atexit). Raises OSError when it
    cannot be made; a hold whose watcher has stopped is still returned (its `warm` raises)."""
    global _HOLD
    with _HOLD_LOCK:
        if _HOLD is None:
            made = PidHold()
            atexit.register(made.close)
            _HOLD = made
        return _HOLD
