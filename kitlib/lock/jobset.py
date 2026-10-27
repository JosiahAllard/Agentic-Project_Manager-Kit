"""The jobs of one held run where a job starts one command (Linux; docs/linux-support-plan.md
section 8, slice 7).

kitlib.lock.run's Slot starts every command of a run (a gate's steps, red-first's selectors) in
its `job`. A Windows job object takes any number of them. A Linux job (kitlib.platform.linux
.contain.Job) starts one command and refuses a second, so on Linux a Slot's job is a JobSet: a
job of its own for each command, behind the one Job interface the Slot, the give-back and the
release use. Windows keeps its single job (one_command() is False there).

    one_command()   True where a job starts one command: a Slot then holds a JobSet
    is_open(job)    is a Job (Windows: its handle; Linux: not closed) or a JobSet still open
    JobSet(mask)    the set: its first job is made at once (so pin, effective and note are
                    known before any command, as a single job's are), each later start makes
                    the next one

A JobSet's reads cover every job still open: pids is their union and contains says "in" when
any job holds the pid. usage is the run's whole use: the CPU seconds of every job, those closed
included, so the total only grows, and the peak and resident memory of the jobs open now, summed
(each job's own peak, so the sum can overstate a peak the jobs never reached at once). narrow and
repin go to every open job and set the mask later jobs start with; when one refuses, the jobs
already changed are put back and the set is unchanged. unpin, terminate, end, leftovers and
close go to every job. A job whose command has exited and which holds no process is closed at
the next start or usage read, its CPU seconds kept, so a long gate does not keep a scope per
step. Nothing here prints.
"""
import kitlib.platform
from kitlib.proc import job as jobmod

# contains: the answer that wins when the jobs disagree (any "in" is in; "unknown" before "out",
# which is before "gone": a process one job sees as out is there).
_RANK = ("in", "unknown", "out", "gone")


def one_command():
    """True where a job starts one command (Linux): a Slot then holds a JobSet."""
    return kitlib.platform.NAME == "linux"


def is_open(job):
    """Is `job` still open: a Windows Job's handle is set, a Linux Job or a JobSet is not closed.
    None is not."""
    if job is None:
        return False
    if hasattr(job, "h"):
        return job.h is not None
    return not getattr(job, "closed", False)


class JobSet:
    """One job per command, as one job. make(mask) makes a job (default kitlib.proc.job.Job,
    looked up at call time); it raises JobError as Job does, here and from start."""

    def __init__(self, mask=None, make=None):
        self._make = make or (lambda m: jobmod.Job(m))
        self._want = mask           # the mask the next job is made with
        self.jobs = []              # the jobs not yet closed by a reap, oldest first
        self._used = set()          # the jobs a start was tried in
        self._cpu = {}              # job -> its CPU seconds at the last read
        self.done_s = 0.0           # the CPU seconds of the jobs reaped
        self._seen = False          # a usage read has given numbers
        self.p, self.closed = None, False
        self._newest = self._new()

    def _new(self):
        job = self._make(self._want)
        self.jobs.append(job)
        self._newest = job
        return job

    def _open(self):
        return [j for j in self.jobs if is_open(j)]

    # The newest job's: the one the next command starts in, or the last one started.
    mask = property(lambda self: self._newest.mask)
    pin = property(lambda self: self._newest.pin)
    effective = property(lambda self: self._newest.effective)
    note = property(lambda self: self._newest.note)

    # ---- start
    def start(self, cmd, before_resume=None, **kw):
        """Starts cmd in a job of its own (the first job, until a start was tried in it); as
        Job.start: OSError when it cannot start, JobError when it cannot be put in its job."""
        if self.closed:
            raise jobmod.JobError("the run's jobs are closed")
        self._reap()
        job = self._newest
        if job in self._used or not is_open(job):
            job = self._new()
        self._used.add(job)
        p = job.start(cmd, before_resume=before_resume, **kw)
        self.p = p
        return p

    def _reap(self):
        """Closes each job a start was tried in whose command has exited (or never started) and
        which holds no process, keeping its CPU seconds."""
        for job in list(self.jobs):
            if job not in self._used or not is_open(job):
                continue
            p = getattr(job, "p", None)
            if p is not None and p.poll() is None:
                continue
            try:
                if job.pids():
                    continue
            except OSError:
                continue
            u = job.usage()
            if u is not None:
                self._cpu[job], self._seen = u[0], True
            self.done_s += self._cpu.pop(job, 0.0)
            self.jobs.remove(job)
            self._used.discard(job)
            job.close()

    # ---- read
    def pids(self):
        """The pids in every open job; raises OSError when one cannot be read."""
        out = []
        for job in self._open():
            for pid in job.pids():
                if pid not in out:
                    out.append(pid)
        return out

    def contains(self, pid):
        """"in" when an open job holds pid; else what the jobs say ("unknown" with none open)."""
        said = [job.contains(pid) for job in self._open()]
        if not said:
            return "unknown"
        return min(said, key=lambda s: _RANK.index(s) if s in _RANK else 1)

    def usage(self):
        """(CPU seconds of every job so far, the open jobs' peak memory summed, their resident
        memory now summed), or None before any job gave numbers."""
        self._reap()
        live = []
        for job in self._open():
            u = job.usage()
            if u is not None:
                self._cpu[job] = u[0]
                live.append(u)
        if live:
            self._seen = True
        if not self._seen:
            return None
        cpu_s = self.done_s + sum(self._cpu.get(job, 0.0) for job in self.jobs)
        return (cpu_s, sum(u[1] for u in live), sum(u[2] for u in live))

    # ---- pin
    def _each(self, how, mask):
        jobs = self._open()
        if self.closed or not jobs:
            return False
        done = []
        for job in jobs:
            old = job.mask
            if not getattr(job, how)(mask):
                for back, was in reversed(done):
                    back.repin(was)
                return False
            done.append((job, old))
        self._want = mask
        return True

    def narrow(self, mask):
        """Every open job narrowed to `mask` (Job.narrow), and later jobs made with it; False,
        every job as it was, when one refuses."""
        return self._each("narrow", mask)

    def repin(self, mask):
        """Every open job's mask set to `mask` (Job.repin, narrow's undo), and later jobs made
        with it; False, every job as it was, when one refuses."""
        return self._each("repin", mask)

    def unpin(self):
        for job in self._open():
            job.unpin()
        self._want = None

    # ---- end
    def terminate(self, code=1):
        """Ends every process in every open job; True when each job's end was asked."""
        said = [job.terminate(code) for job in self._open()]
        return bool(said) and all(said)

    def leftovers(self, pids=None):
        """Moves pids (default: every process in the jobs) out of the jobs that hold them
        (Job.leftovers; a job without it moves none). Every job is asked; then JobError, naming
        each that could not, when any raised. Returns the pids moved."""
        moved, errors = [], []
        for job in self._open():
            move = getattr(job, "leftovers", None)
            if move is None:
                continue
            try:
                moved += move(None if pids is None else
                              [p for p in pids if job.contains(p) == "in"])
            except (jobmod.JobError, OSError) as e:
                errors.append(str(e))
        if errors:
            raise jobmod.JobError("; ".join(errors))
        return moved

    def end(self, kill):
        """kill: every process in every job ended; else they live on, unpinned. Closes them.
        Every job is ended though one raises; then the first error is raised again."""
        self._all("end", kill)

    def close(self):
        """Closes every job though one raises; then the first error is raised again."""
        self._all("close")

    def _all(self, name, *args):
        first = None
        for job in list(self.jobs):
            try:
                getattr(job, name)(*args)
            except BaseException as e:      # noqa: BLE001 - the rest still end; raised below
                if first is None:
                    first = e
        self.closed = True
        if first is not None:
            raise first

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
