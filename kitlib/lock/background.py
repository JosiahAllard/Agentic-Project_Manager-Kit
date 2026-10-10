"""The background tier: a pausable gpu training run that uses what nothing else wants.

run_background() is `machine_lock.py background`. Its queue entry (state bg) holds the layout's
medium_cores cpu slots and one gpu slot while its child runs; the queue plans around them as soft
holdings (kitlib.lock.queue.plan). The child (a trainer using kitlib.lock.bg_client) is asked to
pause through control.json when a run the queue plans to start now needs its cores or gpu slot
(the yield trigger), or a strong hold (owner or game) live now covers them. Once paused it
holds nothing, and it starts again after settings.bg_resume_s with no demand on what is free then
(never what a hold covers). A child still running settings.bg_yield_s after it was asked has its
tree ended (yield-late). Failures write alerts/<run id>.json for the PM's watcher.

The child gets MACHINE_LOCK_BG=<run id>:<wrapper pid>:<wrapper ctime>, MACHINE_LOCK_BG_CONTROL
(its control.json) and MACHINE_LOCK_BG_DIR (background/<run id>.d, which a container may mount).
"""
import contextlib
import json
import os
import subprocess
import time

from kitlib.lock import holds as hl
from kitlib.lock import layout as lay
from kitlib.lock import queue as qm
from kitlib.lock import game
from kitlib.lock.run import (BG_ENV, NEST_ENV, ORIGIN_ENV, PICK, POOLS, XLA_DEFAULT_FRACTION,
                             XLA_FRACTION_ENV, Run, child_code, docker_cpu_env, export_threads,
                             pin_fields, settle)
from kitlib.lock.store import EXIT_REQUIRE, EXIT_USAGE, LockError, Store, iso, write_json
from kitlib.proc.job import Job, cores_text

BG_CONTROL_ENV, BG_DIR_ENV, BG_ROOT_ENV = ("MACHINE_LOCK_BG_CONTROL", "MACHINE_LOCK_BG_DIR",
                                           "MACHINE_LOCK_BG_ROOT")
PAUSE_EXIT = 75                         # the trainer's exit after its checkpoint (bg_client)


def demand_text(fg_gpu, fg_cpu, mine):
    """Why a background child is asked to pause, for control.json and alerts."""
    return "; ".join((["foreground gpu work: " + ", ".join(fg_gpu)] if fg_gpu else [])
                     + (["foreground cpu work: " + ", ".join(fg_cpu)] if fg_cpu else [])
                     + ["%s over its slot" % h for h in mine])


def alert(store, run, how, code, reason):
    """alerts/<run id>.json for the PM's watcher (launcher/pm_watch.py prints BGFAIL)."""
    try:
        write_json(store.path("alerts", run.id + ".json"), {
            "id": run.id, "repo": run.rec["repo"], "slug": run.rec["slug"], "how": how,
            "code": code, "reason": reason, "at": iso(), "cmd": run.rec["cmd"]})
    except OSError as e:
        store.say("the alert for %s could not be written: %s" % (run.id, e))


def bg_free(store, run):
    """A background run's child ended: its entry and record hold nothing until it starts again.
    False when the queue could not be had (settings.bg_free_s) or read: it says so, once, and
    run_background clears the entry at its first later look that has the queue."""
    ok = False
    try:
        with store.lock("queue", store.settings.bg_free_s):
            q = qm.load(store)
            e = qm.entry_of(q, run.id)
            if e is not None and (e["cores"] or e["gpus"]):
                e.update(cores=[], gpus=[], held_at=None)
                qm.save(store, q)
        ok = True
    except OSError as err:
        store.say("background run %s: its child ended, but its queue entry could not be cleared "
                  "(%s); it tries again" % (run.id, err))
    with contextlib.suppress(OSError):
        run.save(slots={}, holding=[], cores=[], gpus=[])
    return ok


def end_child(job, p, wait_s=10):
    """Ends the job's whole tree, then waits on its child p (bounded), so the Popen knows its
    process ended: dropped unwaited, it warns "subprocess N is still running" (ResourceWarning)."""
    job.end(kill=True)
    if p is not None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            p.wait(timeout=wait_s)


def run_background(cmd, repo, slug, expect_s=None, note=None, store=None, env=None, rid=None,
                   detached=False, log=None):
    """Runs cmd as a background run until it exits other than PAUSE_EXIT, stop ends it, or it
    yields late. Returns its exit code (1 for yield-late or a failed start, 130 on Ctrl+C).
    Raises LockError (exit 2 with no gpu here, 10 when the store or queue cannot be had before
    it joined)."""
    store = store or Store()
    s, say = store.settings, store.say
    L = lay.in_force(store)["layout"]
    if not L["gpus"]:
        raise LockError(EXIT_USAGE, "usage: a background run is gpu training, and this machine "
                                    "has no gpu the lock knows of")
    env = dict(os.environ if env is None else env)
    run = Run(store, L, repo, slug, None, note, cmd, "bg", rid, detached, env, log)
    ctl_dir = store.path("background", run.id + ".d")
    ctl = os.path.join(ctl_dir, "control.json")
    for k in (NEST_ENV, ORIGIN_ENV, BG_ROOT_ENV):   # bg_client would prefer BG_ROOT (containers)
        env.pop(k, None)
    env[BG_ENV] = "%s:%d:%d" % (run.id, run.rec["pid"], run.rec["ctime"])
    env[BG_CONTROL_ENV], env[BG_DIR_ENV] = ctl, ctl_dir
    if not env.get(XLA_FRACTION_ENV):
        env[XLA_FRACTION_ENV] = XLA_DEFAULT_FRACTION
    need = {"cpu": L["medium_cores"], "gpu": 1}
    seq, beat, job, p, first, free_since = 0, 0.0, None, None, True, time.monotonic()
    code, how, pause, stopping, told, mutex = 1, "stopped", None, None, None, None
    demand_since, fg_gpu, fg_cpu, qbad, unfreed, joined = None, [], [], None, False, False
    hs_known, hbad = None, None
    try:
        try:
            mutex = store.run_mutex(run.id)
        except OSError as e:
            code, how = EXIT_REQUIRE, "not acquired"
            raise LockError(EXIT_REQUIRE, "require-lock: the run's mutex could not be made (%s); "
                                          "the command was not run" % e)
        run.rec.update(need=need, background=True, expect_s=expect_s, kind="background")
        try:
            os.makedirs(ctl_dir, exist_ok=True)
            run.save(child="waiting", shares={"gpu": "medium"})
        except OSError as e:
            code, how = EXIT_REQUIRE, "not acquired"
            raise LockError(EXIT_REQUIRE, "require-lock: the lock store %s could not be written "
                                          "(%s); the command was not run" % (store.root, e))
        while True:
            now = time.monotonic()
            game.sync(store, L)
            # What the child pauses for: runs the queue plans to start now on its slots, and a
            # strong hold live now over them. Paused, it holds nothing, so the hold is no longer
            # demand: it starts again after bg_resume_s on what no hold covers.
            # Holds that cannot be read are never read as none (HoldsUnreadable fails closed):
            # it keeps the last holds it read and starts the child on nothing until they read.
            try:
                hs_known = hl.records(store, L)
                if hbad is not None:
                    say("background run %s: the holds read again" % run.id)
                hbad = None
            except OSError as err:
                if hbad is None:
                    hbad = str(err)
                    say("background run %s: %s; it keeps the last holds it read and starts "
                        "nothing until they read" % (run.id, err))
            hs = hs_known or []
            cover = {p_: hl.covered(L, p_, 0, hs) for p_ in POOLS}
            mine_held = {"cpu": run.rec["cores"], "gpu": run.rec["gpus"]}
            mine = sorted({hl.text(cover[p_][i]) for p_ in POOLS for i in mine_held[p_]
                           if i in cover[p_] and hl.strong(cover[p_][i])})
            want_start = (p is None and stopping is None and not fg_gpu and not fg_cpu
                          and not mine and hbad is None
                          and (first or now - (free_since or now) >= s.bg_resume_s))
            got, short = None, None
            try:
                with store.lock("queue", 2):
                    q = qm.load(store)
                    before = json.dumps(q, sort_keys=True)
                    qm.prune(store, L, q, run.id)
                    back = joined and qm.entry_of(q, run.id) is None
                    e = qm.entry_of(q, run.id) or qm.join(q, run.rec, {"cpu": 0, "gpu": 0}, "bg")
                    if unfreed and p is None:
                        e.update(cores=[], gpus=[], held_at=None)
                    plan = qm.plan(L, q)[0]
                    starting = [x for x in q["entries"] if x["id"] in plan and plan[x["id"]]["now"]]
                    fg_gpu = [x["id"] for x in starting
                              if set(plan[x["id"]]["gpus"]) & set(e["gpus"])]
                    fg_cpu = [x["id"] for x in starting if x["id"] not in fg_gpu
                              and set(plan[x["id"]]["cores"]) & set(e["cores"])]
                    if want_start and not fg_gpu and not fg_cpu:
                        held = qm.holders(L, q, run.id)
                        claimed = {p_: {i for x in starting for i in plan[x["id"]][PICK[p_]]}
                                   for p_ in POOLS}
                        free = {p_: [i for i in range(lay.size(L, p_)) if i not in held[p_]
                                     and i not in cover[p_] and i not in claimed[p_]]
                                for p_ in POOLS}
                        short = [p_ for p_ in ("gpu", "cpu") if len(free[p_]) < need[p_]]
                        if not short:
                            got = {p_: free[p_][:need[p_]] for p_ in POOLS}
                            e.update(cores=got["cpu"], gpus=got["gpu"], need=dict(need),
                                     held_at=time.time())
                    if json.dumps(q, sort_keys=True) != before:
                        qm.save(store, q)
                if qbad is not None:
                    say("background run %s: the queue reads again%s" % (
                        run.id, "; its entry was gone, so it joined again at the back"
                        if back else ""))
                elif back:
                    say("background run %s: its queue entry was gone; it joined again at the back"
                        % run.id)
                if unfreed:
                    say("background run %s: its queue entry is cleared" % run.id)
                qbad, unfreed, joined = None, False, True
            except qm.QueueUnreadable as err:
                if not joined:
                    code, how = EXIT_REQUIRE, "not acquired"
                    raise LockError(EXIT_REQUIRE, "require-lock: %s; the command was not run" % err)
                got = None
                if qbad is None:
                    qbad = str(err)
                    say("background run %s: %s; it keeps the last demand and looks again"
                        % (run.id, err))
            except OSError:
                got = None                  # the queue was busy: the last demand stands
            demand = fg_gpu + fg_cpu + mine
            if demand:
                free_since = None
                demand_since = now if demand_since is None else demand_since
            else:
                demand_since = None
                if free_since is None:
                    free_since = now
            if stopping is None and run.stop_asked():
                say("run %s was cancelled by stop; the child gets %d s to checkpoint"
                    % (run.id, s.bg_stop_grace_s))
                stopping = now + s.bg_stop_grace_s
            want = bool(demand) or stopping is not None
            if pause != want or now - beat >= s.bg_beat_s:
                seq, beat, pause = seq + 1, now, want
                with contextlib.suppress(OSError):
                    write_json(ctl, {"seq": seq, "pause": pause, "stale_s": s.bg_stale_s,
                                     "at": iso(), "reason": "stop asked" if stopping
                                     else demand_text(fg_gpu, fg_cpu, mine) if demand else None})
            if p is None:
                if stopping:
                    break
                if short and told != short:
                    gcover = sorted(i for i, v in cover["gpu"].items() if hl.strong(v))
                    say("background run %s: %s; the child waits for %s" % (
                        run.id, ("no gpu slot is free (%s)" % "; ".join(
                            sorted({hl.text(cover["gpu"][i]) for i in gcover}))
                                 if gcover else "every gpu slot is taken") if short[0] == "gpu"
                        else "fewer than %s are free" % lay.need_text({"cpu": need["cpu"]}),
                        "one" if short[0] == "gpu" else "them"))
                    told = short
                if got is not None:
                    told = None
                    try:
                        run.save(slots={p_: got[p_] for p_ in POOLS}, holding=list(POOLS),
                                 cores=got["cpu"], gpus=got["gpu"], state="bg")
                        job = Job(lay.cores_mask(L, got["cpu"]))
                        export_threads(env, job)
                        env["MACHINE_LOCK_GPUS"] = ",".join(
                            str(c) for c in lay.gpu_cards_of(L, got["gpu"]))
                        p = job.start(docker_cpu_env(cmd, bool(got["gpu"])),
                                      **run.popen_kw(env))
                    except OSError as err:
                        say("could not start the command: %s" % err)
                        code, how = 1, "failed"
                        alert(store, run, how, code, "could not start the command: %s" % err)
                        return 1
                    say("background run %s: child started (pid %d) in %s, on %s%s" % (
                        run.id, p.pid, lay.slots_text("gpu", got["gpu"]), cores_text(job.effective),
                        "" if first else " again"))
                    first = False
                    with contextlib.suppress(OSError):
                        run.save(child="running", **pin_fields(job))
            else:
                if run.usage.sample(job):
                    with contextlib.suppress(OSError):
                        run.save(**run.usage.fields())
                rc = p.poll()
                if rc is not None:
                    rc = child_code(rc)
                    run.usage.sample(job)
                    run.usage.job_ended()
                    job.end(kill=False)
                    job = p = None
                    unfreed = not bg_free(store, run)
                    if stopping:
                        break
                    if rc == PAUSE_EXIT:
                        say("background run %s: child paused (exit %d)" % (run.id, PAUSE_EXIT))
                        free_since = None if demand else time.monotonic()
                        with contextlib.suppress(OSError):
                            run.save(child="paused")
                        continue
                    code, how = rc, "done" if rc == 0 else "failed"
                    if rc:
                        alert(store, run, how, rc, "exit %d" % rc)
                    return rc
                if stopping and now >= stopping:
                    say("run %s: the child did not pause in time; its process tree is ended"
                        % run.id)
                    end_child(job, p)
                    job = p = None
                    break
                if (stopping is None and demand_since is not None
                        and now - demand_since >= s.bg_yield_s):
                    why = ("bg-yield-late: the child was still running %d s after %s asked it to "
                           "pause; its process tree is ended and its slot freed"
                           % (s.bg_yield_s, demand_text(fg_gpu, fg_cpu, mine)))
                    say("WARNING: background run %s: %s" % (run.id, why))
                    end_child(job, p)
                    job = p = None
                    code, how = 1, "yield-late"
                    alert(store, run, how, code, why)
                    return code
            time.sleep(1)
        return code
    except KeyboardInterrupt:
        code, how = 130, "interrupted"
        return 130
    finally:
        try:
            store.pause("BG_END")
            if job is not None:
                run.usage.sample(job)
                end_child(job, p)
            run.rec.update(run.usage.fields())
            write_json(ctl, {"seq": seq + 1, "pause": True, "stale_s": s.bg_stale_s, "at": iso(),
                             "reason": "the background run ended (%s)" % how})
        except BaseException:   # OSError, or a second Ctrl+C: the job ends with this process
            pass
        finally:
            settle(store, run, code, how, mutex)
