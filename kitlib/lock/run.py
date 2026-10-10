"""A run under the lock.

book() joins the queue, waits for the run's turn and returns a Slot that holds its cpu and gpu
slots. The Slot's launcher starts the run's commands in the run's job object (kitlib.proc.job):
pinned to its cores, measured and, when the run is cancelled or interrupted, ended as a
tree. release() frees what it held and records the run. slot() is book() as a context manager
that yields the launcher. run_command() is `machine_lock.py run`: book, one command, release.

Nothing here prints: progress lines go to store.say, which is the CLI's stderr and
nowhere by default. A refusal raises LockError with the CLI's exit code.

Nesting: a run's commands get MACHINE_LOCK_RUN=<run id>:<wrapper pid>:<wrapper ctime>.
A run started under it (nest_check: the named wrapper is an ancestor, alive, and holds) inherits
each pool its parent holds enough of and takes only the rest; one that needs more of a pool than
its parent has, but some, is refused (exit 2): it would wait for the rest while its parent waits
for it. A run that inherits everything takes no queue place; its commands run in a job nested
in its parent's (still measured and ended as a tree).
"""
import contextlib
import datetime
import json
import math
import os
import re
import signal
import subprocess
import time

from kitlib.lock import game
from kitlib.lock import giveback as gbk
from kitlib.lock import holds as hl
from kitlib.lock import kinds as kinds_mod
from kitlib.lock import layout as lay
from kitlib.lock import queue as qm
from kitlib.lock import notice
from kitlib.lock import site
from kitlib.lock.store import (EXIT_BUSY, EXIT_HOLD, EXIT_REQUIRE, EXIT_RESERVED, EXIT_TIMEOUT,
                               EXIT_USAGE, SESSION_ENV, LockError, Store, caller, iso, local_text,
                               new_id, parse_iso, read_json, remove, tree_role, utc_now, write_json)
from kitlib.proc import bash, cpu, daemons, process
from kitlib.proc.job import Job, JobError, cores_text

POOLS, PICK = lay.POOLS, qm.PICK
NEST_ENV = "MACHINE_LOCK_RUN"
NEST_RE = re.compile(r"^(r-\d{8}T\d{6}Z-[0-9a-f]{6}):(\d+):(\d+)$")
ORIGIN_ENV = "MACHINE_LOCK_ORIGIN"      # a detached run's launcher ancestry, pid:ctime;...
BG_ENV = "MACHINE_LOCK_BG"
CPUSET_ENV, CPUMASK_ENV, CPUS_ENV = "MACHINE_LOCK_CPUSET", "MACHINE_LOCK_CPUMASK", "MACHINE_LOCK_CPUS"
GPUS_ENV = "MACHINE_LOCK_GPUS"          # the gpu cards the run holds, by adapter index
END_ENV = "MACHINE_LOCK_END"            # how the run ended, for --on-release
XLA_FRACTION_ENV, XLA_DEFAULT_FRACTION = "XLA_PYTHON_CLIENT_MEM_FRACTION", "0.3"
KILL_HOW = ("interrupted", "cancelled", "error")
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


# ------------------------------------------------------------------ nesting
def _ancestors():
    try:
        return {(int(p), int(c)) for p, c in process.own_ancestors()}
    except Exception:
        return set()


def nest_check(store, value):
    """(parent, None) when MACHINE_LOCK_RUN's value names the run this process is inside (the
    named wrapper is one of its ancestors, alive, and its record says held), else (None, why).
    parent = {id, pid, ctime, kind, shares, has: {pool: slots it holds or inherited}, cores,
    gpus}."""
    try:
        m = NEST_RE.match(value or "")
        if not m:
            return None, "malformed value"
        rid, pid, ct = m.group(1), int(m.group(2)), int(m.group(3))
        if (pid, ct) not in _ancestors():
            return None, "the named wrapper is not an ancestor of this run"
        d = read_json(store.path("active", rid + ".json"))
        if not d or d.get("state") != "held":
            return None, "the named run holds no lock"
        if (d.get("pid"), d.get("ctime")) != (pid, ct):
            return None, "the record names another wrapper"
        if not process.alive(pid, ct):
            return None, "the holder is dead"
        has = d.get("has") if isinstance(d.get("has"), dict) else {}
        return {"id": rid, "pid": pid, "ctime": ct, "kind": d.get("kind"),
                "shares": dict(d.get("shares") or {}),
                "has": {p: n for p, n in has.items() if p in POOLS and type(n) is int and n > 0},
                "cores": list(d.get("cores") or []), "gpus": list(d.get("gpus") or [])}, None
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


def bg_ancestor(env):
    """True when a background run's wrapper (MACHINE_LOCK_BG) is an ancestor of this process."""
    m = NEST_RE.match(env.get(BG_ENV) or "")
    return bool(m) and (int(m.group(2)), int(m.group(3))) in _ancestors()


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


class Run:
    """A run's record, active/<id>.json while it lives, ended/<id>.json after."""

    def __init__(self, store, L, repo, slug, kind, note, cmd, state, rid=None, detached=False,
                 env=None, log=None):
        env = os.environ if env is None else env
        self.store = store
        self.rec = {"id": rid or new_id("r"), "state": state, "repo": repo, "slug": slug,
                    "kind": kind, "note": note, "pid": os.getpid(), "ctime": process.own_ctime(),
                    "since": iso(), "cmd": [str(c) for c in cmd or ()][:20],
                    "detached": bool(detached), "holding": [], "slots": {}, "cores": [],
                    "gpus": [], "shares": {}, "session": env.get(SESSION_ENV) or None,
                    "ancestors": run_ancestors(detached, env), "role": run_role(detached, env),
                    "layout": lay.slot_key(L),
                    "log": getattr(log, "name", None) if log is not None else None}
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
def acquire(store, run, L, need, wait_s, no_wait, prefer=None):
    """Waits for run's turn in the queue and returns {pool: [slot]} once it holds `need`
    (its entry and record name them). Raises LockError with its entry gone: 11 when a strong
    hold (owner or game) leaves it too few for its wait, or its own whole hold is over or has
    not started; 6 (--no-wait) or 8 when agent holds or runs do; 10 when the queue or the holds
    cannot be read before it joined; 1 when stop cancelled it. It never starts on a slot a hold but its own
    covers within its expected time; one they leave too few of keeps its place, parked. With
    no_wait it waits only for background runs to yield. prefer: its hold's slots."""
    s, say = store.settings, store.say
    deadline = time.monotonic() + (0 if no_wait else math.inf if wait_s is None else wait_s)
    span, hid = run.rec["expect_s"], run.rec.get("hold")
    whole = run.rec.get("hold_scope") == "whole"
    told = said = seen = yielding = ypool = qbad = hbad = None
    joined = back = False
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
        cover, blocking, ablocking = hl.cover_plan(store, L, need, span, own=hid, hs=hs)
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


# ------------------------------------------------------------------ book, Slot, launcher
class Launcher:
    """What slot() yields: starts commands in the run's job. start(cmd, **popen_kw) ->
    Popen (env defaults to the run's: MACHINE_LOCK_RUN, the cpuset, the gpus); run(cmd, **kw) ->
    its exit code, waited for with the run's use sampled and a stop honoured; wait(p) -> exit
    code; pids(); usage() -> the run's use so far; kill() ends every process in the job. id,
    cores (cpu slots), gpus (gpu slots), mask (the global thread mask, or None) and env read the
    run."""

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
        return self._slot.job.pids() if self._slot.job and self._slot.job.h else []

    def usage(self):
        return self._slot.usage_fields()

    def kill(self):
        if self._slot.job is not None:
            self._slot.job.terminate(1)


class Slot:
    """A held run (book()). start/wait/release, or the launcher; a context manager that
    releases (how "error" and the tree ended when the block raised, "interrupted" on Ctrl+C)."""

    def __init__(self, store, run, L, own, parent, mutex, env, on_release_cmd, kind_entry,
                 shares, log=None, gate=False):
        self.store, self.run, self.L, self.own, self.parent = store, run, L, own, parent
        self.mutex, self.on_release_cmd, self.log, self.gate = mutex, on_release_cmd, log, gate
        self.nested = run is None
        self.id = parent["id"] if self.nested else run.id
        self.cores = list(parent["cores"] if self.nested else run.rec["cores"])
        self.gpus = list(parent["gpus"] if self.nested else run.rec["gpus"])
        self.code, self.how, self.released, self.started, self.ended = 0, "done", False, False, None
        self.warn_s = kind_entry["warn_min"] * 60 * store.settings.warn_scale
        self.kind = kind_entry
        self.usage = run.usage if run else Usage(L["cpu_slot_threads"], store.settings.usage_sample_s)
        self.env = dict(env)
        self.env.pop(ORIGIN_ENV, None)
        # Only the command start(give_back=True) starts may give slots back (kitlib.lock.giveback).
        self.env.pop(gbk.GIVE_ENV, None)
        self.offer, self.offer_bad, self.given = None, 0, []
        mask = None
        if not self.nested:
            self.env[NEST_ENV] = "%s:%d:%d" % (run.id, run.rec["pid"], run.rec["ctime"])
            if own["gpu"] and shares.get("gpu") == "training" and not self.env.get(XLA_FRACTION_ENV):
                self.env[XLA_FRACTION_ENV] = XLA_DEFAULT_FRACTION
            if own["cpu"]:
                mask = lay.cores_mask(L, run.rec["cores"])
        try:
            # A run with no cores of its own stays on its parent's: the job nests in the parent's.
            self.job = Job(mask)
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
        """'engine-test holds cpu slots 0-1 for repo/slug (engine-test, run r-...); the command
        runs on cores 0-3'"""
        if self.nested:
            return "nested: run %s already holds what it needs; the command runs in its job" % self.id
        r = self.run.rec
        return "%s holds %s for %s/%s (%s, run %s); the command runs on %s%s" % (
            r["kind"], " + ".join(lay.slots_text(p, r[PICK[p]]) for p in POOLS if r[PICK[p]]),
            r["repo"], r["slug"], r["kind"], self.id,
            cores_text(self.job.effective) if self.own["cpu"] else
            "the cores of run %s" % self.parent["id"], "; " + self.job.note if self.job.note else "")

    def start(self, cmd, give_back=False, **kw):
        """Popen(cmd) in the run's job; OSError when it cannot start. give_back: cmd may give back
        cpu slots it no longer needs while it runs (kitlib.lock.giveback); only a run that holds
        its own cpu slots offers it, and wait() serves it."""
        if self.released:
            raise LockError(1, "run %s has released its slots" % self.id)
        if give_back and not self.nested and self.own["cpu"] and self.job.mask is not None:
            self.offer, self.offer_bad = gbk.request_path(self.store, self.id), 0
            remove(self.offer)
            kw["env"] = dict(kw.get("env") or self.env, **{gbk.GIVE_ENV: self.offer})
        if self.run is not None:
            for k, v in self.run.popen_kw(self.env).items():
                kw.setdefault(k, v)
        else:
            kw.setdefault("env", self.env)
        pin = self.job.pin
        p = self.job.start(docker_cpu_env(cmd, bool(self.gpus)), **kw)
        self.started = True
        if self.job.pin != pin and self.run is not None:
            with contextlib.suppress(OSError):
                self.run.save(**pin_fields(self.job))
        return p

    def stop_asked(self):
        return self.run is not None and self.run.stop_asked()

    def wait(self, p):
        """(exit code, how) of child p: its use sampled every settings.child_poll_s into the
        record; a stop ends the job's tree ("cancelled", 1); past the kind's warn_min a warning,
        never a kill."""
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
                self.store.say("run %s was cancelled by stop; its process tree is ended" % self.id)
                self.job.terminate(1)
                p.wait()
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
        narrow the job, then the record, then the queue entry; else refuse, changing nothing.
        Returns why it was refused, or None. Never raises: a failure refuses."""
        req = {}
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
        done = dict(pin_fields(self.job), given_back={
            "id": req["id"], "cpu": given, "cores": keep, "cpumask": env[CPUMASK_ENV],
            "cpuset": env[CPUSET_ENV], "at": iso()})
        try:
            gbk.shrink(self.store, self.run, keep, done)
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
        `daemons`. A nested slot leaves them to the run it is nested in, unless it is a gate: then
        it stops those in its own job and records none. Never raises: what it cannot read or stop
        runs on, unpinned."""
        try:
            if not site.stops_daemons(self.store, self.gate):
                return
            found = daemons.find(self.job.pids())
            if not found:
                return
            done = daemons.stop(found)
        except Exception as e:
            self.store.say("WARNING: run %s: build daemons not checked: %s" % (self.id, e))
            return
        for pid, label, res in done:
            self.store.say("run %s: stopped the %s (pid %d) it left: %s" % (self.id, label, pid, res))
        if self.run is not None:
            self.run.rec["daemons"] = [{"pid": pid, "what": label, "result": res}
                                       for pid, label, res in done]

    def release(self, code=None, how=None):
        """Ends the run: its job (the tree killed when interrupted, cancelled or an error; else
        its build daemons stopped when it is a gate (stop_daemons) and what else it left runs on,
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
                self.job.end(kill=how in KILL_HOW)
                if self.started:
                    on_release(self.store, self.on_release_cmd, self.env, how, self.run)
        finally:
            if self.run is not None:
                self.ended = settle(self.store, self.run, code, how, self.mutex)
        return self.ended

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is KeyboardInterrupt:
            self.release(130, "interrupted")
        elif et is not None:
            self.release(1, "error")
        else:
            self.release()


def _relayout(store, L):
    """The layout in force now, read under the queue mutex: a layout adopted between book()'s
    first look and its record being saved is seen here; after this, none can be (a live run
    blocks adoption, kitlib.lock.layout.in_force)."""
    with store.lock("queue", store.settings.queue_lock_s):
        cur = store.read("layout.json")
    return cur if lay.valid_layout(cur) and cur["key"] != L["key"] else L


def book(kind, repo, slug, cores=None, hold=None, gpu_share=None, wait_s=None, no_wait=False,
         expect_s=None, note=None, on_release_cmd=None, cmd=None, store=None, kinds_table=None,
         env=None, rid=None, detached=False, log=None, gate=False):
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
    `gate`; by default only a gate's leftover build daemons are stopped (Slot.stop_daemons)."""
    store = store or Store()
    env = dict(os.environ if env is None else env)
    try:
        table = kinds_table or kinds_mod.load(store.kinds_path)
    except kinds_mod.KindsError as e:
        raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % e)
    if kind not in table:
        raise LockError(EXIT_USAGE, "usage: unknown kind %r (known: %s)" % (kind, ", ".join(sorted(table))))
    k = table[kind]
    if not repo or not slug:
        raise LockError(EXIT_USAGE, "usage: a run names its repo and slug")
    if gpu_share not in (None, "training"):
        raise LockError(EXIT_USAGE, "usage: --gpu-share is training or nothing")
    if gpu_share and not k["share"]:
        raise LockError(EXIT_USAGE, "usage: --gpu-share training needs a kind whose gpu is shared")
    if expect_s is not None and not (isinstance(expect_s, (int, float)) and expect_s > 0):
        raise LockError(EXIT_USAGE, "usage: --expect must be a positive duration")
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
    shares = hl.shares_for(k, gpu_share)
    if parent and not any(own.values()):
        return Slot(store, None, L, own, parent, None, env, on_release_cmd, k, shares, log, gate)
    if own["gpu"] and bg_ancestor(env):
        raise LockError(EXIT_USAGE, "usage: a gpu lock inside a background run would pause that "
                                    "run; refused")
    run = Run(store, L, repo, slug, kind, note, cmd, "waiting", rid, detached, env, log)
    hist = None if expect_s is not None else store.history_expect(repo, kind)
    run.rec.update(expect_s=expect_s if expect_s is not None else hist if hist is not None
                   else k["warn_min"] * 60,
                   expect_from="--expect" if expect_s is not None else "history"
                   if hist is not None else "guide",
                   need=own, has={p: n for p, n in has.items() if n}, hold=hold,
                   parent=parent["id"] if parent else None, gpu_share=gpu_share,
                   **({"gate": True} if gate else {}))
    mutex, done, code, how = None, False, EXIT_REQUIRE, "not acquired"
    try:
        try:
            mutex = store.run_mutex(run.id)
        except OSError as e:
            raise LockError(EXIT_REQUIRE, "require-lock: the run's mutex could not be made (%s); "
                                          "the command was not run" % e)
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
            run.usage = Usage(L["cpu_slot_threads"], store.settings.usage_sample_s)
            run.save(need=own, has={p: n for p, n in has.items() if n}, layout=lay.slot_key(L))
        acquire(store, run, L, own, wait_s, no_wait, prefer)
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
    """with slot("engine-test", repo, slug) as launch: launch.run([...]) -- book() as a context
    manager yielding the Launcher; released on leaving the block."""
    s = book(kind, repo, slug, **kw)
    with s:
        yield s.launcher


def run_command(cmd, kind, repo, slug, **kw):
    """`machine_lock.py run`: book, run cmd in the run's job, release. Returns the command's exit
    code (130 on Ctrl+C, 1 when it could not start or stop cancelled it). Raises LockError when
    the run is refused or gives up waiting. A bare bash with no Git Bash to run it
    (kitlib.proc.bash) is refused before the run books: 1, no record."""
    store = kw.get("store") or Store()
    kw["store"] = store
    try:
        bash.resolve(cmd, False, kw.get("env"))
    except bash.BareBashError as e:
        store.say("could not start the command: %s" % e)
        return 1
    s = book(kind, repo, slug, cmd=cmd, **kw)
    code, how = 1, "not started"
    try:
        try:
            p = s.start(cmd, give_back=True)
        except OSError as e:
            store.say("could not start the command: %s" % e)
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


def expected_end(since, expect_s, max_s=10 * 365 * 86400):
    """since + expect_s as an ISO time, or None when either is missing or unreadable."""
    try:
        return iso(parse_iso(since) + datetime.timedelta(seconds=min(float(expect_s), max_s)))
    except (TypeError, ValueError, OverflowError):
        return None


def request_stop(store, rid, by):
    """Writes run rid's stop request (active/<id>.stop: {by_pid, at, by}). False when no such
    run is live."""
    if not os.path.exists(store.path("active", rid + ".json")):
        return False
    write_json(store.path("active", rid + ".stop"), {"by_pid": os.getpid(), "at": iso(), "by": by})
    store.bump()
    return True

