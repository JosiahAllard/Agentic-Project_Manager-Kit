"""The queue: one first-come list of every run that waits for or holds slots.

queue.json is {"ticket": the last ticket given, "entries": [entry]}; an entry is
{id, ticket, state: waiting | held | bg, pid, ctime, repo, slug, kind, need {cpu, gpu},
expect_s, since, held_at, cores, gpus, hold, avoid, prefer, parked, blocked_by, clear_at,
avoid_after, parent, resource, priority, moved, within}. It is read and written only under the store's
"queue" mutex. moved is set only on a waiting entry the owner moved to the top (move_to_top):
{by, name, at, seq}. resource and priority are set only by a library caller of kitlib.lock.run.book
(None otherwise; an entry written before they existed has neither and loads as such): resource
names an exclusive resource the run holds for its whole life, priority "land" plans it ahead of
ordinary waiting runs (plan). within is set only on the entry of a run booked with within
(kitlib.lock.run.book, the CLI's --within): {pool: the only slots it may take}; an entry without
it (or written before it existed) may take any slot.

Planning (plan) gives each waiting entry, in queue order, the earliest time what it needs is free
for its whole expected time, around what holders and runs planned ahead of it take. A run may
start ahead of its turn only when it ends before every run ahead of it is planned to start
(backfill), but for a run ahead whose start its resource alone put off: that one keeps out only
the runs naming its resource. How long a holder still holds is holder_end.
"""
import contextlib
import datetime
import json
import math
import os
import time

from kitlib.lock import layout as lay
from kitlib.lock.store import local_text, write_json

POOLS = lay.POOLS
PICK = {"cpu": "cores", "gpu": "gpus"}
STATES = ("waiting", "held", "bg")
PRIORITIES = ("land",)          # an entry's priority: None (ordinary) or one of these


class QueueUnreadable(OSError):
    """queue.json exists but could not be opened or parsed: the queue is not known."""


def load(store):
    """{'ticket', 'entries'}; entries in any other shape dropped. No queue.json is an empty
    queue. One that exists but cannot be read (another process has it open, or it is not a
    queue) is read again for settings.queue_read_s, then QueueUnreadable: never taken for an
    empty queue, which the next save would write over every entry and the ticket."""
    path = store.path("queue.json")
    end = time.monotonic() + store.settings.queue_read_s
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or not isinstance(d.get("entries", []), list):
                raise ValueError("not a queue")
            break
        except FileNotFoundError:
            d = {}
            break
        except (OSError, ValueError) as err:
            if time.monotonic() >= end:
                raise QueueUnreadable("queue unreadable: %s could not be read (%s); nothing is "
                                      "planned on it or written over it" % (path, err)) from err
            time.sleep(0.1)
    out = [e for e in d.get("entries") or []
           if isinstance(e, dict) and isinstance(e.get("id"), str)
           and e.get("state") in STATES and isinstance(e.get("need"), dict)]
    tickets = [t for t in [d.get("ticket")] + [e.get("ticket") for e in out] if type(t) is int]
    return {"ticket": max(tickets or [0]), "entries": out}


def save(store, q):
    """Under the queue mutex. A reader with queue.json open is waited out for
    settings.queue_write_s, so an instant's read never fails a join."""
    write_json(store.path("queue.json"), q, store.settings.queue_write_s)
    store.bump()


def live(store):
    """The queue as it is now, dead callers' entries left out (nothing written: status reads)."""
    q = load(store)
    q["entries"] = [e for e in q["entries"] if not store.run_dead(e)]
    return q


def entry_of(q, rid):
    return next((e for e in q["entries"] if e["id"] == rid), None)


def entry_holds(L, e, pool):
    """The slots of `pool` entry e holds (cores for cpu, gpu slots for gpu)."""
    n = lay.size(L, pool)
    return [i for i in e.get(PICK[pool]) or [] if type(i) is int and 0 <= i < n]


def entry_need(e, pool):
    n = (e.get("need") or {}).get(pool)
    return n if type(n) is int and n > 0 else 0


def entry_slots(e, key, pool):
    """The slots of `pool` that entry e's `key` names (avoid: what holds not its own cover over
    its time; avoid_after: the same once what parks it ends; prefer: its own hold's)."""
    return {i for i in ((e.get(key) or {}).get(pool) or []) if type(i) is int}


def entry_within(e, pool):
    """The only slots of `pool` entry e may take (its run's --within), or None: any slot. An entry
    without within, or with no slots of `pool` in it, is not confined (book() never writes an
    empty one)."""
    w = entry_slots(e, "within", pool) if isinstance(e.get("within"), dict) else set()
    return w or None


def entry_resource(e):
    """The exclusive resource entry e names, or None (an entry from before resources has none)."""
    r = e.get("resource")
    return r if isinstance(r, str) and r else None


def entry_priority(e):
    """Entry e's priority, one of PRIORITIES, or None: ordinary (an unknown value counts as
    ordinary; book() refuses one before it is written)."""
    p = e.get("priority")
    return p if p in PRIORITIES else None


def resource_holders(q, e):
    """The entries of q that hold (state held or bg) the resource entry e names, but e itself and
    e's parent: a nested run's parent holding it holds it for the run too."""
    r = entry_resource(e)
    if r is None:
        return []
    return [x for x in q["entries"] if x["state"] in ("held", "bg") and entry_resource(x) == r
            and x["id"] not in (e["id"], e.get("parent"))]


def entry_expect(e):
    try:
        return max(1.0, float(e.get("expect_s")))
    except (TypeError, ValueError):
        return 60.0


def holder_end(held_at, expect_s, now):
    """When a holder that took its slots at held_at (epoch s) and expects expect_s is planned to
    let them go, seen at `now`. Before its expected end: that end. Past it (an overrun), or with
    no held_at: now + half its expected time, re-checked on every pass, so it keeps
    sliding ahead while the holder runs on and runs that fit before it can still backfill. The
    one place an overrun is decided."""
    try:
        exp = max(1.0, float(expect_s))
    except (TypeError, ValueError):
        exp = 60.0
    try:
        end = float(held_at) + exp
    except (TypeError, ValueError):
        end = None
    if end is not None and end > now:
        return end
    return now + max(1.0, exp / 2)


def run_text(r):
    return "%s/%s (%s, run %s, pid %s)" % (r.get("repo"), r.get("slug"),
                                           r.get("kind") or "background", r["id"], r.get("pid"))


def held_text(L, e):
    """'cpu slots 2-3 + gpu slot 0'"""
    return " + ".join(lay.slots_text(p, entry_holds(L, e, p)) for p in POOLS
                      if entry_holds(L, e, p))


def holders(L, q, me=None):
    """{pool: {slot: entry}} of what q's entries (but `me`) hold."""
    out = {p: {} for p in POOLS}
    for e in q["entries"]:
        if e["id"] != me:
            for p in POOLS:
                for i in entry_holds(L, e, p):
                    out[p][i] = e
    return out


def join(q, rec, need, state="waiting"):
    """Adds run record rec's entry at the back of q (the next ticket) and returns it."""
    q["ticket"] += 1
    e = {"id": rec["id"], "ticket": q["ticket"], "state": state, "pid": rec["pid"],
         "ctime": rec["ctime"], "repo": rec.get("repo"), "slug": rec.get("slug"),
         "kind": rec.get("kind") or "background", "need": dict(need),
         "expect_s": rec.get("expect_s"), "since": rec.get("since"), "held_at": None,
         "cores": [], "gpus": [], "hold": rec.get("hold"), "avoid": {}, "prefer": {},
         "parked": False, "blocked_by": None, "clear_at": None, "avoid_after": {},
         "parent": rec.get("parent"), "resource": rec.get("resource"),
         "priority": rec.get("priority")}
    q["entries"].append(e)
    return e


def leave(store, rid, wait_s=60):
    """Removes run rid's entry; False when the queue mutex was not had (a dead-caller prune drops
    the entry once the wrapper's run mutex is released)."""
    try:
        with store.lock("queue", wait_s):
            q = load(store)
            n = len(q["entries"])
            q["entries"] = [e for e in q["entries"] if e["id"] != rid]
            if len(q["entries"]) != n:
                save(store, q)
        return True
    except OSError:
        return False


def give_back(store, rid):
    """A run that just took its slots waits again (its entry keeps its ticket); False when the
    queue mutex was not had (it keeps them)."""
    try:
        with store.lock("queue", store.settings.queue_lock_s):
            q = load(store)
            e = entry_of(q, rid)
            if e is None:
                return False
            e.update(state="waiting", cores=[], gpus=[], held_at=None)
            save(store, q)
            return True
    except OSError:
        return False


def prune(store, L, q, me=None, quiet=False):
    """Drops dead wrappers' entries from q, in place, and says what each held. A run that settled
    (its ended record exists) but could not remove its entry is dropped quietly."""
    keep = []
    for e in q["entries"]:
        if e["id"] == me or not store.run_dead(e):
            keep.append(e)
        elif quiet or os.path.exists(store.path("ended", e["id"] + ".json")):
            continue
        elif held_text(L, e):
            store.say("the %s's last holder (run %s) died holding it; it is free%s"
                      % (held_text(L, e), e["id"], "; so is resource %s" % entry_resource(e)
                         if entry_resource(e) else ""))
        elif entry_resource(e) and e["state"] in ("held", "bg"):
            store.say("run %s died holding resource %s; it is free"
                      % (e["id"], entry_resource(e)))
        elif e["state"] == "waiting":
            store.say("run %s's caller died while it waited; its queue entry is dropped" % e["id"])
    q["entries"] = keep


def moved_seq(e):
    """The order of entry e's move to the top (later moves are higher), or 0 when never moved."""
    s = (e.get("moved") or {}).get("seq") if isinstance(e.get("moved"), dict) else None
    return s if type(s) is int and s > 0 else 0


def move_to_top(q, e, by, name, at):
    """Marks waiting entry e of q moved to the top by `by` (who; name: the caller's name) at ISO
    time `at`, after every earlier move, and returns the mark. Only the order changes: e keeps its
    ticket, and no entry holding slots gives them up (plan)."""
    m = {"by": by, "name": name, "at": at,
         "seq": max([moved_seq(x) for x in q["entries"]] + [0]) + 1}
    e["moved"] = m
    return m


def own_turn(e):
    """An entry planned first and never held back: one claiming its own hold (its slots are its
    own) or one nested in a held run (its parent holds slots and waits for it; held back behind
    a run planned for after that parent ends, it would wait for ever)."""
    return bool(e.get("hold") or e.get("parent"))


def plan(L, q, now=None):
    """({run id: {position, start (epoch s or None), cores, gpus, now, backfill}} for q's
    waiting entries, {pool: slots background runs hold}).

    Order: own_turn entries, then entries the owner moved to the top (the latest move first),
    then priority "land" entries (ticket order), then a gpu entry whose gpu slots are free, then
    ticket order. A moved entry is never planned ahead of an own_turn one: a nested run held back
    behind it would keep its parent's slots for ever. Each gets the earliest of now, a holder's
    planned end (holder_end), or a planned run's end or start, at which what it needs is free for
    its whole expected time, never a slot its `avoid` names or one outside its `within`
    (entry_within: it waits for those even while others are free), and no other entry naming its
    resource holds it or is planned to over that time (its parent's hold of it is its own). It
    picks its own hold's slots first, then those no background run holds (they yield), then the
    lowest. It may not start before a run planned ahead of it and still be running when that one
    starts (backfill), unless it is own_turn, or that one's start was put off by its resource
    alone (its slots were free at an earlier time; it is not parked): such a start bars only the
    entries naming that resource, and the rest plan around its slots. A parked entry (holds not its own leave it too
    little now) is planned from clear_at, around avoid_after, so no run behind it passes it."""
    now = time.time() if now is None else now
    sizes = {p: lay.size(L, p) for p in POOLS}
    # ivs: (start, end, {pool: slots it keeps from others}, resource or None, entry id). A
    # background run's slots yield (soft), but a resource it names does not.
    ivs, soft = [], {p: set() for p in POOLS}
    for e in q["entries"]:
        res = {p: set(entry_holds(L, e, p)) for p in POOLS}
        rsc = entry_resource(e) if e["state"] in ("held", "bg") else None
        if e["state"] == "bg":
            for p in POOLS:
                soft[p] |= res[p]
            res = {p: set() for p in POOLS}
        if any(res.values()) or rsc:
            ivs.append((-math.inf, holder_end(e.get("held_at"), entry_expect(e), now), res, rsc,
                        e["id"]))
    hard_gpu = set().union(*[iv[2]["gpu"] for iv in ivs]) if ivs else set()
    waiting = [e for e in q["entries"] if e["state"] == "waiting"]

    def order(e):
        if own_turn(e):
            return (0, 0, e.get("ticket") or 0)
        if moved_seq(e):
            return (1, -moved_seq(e), e.get("ticket") or 0)
        if entry_priority(e) == "land":
            return (2, 0, e.get("ticket") or 0)
        g = entry_need(e, "gpu")
        free = sizes["gpu"] - len(hard_gpu | entry_slots(e, "avoid", "gpu"))
        return (3 if g and g <= free else 4, 0, e.get("ticket") or 0)

    out, starts = {}, []
    for pos, e in enumerate(sorted(waiting, key=order), 1):
        info = out[e["id"]] = {"position": pos, "start": None, "cores": [], "gpus": [],
                               "now": False, "backfill": False}
        exp, own = entry_expect(e), own_turn(e)
        rsc, parent = entry_resource(e), e.get("parent")
        t0, key = now, "avoid"
        if e.get("parked"):
            with contextlib.suppress(TypeError, ValueError):
                t0 = max(now, float(e.get("clear_at")))
            key = "avoid_after"
        avoid = {p: entry_slots(e, key, p) for p in POOLS}
        prefer = {p: entry_slots(e, "prefer", p) for p in POOLS}
        within = {p: entry_within(e, p) for p in POOLS}
        times = {t0} | {iv[1] for iv in ivs if iv[1] > t0} | {s for s in starts if s > t0}
        held_by_rsc = False         # an earlier time had its slots free and only its resource not
        for t in sorted(times):
            if not own and any(t < s < t + exp for s in starts):
                continue
            rsc_busy = rsc and any(r == rsc and o != parent and s0 < t + exp and s1 > t
                                   for s0, s1, _sl, r, o in ivs)
            busy = {p: set() for p in POOLS}
            for s0, s1, r, _rsc, _o in ivs:
                if s0 < t + exp and s1 > t:
                    for p in POOLS:
                        busy[p] |= r[p]
            pick = {}
            for p in POOLS:
                free = [i for i in range(sizes[p]) if i not in busy[p] and i not in avoid[p]
                        and (within[p] is None or i in within[p])]
                if len(free) < entry_need(e, p):
                    break
                pick[p] = sorted(sorted(free, key=lambda i: (i not in prefer[p], i in soft[p], i))
                                 [:entry_need(e, p)])
            else:
                if rsc_busy:
                    held_by_rsc = True
                    continue
                info.update(start=t, cores=pick["cpu"], gpus=pick["gpu"], now=t <= now,
                            backfill=not own and any(t < s for s in starts))
                ivs.append((t, t + exp, {p: set(pick[p]) for p in POOLS}, rsc, e["id"]))
                break
        # A start its resource put off (its slots were free earlier) is no backfill barrier: its
        # slots are planned (ivs), so a run behind it that fits around them starts now; one
        # naming its resource is still kept out by ivs. A parked entry stays a barrier.
        if info["start"] is not None:
            if not held_by_rsc or e.get("parked"):
                starts.append(info["start"])
        elif e.get("parked"):
            starts.append(t0)
    return out, soft


def busy_text(L, q, e, planned):
    """Why entry e does not start now: the runs holding the resource it names, the holders of a
    pool it is short of, what holds not its own cover, then the runs planned ahead of it."""
    held = holders(L, q, e["id"])
    parts = ["resource %s is held by %s" % (entry_resource(e), run_text(x))
             for x in resource_holders(q, e)]
    for p in POOLS:
        if not entry_need(e, p):
            continue
        avoid, within = entry_slots(e, "avoid", p), entry_within(e, p)
        free = [i for i in range(lay.size(L, p)) if i not in held[p] and i not in avoid
                and (within is None or i in within)]
        if len(free) >= entry_need(e, p):
            continue
        by = {}
        for i, x in sorted(held[p].items()):
            if within is None or i in within:
                by.setdefault(x["id"], (x, []))[1].append(i)
        parts += ["%s %s held by %s" % (lay.slots_text(p, sl), "is" if len(sl) == 1 else "are",
                                        run_text(x)) for x, sl in by.values()]
        if avoid & (within or avoid):
            parts.append("a hold covers %s" % lay.slots_text(p, sorted(avoid & (within or avoid))))
        if within is not None:
            parts.append("it runs only within %s (--within)" % lay.slots_text(p, within))
    me = planned.get(e["id"]) or {}
    ahead = [x for x in q["entries"] if x["id"] in planned and x["id"] != e["id"]
             and planned[x["id"]]["position"] < me.get("position", 0)]
    if ahead:
        parts.append("%s ahead of it in the queue: %s" % (
            "a run is" if len(ahead) == 1 else "%d runs are" % len(ahead),
            ", ".join("%s, needing %s" % (run_text(x), lay.need_text(x["need"])) for x in ahead)))
    if not parts:
        parts.append("it needs %s and a background run holds some of it" % lay.need_text(e["need"]))
    return "; ".join(parts)


def short_pools(L, q, e):
    """The pools foreground holders and holds leave entry e too little of now (counting only the
    slots its within allows)."""
    held = holders(L, q, e["id"])
    out = []
    for p in POOLS:
        fg = {i for i, x in held[p].items() if x["state"] != "bg"} | entry_slots(e, "avoid", p)
        within = entry_within(e, p)
        room = set(range(lay.size(L, p))) if within is None else within & set(range(lay.size(L, p)))
        if entry_need(e, p) and len(room - fg) < entry_need(e, p):
            out.append(p)
    return out


def est_text(info):
    """When a planned entry is expected to start, local time."""
    if not info or info.get("start") is None:
        return "not yet planned"
    return local_text(datetime.datetime.fromtimestamp(info["start"], datetime.timezone.utc))
