"""Stale runs: the lock asks a held run to stop once the sessions that booked it are gone and it
has held far past its guide, so a hung command whose agent has ended does not keep its slots
until the owner notices (docs/lock-api.md "Stale runs").

A run is stale when all of these hold (reason):
  - its record is held (not waiting, background or releasing) and its wrapper is live (a dead
    wrapper is Store.live_runs' to reap);
  - it names the agent hosts it was booked under (`hosts`, store.booking_hosts) and every one of
    them is surely gone (store.hosts_gone): none live, none that cannot be told;
  - it has held for longer than max(stale_scale x guide, guide + stale_min_s), the guide being
    the larger of its `expect_s` and its kind's `warn_s`;
  - it is not detached, unless INCLUDE_DETACHED.
A record with no `hosts` (the owner's own runs, runs booked before hosts were recorded) is never
stale.

reap(store, stop) is called by a booking (kitlib.lock.run.acquire) after its first look at the
queue and then every stale_check_s while it waits. It runs under the store's "reap" mutex, taken
without waiting (a booking that does not get it skips that pass), re-reads each record there and
writes the run's own stop request (by "lock", with `why`), never over one already there. The
run's wrapper then ends its own tree as for any stop (Slot.wait), so nothing here signals or ends
a process. The lock-window notice folder gets a stale notice (notice.stale). A wrapper that never
looks at its stop file (it is hung itself) is not escalated: its slots stay held until it dies,
when the dead-run reap frees them.
"""
import os

from kitlib.lock import notice
from kitlib.lock.store import hosts_gone, parse_iso, read_record, utc_now

BY = "lock"
# Whether a detached run (machine_lock.py run --detach, land.py --submit) can be stale. The owner
# decides (hub Q-1646): True takes them as any run; False never does.
INCLUDE_DETACHED = True


def guide_s(rec):
    """The larger of the record's expect_s and warn_s that is a positive number, else None."""
    vals = [v for v in (rec.get("expect_s"), rec.get("warn_s"))
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
    return max(vals) if vals else None


def due_after_s(rec, settings):
    """How long the run may hold before it can be stale, or None when it has no guide."""
    g = guide_s(rec)
    if g is None:
        return None
    return max(settings.stale_scale * g, g + settings.stale_min_s)


def reason(store, rec, now=None):
    """Why held run record rec is stale, as a line; None when it is not. Reads only: the
    snapshot shows it, and reap acts on it."""
    if not isinstance(rec, dict) or rec.get("state") != "held":
        return None
    if rec.get("background") or rec.get("releasing"):
        return None
    if rec.get("detached") and not INCLUDE_DETACHED:
        return None
    due = due_after_s(rec, store.settings)
    if due is None:
        return None
    try:
        held = ((now or utc_now()) - parse_iso(rec.get("held_since") or rec["since"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    if held <= due or not hosts_gone(rec):
        return None
    return ("its booking session has ended (agent host%s %s gone) and it has held %d min, past "
            "%d min (its guide %d min)" % (
                "" if len(rec["hosts"]) == 1 else "s",
                ", ".join("pid %s" % h[0] for h in rec["hosts"]),
                held // 60, due // 60, guide_s(rec) // 60))


def _records(store):
    try:
        names = sorted(os.listdir(store.path("active")))
    except FileNotFoundError:
        return
    for n in names:
        if n.endswith(".json"):
            d, _there = read_record(store.path("active", n))
            if isinstance(d, dict) and d.get("id") == n[:-5]:
                yield d


def reap(store, stop):
    """Asks every stale run to stop: stop(store, id, BY, None, why=reason) (run.request_stop),
    says so and writes the stale notice. Returns the ids it asked. A pass whose "reap" mutex is
    held elsewhere does nothing. Never raises an OSError."""
    asked = []
    try:
        with store.lock("reap", 0):
            for rec in _records(store):
                rid = rec["id"]
                if reason(store, rec) is None:
                    continue
                if os.path.exists(store.path("active", rid + ".stop")):
                    continue
                if not store.run_live(rec):
                    continue
                again, _there = read_record(store.path("active", rid + ".json"))
                if not isinstance(again, dict) or (again.get("pid"), again.get("ctime")) != (
                        rec.get("pid"), rec.get("ctime")):
                    continue
                why = reason(store, again)
                if why is None or not stop(store, rid, BY, None, why=why):
                    continue
                asked.append(rid)
                store.say("the lock ends stale run %s (%s/%s, %s): %s; it asked the run to stop, "
                          "and its wrapper ends its process tree" % (
                              rid, again.get("repo"), again.get("slug"), again.get("kind"), why))
                notice.stale(store, again, why)
    except OSError:
        pass
    return asked
