"""Give back: a held run hands back cpu slots its command no longer needs, while it runs on.

A run books its cpu slots for the whole run; a command whose work drains (a test pool whose
workers run out of modules) can give the surplus back so a queued run takes it at once, not when
the whole run ends. Booking only ever shrinks: nothing given back is taken again, and nothing
about the order of the queue changes.

The command cannot narrow its own pin (the run's wrapper holds the job), so it asks the wrapper:

  1. The wrapper offers it, by setting MACHINE_LOCK_GIVE_BACK (GIVE_ENV) on one command only: the
     command of `machine_lock.py run` (kitlib.lock.run.run_command) and a gate's last step
     (kitlib.gate.runner). Its value is the request file's path, active/<run id>.giveback. A
     nested run, a background run and every other command get no offer (Slot drops the variable
     from what it inherits), so a command they start cannot give away slots another command of
     the run still needs.
  2. The command (Offer.give) writes the request: {id, cpu: [slot ids], by_pid, by_ctime, at},
     then waits for its id in the run record's `given_back` (done) or `give_back_refused`. A
     record it cannot open now (the wrapper is replacing it) is read again, never taken as gone.
  3. The wrapper (Slot.wait, every settings.child_poll_s) reads the request and removes it. It
     refuses (check) unless the asker is alive and in the run's job, the run holds its cpu slots
     itself (not nested) and is pinned to them, the slots are some of its own and at least one
     stays, and no gpu slot is named (the CUDA environment a command started with cannot be
     narrowed). Then, in this order:
       a. the job's affinity narrows to the slots it keeps (kitlib.proc.job.Job.narrow): every
          process in the run's job, and in jobs nested in it, is moved off the slots it gives
          back before anything says they are free. A process outside the job is not moved: a
          container that `docker exec` or `docker run` started runs in the container's VM with
          the cpuset it was given (MACHINE_LOCK_CPUSET at its start), and keeps it;
       b. under the queue mutex (shrink), the run record first (cores, slots, has.cpu, the pin
          fields), then the queue entry's cores, then the ack: the record's given_back. The
          queue write bumps the store revision, so waiters see it on their next look
          (settings.queue_poll_s). The ack comes last so an asker never hears "given" while
          the queue still holds the slots;
       c. the commands it starts later get the narrower MACHINE_LOCK_CPUSET/CPUS/CPUMASK.
     A failure at a, or at b before the queue write lands, puts back what changed and refuses:
     the record and the queue entry keep every slot booked, and the job's pin is widened back
     to them (Job.repin), which moves its processes back onto them too. If Windows refuses that
     widening, the refusal says the job stays narrowed to the slots kept, and a later request
     that keeps any other slot is refused (Job.narrow only narrows).
     A failed ack write, after the queue write landed, undoes nothing: the slots are given back.
     The wrapper keeps the ack in memory and writes it again on its next poll (Slot.ack_due), so
     the asker hears "given" late, or "timeout" if its wait_s ends first; held() then shows the
     narrower cores. A re-ask for the same slots is refused ("not this run's") and changes
     nothing. A wrapper that dies between the queue write and the ack takes the asker with it
     (the run's job is kill-on-close), so no one waits on an ack that never comes.

Fail safe. The queue entry is the allocation every planner reads (this kit's and the installed
machine_lock.py's), and it is written after the record: a wrapper that dies between the record and
the queue leaves the slots held in the queue (never handed to a waiter twice), and a dead wrapper's
entry is dropped by the next look anyway. Whether the queue write landed is read back from
queue.json under the same mutex, never assumed: an interrupt (even Ctrl+C) after it lands puts
nothing back. The installed tool reads a shrunk record and entry as it reads any (cores, slots,
has); the request file is not a .json, so neither tool reads it as a run record.
"""
import copy
import os
import secrets
import time

from kitlib.lock import queue as qm
from kitlib.lock.store import iso, read_json, read_record, write_json
from kitlib.proc import process

GIVE_ENV = "MACHINE_LOCK_GIVE_BACK"
SUFFIX = ".giveback"
POLL_S = 0.05


def request_path(store, rid):
    return store.path("active", rid + SUFFIX)


# ------------------------------------------------------------------ the wrapper's side
def read_request(path):
    """The request at path as {id, cpu, gpu, by_pid, by_ctime}, None when there is none, or
    {"bad": why} when it is not a request."""
    if not os.path.exists(path):
        return None
    d = read_json(path)
    if d is None:
        return {"bad": "not a JSON object"}
    cpu, gpu = d.get("cpu"), d.get("gpu", [])
    if not isinstance(d.get("id"), str) or not d["id"]:
        return {"bad": "no request id"}
    if not isinstance(cpu, list) or not cpu or any(type(i) is not int for i in cpu):
        return {"bad": "cpu is not a list of slot ids", "id": d["id"]}
    if not isinstance(gpu, list):
        # 0, null or any other non-list is not "no gpu slots": refused, never read as none.
        return {"bad": "gpu is not a list of slot ids", "id": d["id"]}
    if type(d.get("by_pid")) is not int or type(d.get("by_ctime")) is not int:
        return {"bad": "the asker is not named", "id": d["id"]}
    return {"id": d["id"], "cpu": sorted(set(cpu)), "gpu": gpu,
            "by_pid": d["by_pid"], "by_ctime": d["by_ctime"]}


def check(req, cores, nested, own_cpu, pin, job_pids, alive):
    """Why request req cannot be done (None: it can). cores: the cpu slots the run holds;
    job_pids: the pids in its job now; alive(pid, ctime)."""
    if "bad" in req:
        return "the request is malformed (%s)" % req["bad"]
    if nested or not own_cpu:
        return "the run holds no cpu slots of its own (nested in another run's)"
    if req["gpu"]:
        return "gpu slots cannot be given back (a command's CUDA environment cannot be narrowed)"
    if pin != "pinned":
        return "the run is not pinned to its cores (pin %s)" % pin
    if req["by_pid"] not in job_pids or not alive(req["by_pid"], req["by_ctime"]):
        return "the asker (pid %d) is not a live process of this run" % req["by_pid"]
    extra = [i for i in req["cpu"] if i not in cores]
    if extra:
        return "cpu slot(s) %s are not this run's" % ",".join(map(str, extra))
    if len(req["cpu"]) >= len(cores):
        return "a run keeps at least one cpu slot (it holds %d)" % len(cores)
    return None


def shrink(store, run, keep, fields, ack):
    """Writes the narrower booking under the queue mutex, in three writes: the run record
    (cores, slots, has.cpu and `fields`, the pin fields), then the queue entry's cores, then the
    record's `given_back` = ack. The ack is written only once the queue says it is true.

    The mutex not taken raises OSError with nothing written. A failed write that queue.json,
    read back, shows came before the queue write landed raises it after putting run.rec back
    exactly as it was (Run.save changes run.rec before it writes, so a failed record write would
    otherwise leave the narrower booking in memory for the next save to persist). Once the queue write has landed the slots are given back, whatever follows: a
    failed ack write rolls nothing back (the job widened onto slots the queue has freed would be
    booked twice); the ack stays in run.rec for the next save, and shrink returns False. True:
    the ack is on disk. Whether the queue write landed is read back from queue.json (under the
    mutex nothing else writes it): one that landed before an Exception is committed, and one
    that landed before a KeyboardInterrupt (or any other BaseException) re-raises it with
    nothing put back. A queue.json that cannot be read back is taken as landed: the error is
    never on the side that books a slot twice, only on the side that leaves one unused (the
    queue may still hold slots the narrowed job no longer runs on, until the run ends)."""
    before = copy.deepcopy(run.rec)
    slots = dict(run.rec.get("slots") or {}, cpu=list(keep))
    has = dict(run.rec.get("has") or {}, cpu=len(keep))
    with store.lock("queue", store.settings.queue_lock_s):
        try:
            run.save(cores=list(keep), slots=slots, has=has, **fields)
            q = qm.load(store)
            e = qm.entry_of(q, run.id)
            if e is not None:
                e["cores"] = list(keep)
                qm.save(store, q)
        except BaseException as exc:
            # Only a queue read back that says the write did not land is put back. Unreadable
            # (None) counts as landed: the job stays narrow and the queue may still count the
            # slots held, which wastes them; putting back (the caller then widens the job) could
            # put the job on slots a landed write freed, which books them twice.
            if queue_books(store, run.id, keep) is False:
                # The queue still holds every slot: the record says so again, in memory, then
                # on disk (a failed first write left the old record there; if this write fails
                # too after one that landed, the record on disk under-reports and the queue
                # still counts them held).
                run.rec.clear()
                run.rec.update(before)
                try:
                    run.save()
                except OSError:
                    pass
                raise
            if not isinstance(exc, Exception):
                raise
        # Committed: the queue entry no longer holds the slots given back.
        try:
            run.save(given_back=ack)
        except OSError:
            return False
        return True


def queue_books(store, rid, keep):
    """True when queue.json on disk gives run rid's entry exactly the cores `keep` (the shrink's
    queue write landed); False when it gives it anything else, or has no such entry; None when
    it stays unreadable past queue_read_s (qm.load waits out a reader that has it open), so
    whether the write landed is not known."""
    try:
        e = qm.entry_of(qm.load(store), rid)
    except OSError:
        return None
    return e is not None and e.get("cores") == list(keep)


# ------------------------------------------------------------------ the command's side
# The command reads the run record with store.read_record (re-exported here): (None, False) is
# no record, the run is gone (finish removes it); (None, True) is a record it cannot read now,
# most often one the wrapper's os.replace is swapping (under load an asker heard "gone" from a
# run that went on to refuse). That one is read again, never taken for gone or for no slots.
WITHDRAW_S = 2.0            # how long a timed-out give tries to withdraw a request it cannot read


class Offer:
    """A command's offer to give back cpu slots (from_env: None when it has none)."""

    def __init__(self, path):
        self.path = path
        self.rid = os.path.basename(path)[:-len(SUFFIX)]
        self.record_path = os.path.join(os.path.dirname(path), self.rid + ".json")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        path = env.get(GIVE_ENV) or ""
        if not path.endswith(SUFFIX) or not os.path.isabs(path):
            return None
        o = cls(path)
        nest = (env.get("MACHINE_LOCK_RUN") or "").split(":")[0]       # kitlib.lock.run.NEST_ENV
        return o if nest == o.rid else None

    def held(self):
        """The cpu slots the run holds now (its record's cores): [] when it is gone (no record,
        or one not held); None when its record cannot be read now, which is never no slots."""
        rec, there = read_record(self.record_path)
        if not there:
            return []
        if rec is None:
            return None
        return [i for i in rec.get("cores") or [] if type(i) is int] if rec.get("state") == "held" else []

    def give(self, cpu, wait_s=10.0):
        """Asks the wrapper to give back cpu slots `cpu`; waits up to wait_s for its answer.
        ("given", the record's given_back: {id, cpu (all given back so far), cores (kept),
        cpumask, cpuset, at}), ("refused", why), ("gone", why) or ("timeout", why). "gone" only
        when the record is not there or not held; one it cannot read now (read_record) is read
        again on the next poll. After a timeout the request is withdrawn (withdraw); held()
        says what the run holds."""
        rid = secrets.token_hex(6)
        write_json(self.path, {"id": rid, "cpu": sorted(set(cpu)), "by_pid": os.getpid(),
                               "by_ctime": process.own_ctime(), "at": iso()})
        end = time.monotonic() + wait_s
        while True:
            rec, there = read_record(self.record_path)
            if not there or (rec is not None and rec.get("state") != "held"):
                return "gone", "run %s holds nothing now" % self.rid
            if rec is not None:
                if (rec.get("given_back") or {}).get("id") == rid:
                    return "given", rec["given_back"]
                if (rec.get("give_back_refused") or {}).get("id") == rid:
                    return "refused", rec["give_back_refused"].get("why")
            if time.monotonic() >= end:
                why = "run %s did not answer within %g s" % (self.rid, wait_s)
                if not self.withdraw(rid):
                    why += ("; its request could not be withdrawn within %g s, so the run may "
                            "still act on it" % WITHDRAW_S)
                return "timeout", why
            time.sleep(POLL_S)

    def withdraw(self, rid):
        """Removes request rid unless the wrapper has taken it. True: it is not on disk as rid
        now (removed here, taken, or another request). A request that cannot be read or removed
        now (the wrapper has it open) is tried again every POLL_S for WITHDRAW_S, then False:
        left there, it would be served after its asker stopped waiting."""
        end = time.monotonic() + WITHDRAW_S
        while True:
            d, there = read_record(self.path)
            if not there or (d is not None and d.get("id") != rid):
                return True
            if d is not None:
                try:
                    os.remove(self.path)
                    return True
                except FileNotFoundError:
                    return True
                except OSError:
                    pass
            if time.monotonic() >= end:
                return False
            time.sleep(POLL_S)


def surplus(cores, threads_per_slot, threads_needed):
    """The cpu slots of `cores` to give back when threads_needed threads are all the command
    still uses: the highest ids first, keeping ceil(threads_needed / threads_per_slot) slots,
    at least one. The lowest stay because a run plans onto its lowest free slots first."""
    keep = max(1, -(-max(0, threads_needed) // max(1, threads_per_slot)))
    return sorted(cores)[keep:]


def threads_per_slot(cores, mask):
    """Threads each held cpu slot has: the mask's threads over the slots (one when unknown)."""
    try:
        n = bin(int(mask, 16) if isinstance(mask, str) else int(mask)).count("1")
    except (TypeError, ValueError):
        return 1
    return max(1, n // len(cores)) if cores else 1


def drainer(say, env=None):
    """For a command whose work drains (kit_tests' pool): a give(threads_needed) that gives back
    the cpu slots past what threads_needed threads use (surplus), says what happened, and returns
    the ack (given_back) or None; None itself when the command has no offer. After a refusal, a
    timeout or the run's end it asks no more; only a refusal is said as "the run keeps them" (a
    timeout says what the record books then, which a lost ack's give-back already shows).
    A record it cannot read now (held() None) asks nothing that time and is read again on the
    next call; threads per slot come from the first record it reads, before anything is given
    back (the command's mask is the one it started with)."""
    env = os.environ if env is None else env
    offer = Offer.from_env(env)
    if offer is None:
        return None
    mask = env.get("MACHINE_LOCK_CPUMASK")      # run.CPUMASK_ENV
    state = {"off": False, "tps": None}

    def give(threads_needed):
        if state["off"]:
            return None
        cores = offer.held()
        if cores is None:
            return None
        if state["tps"] is None:
            state["tps"] = threads_per_slot(cores, mask)
        extra = surplus(cores, state["tps"], threads_needed)
        if not extra:
            return None
        res, info = offer.give(extra)
        if res == "given":
            say("give back: %d thread(s) still needed; gave back cpu slot(s) %s, the run holds %s"
                % (threads_needed, ",".join(map(str, extra)), ",".join(map(str, info["cores"]))))
            return info
        state["off"] = True
        if res == "refused":
            say("give back: cpu slot(s) %s not given back (%s: %s); the run keeps them"
                % (",".join(map(str, extra)), res, info))
        else:
            # No answer is not a "no": the wrapper may have given them back and not yet written
            # its ack (a lost ack is written on its next poll), or be mid-write.
            now = offer.held()
            say("give back: cpu slot(s) %s: no answer (%s: %s); %s"
                % (",".join(map(str, extra)), res, info,
                   "the run's record cannot be read now" if now is None else
                   "the run's record books %s now" % (",".join(map(str, now)) or "nothing")))
        return None
    return give
