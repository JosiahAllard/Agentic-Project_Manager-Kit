"""gate.py stop: one gate run ended by its machine-lock run id (docs/gate.md "Stopping a gate").

The run id is the one the gate's held line prints (`run r-...`) and `machine_lock.py status`
shows: the lock's id for the run, the only name a gate run has that no other run shares. The
result record's run_id is another id, and a gate has no pid of its own to give. What the run is
comes from its lock record alone, and nothing is ended that the record does not name:

  no record, or not a gate   refused: `gate` is set only by the gate's own bookings
  a landing's                refused, live or ended, however it shows: the landing's own lock run
                             (priority, resource, note), a run under a landing's process (the
                             record's ancestors, parent), a running land record of its process, or
                             the holder of the landing lock of -C's repository. A landing's lock
                             is never broken here, and one that cannot be read is held
  another session's          the lock's own rule (api.end_run): the owner, or the agent session
                             the record's ancestors show it belongs to; refused otherwise
  ended, or its process gone ALREADY GONE, exit 0, what is left of it settled: a dead run's lock
                             record goes the way any booking moves it (store.live_runs, prune),
                             its running result record is finished `error` (finish_dead)

A live run is stopped the way the lock stops any run (api.end_run): its own process ends its tree
(kitlib.lock.run.Slot.wait), starts no further step, releases its lock as `cancelled` and writes
its result record, `stopped` (kitlib.gate.runner.stopped_outcome). Only a run that has not ended
within settings.stop_s (its process is hung itself, not polling) is ended from here, and only with
process.terminate_verified, which ends a pid only if its creation time is still the record's,
judged on the handle it kills through: never by name, never a recycled pid. Its job closes with
it, which ends the tree; a process that left the job is named and never ended. The lock's reap
then frees the run, and the result record is finished `stopped` from here (result.finish_stopped).

`stop_run` is the whole of it. It says nothing but through `say`, and never exits. Exit codes: OK
(stopped, or already gone), FAILED (it could not be ended), USAGE (no run id), REFUSED (nothing was
touched); G.STOPPED is the stopped gate's own exit, not this one's.
"""
import contextlib
import time

from kitlib import gate as G
from kitlib.gate import result
from kitlib.lock import api
from kitlib.proc import process
from kitlib.records import landing

RESULT_WAIT_S = 10.0    # once the lock has let a stopped run go: for its process to write its record
FORCE_WAIT_S = 10.0     # once its process is ended: for it and its tree to be gone
POLL_S = 0.25


def _quiet(line):
    pass


def _refused(text):
    return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was stopped' % text], stopped=False)


def _name(rec):
    return '%s/%s' % (rec.get('repo'), rec.get('slug'))


def _until(done, wait_s):
    """True once done() is true, False when `wait_s` seconds passed first."""
    end = time.monotonic() + wait_s
    while not done():
        if time.monotonic() >= end:
            return False
        time.sleep(POLL_S)
    return True


def is_landing(rec):
    """True for a landing's own lock run: only kitlib.gate.land.Lander.book_slot books these."""
    return (rec.get('priority') == 'land' or bool(rec.get('resource'))
            or str(rec.get('note') or '').startswith('landing:'))


def _identities(rec):
    """{(pid, ctime)} of the run's process and of every process above it, from its lock record."""
    pairs = [(rec.get('pid'), rec.get('ctime'))]
    for a in rec.get('ancestors') or ():
        if isinstance(a, (list, tuple)) and len(a) == 2:
            pairs.append((a[0], a[1]))
    return set(pairs)


def _holder_reason(root, me):
    """Why the landing lock of `root`'s repository rules the run out, or None. No repository
    there: nothing to read. A lock that cannot be read is held, by nobody it can name."""
    try:
        lock = landing.LandingLock(root)
    except landing.LockError:
        return None
    try:
        holder = lock.read()
    except landing.LockError as e:
        return 'the landing lock cannot be read, so whose it is cannot be told: %s' % e
    if holder and (holder.get('pid'), holder.get('pid_start')) in me:
        return 'it holds the landing lock (%s)' % lock.path
    return None


def landing_reason(rec, store, root=None):
    """Why the run of lock record `rec` is a landing's, or None: the module docstring's list."""
    if is_landing(rec):
        return 'it is a landing\'s gate (%s)' % (rec.get('note') or rec.get('priority'))
    me = _identities(rec)
    try:
        runs = store.live_runs()
    except OSError as e:
        return 'the live runs cannot be read, so a landing above it cannot be ruled out: %s' % e
    for d in runs:
        if is_landing(d) and ((d.get('pid'), d.get('ctime')) in me
                              or d.get('id') == rec.get('parent')):
            return 'it runs under landing run %s' % d.get('id')
    for _, r in result.machine_records('land'):
        if r.get('verdict') == result.RUNNING and (r.get('pid'), r.get('ctime')) in me:
            return 'it runs under the landing of %s' % r.get('branch')
    return _holder_reason(root, me) if root else None


def _record_of(rec):
    """(path, result record) of the gate the process of lock record `rec` is running or ran, or
    None: its result record names that process by the same (pid, ctime) (a rerun, a daily full
    run and a machine with no result folder write none). The latest, when several do."""
    hits = [(p, r) for p, r in result.machine_records('gate')
            if r.get('pid') == rec.get('pid') and r.get('ctime') == rec.get('ctime')]
    hits.sort(key=lambda h: str(h[1].get('started_at')))
    return hits[-1] if hits else None


def _final(found, stopped, wait_s=0.0):
    """The record of `found` (_record_of) once it is settled, or None when it is not this run's
    any more: waited for up to `wait_s` while it is running, and a running one whose process is
    gone is finished here: `stopped` (`stopped`) or `error` (finish_dead)."""
    path, rec = found
    end = time.monotonic() + wait_s
    while True:
        now, _ = result.read_path(path)
        if not isinstance(now, dict) or now.get('run_id') != rec.get('run_id'):
            return None
        if now.get('verdict') != result.RUNNING:
            return now
        if result.running_state(now) == 'dead':
            if stopped:
                return result.finish_stopped(now, path, 'STOPPED: gate.py stop ended the gate '
                                             'before its verdict; no gate-OK record')
            return result.finish_dead(None, now, path=path)
        if time.monotonic() >= end:
            return now
        time.sleep(POLL_S)


def _record_line(found, final):
    if found is None:
        return 'result record: none names this run'
    if final is None:
        return 'result record: %s now holds another run\'s' % found[0]
    if final.get('verdict') == result.RUNNING:
        return 'result record: still running, its process has not finished it: %s' % found[0]
    return 'result record: %s: %s' % (final.get('verdict'), found[0])


def _reap(rid, store):
    """The lock's own reap of a run whose process is gone (what any booking does: it moves only
    records of processes surely gone, and never a live run's), and a line saying where it left
    run `rid`."""
    with contextlib.suppress(OSError):
        store.live_runs(prune=True)
    ended = store.read('ended/%s.json' % rid)
    if ended is None:
        return 'lock: its record is still in active/; the next booking moves it'
    return 'lock: the run is ended (%s) and its slots are free' % ended.get('how')


def _by_stop(ended):
    """True when ended lock record `ended` is a run a stop ended: `cancelled` in its job, or
    while it waited for slots, which the lock ends `not acquired`, code 1 (no other refusal
    has that code) with the stop request's `stopped_by` on the record."""
    return ended.get('how') == 'cancelled' or (
        ended.get('how') == 'not acquired' and ended.get('code') == 1
        and bool(ended.get('stopped_by')))


def _gone(rid, rec, ended, found):
    """OK for a run that had already ended when it was asked about."""
    final = _final(found, _by_stop(ended)) if found else None
    return G.Outcome(G.OK, ['ALREADY GONE: gate run %s (%s) had ended (%s, code %s); nothing was '
                            'stopped' % (rid, _name(rec), ended.get('how'), ended.get('code')),
                            _record_line(found, final)], stopped=False, result=final)


def _died(rid, rec, found, store):
    """OK for a run whose process is gone with its record still active: settled, no stop claimed."""
    line = _reap(rid, store)
    final = _final(found, False) if found else None
    return G.Outcome(G.OK, ['ALREADY GONE: gate run %s (%s): its process had died; nothing was '
                            'stopped' % (rid, _name(rec)), line, _record_line(found, final)],
                     stopped=False, result=final)


def _settled(rid, rec, found, store):
    """The outcome once the run is not live: its own process ended it, or it died."""
    ended = store.read('ended/%s.json' % rid)
    if ended is None:
        return _died(rid, rec, found, store)
    if not _by_stop(ended):
        final = _final(found, False, RESULT_WAIT_S) if found else None
        return G.Outcome(G.OK, ['ALREADY GONE: gate run %s (%s) ended by itself (%s, code %s) '
                                'before the stop reached it; nothing was stopped' % (
                                    rid, _name(rec), ended.get('how'), ended.get('code')),
                                _record_line(found, final)], stopped=False, result=final)
    final = _final(found, True, RESULT_WAIT_S) if found else None
    return G.Outcome(G.OK, ['STOPPED: gate run %s (%s): its process tree is ended and its lock '
                            'is free' % (rid, _name(rec)), _record_line(found, final)],
                     stopped=True, result=final)


def _force(rid, rec, found, store, say):
    """End the wrapper of a run that did not end on its stop request. Only if its lock record
    still names the process first read, still a gate and no landing, and only if that process is
    still that one (terminate_verified)."""
    pid, ctime = rec.get('pid'), rec.get('ctime')
    now = store.read('active/%s.json' % rid)
    if now is None:
        return _settled(rid, rec, found, store)
    if ((now.get('pid'), now.get('ctime')) != (pid, ctime) or not now.get('gate')
            or is_landing(now)):
        return G.Outcome(G.FAILED, ['STOP FAILED: gate run %s: its lock record changed while it '
                                    'was being stopped; nothing was ended' % rid], stopped=False)
    if not isinstance(ctime, int):
        return G.Outcome(G.FAILED, ['STOP FAILED: gate run %s did not end on its stop request, and '
                                    'its record has no creation time to verify pid %s by; nothing '
                                    'was ended' % (rid, pid)], stopped=False)
    say('gate run %s did not end on its stop request; ending its process' % rid)
    left = process.descendants(pid, ctime)
    how = process.terminate_verified(pid, ctime)
    if how in ('gone', 'reused'):           # not that process any more: nothing of it to end
        return _settled(rid, rec, found, store)
    if how != 'killed':
        return G.Outcome(G.FAILED, ['STOP FAILED: gate run %s: pid %s cannot be ended (%s); its '
                                    'stop request stays' % (rid, pid, how)], stopped=False)
    if not _until(lambda: not process.alive(pid, ctime), FORCE_WAIT_S):
        return G.Outcome(G.FAILED, ['STOP FAILED: gate run %s: pid %s was ended and has not gone '
                                    'within %d s' % (rid, pid, FORCE_WAIT_S)], stopped=False)
    _until(lambda: not any(process.alive(p, c) for p, c in left), FORCE_WAIT_S)
    line = _reap(rid, store)
    final = _final(found, True) if found else None
    stayed = ['NOT ENDED: pid %s, started under the run, is outside its job' % p
              for p, c in left if process.alive(p, c)]
    return G.Outcome(G.OK, ['STOPPED: gate run %s (%s) did not end on its stop request: its '
                            'process %s is ended and its job with it' % (rid, _name(rec), pid),
                            line, _record_line(found, final)] + stayed,
                     stopped=True, forced=True, result=final)


def _live(rid, store):
    d = api.run_record(rid, store)
    return isinstance(d, dict) and (d.get('ok') is False or bool(d.get('live')))


def _stop_live(rid, rec, store, actor, say):
    found = _record_of(rec)                 # before the run can end and its record move on
    asked = api.end_run(actor, rid, store)
    if not asked.get('ok'):
        err = asked.get('error') or {}
        if err.get('code') == 'not_found':
            return _settled(rid, rec, found, store)
        return _refused(err.get('message') or 'the lock refused the stop')
    wait_s = store.settings.stop_s
    say('stop asked of gate run %s (%s); waiting up to %d s for it to end' % (
        rid, _name(rec), wait_s))
    if _until(lambda: not _live(rid, store), wait_s):
        return _settled(rid, rec, found, store)
    return _force(rid, rec, found, store, say)


def stop_run(rid, store, root=None, actor=None, say=None):
    """Stop gate run `rid` of the lock `store` (the module docstring): an Outcome, with the facts
    `stopped` (bool) and, past a refusal, `result` (its result record, or None). `root`: a checkout
    whose repository's landing lock is read too. `actor`: as for api.end_run."""
    say = say or _quiet
    got = api.run_record(rid, store)
    if isinstance(got, dict) and got.get('ok') is False:
        err = got.get('error') or {}
        if err.get('code') == 'invalid':
            return G.Outcome(G.USAGE, ['USAGE: %s; a run id is the r-... one on the gate\'s held '
                                       'line and in machine_lock.py status' % err.get('message')],
                             stopped=False)
        return _refused('the lock cannot read run %s: %s' % (rid, err.get('message')))
    active = store.read('active/%s.json' % rid)
    rec = got or active
    if rec is None:
        return _refused('the lock has no record of run %s, so nothing shows it is a gate' % rid)
    if not rec.get('gate'):
        return _refused('run %s (%s, %s) is not a gate run; gate.py stop ends gate runs only' % (
            rid, rec.get('kind'), _name(rec)))
    why = landing_reason(rec, store, root)
    if why:
        return _refused('gate.py stop never touches a landing, and run %s: %s' % (rid, why))
    if got is not None and got.get('live'):
        return _stop_live(rid, rec, store, actor, say)
    found = _record_of(rec)
    if got is not None:
        return _gone(rid, rec, got, found)
    return _died(rid, rec, found, store)
