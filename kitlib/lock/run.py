"""A run under the lock.

book() joins the queue, waits for the run's turn and returns a Slot that holds its cpu and gpu
slots. The Slot's launcher starts the run's commands in the run's job object (kitlib.proc.job):
pinned to its cores, measured and, when the run is cancelled, interrupted or errs, ended as a
tree but for a build daemon that serves or may serve another job (Slot.end_tree). release()
frees what it held and records the run. slot() is book() as a context manager that yields the
launcher. run_command() is `machine_lock.py run`: book, one command, release.

Nothing here prints: progress lines go to store.say, which is the CLI's stderr and
nowhere by default. A refusal raises LockError with the CLI's exit code.

Nesting: a run's commands get MACHINE_LOCK_RUN=<run id>:<wrapper pid>:<wrapper ctime>.
A run started under it (nest_check: the named wrapper is an ancestor, alive, and holds) inherits
each pool its parent holds enough of and takes only the rest; one that needs more of a pool than
its parent has, but some, is refused (exit 2): it would wait for the rest while its parent waits
for it. A run that inherits everything takes no queue place; its commands run in a job nested
in its parent's (still measured and ended as a tree).

On Linux a job starts one command (kitlib.platform.linux.contain), so a Slot's job is a
kitlib.lock.jobset JobSet there: a job of its own per command, read, pinned and ended as one.
"""
import contextlib
import ctypes
import datetime
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time

import kitlib.platform
from kitlib.lock import game
from kitlib.lock import giveback as gbk
from kitlib.lock import holds as hl
from kitlib.lock import jobset
from kitlib.lock import kinds as kinds_mod
from kitlib.lock import layout as lay
from kitlib.lock import queue as qm
from kitlib.lock import notice
from kitlib.lock import site
from kitlib.lock import stale
from kitlib.lock.store import (EXIT_BUSY, EXIT_HOLD, EXIT_REQUIRE, EXIT_RESERVED, EXIT_TIMEOUT,
                               EXIT_USAGE, LockError, Store, booking_hosts, caller, iso,
                               local_text, new_id, parse_iso, read_record, remove, session_of,
                               tree_role, utc_now, write_json)
from kitlib.platform.linux import process as procfs
from kitlib.proc import bash, cpu, daemons, process, win32, winwatch
from kitlib.proc.job import Job, JobError, cores_text

POOLS, PICK = lay.POOLS, qm.PICK
NEST_ENV = "MACHINE_LOCK_RUN"
RUN_ID_RE = re.compile(r"r-\d{8}T\d{6}Z-[0-9a-f]{6}")   # store.new_id("r")
NEST_RE = re.compile(r"^(%s):(\d+):(\d+)$" % RUN_ID_RE.pattern)
CLAIM_TRIES = 16            # claim_id: how many ids it draws before it gives up
ORIGIN_ENV = "MACHINE_LOCK_ORIGIN"      # a detached run's launcher ancestry, pid:ctime;...
BG_ENV = "MACHINE_LOCK_BG"
CPUSET_ENV, CPUMASK_ENV, CPUS_ENV = "MACHINE_LOCK_CPUSET", "MACHINE_LOCK_CPUMASK", "MACHINE_LOCK_CPUS"
GPUS_ENV = "MACHINE_LOCK_GPUS"          # the gpu cards the run holds, by adapter index
END_ENV = "MACHINE_LOCK_END"            # how the run ended, for --on-release
XLA_FRACTION_ENV, XLA_DEFAULT_FRACTION = "XLA_PYTHON_CLIENT_MEM_FRACTION", "0.3"
KILL_HOW = ("interrupted", "cancelled", "error")
# A run watches for console windows (kitlib.proc.winwatch) from its first command's start to
# its release (Slot.watch_stop); False turns the monitor off. Windows only: there are no console
# windows to watch elsewhere.
WATCH_WINDOWS = True
# end_sparing, when it spares a shared daemon: how many passes it ends the job's other
# processes in, how long it waits after each for them to leave the job, and how long after the
# last for one still listed to exit before it says it runs on.
END_PASSES, END_PASS_S, END_WAIT_S = 10, 0.1, 1.0
# end_sparing's result for a daemon when daemons.stop raised (it may have ended some first).
NOT_JUDGED = "not judged "
# What a `docker exec` run holding no gpu slot gets, so the process in the container keeps
# off the gpus the lock gave other runs (the job cannot reach into the container).
DOCKER_CPU_ENV = (("CUDA_VISIBLE_DEVICES", ""), ("JAX_PLATFORMS", "cpu"))
# docker exec options that take a value as the next argument (`docker exec --help`).
DOCKER_EXEC_VALUE_OPTS = ("-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir",
                          "--detach-keys")


def wait_text(wait_s):
    return "no limit" if wait_s is None else "%d s" % wait_s


def child_code(code):
    return code - (1 << 32) if code > 0x7FFFFFFF else code     # NTSTATUS exit codes


def filetime_now():
    """The system time now as a FILETIME count (100 ns units since 1601), as
    GetSystemTimeAsFileTime reads it. _end_others compares process creation times
    (kitlib.proc.process times) with it. That a process created after this call never has an
    earlier creation time rests on Windows stamping creation times from this same system clock:
    inferred from the documented clocks, not measured, and void if the clock is set back.
    On Linux: the seam's stamp in the Linux ctime's encoding, the boot and the clock ticks since
    it (kitlib.platform.linux.process stamp_now)."""
    if kitlib.platform.NAME == "linux":
        return procfs.stamp_now()
    ft = win32.FILETIME()
    win32.K32.GetSystemTimeAsFileTime(ctypes.byref(ft))
    return ft.value()


# ------------------------------------------------------------------ nesting
def _ancestors():
    try:
        return {(int(p), int(c)) for p, c in process.own_ancestors()}
    except Exception:
        return set()


def _own_pidns():
    """This process's pid namespace (Linux), else None."""
    return procfs.pidns() if kitlib.platform.NAME == "linux" else None


def nest_check(store, value):
    """(parent, None) when MACHINE_LOCK_RUN's value names the run this process is inside (the
    named wrapper is one of its ancestors, alive, and its record says held), else (None, why).
    On Linux a process in a job's own pid namespace (contain's W1) sees none of its ancestors past
    that job's pid 1, so the wrapper is not one of them; there it is in the run when its record
    lists this process's pid namespace in `jobns` (Slot.start records each job's before the
    command runs) and the run's lock is held (store.run_mutex_held).
    parent = {id, pid, ctime, kind, shares, resource (None: it names none), has: {pool: slots it
    holds or inherited}, cores, gpus}.
    A record that is there but cannot be read (its wrapper's os.replace is swapping it) is read
    again for settings.queue_read_s, then LockError (EXIT_REQUIRE): never "holds no lock", which
    would have this run take its own lock inside one that holds, and wait on that run's slots."""
    try:
        m = NEST_RE.match(value or "")
        if not m:
            return None, "malformed value"
        rid, pid, ct = m.group(1), int(m.group(2)), int(m.group(3))
        ancestor = (pid, ct) in _ancestors()
        ns = None if ancestor else _own_pidns()
        if not ancestor and ns is None:
            return None, "the named wrapper is not an ancestor of this run"
        path = store.path("active", rid + ".json")
        d, there = read_record(path, store.settings.queue_read_s)
        if there and d is None:
            raise LockError(EXIT_REQUIRE, "require-lock: %s names run %s, whose record %s could "
                                          "not be read for %g s; the command was not run"
                            % (NEST_ENV, rid, path, store.settings.queue_read_s))
        if not d or d.get("state") != "held":
            return None, "the named run holds no lock"
        if (d.get("pid"), d.get("ctime")) != (pid, ct):
            return None, "the record names another wrapper"
        if not ancestor:
            jobns = d.get("jobns")
            if not isinstance(jobns, list):
                return None, "the named wrapper is not an ancestor of this run"
            if ns not in jobns:
                return None, ("the named wrapper is not an ancestor of this run, nor is this run "
                              "in a job it started")
            if store.run_mutex_held(rid) is not True:
                return None, "the holder is dead"
        elif not process.alive(pid, ct):
            return None, "the holder is dead"
        has = d.get("has") if isinstance(d.get("has"), dict) else {}
        return {"id": rid, "pid": pid, "ctime": ct, "kind": d.get("kind"),
                "shares": dict(d.get("shares") or {}), "resource": d.get("resource"),
                "has": {p: n for p, n in has.items() if p in POOLS and type(n) is int and n > 0},
                "cores": list(d.get("cores") or []), "gpus": list(d.get("gpus") or [])}, None
    except LockError:
        raise
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def nest_need(parent, kind, need):
    """(own, has, None) for a run of `kind` needing `need` nested in `parent` (None: not
    nested): own, what it takes itself; has, what it holds or inherits, per pool. Else (None,
    None, why)."""
    own, has = dict(need), {p: need[p] for p in POOLS}
    for p in POOLS:
        ph = (parent or {}).get("has", {}).get(p, 0)
        if not need[p] or not ph:
            continue
        if ph < need[p]:
            return None, None, ("a %s run needs %s, but the run it is nested in (%s, run %s) holds "
                                "%s; start it outside that run" % (
                                    kind, lay.need_text({p: need[p]}), parent.get("kind"),
                                    parent["id"], lay.need_text({p: ph})))
        own[p], has[p] = 0, ph
    return own, has, None


def bg_ancestor(env, store=None):
    """True when a background run's wrapper (MACHINE_LOCK_BG) is an ancestor of this process.
    On Linux, given the store, also when the wrapper's record names a pid namespace other than
    this process's (a job's own, contain's W1): its ancestors cannot be seen from in there, so
    it is taken as one (fail closed: the caller refuses a gpu lock)."""
    m = NEST_RE.match(env.get(BG_ENV) or "")
    if not m:
        return False
    if (int(m.group(2)), int(m.group(3))) in _ancestors():
        return True
    ns = _own_pidns() if store is not None else None
    if ns is None:
        return False
    d, _there = read_record(store.path("active", m.group(1) + ".json"))
    theirs = d.get("pidns") if isinstance(d, dict) else None
    return theirs is not None and theirs != ns


def run_ancestors(detached, env):
    """[[pid, ctime]] of the processes this run was started under (stop's and the API's
    ownership check). A detached wrapper's parents are WMI's, so it adds its launcher's."""
    out = _ancestors()
    if detached:
        for item in (env.get(ORIGIN_ENV) or "").split(";"):
            m = re.fullmatch(r"(\d+):(\d+)", item)
            if m:
                out.add((int(m.group(1)), int(m.group(2))))
    return sorted([p, c] for p, c in out)


def origin_pairs(env):
    """[(pid, ctime)] a detached wrapper's launcher recorded in ORIGIN_ENV."""
    out = []
    for item in (env.get(ORIGIN_ENV) or "").split(";"):
        m = re.fullmatch(r"(\d+):(\d+)", item)
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def run_role(detached, env):
    """owner or agent: who started this run, from the process tree, for admits (only the
    owner's runs use an owner whole hold). A detached wrapper's own parents are WMI's, so
    its launcher's ancestry decides (store.tree_role)."""
    if detached:
        return tree_role(origin_pairs(env))
    return caller()["role"]


# ------------------------------------------------------------------ the record
class Usage:
    """A run's measured use, from its job object: CPU seconds, the most physical cores
    busy over any sample window (CPU time per second over the threads a core has), the process
    tree's peak working set and the job's peak committed memory; and, for live views, the cores
    busy over the last window and the working set now. A background run's restarts add up."""

    def __init__(self, threads_per_core=1, sample_s=5.0):
        self.tpc, self.sample_s = max(1, threads_per_core), sample_s
        self.done_s, self.cur_s, self.peak_cores, self.last = 0.0, 0.0, 0.0, None
        self.peak_mem, self.peak_commit, self.now_cores, self.now_mem = 0, 0, 0.0, 0

    def sample(self, job):
        """Reads the job; True when a sample window closed (the record is worth saving)."""
        u = job.usage() if job is not None else None
        if u is None:
            return False
        self.cur_s, commit, mem = u
        self.now_mem = mem
        self.peak_mem, self.peak_commit = max(self.peak_mem, mem), max(self.peak_commit, commit)
        t = time.monotonic()
        if self.last is None:
            self.last = (t, self.cur_s)
            return False
        if t - self.last[0] < self.sample_s:
            return False
        rate = (self.cur_s - self.last[1]) / (t - self.last[0])
        self.now_cores = rate / self.tpc
        self.peak_cores = max(self.peak_cores, self.now_cores)
        self.last = (t, self.cur_s)
        return True

    def job_ended(self):
        self.done_s, self.cur_s, self.last, self.now_cores, self.now_mem = (
            self.done_s + self.cur_s, 0.0, None, 0.0, 0)

    def fields(self):
        return {"cpu_s": round(self.done_s + self.cur_s, 1),
                "peak_cpu_cores": round(self.peak_cores, 2),
                "peak_mem_mb": int(self.peak_mem // 2**20),
                "peak_commit_mb": int(self.peak_commit // 2**20),
                "cpu_cores_now": round(self.now_cores, 2), "mem_mb_now": int(self.now_mem // 2**20)}


def check_run_id(rid):
    """Raises LockError (usage, exit 2) unless rid is None or shaped as store.new_id("r") makes a
    run id: a given id names files under the store, so nothing else may reach a path."""
    if rid is not None and not (isinstance(rid, str) and RUN_ID_RE.fullmatch(rid)):
        raise LockError(EXIT_USAGE, "usage: %r is not a run id (r-<UTC second>-<6 hex>)" % (rid,))


def id_left(store, rid):
    """The first file of an earlier run with id rid still in the store (its record, stop file,
    ended record or background folder), or None."""
    for p in (store.path("active", rid + ".json"), store.path("active", rid + ".stop"),
              store.path("ended", rid + ".json"), store.path("background", rid + ".d")):
        if os.path.lexists(p):
            return p
    return None


def claim_id(store, rid=None, tries=CLAIM_TRIES):
    """(id, its run mutex, held): the id a new run takes, claimed before any of its records exist.
    An id is taken while another holds its run mutex or anything of an earlier run with it is
    left (id_left). The files are looked at once the mutex is held: no live run then has the id,
    and a run that had it wrote its ended record before it let the mutex go (settle). A drawn id
    that is taken is drawn again, up to `tries` times; a given one (rid, as --detached-id) is
    refused. Raises LockError: usage (exit 2) for a rid not shaped as a run id, exit 10 when no id
    is had. A refusal writes nothing."""
    check_run_id(rid)
    why = None
    for _ in range(1 if rid else tries):
        cand = rid or new_id("r")
        try:
            mutex = store.run_mutex(cand)
        except OSError as e:
            why = str(e)
            continue
        left = id_left(store, cand)
        if left is None:
            return cand, mutex
        mutex.release()
        why = "%s exists" % left
    if rid:
        raise LockError(EXIT_REQUIRE, "require-lock: run id %s is taken (%s); the command was "
                                      "not run" % (rid, why))
    raise LockError(EXIT_REQUIRE, "require-lock: no free run id in %d draws (the last: %s); the "
                                  "command was not run" % (tries, why))


class Run:
    """A run's record, active/<id>.json while it lives, ended/<id>.json after. Its id comes from
    claim_id (book, run_background); one made here is not checked against the store."""

    def __init__(self, store, L, repo, slug, kind, note, cmd, state, rid=None, detached=False,
                 env=None, log=None):
        env = os.environ if env is None else env
        self.store = store
        ancestors = run_ancestors(detached, env)
        self.rec = {"id": rid or new_id("r"), "state": state, "repo": repo, "slug": slug,
                    "kind": kind, "note": note, "pid": os.getpid(), "ctime": process.own_ctime(),
                    "since": iso(), "cmd": [str(c) for c in cmd or ()][:20],
                    "detached": bool(detached), "holding": [], "slots": {}, "cores": [],
                    "gpus": [], "shares": {}, "session": session_of(env),
                    "ancestors": ancestors, "hosts": booking_hosts(ancestors),
                    "role": run_role(detached, env),
                    "layout": lay.slot_key(L),
                    "log": getattr(log, "name", None) if log is not None else None}
        if kitlib.platform.NAME == "linux":
            self.rec["pidns"] = procfs.pidns()  # its pid is this namespace's (store.record_state)
        self.id = self.rec["id"]
        self.rel = "active/%s.json" % self.id
        self.log = log
        self.usage = Usage(L["cpu_slot_threads"], store.settings.usage_sample_s)
        self.ended = None

    def save(self, **kw):
        self.rec.update(kw)
        self.store.write(self.rel, self.rec)

    def stop_asked(self):
        return os.path.exists(self.store.path("active", self.id + ".stop"))

    def finish(self, code, how):
        """The record moves to the ended folder; returns it."""
        self.ended = dict(self.rec, state="ended", code=code, how=how, ended=iso())
        stop, _ = read_record(self.store.path("active", self.id + ".stop"))
        if stop:
            self.ended["stopped_by"] = {"by": stop.get("by"), "session": stop.get("session")}
            if stop.get("why"):
                self.ended["stopped_by"]["why"] = stop["why"]
        with contextlib.suppress(OSError):
            write_json(self.store.path("ended", self.id + ".json"), self.ended)
        remove(self.store.path("active", self.id + ".json"))
        remove(self.store.path("active", self.id + ".stop"))
        remove(gbk.request_path(self.store, self.id))
        self.store.bump()
        return self.ended

    def popen_kw(self, env):
        kw = {"env": env}
        if self.log is not None:
            kw.update(stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        return kw


def docker_cpu_env(cmd, holds_gpu):
    """cmd with `-e CUDA_VISIBLE_DEVICES= -e JAX_PLATFORMS=cpu` put right after `exec` when cmd is
    `docker exec ...` (an argument list) and the run holds no gpu slot; cmd itself
    otherwise. A variable the caller already sets with -e/--env before the container keeps the
    caller's value: it is not added again."""
    if holds_gpu or not isinstance(cmd, (list, tuple)) or len(cmd) < 2:
        return cmd
    if os.path.basename(str(cmd[0])).lower() not in ("docker", "docker.exe") or cmd[1] != "exec":
        return cmd
    given, args, i = set(), [str(a) for a in cmd[2:]], 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        a, val = args[i], None
        if a in DOCKER_EXEC_VALUE_OPTS:
            val = args[i + 1] if i + 1 < len(args) else ""
            i += 1
        elif a.startswith("--env="):
            a, val = "--env", a[len("--env="):]
        elif a.startswith("-e") and len(a) > 2:
            a, val = "-e", a[2:]
        if a in ("-e", "--env") and val is not None:
            given.add(val.split("=", 1)[0])
        i += 1
    add = []
    for name, value in DOCKER_CPU_ENV:
        if name not in given:
            add += ["-e", "%s=%s" % (name, value)]
    return list(cmd[:2]) + add + list(cmd[2:])


def export_threads(env, job):
    """The threads the command runs on (every thread of this machine when it is not pinned), as
    a Linux cpuset list of global thread ids and a hex mask (docker --cpuset-cpus)."""
    mask = job.mask if job.mask is not None else cpu.all_mask()
    env[CPUSET_ENV] = env[CPUS_ENV] = cpu.mask_text(mask)
    env[CPUMASK_ENV] = hex(mask)


def pin_fields(job):
    """The run record's pin fields from its job: pinned only when the mask is in force; pin is
    "pinned", "overridden" (an outer job's limit wins) or None; threads what it runs on."""
    return {"pinned": job.pin == "pinned", "pin": job.pin, "pin_note": job.note,
            "threads": cpu.mask_text(job.effective) if job.effective else None}


# ------------------------------------------------------------------ waiting for a turn
def acquire(store, run, L, need, wait_s, no_wait, prefer=None, within=None):
    """Waits for run's turn in the queue and returns {pool: [slot]} once it holds `need`
    (its entry and record name them). Raises LockError with its entry gone: 11 when a strong
    hold (owner or game) leaves it too few for its wait, or its own whole hold is over or has
    not started; 6 (--no-wait) or 8 when agent holds or runs do; 10 when the queue or the holds
    cannot be read before it joined; 1 when stop cancelled it. It never starts on a slot a hold but its own
    covers within its expected time; one they leave too few of keeps its place, parked. With
    no_wait it waits only for background runs to yield. prefer: its hold's slots. within: {pool:
    the only slots it may take} (book's within): its entry carries it (kitlib.lock.queue.plan)
    and only holds over those slots park it. After its first look at the queue it moves dead
    runs' records to ended/ (Store.live_runs prune), whether or not it then gets its slots."""
    s, say = store.settings, store.say
    deadline = time.monotonic() + (0 if no_wait else math.inf if wait_s is None else wait_s)
    span, hid = run.rec["expect_s"], run.rec.get("hold")
    whole = run.rec.get("hold_scope") == "whole"
    told = said = seen = yielding = ypool = qbad = hbad = stale_at = None
    joined = back = reaped = False
    while True:
        now = utc_now()
        game.sync(store, L)
        try:
            hs = hl.records(store, L)
        except hl.HoldsUnreadable as err:
            # Never planned on as no holds (that frees every slot a hold covers). A run not yet
            # in the queue exits; one already waiting keeps its place, takes nothing, and looks
            # again.
            if not joined:
                raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % err)
            if hbad is None:
                hbad = str(err)
                say("run %s: %s; it keeps its place and looks again" % (run.id, err))
            why = ("cancelled" if run.stop_asked() else "--no-wait" if no_wait else
                   "timeout" if time.monotonic() >= deadline else None)
            if why:
                qm.leave(store, run.id)
            if why == "cancelled":
                raise LockError(1, "run %s was cancelled by stop while it waited; the command "
                                   "was not run" % run.id)
            if why == "--no-wait":
                raise LockError(EXIT_BUSY, "NOT ACQUIRED: the holds are unreadable; --no-wait")
            if why == "timeout":
                raise LockError(EXIT_TIMEOUT, "NOT ACQUIRED: the holds are unreadable; waited %s "
                                              "(its --max-wait)" % wait_text(wait_s))
            time.sleep(s.queue_poll_s)
            continue
        if hbad is not None:
            say("run %s: the holds read again" % run.id)
            hbad = None
        h = next((x for x in hs if x["id"] == hid), None) if hid else None
        if whole and h is None:
            qm.leave(store, run.id)
            raise LockError(EXIT_RESERVED, "refused: owner hold %s (quiet hours) is over or was "
                            "ended, and run %s runs only inside it (--hold). Exit 11."
                            % (hid, run.id))
        cover, blocking, ablocking = hl.cover_plan(store, L, need, span, own=hid, hs=hs,
                                                   within=within)
        park, clear, why_kind = [], None, None
        if whole and h["_start"] > now:
            park, clear, why_kind = [h], h["_start"], "start"
        elif blocking:
            park, clear, why_kind = blocking, max(x["_end"] for x in blocking), "strong"
        elif ablocking:
            park, clear, why_kind = ablocking, max(hl.clear_at(b) for b in ablocking), "agent"
        ids = [x["id"] for x in park]
        key = {p: sorted(cover.get(p, {})) for p in POOLS}
        if seen is not None and key != seen and not park:
            say("run %s: the holds changed while it waited; it plans again" % run.id)
        seen = key
        if park and (no_wait or (clear - now).total_seconds() > deadline - time.monotonic()):
            qm.leave(store, run.id)
            why = "--no-wait" if no_wait else "its wait ran out" if told is not None else (
                {"start": "it starts at %s", "strong": "it ends at %s",
                 "agent": "it keeps them until %s"}[why_kind] % local_text(clear)
                + ", after this run's wait (%s)" % wait_text(wait_s))
            if why_kind == "start":
                raise LockError(EXIT_RESERVED, "refused: owner hold %s (quiet hours) has not "
                                "started, and run %s runs only inside it (--hold); %s. Exit 11."
                                % (hid, run.id, why))
            if why_kind == "strong":
                raise LockError(EXIT_RESERVED, hl.refusal_text(run.id, need, span, park, why))
            raise LockError(EXIT_BUSY if no_wait else EXIT_TIMEOUT, "NOT ACQUIRED: %s; %s" % (
                hl.agent_wait_text(run.id, need, span, park), why))
        if park and told != ids:
            say({"start": "run %s waits for %s to start (at %s); it runs only inside it",
                 "strong": "run %s waits for %s to end (by %s) before it takes a slot",
                 "agent": "run %s waits for %s (until %s) before it takes a slot"}[why_kind] % (
                run.id, "; ".join(hl.text(x) for x in park), local_text(clear)))
            told = ids
            with contextlib.suppress(OSError):
                run.save(blocked_by=ids)
        elif not park and told is not None:
            say("run %s: the hold in its way is over; it takes its slots" % run.id)
            told = None
            with contextlib.suppress(OSError):
                run.save(blocked_by=None)
        stop = run.stop_asked()
        result, text, got, moved = None, None, None, None
        try:
            with store.lock("queue", s.queue_lock_s if not joined else 2):
                q = qm.load(store)
                before = json.dumps(q, sort_keys=True)
                qm.prune(store, L, q, run.id)
                # Its entry gone while it waited (an unreadable queue.json was set aside): it
                # joins again, at the back.
                back = joined and qm.entry_of(q, run.id) is None
                e = qm.entry_of(q, run.id) or qm.join(q, run.rec, need)
                # A parked entry is planned from clear_at, around what else covers its slots
                # (avoid_after); plan holds later runs to it.
                e.update(avoid={p: sorted(cover[p]) for p in POOLS if cover.get(p)},
                         parked=bool(park), blocked_by=ids or None, prefer=prefer or {},
                         clear_at=clear.timestamp() if park else None,
                         avoid_after={p: sorted(i for i, x in cover[p].items() if x["id"] not in ids)
                                      for p in POOLS if cover.get(p)} if park else {})
                if within:
                    e["within"] = within
                planned = qm.plan(L, q)[0]
                info = planned[run.id]
                held = qm.holders(L, q, run.id)
                in_way = {p: [held[p][i] for i in info[PICK[p]] if i in held[p]] for p in POOLS}
                # A parked run takes nothing, even planned now: it looks again with the holds
                # read anew.
                ready = info["now"] and not park and not any(in_way.values())
                bg_only = info["now"] and not ready
                if ready:
                    got = {p: info[PICK[p]] for p in POOLS}
                    moved = e.get("moved")
                    e.update(state="held", cores=got["cpu"], gpus=got["gpu"], held_at=time.time(),
                             parked=False, blocked_by=None)
                elif stop:
                    result = 1
                elif no_wait and bg_only and yielding is not None and time.monotonic() > yielding:
                    result, text = EXIT_BUSY, ("NOT ACQUIRED: %s; --no-wait, and the background "
                                               "run did not yield in time"
                                               % qm.busy_text(L, q, e, planned))
                elif no_wait and bg_only:
                    if yielding is None:
                        ypool = next(p for p in ("gpu", "cpu") if in_way[p])
                        wait_y = s.bg_yield_s + s.bg_yield_margin_s
                        yielding = time.monotonic() + wait_y
                        text = ("run %s: --no-wait, but only a background run holds the %s it "
                                "needs; it asks it to pause and waits up to %d s for that" % (
                                    run.id, "cores" if ypool == "cpu" else "gpu slot", wait_y))
                elif no_wait:
                    short = qm.short_pools(L, q, e)
                    result = EXIT_BUSY
                    if yielding is not None and short:
                        text = "NOT ACQUIRED: foreground work took the %s it %s: %s; --no-wait" % (
                            ypool if ypool in short else short[0],
                            "waited for" if ypool in short else "needs next",
                            qm.busy_text(L, q, e, planned))
                    else:
                        text = "NOT ACQUIRED: %s; --no-wait" % qm.busy_text(L, q, e, planned)
                elif time.monotonic() >= deadline:
                    result, text = EXIT_TIMEOUT, "NOT ACQUIRED: %s; waited %s (its --max-wait)" % (
                        qm.busy_text(L, q, e, planned), wait_text(wait_s))
                elif not park and said != info["position"]:
                    said = info["position"]
                    text = "run %s waits in the queue: position %d of %d, expected to start %s%s; %s" % (
                        run.id, info["position"], len(planned), qm.est_text(info),
                        " (ahead of its turn: it ends before the runs ahead of it start)"
                        if info["backfill"] else "", qm.busy_text(L, q, e, planned))
                if result is not None:
                    q["entries"].remove(e)
                if json.dumps(q, sort_keys=True) != before:
                    qm.save(store, q)
                joined = True
        except qm.QueueUnreadable as err:
            # Never planned on as empty (its save would wipe every entry). A run not yet in the
            # queue exits; one already waiting keeps its place, writes nothing, and looks again.
            if not joined:
                raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % err)
            if qbad is None:
                qbad, said = str(err), None
                say("run %s: %s; it keeps its place and looks again" % (run.id, err))
            if stop:
                raise LockError(1, "run %s was cancelled by stop while it waited; the command "
                                   "was not run" % run.id)
            if no_wait and (yielding is None or time.monotonic() > yielding):
                raise LockError(EXIT_BUSY, "NOT ACQUIRED: the queue is unreadable; --no-wait")
            if not no_wait and time.monotonic() >= deadline:
                raise LockError(EXIT_TIMEOUT, "NOT ACQUIRED: the queue is unreadable; waited %s "
                                              "(its --max-wait)" % wait_text(wait_s))
            time.sleep(s.queue_poll_s)
            continue
        except OSError as err:
            if not joined:
                raise LockError(EXIT_REQUIRE, "require-lock: the queue could not be read or "
                                              "written (%s); the command was not run" % err)
            time.sleep(s.queue_poll_s)
            continue
        if not reaped:
            # Once, after the first prune of the queue said which dead runs it dropped: records
            # in active/ whose wrapper is surely gone (Store.run_live) move to ended/, how
            # "reaped". Nothing else takes them out, and every read of the store walks them. An
            # ended record makes a later prune drop the run's entry quietly (queue.prune).
            reaped = True
            with contextlib.suppress(OSError):
                store.live_runs(prune=True)
        if stale_at is None or time.monotonic() >= stale_at:
            # Then, and every stale_check_s while it waits: a held run whose booking sessions
            # are gone and that has held far past its guide is asked to stop (kitlib.lock.stale).
            # Outside the queue mutex; a reap that fails never fails this booking.
            stale_at = time.monotonic() + s.stale_check_s
            try:
                stale.reap(store, request_stop)
            except Exception as err:        # noqa: BLE001
                say("run %s: the stale-run look failed (%s: %s)" % (run.id, type(err).__name__, err))
        if qbad is not None:
            say("run %s: the queue reads again%s" % (
                run.id, "; its entry was gone, so it joined again at the back" if back else ""))
        elif back:
            say("run %s: its queue entry was gone; it joined again at the back" % run.id)
        qbad = None
        if result == 1:
            raise LockError(1, "run %s was cancelled by stop while it waited; the command was "
                               "not run" % run.id)
        if result is not None:
            raise LockError(result, text)
        if text:
            say(text)
        if got is not None:
            # A hold made between this look at them and the take: the run gives back what it
            # covers and plans again (it then waits, takes others, or exits 11, 6, 8).
            try:
                now_cover = hl.cover_plan(store, L, need, span, own=hid)[0]
            except hl.HoldsUnreadable:
                qm.give_back(store, run.id)     # it looks again; the loop's top waits on them
                continue
            hit = [now_cover[p][i] for p in POOLS for i in got[p] if i in now_cover.get(p, {})]
            if hit and qm.give_back(store, run.id):
                say("run %s: %s now covers %s; it gives it back and plans again" % (
                    run.id, hl.text(hit[0]),
                    " + ".join(lay.slots_text(p, got[p]) for p in POOLS if got[p])))
                continue
            with contextlib.suppress(OSError):
                run.save(state="held", held_since=iso(), blocked_by=None, cores=got["cpu"],
                         gpus=got["gpu"], slots={p: got[p] for p in POOLS if got[p]},
                         holding=[p for p in POOLS if got[p]], **({"moved": moved} if moved else {}))
            return got
        time.sleep(s.queue_poll_s)


def settle(store, run, code, how, mutex=None):
    """The run's queue entry goes (what it held is free), its record moves to ended and its
    history line is written, then its run mutex is released. Ctrl+C is ignored meanwhile
    (on the main thread), so a second Ctrl+C cannot leave its entry or record behind. With the
    queue unreadable the record still moves: the next look drops the entry once the mutex is
    gone. Returns the ended record."""
    try:
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:                      # not the main thread
        old = None
    try:
        store.pause("SETTLE")
        ended = None
        try:
            with store.lock("queue", 60):
                q = qm.load(store)
                n = len(q["entries"])
                q["entries"] = [e for e in q["entries"] if e["id"] != run.id]
                if len(q["entries"]) != n:
                    qm.save(store, q)
                ended = run.finish(code, how)
        except OSError:
            pass
        if ended is None:
            ended = run.finish(code, how)
        store.history_append(ended)
        notice.ended(store, ended)
        if mutex is not None:
            mutex.release()
        return ended
    finally:
        if old is not None:
            signal.signal(signal.SIGINT, old)


def on_release(store, cmdline, env, how, run=None):
    """The caller's --on-release command: after the run's command ended (any way but the wrapper
    dying) and before its slots are freed; for work the job cannot end (a process `docker exec`
    started in a container). It runs in a job of its own, ended as a tree past
    settings.on_release_s or on a second Ctrl+C. Its exit is reported, never the run's. The
    record says `releasing` meanwhile, so stop waits for it on its own budget."""
    if not cmdline:
        return
    e = dict(env)
    e[END_ENV] = how
    limit = store.settings.on_release_s
    if run is not None:
        with contextlib.suppress(OSError):
            run.save(releasing=iso(), release_s=limit)
    job = None
    try:
        job = Job(None)
        p = job.start(cmdline, shell=True, **(run.popen_kw(e) if run else {"env": e}))
        try:
            rc = p.wait(limit)
        except subprocess.TimeoutExpired:
            job.end(kill=True)
            store.say("WARNING: the --on-release command ran past %d s; its process tree was ended"
                      % limit)
            return
        if rc:
            store.say("WARNING: the --on-release command exited %d" % child_code(rc))
    except OSError as err:
        store.say("WARNING: the --on-release command could not start: %s" % err)
    except BaseException:                   # a second Ctrl+C
        if job is not None:
            job.end(kill=True)
        store.say("Ctrl+C: the --on-release command's process tree was ended")
    finally:
        if job is not None:
            job.end(kill=False)


# ------------------------------------------------------------------ ending a run's tree
def tell_daemons(store, rid, rec, done):
    """Says each of daemons.stop's results for run rid and lists them in rec's `daemons` (rec
    None, a nested slot's: not listed): a pid already listed (by an earlier judgement of this
    run, as a second end_tree after a Ctrl+C makes) takes its new result; the others listed
    stay."""
    for pid, label, res in done:
        if res.startswith("left: "):
            store.say("run %s: left the %s (pid %d) running: %s"
                      % (rid, label, pid, res[len("left: "):]))
        elif res.startswith(NOT_JUDGED):
            store.say("WARNING: run %s: the %s (pid %d) was %s; its tree is spared if it "
                      "still runs" % (rid, label, pid, res))
        else:
            store.say("run %s: stopped the %s (pid %d) it left: %s" % (rid, label, pid, res))
    if rec is not None:
        new = [{"pid": pid, "what": label, "result": res} for pid, label, res in done]
        judged = {d["pid"] for d in new}
        rec["daemons"] = [d for d in rec.get("daemons") or ()
                          if d.get("pid") not in judged] + new


def move_leftovers(store, rid, job):
    """Before run rid's job is closed without a kill: unpins it (job.unpin), then moves what still
    runs in it out of it (Job.leftovers, Linux: into a scope of its own, or, on W2, out of the
    job's kill), so it runs on, unpinned, and the run's own scope ends. The unpin comes first:
    on S2 and W2 the pin is each process's affinity, and the job's own unpin (at its close)
    reaches only the processes still in it, so a moved one, and what it starts later, would keep
    cores the lock has given to other runs. An unpin that fails is said and the move goes on. A
    job with no leftovers (a Windows job object, which cannot let a process out) is neither
    unpinned nor moved here. A move that fails is said and never fails the release: what is left
    runs on in the job, unpinned, as on Windows. Returns the pids moved."""
    move = getattr(job, "leftovers", None)
    if move is None or not jobset.is_open(job):
        return []
    try:
        job.unpin()
    except Exception as e:                  # noqa: BLE001 - the move and the release go on
        with contextlib.suppress(Exception):
            store.say("WARNING: run %s: its job could not be unpinned before what it left was "
                      "moved out: %s" % (rid, e))
    try:
        moved = list(move() or [])
    except Exception as e:                  # noqa: BLE001 - the release goes on
        with contextlib.suppress(Exception):
            store.say("WARNING: run %s: what it left could not be moved out of its job: %s; it "
                      "runs on there, unpinned" % (rid, e))
        return []
    if moved:
        with contextlib.suppress(Exception):
            store.say("run %s: left %s running, moved out of its job"
                      % (rid, ", ".join("pid %d" % p for p in moved)))
    return moved


def end_sparing(store, rid, job, rec):
    """Ends every process in run rid's job but a build daemon another job's client uses or may
    use, with that daemon's tree: every daemon kitlib.proc.daemons stop leaves, so any MSBuild
    node and the compiler server (PIPE_LABELS: their clients cannot be told), with what they
    started. True when it spared one: the caller then closes the job without killing it
    (job.end(kill=False)). The kill path of a foreground run (Slot.end_tree) and of a background
    one (kitlib.lock.background end_child). A daemon is shared: one this run's client started
    sits in this job, yet any client on the machine may be building on it, and
    TerminateJobObject would end it under that client. Windows cannot take a process out of a
    job, so the daemons are judged first (kitlib.proc.daemons stop, as Slot.stop_daemons does,
    whatever machine_lock.daemons says: one serving only this job is ended, as the kill always
    did) and the rest are ended one by one. The judgement is said and listed in rec's `daemons`
    (tell_daemons).

    No job, or a closed one: nothing is done. Nothing found, or nothing left:
    TerminateJobObject, as before. The job's processes or the daemons unreadable:
    TerminateJobObject (fail closed: the run's tree surely ends). When daemons.stop raises,
    every daemon found is "not judged" (stop may have ended some) and each one still running at
    its creation time is spared. Otherwise, in up to END_PASSES passes (_end_others), every job
    process outside the spared trees (each left daemon, at its creation time or, when that
    cannot be read, while not surely gone, with its descendants, read again each pass so a
    worker it starts is spared too) is ended by terminate_verified, oldest first so a parent
    cannot start more. One still running in the job after the last pass (and END_WAIT_S more)
    is said, and runs on once the job is closed unpinned. A store.say that fails is dropped:
    what is ended and spared is unchanged. When it fails in tell_daemons, the results after it
    are not said and rec is not given this judgement's results, so a pid judged again keeps its
    earlier one there.

    Each step catches Exception, so what can go up is a BaseException that is not an Exception,
    such as the KeyboardInterrupt of a second Ctrl+C. It goes up wherever it lands, the job then
    partly ended; the caller ends the whole job, or judges it again."""
    if not jobset.is_open(job):
        return False
    try:
        found = daemons.find(job.pids())
    except Exception as e:                  # noqa: BLE001 - cannot tell: the kill goes ahead
        try:
            store.say("WARNING: run %s: build daemons not checked before its tree was "
                      "ended: %s" % (rid, e))
        except Exception:                   # noqa: BLE001 - not said: the kill goes ahead
            pass
        found = []
    if not found:
        job.terminate(1)
        return False
    try:
        done = daemons.stop(found, job.contains)
    except Exception as e:                  # noqa: BLE001 - cannot judge: spare what still runs
        # stop may have ended some before it raised: none is said to be left running.
        done = [(pid, label, "%s(%s: %s)" % (NOT_JUDGED, type(e).__name__, e))
                for pid, _ct, label in found]
    try:
        tell_daemons(store, rid, rec, done)
    except Exception:                       # noqa: BLE001 - not said: the kill goes on as judged
        pass
    left = {pid for pid, _label, res in done if res.startswith(("left: ", NOT_JUDGED))}
    kept = [(pid, ct) for pid, ct, _label in found if pid in left]
    if not kept:
        job.terminate(1)
        return False
    try:
        rest = _end_others(job, kept)
    except Exception as e:                  # noqa: BLE001 - the job cannot be read: end it all
        job.terminate(1)
        try:
            store.say("WARNING: run %s: its job could not be read while its tree was "
                      "ended (%s); the whole job was ended, the daemons it had left "
                      "among them" % (rid, e))
        except Exception:                   # noqa: BLE001 - not said: the job is ended
            pass
        return False
    for pid in rest:
        try:
            store.say("WARNING: run %s: pid %d in its job could not be ended; it runs "
                      "on, unpinned" % (rid, pid))
        except Exception:                   # noqa: BLE001 - not said: the spared stay spared
            pass
    return True


def _end_others(job, kept):
    """end_sparing's passes: the pids still in job outside the trees of `kept` [(pid, ctime)]
    after the last one. Each pass takes a stamp (filetime_now), reads the job's pids, then the
    process table, and ends only targets created before the stamp, each by terminate_verified
    at the creation time it read. Raises OSError when the job or the process table cannot be
    read."""
    own = os.getpid()
    for n in range(END_PASSES + 1):
        # The job first, then the table: a listed process that still runs was running when
        # the table was read, so a worker a spared daemon started before the list shows in
        # the daemon's tree; one started after the list waits for the next pass.
        # The stamp before the list: a process at a listed pid created before it was alive
        # at the list, so it is the one listed (two live processes never share a pid). One
        # created at or after it may hold a reused pid: it is not ended in this pass (a new
        # job process is seen again by the next).
        stamp = filetime_now()
        pids = job.pids()
        snap = process.snapshot()
        spared = {own}
        for pid, ct in kept:
            # Its creation time unreadable (it cannot be opened) yet not surely gone: spared,
            # erring toward the client building on it.
            now = process.proc_ctime(pid)
            if now == ct or (now is None and process.alive(pid, ct)):
                spared.add(pid)
                spared.update(p for p, _c in process.descendants(pid, ct, snap))
        targets = [p for p in pids if p not in spared]
        if not targets:
            return []
        if n == END_PASSES:
            return _running(job, targets)
        aged = []
        for pid in targets:
            ct = process.proc_ctime(pid)
            if ct is not None and ct < stamp:
                aged.append((ct, pid))
        for ct, pid in sorted(aged):        # oldest first: a parent before its children
            process.terminate_verified(pid, ct)
        time.sleep(END_PASS_S)
    return []


def _running(job, targets):
    """The last pass's targets that still run in job, after waiting up to END_WAIT_S for them
    to exit: a pid the job listed may name a process that has exited since (one the last pass
    ended among them), and that one is not said to run on. A pid that cannot be opened counts
    as running (process.alive), as one Windows cannot place in or out of the job does."""
    end = time.monotonic() + END_WAIT_S
    while True:
        targets = [p for p in targets
                   if process.alive(p, None) and job.contains(p) != "out"]
        if not targets or time.monotonic() >= end:
            return targets
        time.sleep(END_PASS_S)


# ------------------------------------------------------------------ book, Slot, launcher
class Launcher:
    """What slot() yields: starts commands in the run's job. start(cmd, **popen_kw) ->
    Popen (env defaults to the run's: MACHINE_LOCK_RUN, the cpuset, the gpus); run(cmd, **kw) ->
    its exit code, waited for with the run's use sampled and a stop honoured; wait(p) -> exit
    code; pids(); usage() -> the run's use so far; kill() ends every process in the job but a
    build daemon Slot.end_tree spares (one another job's client uses or may use), with its tree.
    id, cores (cpu slots), gpus (gpu slots), mask (the global thread mask, or None) and env read
    the run."""

    def __init__(self, slot):
        self._slot = slot

    id = property(lambda self: self._slot.id)
    cores = property(lambda self: list(self._slot.cores))
    gpus = property(lambda self: list(self._slot.gpus))
    mask = property(lambda self: self._slot.job.effective if self._slot.job else None)
    env = property(lambda self: dict(self._slot.env))

    def start(self, cmd, **kw):
        return self._slot.start(cmd, **kw)

    def wait(self, p):
        return self._slot.wait(p)[0]

    def run(self, cmd, **kw):
        return self.wait(self.start(cmd, **kw))

    def pids(self):
        return self._slot.job.pids() if jobset.is_open(self._slot.job) else []

    def usage(self):
        return self._slot.usage_fields()

    def kill(self):
        if self._slot.job is not None:
            self._slot.end_tree()


def note_jobns(run, then=None):
    """A before_resume for a run's job start (Linux): when the job gives its command a pid
    namespace of its own (p.pidns, contain's W1), the run's record lists it in `jobns` before
    the command runs, so a run nested in it is seen to be (nest_check). Then then(p), if given.
    A record that cannot be written is left: the nested run then takes its own lock."""
    def before(p):
        ns = getattr(p, "pidns", None)
        if ns is not None and ns not in run.rec.setdefault("jobns", []):
            run.rec["jobns"].append(ns)
            with contextlib.suppress(OSError):
                run.save()
        if then is not None:
            then(p)
    return before


class Slot:
    """A held run (book()). start/wait/release, or the launcher; a context manager that
    releases (how "error" and the tree ended when the block raised, "interrupted" on Ctrl+C).
    A block that raised names its exception on the ended record (exception_fields)."""

    def __init__(self, store, run, L, own, parent, mutex, env, on_release_cmd, kind_entry,
                 shares, log=None, gate=False):
        self.store, self.run, self.L, self.own, self.parent = store, run, L, own, parent
        self.mutex, self.on_release_cmd, self.log, self.gate = mutex, on_release_cmd, log, gate
        self.nested = run is None
        self.id = parent["id"] if self.nested else run.id
        self.cores = list(parent["cores"] if self.nested else run.rec["cores"])
        self.gpus = list(parent["gpus"] if self.nested else run.rec["gpus"])
        self.code, self.how, self.released, self.started, self.ended = 0, "done", False, False, None
        # end_tree: the tree was ended since the last start; it spared a shared daemon's tree.
        self.tree_ended, self.spared = False, False
        self.warn_s = kind_entry["warn_min"] * 60 * store.settings.warn_scale
        self.kind = kind_entry
        self.usage = run.usage if run else Usage(L["cpu_slot_threads"], store.settings.usage_sample_s)
        self.env = dict(env)
        self.env.pop(ORIGIN_ENV, None)
        # Only the command start(give_back=True) starts may give slots back (kitlib.lock.giveback).
        self.env.pop(gbk.GIVE_ENV, None)
        # ack_due: a give-back's ack (the record's given_back) is still to be written.
        self.offer, self.offer_bad, self.given, self.ack_due = None, 0, [], False
        # The window monitor (start, watch_stop) and what it saw: None until it stops.
        self.watch, self.windows, self.windows_error, self.windows_gone = None, None, None, 0
        mask = None
        if not self.nested:
            self.env[NEST_ENV] = "%s:%d:%d" % (run.id, run.rec["pid"], run.rec["ctime"])
            if own["gpu"] and shares.get("gpu") == "training" and not self.env.get(XLA_FRACTION_ENV):
                self.env[XLA_FRACTION_ENV] = XLA_DEFAULT_FRACTION
            if own["cpu"]:
                mask = lay.cores_mask(L, run.rec["cores"])
        try:
            # A run with no cores of its own stays on its parent's: the job nests in the parent's.
            # Where a job starts one command (Linux), a job per command (kitlib.lock.jobset).
            self.job = jobset.JobSet(mask, make=Job) if jobset.one_command() else Job(mask)
        except JobError as e:
            raise LockError(1, "could not start the command: %s" % e)
        if own["cpu"]:
            export_threads(self.env, self.job)
        if own["gpu"]:
            self.env[GPUS_ENV] = ",".join(str(c) for c in lay.gpu_cards_of(L, run.rec["gpus"]))
        if not self.nested:
            with contextlib.suppress(OSError):
                run.save(**pin_fields(self.job))
        self.launcher = Launcher(self)

    def held_text(self):
        """'test holds cpu slots 0-1 for repo/slug (test, run r-...); the command
        runs on cores 0-3'"""
        if self.nested:
            return "nested: run %s already holds what it needs; the command runs in its job" % self.id
        r = self.run.rec
        return "%s holds %s for %s/%s (%s, run %s); the command runs on %s%s" % (
            r["kind"], " + ".join(lay.slots_text(p, r[PICK[p]]) for p in POOLS if r[PICK[p]]),
            r["repo"], r["slug"], r["kind"], self.id,
            cores_text(self.job.effective) if self.own["cpu"] else
            "the cores of run %s" % self.parent["id"], "; " + self.job.note if self.job.note else "")

    def note_tree(self, tree):
        """Records `tree`, the git tree hash this held run now tests (book's `tree`; a landing's
        merge, run in the slot the landing holds), on its record and so its history line. A
        nested slot has no record of its own and records nothing; nor does an empty tree. A
        failed write is ignored: the record is a note, never what holds the slots."""
        if self.nested or not tree:
            return
        with contextlib.suppress(OSError):
            self.run.save(tree=tree)

    def start(self, cmd, give_back=False, **kw):
        """Popen(cmd) in the run's job; OSError when it cannot start, AliasStubError (one) when
        its program is a WindowsApps App Execution Alias, which would start it outside the job
        (alias_check). give_back: cmd may give back cpu slots it no longer needs while it runs
        (kitlib.lock.giveback); only a run that holds its own cpu slots offers it, and wait()
        serves it."""
        if self.released:
            raise LockError(1, "run %s has released its slots" % self.id)
        alias_check(cmd)
        if give_back and not self.nested and self.own["cpu"] and self.job.mask is not None:
            self.offer, self.offer_bad = gbk.request_path(self.store, self.id), 0
            remove(self.offer)
            kw["env"] = dict(kw.get("env") or self.env, **{gbk.GIVE_ENV: self.offer})
        if self.run is not None:
            for k, v in self.run.popen_kw(self.env).items():
                kw.setdefault(k, v)
        else:
            kw.setdefault("env", self.env)
        if (WATCH_WINDOWS and kitlib.platform.NAME == "windows" and self.watch is None
                and self.windows is None):
            self.watch_start()
        if self.run is not None and kitlib.platform.NAME == "linux":
            kw["before_resume"] = note_jobns(self.run, kw.get("before_resume"))
        pin = self.job.pin
        p = self.job.start(docker_cpu_env(cmd, bool(self.gpus)), **kw)
        self.started, self.tree_ended = True, False
        if self.job.pin != pin and self.run is not None:
            with contextlib.suppress(OSError):
                self.run.save(**pin_fields(self.job))
        return p

    def watch_start(self):
        """Starts the window monitor. Never raises: a monitor that cannot start is windows_error
        (and the record's `windows_error`), and the run goes on unwatched. self.watch is set before
        m.start(), which can wait up to winwatch.START_WAIT_S: a Ctrl+C inside it leaves the monitor
        for the release's watch_stop to stop, so its thread and hooks never outlive the run."""
        m = None
        try:
            m = winwatch.Monitor(ours=self.window_owner)
            self.watch = m
            m.start()
        except Exception as e:      # noqa: BLE001 - the monitor never fails a run
            self.watch = None
            with contextlib.suppress(Exception):
                if m is not None:
                    m.stop()
            self.windows, self.windows_error = [], "%s: %s" % (type(e).__name__, e)
            if self.run is not None:
                self.run.rec["windows_error"] = self.windows_error

    def window_owner(self, pid):
        """Is process pid this run's, for the window monitor: True (in the run's job, or a job
        nested in it), False (not in it) or None (gone, or Windows will not say)."""
        job = self.job
        return {"in": True, "out": False}.get(job.contains(pid) if job is not None else None)

    def watch_stop(self):
        """Stops the window monitor, once, and keeps what it saw: self.windows (a list of
        winwatch.Window), windows_error and windows_gone, and in the run's record `windows` (each
        Window.as_dict(), when any was seen, at most winwatch.RECORD_CAP of them, with
        `windows_truncated` the count left out) and `windows_error`. Returns self.windows: None
        when nothing was watched. Never raises an Exception: a monitor that fails to stop is its
        error. self.watch is cleared only once m.stop() has returned or raised an Exception, so a
        Ctrl+C inside it leaves the monitor for the release's second call to stop."""
        m = self.watch
        if m is None:
            return self.windows
        try:
            windows = list(m.stop())
        except Exception as e:      # noqa: BLE001 - the monitor never fails a run
            windows = []
            m.error = m.error or "%s: %s" % (type(e).__name__, e)
        self.watch = None
        self.windows, self.windows_error = windows, m.error
        self.windows_gone = getattr(m, "gone", 0)
        if self.run is not None:
            if windows:
                kept, cut = winwatch.records(windows)
                self.run.rec["windows"] = kept
                if cut:
                    self.run.rec["windows_truncated"] = cut
            if m.error:
                self.run.rec["windows_error"] = m.error
        return self.windows

    def window_lines(self, fails=False):
        """The window report (winwatch.report_lines) once the monitor has stopped; [] when
        nothing was watched. fails: the caller's verdict counts a window."""
        if self.windows is None:
            return []
        return winwatch.report_lines(self.windows, self.windows_error, self.windows_gone, fails)

    def stop_asked(self):
        return self.run is not None and self.run.stop_asked()

    def wait(self, p):
        """(exit code, how) of child p: its use sampled every settings.child_poll_s into the
        record; a stop ends the job's tree ("cancelled", 1; end_tree); past the kind's warn_min a
        warning, never a kill. A stop then names each process descended from p that is outside
        the job and still alive (escaped, escape_lines): it is not ended."""
        t0, warned = time.monotonic(), False
        while True:
            try:
                code, how = child_code(p.wait(self.store.settings.child_poll_s)), "done"
                break
            except subprocess.TimeoutExpired:
                pass
            if self.usage.sample(self.job) and self.run is not None:
                with contextlib.suppress(OSError):
                    self.run.save(**self.usage.fields())
            if self.offer is not None:
                self.serve_give_back()
            if self.stop_asked():
                req, _there = read_record(self.store.path("active", self.id + ".stop"))
                if isinstance(req, dict) and req.get("by") == stale.BY and req.get("why"):
                    self.store.say("run %s was ended by the lock: %s; its process tree is ended"
                                   % (self.id, req["why"]))
                else:
                    self.store.say("run %s was cancelled by stop; its process tree is ended"
                                   % self.id)
                found = escaped(p.pid, self.job.contains)   # read while their parents live
                self.end_tree()
                try:                        # the command itself may be a daemon it spared
                    p.wait(10 if self.spared else None)
                except subprocess.TimeoutExpired:
                    pass
                for line in escape_lines(self.id, found):
                    self.store.say(line)
                code, how = 1, "cancelled"
                break
            if not warned and time.monotonic() - t0 > self.warn_s:
                self.store.say("WARNING: run %s (%s) has held its lock past %d min, the guide for "
                               "its kind; it is not stopped. Check it is still making progress."
                               % (self.id, self.run.rec["kind"] if self.run else "nested",
                                  self.warn_s // 60))
                warned = True
        self.code, self.how = code, how
        if self.offer is not None:
            remove(self.offer)
            self.offer = None
        return code, how

    def serve_give_back(self):
        """Does the offered command's give-back request, if one waits (kitlib.lock.giveback):
        narrow the job, then the record, then the queue entry, then the ack; else refuse,
        changing nothing. First it writes an ack an earlier poll could not.
        Returns why it was refused, or None. Never raises: a failure refuses."""
        req = {}
        if self.ack_due:
            # A give-back is done but its ack write failed: the record in memory carries it.
            with contextlib.suppress(OSError):
                self.run.save()
                self.ack_due = False
        try:
            req = gbk.read_request(self.offer) if self.offer else None
            if req is None:
                return None
            if "bad" in req and "id" not in req and self.offer_bad < 2:
                self.offer_bad += 1         # a file read mid-replace: it looks again next poll
                return None
            self.offer_bad = 0
            remove(self.offer)
            try:
                pids = self.job.pids()
            except OSError:
                pids = []
            why = gbk.check(req, self.cores, self.nested, bool(self.own["cpu"]), self.job.pin,
                            pids, process.alive)
            if why is None:
                why = self._give_back(req)
        except Exception as e:              # noqa: BLE001 - the run goes on holding every slot
            req, why = req if isinstance(req, dict) else {}, "%s: %s" % (type(e).__name__, e)
        if why is not None:
            self.store.say("run %s: give back refused: %s" % (self.id, why))
            if req.get("id") and self.run is not None:
                with contextlib.suppress(OSError):
                    self.run.save(give_back_refused={"id": req["id"], "why": why, "at": iso()})
        return why

    def _give_back(self, req):
        keep = [i for i in self.cores if i not in req["cpu"]]
        old = self.job.mask
        if not self.job.narrow(lay.cores_mask(self.L, keep)):
            return "the run's job could not be narrowed to %s" % lay.slots_text("cpu", keep)
        given = sorted(set(self.given) | set(req["cpu"]))
        env = dict(self.env)
        export_threads(env, self.job)
        ack = {"id": req["id"], "cpu": given, "cores": keep, "cpumask": env[CPUMASK_ENV],
               "cpuset": env[CPUSET_ENV], "at": iso()}
        try:
            # False: the slots are given back but the ack is not on disk yet; it stays in the
            # record in memory and serve_give_back writes it again on the next poll.
            self.ack_due = not gbk.shrink(self.store, self.run, keep, pin_fields(self.job), ack)
        except Exception as e:              # noqa: BLE001 - the queue still holds every slot
            # Widening the job's pin moves its processes back onto every slot (Job.repin).
            if self.job.repin(old):
                return ("the lock store could not be written (%s); the run keeps every slot "
                        "booked and its job is pinned to them again" % e)
            return ("the lock store could not be written (%s); the run keeps every slot booked, "
                    "but its job stays narrowed to %s (the pin could not be widened again)"
                    % (e, cores_text(self.job.effective)))
        self.cores, self.given, self.env = keep, given, env
        self.store.say("run %s gave back %s; it holds %s; its commands run on %s" % (
            self.id, lay.slots_text("cpu", req["cpu"]), lay.slots_text("cpu", keep),
            cores_text(self.job.effective)))
        return None

    def usage_fields(self):
        self.usage.sample(self.job)
        return self.usage.fields()

    def stop_daemons(self):
        """The build daemons (kitlib.proc.daemons) still in the run's job are stopped with their
        children when the machine config's machine_lock.daemons says so (kitlib.lock.site
        stops_daemons: by default only after a gate run); each is said and listed in the record's
        `daemons`. A daemon that serves a client outside the run's job (another run's build is
        using it), or whose clients cannot be told, is left running and its result says why
        (kitlib.proc.daemons stop). A nested slot leaves them to the run it is nested in, unless
        it is a gate: then it stops those in its own job and records none. Never raises: what it
        cannot read or stop runs on, unpinned, and a store.say that fails is dropped (the record
        is then not given these results)."""
        try:
            if not site.stops_daemons(self.store, self.gate):
                return
            found = daemons.find(self.job.pids())
            if not found:
                return
            done = daemons.stop(found, self.job.contains)
        except Exception as e:
            try:
                self.store.say("WARNING: run %s: build daemons not checked: %s" % (self.id, e))
            except Exception:               # noqa: BLE001 - not said: the release goes on
                pass
            return
        try:
            self.tell_daemons(done)
        except Exception:                   # noqa: BLE001 - not said: the release goes on
            pass

    def tell_daemons(self, done):
        """tell_daemons for this run: its results said, and listed in its record (none for a
        nested slot)."""
        tell_daemons(self.store, self.id, self.run.rec if self.run is not None else None, done)

    def end_tree(self):
        """The kill path (a cancel, Ctrl+C, an error, Launcher.kill): end_sparing on the run's
        job, which ends every process in it but a build daemon another job's client uses or may
        use, with that daemon's tree, and lists its judgement in the record's `daemons`. Sets
        self.spared to what end_sparing returns; release then closes the job without killing it.
        A second end_tree (the release's, after a Ctrl+C) adds its results to the record's
        `daemons` (tell_daemons), so what the first one stopped stays listed.

        Catches BaseException, so a KeyboardInterrupt (a second Ctrl+C) or any other BaseException
        that is not an Exception is caught, tree_ended and spared are cleared, and it is raised
        again. The release that follows then ends the tree again, and a release whose own
        end_tree it interrupts ends the whole job."""
        try:
            self._end_tree()
        except BaseException:               # a second Ctrl+C: release ends the tree again
            self.tree_ended = self.spared = False
            raise

    def _end_tree(self):
        self.tree_ended, self.spared = True, False
        self.spared = end_sparing(self.store, self.id, self.job,
                                  self.run.rec if self.run is not None else None)

    def release(self, code=None, how=None):
        """Ends the run: its job (the tree killed when interrupted, cancelled or an error, but for
        a build daemon end_tree spares, which runs on with its tree, unpinned; else its
        build daemons stopped when it is a gate (stop_daemons) and what else it left runs on,
        unpinned), its --on-release command, then its queue entry and record (settle). Returns
        the ended record (None for a nested slot). Once."""
        if self.released:
            return self.ended
        self.released = True
        code = self.code if code is None else code
        how = self.how if how is None else how
        try:
            if self.job is not None:
                if how == "interrupted" and self.job.p is not None:
                    try:                    # Ctrl+C reached the command too; give it 10 s
                        self.job.p.wait(10)
                    except BaseException:
                        pass
                if self.started:
                    self.usage.sample(self.job)
                    if self.run is not None:
                        self.run.rec.update(self.usage.fields())
                if self.started and (self.run is not None or self.gate) and how not in KILL_HOW:
                    self.stop_daemons()
                if how in KILL_HOW and not self.tree_ended:
                    try:
                        self.end_tree()
                    except BaseException:   # Ctrl+C again mid-kill: the whole job, as before
                        self.job.end(kill=True)
                        raise
                self.watch_stop()           # while the job can still say whose a window is
                # A spared daemon's tree is still in the job: closing it with kill-on-close set,
                # or TerminateJobObject, would end it. What runs on leaves the job where it can
                # (Linux: move_leftovers).
                kill = how in KILL_HOW and not self.spared
                if not kill:
                    move_leftovers(self.store, self.id, self.job)
                self.job.end(kill=kill)
                if self.started:
                    on_release(self.store, self.on_release_cmd, self.env, how, self.run)
        finally:
            self.watch_stop()
            if self.run is not None:
                self.ended = settle(self.store, self.run, code, how, self.mutex)
        return self.ended

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is KeyboardInterrupt:
            self.release(130, "interrupted")
        elif et is not None:
            if self.run is not None and not self.released:
                self.run.rec.update(exception_fields(et, ev))
            self.release(1, "error")
        else:
            self.release()


def exception_fields(et, ev):
    """What a run's ended record says of the exception that ended its block `error` (how stays
    error): `exception`, its type (a builtin's bare name, else module.qualname), and
    `exception_text`, str(ev) cut to 200 characters and escaped to ASCII."""
    mod = getattr(et, "__module__", None)
    name = getattr(et, "__qualname__", None) or getattr(et, "__name__", "?")
    try:
        text = str(ev)[:200]
    except Exception:                       # noqa: BLE001 - a __str__ that raises
        text = "?"
    return {"exception": name if mod in (None, "builtins") else "%s.%s" % (mod, name),
            "exception_text": text.encode("ascii", "backslashreplace").decode("ascii")}


def _relayout(store, L):
    """The layout in force now, read under the queue mutex: a layout adopted between book()'s
    first look and its record being saved is seen here; after this, none can be (a live run
    blocks adoption, kitlib.lock.layout.in_force)."""
    with store.lock("queue", store.settings.queue_lock_s):
        cur = store.read("layout.json")
    return cur if lay.valid_layout(cur) and cur["key"] != L["key"] else L


def within_of(L, within, kind, need, own, parent, machine):
    """(the thread list as text, [cpu slot]) of book's within on layout L: the slots whose threads
    all lie in it. machine: the thread ids this machine has. A thread it names that the owner's
    division leaves out of every slot is no error; it is just not used. LockError 2 when it is not
    a thread list, names a thread the machine does not have, holds no whole slot, holds fewer
    slots than the run takes itself (own), or the run is nested and inherits its parent's cores
    while they are not all inside it."""
    try:
        if isinstance(within, str):
            ts = lay.parse_threads(within)
        elif (isinstance(within, (list, tuple, set, frozenset, range)) and within
              and all(type(t) is int and 0 <= t <= lay.MAX_THREAD for t in within)):
            ts = sorted(set(within))
        else:
            raise ValueError("a thread list is like 8-15,20 (or a list of thread ids), not %r"
                             % (within,))
    except ValueError as e:
        raise LockError(EXIT_USAGE, "usage: --within: %s. Exit 2." % e)
    text = cpu.mask_text(cpu.bits_mask(ts))
    stray = [t for t in ts if t not in machine]
    if stray:
        raise LockError(EXIT_USAGE, "usage: --within %s: thread%s %s %s not threads of this "
                                    "machine (it has threads %s). Exit 2." % (
                                        text, "" if len(stray) == 1 else "s",
                                        cpu.mask_text(cpu.bits_mask(stray)),
                                        "is" if len(stray) == 1 else "are",
                                        cpu.mask_text(cpu.bits_mask(machine))))
    slots = lay.within_slots(L, ts)
    if not slots:
        raise LockError(EXIT_USAGE, "usage: --within %s holds no whole cpu slot (a slot is %d "
                                    "threads, all of them inside). Exit 2."
                        % (text, L["cpu_slot_threads"]))
    if own["cpu"] > len(slots):
        raise LockError(EXIT_USAGE, "usage: --within %s holds %d whole cpu slot%s (%s), but a %s "
                                    "run takes %d; ask for fewer with --cores %d or less. Exit 2."
                        % (text, len(slots), "" if len(slots) == 1 else "s",
                           lay.slots_text("cpu", slots), kind, own["cpu"], len(slots)))
    if parent and need["cpu"] and not own["cpu"]:
        pc = [i for i in parent.get("cores") or [] if type(i) is int]
        if not pc or not set(pc) <= set(slots):
            raise LockError(EXIT_USAGE, "usage: nested run refused: it runs on the cores of the "
                                        "run it is nested in (run %s, %s), and they are not all "
                                        "inside --within %s; start it outside that run. Exit 2."
                            % (parent["id"], lay.slots_text("cpu", pc), text))
    return text, slots


def check_resource(resource):
    """book()'s resource rule: None, or a non-empty string; anything else is LockError usage
    (exit 2). The CLI calls it before a detach, so a refusal is never left in a detached log."""
    if resource is not None and not (isinstance(resource, str) and resource):
        raise LockError(EXIT_USAGE, "usage: a resource is a non-empty string, not %r" % (resource,))


def book(kind, repo, slug, cores=None, hold=None, gpu_share=None, wait_s=None, no_wait=False,
         expect_s=None, note=None, on_release_cmd=None, cmd=None, store=None, kinds_table=None,
         env=None, rid=None, detached=False, log=None, gate=False, resource=None, priority=None,
         within=None, tree=None, branch=None, checkout=None):
    """Waits for a run of kind `kind` for repo/slug to hold its slots and returns its Slot.
    Raises LockError (the CLI's exit code) when it does not. cores: fewer cpu slots than the
    kind's size, 1..that size, never more; the kind's size is its whole number of cores
    (at most agent_cap), or an alias's (heavy is every slot inside an owner whole hold); a table
    with a size past agent_cap fails closed (exit 10). hold: an owner whole
    hold or agent hold to run inside. gpu_share "training": one gpu slot on a shareable
    kind. wait_s: the longest wait (None: no limit); no_wait: now or never. expect_s: how long it
    expects to hold (default: its history's median, else its own hold's end, else warn_min).
    env: the environment its commands get (default os.environ); rid, detached and log are the
    CLI's (a detached run's id and log file). gate: a gate run (kitlib.gate.runner), recorded as
    `gate`; by default only a gate's leftover build daemons are stopped (Slot.stop_daemons).
    resource: an exclusive resource (a non-empty string, e.g. "landing:<git common dir>") the run
    holds for its whole life: it does not start while another run holding it is alive (the queue
    entry and record carry it). A nested run naming one its parent does not hold joins the queue
    for it even when the parent covers all it needs. priority "land": its entry plans ahead of
    every ordinary waiting run (kitlib.lock.queue.plan), for library callers only. The CLI takes
    resource (`machine_lock.py run --resource`), not priority; the MCP server takes neither.
    within: the threads the run must stay inside (the CLI's --within: a Linux cpuset list such as '8-15', or global thread ids), e.g. a container's cpuset; it takes
    only cpu slots whose threads all lie inside (within_of), and waits for those while others are
    free. The record carries `within` (the list) and `within_slots`; its queue entry `within`.
    tree: the git tree hash the run tests (a gate's: kitlib.gate.runner.gated), recorded as `tree`
    and kept in its history line, so a gate's run is matched to the exact tree it tested;
    Slot.note_tree records it on a run already held. branch: the git branch the run is for (a
    branch gate's, which kitlib.gate.budget counts its runs by), recorded as `branch` and kept in
    its history line. checkout: the folder the run is booked from (the CLI's current directory):
    a kind that is neither a kit kind nor a kit alias is looked up in the "kinds" section of its
    repository's main checkout `.kit.json` (kitlib.lock.kinds.with_repo); None: kit kinds only."""
    store = store or Store()
    env = dict(os.environ if env is None else env)
    try:
        table = kinds_table or kinds_mod.load(store.kinds_path)
    except kinds_mod.KindsError as e:
        raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % e)
    try:
        kind = kinds_mod.resolve(kind, table, store.kinds_path)
        table = kinds_mod.with_repo(table, kind, checkout, store.kinds_path)
    except kinds_mod.KindsError as e:
        raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % e)
    if kind not in table:
        raise LockError(EXIT_USAGE, "usage: unknown kind %r (known: %s%s)" % (
            kind, ", ".join(sorted(table)), "" if checkout is None else
            "; a repository's own kinds are read from the \"kinds\" section of the .kit.json "
            "in the main checkout of %s" % checkout))
    k = table[kind]
    if not repo or not slug:
        raise LockError(EXIT_USAGE, "usage: a run names its repo and slug")
    if gpu_share not in (None, "training"):
        raise LockError(EXIT_USAGE, "usage: --gpu-share is training or nothing")
    if gpu_share and not k["share"]:
        raise LockError(EXIT_USAGE, "usage: --gpu-share training needs a kind whose gpu is shared")
    if expect_s is not None and not (isinstance(expect_s, (int, float)) and expect_s > 0):
        raise LockError(EXIT_USAGE, "usage: --expect must be a positive duration")
    if priority is not None and priority not in qm.PRIORITIES:
        raise LockError(EXIT_USAGE, "usage: unknown priority %r (known: %s)"
                        % (priority, ", ".join(qm.PRIORITIES)))
    check_resource(resource)
    check_run_id(rid)
    L = lay.in_force(store)["layout"]
    try:                            # a size the layout cannot give fails closed
        kinds_mod.check_cap(table, L["agent_cap"])
    except kinds_mod.KindsError as e:
        raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % e)
    whole = False
    if hold:
        try:
            h = hl.find(store, L, hold)
        except OSError as e:        # HoldsUnreadable among them: fail closed
            raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % e)
        whole = bool(h and h["owner"] == "owner" and h["scope"] == "whole")

    def sized(L):
        try:                        # a table that skipped kinds.load fails closed
            top = lay.kind_cores(L, k, whole)
        except kinds_mod.KindsError as e:
            raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % e)
        if cores is not None:
            if type(cores) is not int or not 1 <= cores <= top:
                raise LockError(EXIT_USAGE, "usage: --cores %s: a %s run takes 1 to %d cpu slots, "
                                            "its kind's size; a run asks for fewer, never more"
                                            % (cores, kind, top))
        return lay.need_for(L, k, gpu_share, cores, whole)

    need = sized(L)
    parent = None
    if env.get(NEST_ENV):
        parent, why = nest_check(store, env[NEST_ENV])
        if parent is None:
            store.say("%s ignored (%s); this run takes its own lock" % (NEST_ENV, why))
    own, has, why = nest_need(parent, kind, need)
    if own is None:
        raise LockError(EXIT_USAGE, "usage: nested run refused: %s. Exit 2." % why)
    wtext = wslots = None
    if within is not None:
        if hold:
            raise LockError(EXIT_USAGE, "usage: --within and --hold do not go together: a run "
                                        "inside a hold takes the hold's slots. Exit 2.")
        machine = set(lay.hardware(store)["threads"])
        wtext, wslots = within_of(L, within, kind, need, own, parent, machine)
    shares = hl.shares_for(k, gpu_share)
    if parent and not any(own.values()) and (resource is None or parent.get("resource") == resource):
        return Slot(store, None, L, own, parent, None, env, on_release_cmd, k, shares, log, gate)
    if own["gpu"] and bg_ancestor(env, store):
        raise LockError(EXIT_USAGE, "usage: a gpu lock inside a background run would pause that "
                                    "run; refused")
    # The id first, its run mutex held: an id another run has, live or ended, is never written
    # over, and a refusal here writes nothing (claim_id).
    rid, mutex = claim_id(store, rid)
    try:
        run = Run(store, L, repo, slug, kind, note, cmd, "waiting", rid, detached, env, log)
        hist = None if expect_s is not None else store.history_expect(
            repo, kind, store.run_mode(priority, branch, gate))
        run.rec.update(expect_s=expect_s if expect_s is not None else hist if hist is not None
                       else k["warn_min"] * 60,
                       expect_from="--expect" if expect_s is not None else "history"
                       if hist is not None else "guide",
                       warn_s=k["warn_min"] * 60 * store.settings.warn_scale,
                       need=own, has={p: n for p, n in has.items() if n}, hold=hold,
                       parent=parent["id"] if parent else None, gpu_share=gpu_share,
                       **({"gate": True} if gate else {}),
                       **({"tree": tree} if tree else {}),
                       **({"branch": branch} if branch else {}),
                       **({"resource": resource} if resource else {}),
                       **({"priority": priority} if priority else {}),
                       **({"within": wtext, "within_slots": {"cpu": wslots}} if wtext else {}))
    except BaseException:
        mutex.release()
        raise
    done, code, how = False, EXIT_REQUIRE, "not acquired"
    try:
        prefer = None
        if hold:
            # The claim first, so the run plans around every hold but its own.
            got_h, why = hl.claim(store, L, run.rec, hold, shares)
            if got_h is None:
                raise LockError(EXIT_HOLD, "NOT ACQUIRED: %s; exit 12" % why)
            run.rec.update(hold_owner=got_h["owner"], hold_scope=got_h["scope"])
            if expect_s is None:
                run.rec.update(expect_s=max(60, int((got_h["_end"] - utc_now()).total_seconds())),
                               expect_from="hold")
            if got_h["owner"] == "agent":
                try:
                    lost, to = hl.lost(got_h, [x for x in hl.records(store, L) if hl.strong(x)])
                except OSError as e:
                    raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % e)
                prefer = {p: [i for i in got_h["slots"].get(p, []) if i not in lost.get(p, [])]
                          for p in POOLS}
                store.say("run %s claims %s%s" % (run.id, hl.text(got_h), (
                    "; it lost %s to %s, which always win; it plans around them as any run "
                    "does" % (hl.slots_text(lost), ", ".join(to))) if lost else ""))
            else:
                store.say("run %s runs inside %s" % (run.id, hl.text(got_h)))
        try:
            run.save(inherited=[p for p in POOLS if need[p] and not own[p]], shares=shares)
            L2 = _relayout(store, L)
        except OSError as e:
            raise LockError(EXIT_REQUIRE, "require-lock: the lock store %s could not be written "
                                          "(%s); the command was not run" % (store.root, e))
        if L2 is not L:
            L, need = L2, sized(L2)
            own, has, why = nest_need(parent, kind, need)
            if own is None:
                raise LockError(EXIT_USAGE, "usage: nested run refused: %s. Exit 2." % why)
            if wtext:
                wtext, wslots = within_of(L, within, kind, need, own, parent, machine)
                run.save(within=wtext, within_slots={"cpu": wslots})
            run.usage = Usage(L["cpu_slot_threads"], store.settings.usage_sample_s)
            run.save(need=own, has={p: n for p, n in has.items() if n}, layout=lay.slot_key(L))
        acquire(store, run, L, own, wait_s, no_wait, prefer, {"cpu": wslots} if wtext else None)
        if run.rec.get("hold_owner") == "agent":
            hl.claimed(store, run.id, hold)
        how = "not started"
        slot = Slot(store, run, L, own, parent, mutex, env, on_release_cmd, k, shares, log, gate)
        if parent is None:
            notice.started(store, run, kind, k, L)
        done = True
        return slot
    except LockError as e:
        code = e.code
        raise
    except KeyboardInterrupt:
        code, how = 130, "interrupted"
        raise
    except BaseException:
        code = 1
        raise
    finally:
        if not done:
            settle(store, run, code, how, mutex)


@contextlib.contextmanager
def slot(kind, repo, slug, **kw):
    """with slot("test", repo, slug) as launch: launch.run([...]) -- book() as a context
    manager yielding the Launcher; released on leaving the block."""
    s = book(kind, repo, slug, **kw)
    with s:
        yield s.launcher


NO_CWD_ENV = "NoDefaultCurrentDirectoryInExePath"


def bare_hint(cmd, err):
    """The text that follows a failed start of a list command whose first item is a bare name
    (no folder part) that is a file in this process's folder while NoDefaultCurrentDirectoryInExePath
    is set: Windows then does not look in the current folder for a bare name (agent hosts set it),
    so the hint names the folder and the .\\<name> form that runs. "" in every other case,
    including a current folder that cannot be read (removed under us)."""
    if not isinstance(err, FileNotFoundError) or isinstance(cmd, str) or not cmd:
        return ""
    name = str(cmd[0])
    if not name or os.path.dirname(name) or not os.environ.get(NO_CWD_ENV):
        return ""
    try:
        here = os.getcwd()
    except OSError:
        return ""
    if not os.path.isfile(os.path.join(here, name)):
        return ""
    return ("; %s is in %s, but %s is set, so Windows does not look in the current folder for a "
            "bare name: run it as .\\%s" % (name, here, NO_CWD_ENV, name))


SCRIPT_EXTS = (".bat", ".cmd")
# What cmd.exe expands or splits even inside double quotes: a .bat or .cmd cannot take it.
CMD_UNSAFE = (("%", "`%`"), ('"', '`"`'), ("\n", "a line break"), ("\r", "a line break"))
# What cmd.exe reads as syntax outside double quotes: an argument holding one is quoted.
CMD_QUOTE = frozenset(" \t\x0b\x0c&|<>^(),;=")


class ScriptArgError(OSError):
    """A command script_command will not start: a .bat or .cmd that cannot start as it would start
    it (an argument it cannot take literally, or no cmd.exe under SystemRoot), or a WindowsApps
    App Execution Alias (AliasStubError); an OSError, so the "could not start the command" path
    reports it."""


class AliasStubError(ScriptArgError):
    """A command whose program is a WindowsApps App Execution Alias (alias_check)."""


# A python interpreter's name, as the WindowsApps aliases spell it: python, python3, python3.12,
# pythonw, pythonw3, pythonw3.12, and the py launcher's py and pyw (a trailing .exe dropped first).
PYTHON_NAME_RE = re.compile(r"^(?:python(?P<w>w?)(?:3(?:\.\d+)?)?|(?P<py>py)(?P<pyw>w?))$",
                            re.IGNORECASE)


def windows_apps_dir():
    """%LOCALAPPDATA%\\Microsoft\\WindowsApps, where the App Execution Aliases live, or None when
    LOCALAPPDATA is unset."""
    local = os.environ.get("LOCALAPPDATA")
    return os.path.join(local, "Microsoft", "WindowsApps") if local else None


def under_windows_apps(path):
    """Is path windows_apps_dir() or inside it (a package's subfolder included)?"""
    apps = windows_apps_dir()
    if not apps or not path:
        return False
    a = os.path.normcase(os.path.abspath(apps)).rstrip(os.sep)
    p = os.path.normcase(os.path.abspath(path))
    return p == a or p.startswith(a + os.sep)


def created_program(name, dirs=None):
    """The file CreateProcess starts for name (an argument list's first item), or None when it
    finds none: name itself when it has an extension, else name.exe (the only one CreateProcess
    appends). A name with a folder part is that file; a bare one is the first found in dirs
    (default: _create_process_dirs(), then this process's PATH)."""
    cand = name if os.path.splitext(name)[1] else name + ".exe"
    if os.path.dirname(cand):
        return os.path.abspath(cand) if os.path.isfile(cand) else None
    if dirs is None:
        dirs = _create_process_dirs() + _path_dirs(os.environ.get("PATH"))
    for d in dirs:
        p = os.path.join(d, cand)
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def alias_instead(cmd, found):
    """What alias_check's refusal of cmd (whose program, found, is an alias) tells the caller to do
    instead. A python name (PYTHON_NAME_RE), or found being this interpreter itself: when
    sys.executable is itself under WindowsApps (a Store python), to run the kit under a python.org
    or Python install manager interpreter, as no path here would do; else to run this
    interpreter (pythonw.exe beside it for a windowed name, when it is there): with cmd's
    arguments, or, for the py launcher, with them left for the caller to adjust (py's own flags
    are not python's). Any other name: to name the program's own path."""
    stem = os.path.basename(str(cmd[0]))
    if stem.lower().endswith(".exe"):
        stem = stem[:-4]
    m = PYTHON_NAME_RE.match(stem)
    me = os.path.abspath(sys.executable) if sys.executable else None
    if (m or (me and os.path.normcase(found) == os.path.normcase(me))) and \
            (not me or under_windows_apps(me)):
        return ("this kit itself runs under a WindowsApps python (%s): run it under a python.org or "
                "Python install manager interpreter instead, not the Store python or its alias"
                % (me or "no sys.executable"))
    if not m:
        return "name the program's own path instead, not the alias"
    exe = me
    if m.group("w") or m.group("pyw"):
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.isfile(w) else exe
    if m.group("py"):
        return ('run "%s" in place of %s instead, with the arguments adjusted for python (py\'s own '
                'flags, such as -3.12, are not python\'s)' % (exe, stem))
    rest = subprocess.list2cmdline([str(a) for a in cmd[1:]])
    return 'run "%s"%s instead' % (exe, " " + rest if rest else "")


def alias_check(cmd):
    """Raises AliasStubError when cmd (an argument list, on Windows) starts a WindowsApps App
    Execution Alias: its first item, or the file CreateProcess finds for it (created_program),
    is under windows_apps_dir(). An alias's launch runs its program outside the job of whoever
    starts it: unpinned, on every core, and alive after the job is ended. Refused, never swapped,
    even when it is this interpreter itself; the refusal says what to do instead (alias_instead).
    A bare bash is left to kitlib.proc.bash (Git Bash), and so is anything else when LOCALAPPDATA
    is unset."""
    if kitlib.platform.NAME != "windows" or not isinstance(cmd, (list, tuple)) or not cmd:
        return
    name = str(cmd[0])
    if name.strip().lower() in bash.NAMES or not windows_apps_dir():
        return
    found = created_program(name)
    if not found or not under_windows_apps(found):
        return
    raise AliasStubError("%s is the WindowsApps App Execution Alias %s: what it starts runs "
                         "outside the run's job, on every core, and outlives a stop; %s"
                         % (name, found, alias_instead(cmd, found)))


def path_alias_line():
    """`status`'s warning when the first python.exe on this process's PATH is a WindowsApps alias,
    else None (and None off Windows): a .bat, cmd or bash the run starts finds python there, and
    the alias starts it outside the run's job (alias_check sees only the run's own command)."""
    if kitlib.platform.NAME != "windows" or not windows_apps_dir():
        return None
    found = created_program("python", dirs=_path_dirs(os.environ.get("PATH")))
    if not found or not under_windows_apps(found):
        return None
    real = (os.path.dirname(os.path.abspath(sys.executable))
            if sys.executable and not under_windows_apps(sys.executable)
            else "the real interpreter's folder")
    return ("python: first on PATH is the WindowsApps alias %s; a non-python child's python "
            "escapes the run's job; put %s ahead of it on the user PATH" % (found, real))


ESCAPE_CAVEAT = ("found by the parent pid chain, which a reused pid or an exited parent can make "
                 "wrong or incomplete")


def escaped(pid, contains, snap=None):
    """[(pid, ctime, exe name)] of the live descendants of the live process pid (a run's command),
    by the parent pid chain of one Toolhelp snapshot (process.descendants), that are outside the
    run's job (contains(pid) == "out"): an App Execution Alias, for one, starts its program
    outside the job of whoever starts it. Measured 2026-10-29, cmd.exe in a job running the
    python3 alias: the alias's stub (python3.exe) and the interpreter it starts are both outside
    the job, the stub's parent pid is cmd.exe and the interpreter's is the stub, so the walk finds
    both while cmd.exe and the stub live. Inexact (ESCAPE_CAVEAT): a process whose parent has
    exited is not found, and a reused pid can be. For a report only: nothing outside the job is
    ever ended. [] off Windows and when the processes cannot be read; never raises an Exception."""
    if kitlib.platform.NAME != "windows":
        return []
    try:
        snap = process.snapshot() if snap is None else snap
        names = {p: n for p, _pp, n in snap}
        return [(c, ct, names.get(c) or "?")
                for c, ct in process.descendants(pid, process.proc_ctime(pid), snap)
                if contains(c) == "out"]
    except Exception:                       # noqa: BLE001 - a report only: the stop goes on
        return []


def escape_lines(rid, found):
    """The warnings for escaped's processes (found) still alive once run rid's tree is ended;
    [] when none is. Never raises an Exception."""
    try:
        left = [(p, exe) for p, ct, exe in found if process.alive(p, ct)]
    except Exception:                       # noqa: BLE001 - a report only
        return []
    return ["WARNING: run %s: pid %d (%s) descends from the run's command but is outside the run's "
            "job: stop did not end it and it runs on, unpinned; the lock never ends a process "
            "outside the job (%s)"
            % (rid, p, exe, ESCAPE_CAVEAT) for p, exe in left]


def comspec():
    """The cmd.exe that runs a .bat or .cmd: %SystemRoot%\\System32\\cmd.exe, never ComSpec (another
    shell would read the quoting otherwise) nor a bare cmd.exe (looked for in the current folder
    first). Raises ScriptArgError when SystemRoot is unset or holds no such file."""
    root = os.environ.get("SystemRoot")
    exe = os.path.join(root, "System32", "cmd.exe") if root else None
    if not exe or not os.path.isfile(exe):
        raise ScriptArgError("cmd.exe not found under SystemRoot")
    return exe


def _path_dirs(path):
    return [d.strip().strip('"') for d in (path or "").split(os.pathsep) if d.strip().strip('"')]


def _create_process_dirs():
    """Where CreateProcess looks for a bare name, PATH aside: this program's folder, the current
    folder (unless NoDefaultCurrentDirectoryInExePath), then the system folders."""
    root = os.environ.get("SystemRoot") or ""
    dirs = [os.path.dirname(os.path.abspath(sys.executable))]
    if not os.environ.get("NoDefaultCurrentDirectoryInExePath"):
        dirs.append(os.getcwd())
    if root:
        dirs += [os.path.join(root, "System32"), os.path.join(root, "System"), root]
    return dirs


def _bare_script(name, path):
    """The .bat or .cmd a bare name (no folder part) runs, or None (cmd as given). A name that
    ends .bat or .cmd is the file CreateProcess would start, in its order: its folders (the
    current one among them unless NoDefaultCurrentDirectoryInExePath), then this process's PATH;
    else the first in `path`'s folders. An extensionless name is never looked for in the current
    folder, and never in place of what CreateProcess starts today: one it finds as name.exe or
    name.com (its folders, this process's PATH or `path`) stays as given, so a python.bat shim
    ahead of python.exe on PATH leaves `python` alone; so does any other extension
    (CreateProcess runs the name itself). Otherwise `path`'s folders in order, each over
    PATHEXT's order; the first file there decides."""
    ext = os.path.splitext(name)[1].lower()
    if ext and ext not in SCRIPT_EXTS:
        return None
    if ext:
        for d in _create_process_dirs() + _path_dirs(os.environ.get("PATH")):
            if os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
        cands = [name]
    else:
        progs = (name + ".exe", name + ".com")
        dirs = _create_process_dirs() + _path_dirs(os.environ.get("PATH")) + _path_dirs(path)
        if any(os.path.isfile(os.path.join(d, n)) for d in dirs for n in progs):
            return None
        exts = (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        cands = [name + e for e in exts if e]
    for d in _path_dirs(path):
        for c in cands:
            p = os.path.join(d, c)
            if os.path.isfile(p):
                return p if os.path.splitext(p)[1].lower() in SCRIPT_EXTS else None
    return None


def _cmd_word(text, what):
    """text as one word of the cmd.exe line script_command builds: as given when cmd reads no
    syntax in it, else in double quotes (backslashes before the closing quote doubled, so the
    program the script hands it to does not read an escaped quote). Raises ScriptArgError when
    it holds what cmd expands or splits even inside quotes (CMD_UNSAFE)."""
    for ch, shown in CMD_UNSAFE:
        if ch in text:
            raise ScriptArgError("%s holds %s; cmd.exe would expand or split a `%%`, `\"` or line "
                                 "break, so a .bat or .cmd cannot take it literally" % (what, shown))
    if text and not CMD_QUOTE.intersection(text):
        return text
    return '"%s%s"' % (text, "\\" * (len(text) - len(text.rstrip("\\"))))


def script_command(cmd, env=None):
    """What run_command starts for cmd (an argument list) on Windows: cmd as given, unless its
    first item names a .bat or .cmd. A trailing dot or space after .bat or .cmd is dropped, as
    Windows drops it (x.bat. starts x.bat). A name with a folder part (./gradlew) is looked up
    only beside itself, as shutil.which does: itself when it ends in a PATHEXT extension, else
    name+each PATHEXT extension (CreateProcess tries the name itself, WinError 193 for a shell
    script, and appends only .exe). A bare name: _bare_script (x.bat where CreateProcess would
    find it, the current folder included; else on env's PATH). A .bat or .cmd, so
    found or named, starts as `"<SystemRoot>\\System32\\cmd.exe" /d /v:off /s /c ""<its full
    path>" <args>"` (comspec), a string the caller starts with shell=False: cmd.exe is the
    process the run's job takes, and the batch's children inherit the job. /v:off: delayed
    expansion set in the registry would expand !VAR! in an argument. CreateProcess's own start of
    a .bat quotes for a program, not for cmd, so an unquoted & or a % in an argument there runs
    as cmd syntax; here each argument goes through _cmd_word (ScriptArgError for a %, " or line
    break). Anything else (an .exe or .com, no match, a string cmd) and every other platform:
    cmd itself. First, a command whose program is a WindowsApps App Execution Alias raises
    AliasStubError (alias_check)."""
    if kitlib.platform.NAME != "windows" or not isinstance(cmd, (list, tuple)) or not cmd:
        return cmd
    alias_check(cmd)
    name = str(cmd[0])
    trimmed = name.rstrip(" .")        # Windows drops them: CreateProcess starts x.bat for x.bat.
    if os.path.splitext(trimmed)[1].lower() in SCRIPT_EXTS:
        name = trimmed
    path = (os.environ if env is None else env).get("PATH")
    if os.path.dirname(name):
        found = shutil.which(name, path=path)       # a folder part: beside itself only
    else:
        found = _bare_script(name, path)
    if not found or os.path.splitext(found)[1].lower() not in SCRIPT_EXTS:
        return cmd
    root, fext = os.path.splitext(os.path.abspath(found))
    bat = root + fext.lower()           # which's PATHEXT case (.BAT): the file's own is moot
    base = os.path.basename(bat)
    if "%" in bat:                      # a path holds no " or line break
        raise ScriptArgError("the path of %s holds `%%`; cmd.exe would expand it, so it cannot "
                             "start %s" % (base, bat))
    words = ['"%s"' % bat] + [_cmd_word(str(a), "argument %d of %s" % (i, base))
                              for i, a in enumerate(cmd[1:], 1)]
    return '"%s" /d /v:off /s /c "%s"' % (comspec(), " ".join(words))


def run_command(cmd, kind, repo, slug, **kw):
    """`machine_lock.py run`: book, run cmd in the run's job, release, then say the run's window
    report (Slot.window_lines). Returns the command's exit code (130 on Ctrl+C, 1 when it could
    not start or stop cancelled it). Raises LockError when the run is refused or gives up
    waiting. A bare bash with no Git Bash to run it (kitlib.proc.bash), an argument a .bat or
    .cmd cannot take, or a WindowsApps App Execution Alias (script_command) is refused before the
    run books: 1, no record. The command
    starts in this process's folder; a bare name there that does not start is said with the
    .\\<name> form that runs (bare_hint). A .bat or .cmd, named or found for a name with no
    extension, starts through cmd.exe (script_command); the record keeps cmd as given.

    route: None, or route(cmd, kind, repo, slug, kw) asked after those checks and before the
    run books. None from it runs the command here; a number is the run's exit code, and nothing
    is booked here (kitlib.loadnode.dispatch runs it on a load node, docs/load-nodes.md)."""
    route = kw.pop("route", None)
    store = kw.get("store") or Store()
    kw["store"] = store
    try:
        bash.resolve(cmd, False, kw.get("env"))
        start = script_command(cmd, kw.get("env"))
    except (bash.BareBashError, ScriptArgError) as e:
        store.say("could not start the command: %s" % e)
        return 1
    if route is not None:
        code = route(cmd, kind, repo, slug, kw)
        if code is not None:
            return code
    s = book(kind, repo, slug, cmd=cmd, **kw)
    code, how = 1, "not started"
    try:
        try:
            p = s.start(start, give_back=True)
        except OSError as e:
            store.say("could not start the command: %s%s" % (e, bare_hint(cmd, e)))
            return 1
        store.say(s.held_text())
        code, how = s.wait(p)
        return code
    except KeyboardInterrupt:
        code, how = 130, "interrupted"
        store.say("Ctrl+C: run %s ends" % s.id)
        return 130
    finally:
        s.release(code, how)
        for line in s.window_lines():
            store.say(line)


def expected_end(since, expect_s, max_s=10 * 365 * 86400):
    """since + expect_s as an ISO time, or None when either is missing or unreadable."""
    try:
        return iso(parse_iso(since) + datetime.timedelta(seconds=min(float(expect_s), max_s)))
    except (TypeError, ValueError, OverflowError):
        return None


def request_stop(store, rid, by, session=None, why=None):
    """Writes run rid's stop request (active/<id>.stop: {by_pid, at, by, session}, and why when
    given; session is the asker's session id or None, why the stale-run reap's reason,
    kitlib.lock.stale). False when no such run is live."""
    if not os.path.exists(store.path("active", rid + ".json")):
        return False
    doc = {"by_pid": os.getpid(), "at": iso(), "by": by, "session": session}
    if why:
        doc["why"] = why
    write_json(store.path("active", rid + ".stop"), doc)
    store.bump()
    return True

