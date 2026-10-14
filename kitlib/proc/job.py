"""Job objects: the one place a process tree is started pinned, measured and ended as a tree.
It replaces machine_lock's Job, winproc's job helpers and pytests' Job.

A job never allows breakaway (no JOB_OBJECT_LIMIT_BREAKAWAY_OK): Git's sh and bash (MSYS2) start
every child with CREATE_BREAKAWAY_FROM_JOB whenever the job allows it, and measured 2026-10-05
with it allowed, what `sh -c` started was in no job, on every thread, and outlived the wrapper.
A program that asks to break away anyway has that start refused (access denied).

Pinning past 64 logical processors: a mask whose threads are all in processor group 0 is
set as the job's affinity limit, as before; any other mask (global ids, kitlib.proc.cpu) is set
as JobObjectGroupInformationEx, one GROUP_AFFINITY per group. The second path has no test on real
multi-group hardware (this machine has one group); its buffers are unit-tested.

No console window from a parent without a console: Job.start passes its flags through
no_window_flags. A console program started by a process with no console at all (the guard hook
under pythonw.exe) gets a new console, and Windows shows a window for it; CREATE_NO_WINDOW gives it
a console with no window. A parent that has a console, shown or hidden, keeps its children on that
console as before. The test is "attached to a console" (GetConsoleProcessList), not
GetConsoleWindow: measured 2026-10-10, a python started with CREATE_NO_WINDOW, and an agent's
shell, have a console whose GetConsoleWindow() is 0. A caller that wants a visible window passes
CREATE_NEW_CONSOLE. A start that needs no job gets the same flags through kitlib.proc.spawn (run,
call, popen); tests/proc/test_spawn_sites.py holds every start in the kit to one of the two.
has_console lives in kitlib.proc.console, whose process-wide CreateProcess hook is the backstop
for a start that goes through neither.
"""
import ctypes
import signal
import subprocess
import sys

from . import bash, cpu, win32
from .console import CONSOLE_CHOICE, has_console  # noqa: F401  (job.has_console stays a name)
from .process import process_working_set
from .win32 import K32

ERROR_MORE_DATA = win32.ERROR_MORE_DATA


def no_window_flags(flags, console=None):
    """Popen creationflags for a child of this process: `flags` plus CREATE_NO_WINDOW when this
    process has no console (`console` False; None asks has_console()) and `flags` makes no console
    choice of its own (CREATE_NEW_CONSOLE, DETACHED_PROCESS or CREATE_NO_WINDOW). Otherwise
    `flags` unchanged, so a child of a console parent shares that console as before."""
    if flags & CONSOLE_CHOICE:
        return flags
    if console is None:
        console = has_console()
    return flags if console else flags | win32.CREATE_NO_WINDOW


def resume(proc_or_handle):
    """Resume a CREATE_SUSPENDED child through its process handle (a Popen or an int); raises
    OSError when NtResumeProcess refuses."""
    status = win32.resume(proc_or_handle)
    if status != 0:
        raise OSError("NtResumeProcess returned NTSTATUS 0x%08X" % (status & 0xFFFFFFFF))


def affinity_plan(mask):
    """How a global thread mask is set on a job: None (no pin), ("basic", mask) when every thread
    is in group 0, else ("groups", [(group, 64-bit mask)])."""
    if mask is None:
        return None
    parts = cpu.split(mask)
    if not parts:
        return None
    if list(parts) == [0]:
        return ("basic", parts[0])
    return ("groups", sorted(parts.items()))


def group_affinities(pairs):
    """A ctypes GROUP_AFFINITY array for [(group, mask)]."""
    arr = (win32.GROUP_AFFINITY * len(pairs))()
    for i, (g, m) in enumerate(pairs):
        arr[i].Group, arr[i].Mask = g, m
    return arr


def _set_ext(h, flags, affinity=0):
    info = win32.EXT_LIMITS()
    info.Basic.LimitFlags = flags
    info.Basic.Affinity = affinity
    return bool(K32.SetInformationJobObject(h, win32.JOB_EXT_LIMITS, ctypes.byref(info),
                                            ctypes.sizeof(info)))


def _set_groups(h, pairs):
    arr = group_affinities(pairs)
    return bool(K32.SetInformationJobObject(h, win32.JOB_GROUP_INFO_EX, ctypes.byref(arr),
                                            ctypes.sizeof(arr)))


def pid_list(h):
    """The pids in job h now; raises OSError when the job cannot be asked."""
    ids = win32.pid_list(64)
    if not K32.QueryInformationJobObject(h, win32.JOB_PID_LIST, ctypes.byref(ids),
                                         ctypes.sizeof(ids), None):
        if ctypes.get_last_error() != ERROR_MORE_DATA:
            raise ctypes.WinError(ctypes.get_last_error())
        ids = win32.pid_list(ids.assigned + 32)      # more processes than room: once more
        if not K32.QueryInformationJobObject(h, win32.JOB_PID_LIST, ctypes.byref(ids),
                                             ctypes.sizeof(ids), None) and not ids.listed:
            raise ctypes.WinError(ctypes.get_last_error())
    return [int(p) for p in ids.ids[:ids.listed]]


HELD_SIGNALS = tuple(s for s in (signal.SIGINT, getattr(signal, "SIGBREAK", None))
                     if s is not None)


class _HeldDefault(KeyboardInterrupt):
    """A held signal whose handler was SIG_DFL: Job.start ends the child, then _Held.close puts
    SIG_DFL back and raises the signal, which ends this process as it would have at once."""


class _Held:
    """Ctrl+C and Ctrl+Break held off while Job.start makes a suspended child and puts it in the
    job. A KeyboardInterrupt between CreateProcess and the assign would leave the child suspended
    in no job, never resumed or ended; so would SIGBREAK under SIG_DFL (Python's own default for
    it: Ctrl+Break, or the console closing, ends the process there and then). Meanwhile each held
    signal runs a handler that only queues it. kitlib.lock.run.settle masks SIGINT the same way
    (on the main thread, put back in a finally), but with SIG_IGN; here a signal is late, never
    lost.

    deliver() hands each signal queued so far to the handler it replaced, still held, with the
    caller's frame: one that raises (Python's SIGINT default raises KeyboardInterrupt) raises
    where Job.start still ends the child; SIG_IGN drops it; SIG_DFL raises _HeldDefault, so the
    child is ended before close() lets the default act. close() puts every handler back, then
    raises again with signal.raise_signal each signal still queued: one that came after the last
    deliver(), or while a failed start ended its child. Nothing is held off the main thread
    (signal.signal raises ValueError there, and a signal's handler runs only on the main
    thread), and a signal whose handler was not set from Python (getsignal None: it could not be
    put back) is not held.

    signal.signal runs any pending handler before it swaps, and one may raise there: a Ctrl+C
    while the next handler is set back, or a Ctrl+Break whose own handler raises while it is
    held. Either way every handler is still put back (close() sets each again until it takes,
    up to RETRIES tries: every failed try ran a handler, so the signal behind it is not lost),
    the queue is still raised again, and the first error is raised after. A _Held whose
    __init__ raises has put back what it held, so Job.start makes it before its try."""

    RETRIES = 32        # failed sets of one handler in close(), each a signal's handler raising

    def __init__(self, signals=HELD_SIGNALS):
        self.before, self.queue = {}, []
        try:
            for s in signals:
                before = signal.getsignal(s)
                if before is None:
                    continue
                try:
                    signal.signal(s, self._note)
                except ValueError:              # not the main thread
                    return
                self.before[s] = before
        except BaseException:
            self.close()
            raise

    def _note(self, signum, frame):
        self.queue.append(signum)

    def deliver(self):
        frame = sys._getframe(1)
        while self.queue:
            s = self.queue[0]
            before = self.before[s]
            if before is signal.SIG_DFL:
                raise _HeldDefault("signal %d held while a child started" % s)
            self.queue.pop(0)
            if callable(before):
                before(s, frame)

    def close(self):
        first = None
        for s, handler in list(self.before.items()):
            for _ in range(self.RETRIES):
                try:
                    signal.signal(s, handler)
                    break
                except BaseException as e:      # a pending signal's handler ran and raised
                    first = e if first is None else first
            del self.before[s]
        while self.queue:                       # read after the sets: _note may add to it
            s = self.queue.pop(0)
            try:
                signal.raise_signal(s)
            except BaseException as e:
                first = e if first is None else first
        if first is not None:
            raise first


class JobError(OSError):
    """The job could not be made, or a process could not be put in it. Nothing runs outside a
    job (fail closed)."""


class Job:
    """A job object pinned to global thread mask `mask` (None: every thread). Kill-on-close while
    live, so the tree ends with its owner; end(kill=False) clears it, so daemons the command left
    survive (unpinned). `note` says why a pin was dropped (the machine has none of the
    mask's threads, or Windows refused it); the job itself is never dropped.

    `pin` is "pinned" (mask set), None (no pin), or "overridden": the first command's affinity,
    read back once it is in the job, is not the mask, because an outer job's limit wins (as
    process.set_affinity says). Then `effective` is the mask it really runs on and `note` says so.
    Read back only for a group-0 mask; a multi-group pin is not checked."""

    def __init__(self, mask=None, kill_on_close=True):
        self.h, self.note, self.p = K32.CreateJobObjectW(None, None), None, None
        if not self.h:
            raise JobError("CreateJobObjectW: windows error %d" % ctypes.get_last_error())
        try:
            self.kill_on_close = kill_on_close
            every = cpu.all_mask()
            self.mask = (mask & every) if mask is not None else None
            if mask is not None and not self.mask:
                self.note, self.mask = "this machine has none of cores %s" % cores_text(mask), None
            if not self._limit(kill_on_close) and self.mask is not None:
                self.note = "the core pin could not be set (windows error %d)" % ctypes.get_last_error()
                self.mask = None
                if not self._limit(kill_on_close):
                    raise JobError("SetInformationJobObject: windows error %d"
                                   % ctypes.get_last_error())
            self.pin = "pinned" if self.mask is not None else None
            self.effective = self.mask
        except BaseException:
            self.close()
            raise

    def check_pin(self, proc):
        """Reads proc's affinity back (it is in the job): an outer job's limit wins over this
        job's, so a mask it overrides makes pin "overridden" and effective what it runs on."""
        plan = affinity_plan(self.mask)
        if self.pin != "pinned" or plan is None or plan[0] != "basic":
            return self.pin
        pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
        if K32.GetProcessAffinityMask(int(getattr(proc, "_handle", proc)), ctypes.byref(pm),
                                      ctypes.byref(sm)) and pm.value != plan[1]:
            self.pin, self.effective = "overridden", pm.value
            self.note = ("an outer job's affinity limit wins: the command runs on threads %s, "
                         "not %s" % (cpu.mask_text(pm.value), cpu.mask_text(self.mask)))
        return self.pin

    def _limit(self, kill):
        flags = win32.JOB_KILL_ON_CLOSE if kill else 0
        plan = affinity_plan(self.mask)
        if plan is None:
            return _set_ext(self.h, flags)
        if plan[0] == "basic":
            return _set_ext(self.h, flags | win32.JOB_AFFINITY, plan[1])
        return _set_ext(self.h, flags) and _set_groups(self.h, plan[1])

    def narrow(self, mask):
        """Pins the pinned job to `mask`, some of the threads it is pinned to now, while its
        processes run: Windows moves every process in the job, and in jobs nested in it, onto
        `mask` at once (measured 2026-10-10: a live process in a nested job without a pin of its
        own followed, and so did one started after). True when set; False, the pin unchanged,
        when the job is not pinned, `mask` is not a non-empty part of its mask, or Windows
        refused. repin(old) puts a wider mask back."""
        if self.h is None or self.pin != "pinned" or self.mask is None:
            return False
        if not mask or mask & ~self.mask:
            return False
        return self.repin(mask)

    def repin(self, mask):
        """Sets the pinned job's mask to `mask` (narrow's undo); False, unchanged, when refused.
        A wider mask moves the job's live processes back onto all of it too, in nested jobs as
        well (measured 2026-10-10: a process narrowed from 2 threads to 1 ran on both again at
        once, in the job and in a job nested in it)."""
        if self.h is None or self.pin != "pinned" or not mask:
            return False
        old = self.mask
        self.mask = mask
        if not self._limit(self.kill_on_close):
            self.mask = old
            self._limit(self.kill_on_close)
            return False
        self.effective = mask
        return True

    def assign(self, proc):
        """Put a live process (a Popen, or a handle int) in the job; raises JobError."""
        h = int(getattr(proc, "_handle", proc))
        if not K32.AssignProcessToJobObject(self.h, h):
            raise JobError("AssignProcessToJobObject: windows error %d" % ctypes.get_last_error())

    def assign_self(self):
        if not K32.AssignProcessToJobObject(self.h, K32.GetCurrentProcess()):
            raise JobError("AssignProcessToJobObject: windows error %d" % ctypes.get_last_error())

    def start(self, cmd, before_resume=None, **kw):
        """Popen(cmd) created suspended, put in the job, then resumed: no instant runs outside it
        or unpinned. before_resume(p), if given, runs while it is still suspended (a pid shield,
        say). Raises OSError when the command cannot start, and JobError (the child ended) when it
        cannot be put in the job or resumed. A bare leading bash runs Git Bash, never WSL's
        (kitlib.proc.bash; BareBashError, an OSError, when there is none). `creationflags` go
        through no_window_flags: from a parent with no console, no console window.

        Ctrl+C and Ctrl+Break are held from making the child to its resume (_Held). Once the
        child is in the job, before it is resumed, they go to the handlers they would have
        reached: one that raises ends the child unrun and start raises it; under SIG_DFL the
        child is ended, then the default ends this process. One that comes during the resume is
        handed on just after it, the same way. One in the last instants, after that hand-on and
        before the handlers are back, is raised as start returns, with self.p the child, running
        in the job. Under a handler that raises, start raises it, and the caller ends the child
        as for a Ctrl+C one instant after start: end(kill=True), or closing a kill-on-close job.
        Under SIG_DFL the default ends this process there; a kill-on-close job ends the child
        with it, but with Job(kill_on_close=False) nothing ends the child, and it runs on."""
        cmd = bash.resolve(cmd, kw.get("shell", False), kw.get("env"))
        flags = no_window_flags(kw.pop("creationflags", 0)) | win32.CREATE_SUSPENDED
        held = _Held()      # making the child to its resume; one that raises holds nothing
        try:
            p = subprocess.Popen(cmd, creationflags=flags, **kw)
            try:
                self.assign(p)
                self.check_pin(p)
                if before_resume is not None:
                    before_resume(p)
                held.deliver()  # in the job and never run: one that raises ends it unrun
                status = win32.resume(p)
                if status != 0:
                    raise JobError("the suspended command could not be resumed (NTSTATUS 0x%08X)"
                                   % (status & 0xFFFFFFFF))
                held.deliver()  # one that came during the resume
            except BaseException:
                p.kill()
                p.wait()
                raise
            self.p = p
        finally:
            held.close()
        return p

    def pids(self):
        return pid_list(self.h)

    def contains(self, pid):
        """Is process `pid` in this job (in a job nested in it counts as in)? "in", "out",
        "gone" (no such process) or "unknown" (it cannot be opened, pid 0 or 4 among them, or
        Windows will not say). Asked on an open handle, so the answer is about one process."""
        pid = int(pid)
        if self.h is None or pid in (0, 4):
            return "unknown"
        h = K32.OpenProcess(win32.QUERY_LIMITED, False, pid)
        if not h:
            return "gone" if ctypes.get_last_error() == win32.ERROR_INVALID_PARAMETER \
                else "unknown"
        try:
            got = win32.BOOL(0)
            if not K32.IsProcessInJob(h, self.h, ctypes.byref(got)):
                return "unknown"
            return "in" if got.value else "out"
        finally:
            K32.CloseHandle(h)

    def usage(self):
        """(CPU seconds so far, the job's peak committed memory, its process tree's working set
        now) in seconds and bytes, or None (ended). The working set is at
        least the command's own peak, which its handle keeps after it exits."""
        if self.h is None:
            return None
        acc, ext = win32.JOB_ACCOUNTING(), win32.EXT_LIMITS()
        if not (K32.QueryInformationJobObject(self.h, win32.JOB_ACCOUNTING_INFO,
                                              ctypes.byref(acc), ctypes.sizeof(acc), None)
                and K32.QueryInformationJobObject(self.h, win32.JOB_EXT_LIMITS, ctypes.byref(ext),
                                                  ctypes.sizeof(ext), None)):
            return None
        own = process_working_set(int(self.p._handle))[0] if self.p is not None else 0
        return ((acc.TotalUserTime + acc.TotalKernelTime) / 1e7, int(ext.PeakJobMemoryUsed),
                max(own, self.tree_working_set()))

    def tree_working_set(self):
        """Bytes: the working sets of the job's processes now, summed; a process
        that has gone or cannot be opened counts 0."""
        try:
            pids = self.pids()
        except OSError:
            return 0
        total = 0
        for pid in pids:
            h = K32.OpenProcess(win32.QUERY_LIMITED, False, pid)
            if h:
                try:
                    total += process_working_set(h)[1]
                finally:
                    K32.CloseHandle(h)
        return total

    def terminate(self, code=1):
        return bool(self.h) and bool(K32.TerminateJobObject(self.h, code))

    def set_kill_on_close(self, on):
        self.kill_on_close = bool(on)
        return self._limit(self.kill_on_close)

    def end(self, kill):
        """kill: end every process in the job. Else let them live, unpinned. Closes the job."""
        if self.h is None:
            if kill and self.p is not None and self.p.poll() is None:
                self.p.kill()
            return
        if kill:
            K32.TerminateJobObject(self.h, 1)
        else:
            self.unpin()
        self.close()

    def unpin(self):
        """The processes left in the job (a daemon the command started) get every
        thread back, then the job drops its affinity limit and kill-on-close. Dropping the limit
        alone leaves each process on the run's threads, so every thread is set first."""
        if self.mask is not None:
            self.mask = cpu.all_mask()
            self._limit(False)
            self.mask = None
        self.pin = self.effective = None
        self.kill_on_close = False
        self._limit(False)

    def close(self):
        if self.h:
            K32.CloseHandle(self.h)
        self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def cores_text(mask):
    if mask is None:
        return "all cores"
    return "cores " + cpu.mask_text(mask) if mask else "no cores"
