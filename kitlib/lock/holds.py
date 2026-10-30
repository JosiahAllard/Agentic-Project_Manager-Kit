"""Holds: one record for what the owner reserves, what an agent books and what game mode
takes. holds/<id>.json, read and written under the store's "holds" mutex.

    {id, owner: owner | agent | game, scope, job, start, end, note, created, by, session,
     ancestors}

Every hold names its job: an owner hold's is OWNER, an agent hold's its repo/slug, a game
hold's the game. A record from before holds named a job has it read from those. A hold file that cannot
be read, or is not a hold, fails closed (HoldsUnreadable): it is never read as absent.

owner holds (the old reservations, "quiet hours"), made by the owner only:
    scope whole   every slot, for the owner's own runs (see admits): only a run the
                  owner started may run inside it (run --hold), taking every core.
    scope half    the division's half_cpu_slots and half_gpu_slots, picked when it starts
                  (around runs then live) and recorded as slots and slots_at.
    scope slots   the slots it names.
game holds (kitlib.lock.game): scope domain, the cpu slots of one L3 domain; gpu none.
agent holds (the old bookings): the slots a run of {repo, slug, kind, gpu_share} needs,
    picked when it is made; claimed by that run (run --hold); lapse hold_grace_s after their start
    unclaimed; end "claimed" once their run holds its slots.

Owner and game holds are strong: they win over agent holds (an agent hold loses the slots they
cover, for its whole window) and a run they leave too little parks, then exits 11. Agent holds
leave a run too little: it parks, then exits 6 (--no-wait) or 8.
"""
import contextlib
import datetime
import json
import os
import time

from kitlib.lock import kinds as kinds_mod
from kitlib.lock import layout as lay
from kitlib.lock.store import (EXIT_BUSY, EXIT_HOLD, EXIT_RESERVED, EXIT_USAGE, LockError,
                               iso, local_ok, local_text, new_id, parse_iso, public, read_json,
                               utc_now, write_json, remove, ID_RE)

POOLS = lay.POOLS
OWNERS = ("owner", "agent", "game")
SCOPES = {"owner": ("whole", "half", "slots"), "agent": ("slots",), "game": ("domain",)}
PICK_COST = {"strong": 4, "run": 3, "agent": 2, "background": 1}
STR_KEYS = ("note", "repo", "slug", "kind", "by")
EDITABLE = {"owner": ("start", "end", "note"), "agent": ("end", "note"), "game": ("end", "note")}
OWNER_JOB = "OWNER"


class HoldsUnreadable(OSError):
    """A hold file exists but could not be read, or is not a hold: the holds are not known, so
    nothing is planned on them (a run waits or refuses)."""


def strong(h):
    return h.get("owner") in ("owner", "game")


# ------------------------------------------------------------------ jobs
def job_of(h):
    """The job hold h names: OWNER for an owner hold, repo/slug for an agent hold, the
    game for a game hold; None when it names none."""
    if h.get("owner") == "owner":
        return OWNER_JOB
    if h.get("owner") == "agent":
        return "%s/%s" % (h["repo"], h["slug"]) if h.get("repo") and h.get("slug") else None
    g = h.get("game") if isinstance(h.get("game"), dict) else {}
    j = g.get("name") or g.get("exe")
    return j if isinstance(j, str) and j else None


def admits(h, rec):
    """(True, None) when run record rec may run inside owner whole hold h, else (False, why).
    A whole hold is the owner's (job OWNER), and only a run the owner started may use it
    (rec role owner, from its process tree: kitlib.lock.store.tree_role). The one place this is
    decided."""
    if (rec or {}).get("role") == "owner":
        return True, None
    return False, ("%s is the owner's (job %s): only the owner's own runs may run inside it; an "
                   "agent books an agent hold (hold agent)" % (text(h), OWNER_JOB))


# ------------------------------------------------------------------ reading
def _path(store, hid):
    return store.path("holds", hid + ".json")


def _checked(d, n):
    """d, the hold record read from file n, with job, _start and _end; ValueError when it is not
    one."""
    try:
        if not isinstance(d, dict) or not isinstance(d["id"], str) or d["id"] + ".json" != n:
            raise ValueError("its id is not its file name")
        if d.get("owner") not in OWNERS or d.get("scope") not in SCOPES[d["owner"]]:
            raise ValueError("no such owner or scope")
        if any(d.get(k) is not None and not isinstance(d[k], str) for k in STR_KEYS + ("job",)):
            raise ValueError("a text field is not text")
        job = job_of(d)
        if d.get("job") is None:
            d["job"] = job                  # made before holds named a job
        if not d["job"] or (d["owner"] == "owner") != (d["job"] == OWNER_JOB):
            raise ValueError("it names no job, or an owner hold's job is not OWNER")
        d["_start"], d["_end"] = parse_iso(d["start"]), parse_iso(d["end"])
        local_ok(d["_start"], d["_end"])
    except (TypeError, KeyError, AttributeError, OverflowError, OSError) as e:
        raise ValueError("%s: %s" % (type(e).__name__, e))
    return d


def _read_all(store):
    """The hold records, with _start and _end (datetimes). A file that cannot be read, or is not a
    hold (edited by hand, half written), is read again for settings.queue_read_s, as the queue
    is, then HoldsUnreadable: never skipped, since a hold read as absent frees its slots. So is
    a folder that cannot be listed (no folder: no holds)."""
    end_at = time.monotonic() + store.settings.queue_read_s
    while True:
        try:
            names = sorted(os.listdir(store.path("holds")))
            break
        except FileNotFoundError:
            return
        except OSError as err:
            if time.monotonic() >= end_at:
                raise HoldsUnreadable("holds unreadable: %s (%s); nothing is planned on the "
                                      "holds until it can be listed" % (store.path("holds"), err)
                                      ) from err
            time.sleep(0.1)
    for n in names:
        if not n.endswith(".json"):
            continue
        path = store.path("holds", n)
        end_at = time.monotonic() + store.settings.queue_read_s
        while True:
            try:
                with open(path, encoding="utf-8") as f:
                    d = _checked(json.load(f), n)
                break
            except FileNotFoundError:
                d = None                    # ended meanwhile
                break
            except (OSError, ValueError) as err:
                if time.monotonic() >= end_at:
                    raise HoldsUnreadable("holds unreadable: %s (%s); nothing is planned on the "
                                          "holds until it reads as a hold or is removed"
                                          % (path, err)) from err
                time.sleep(0.1)
        if d is not None:
            yield d


def expired_games(store):
    """The game holds past their end still in the holds folder (game.sync ends them itself)."""
    now = utc_now()
    return [d for d in _read_all(store) if d["owner"] == "game" and d["_end"] <= now]


def _end_expired(store, d):
    """Ends strong hold d as expired; a game hold's game gets its affinity back first when it
    still runs (kitlib.lock.game.unpin_expired), so a lapsed game hold never leaves it pinned."""
    extra = {}
    if d["owner"] == "game":
        from kitlib.lock import game
        extra = game.unpin_expired(store, d)
    end(store, d, "expired", **extra)


def recorded_slots(L, h):
    """The slots record h names ({pool: [slot]}), or None when it names none it can use. A slot
    past this layout's pools is dropped (the division changed under it)."""
    sl = h.get("slots")
    if not isinstance(sl, dict):
        return None
    out = {}
    for p in POOLS:
        v = sl.get(p, [])
        if not isinstance(v, list):
            return None
        out[p] = sorted({i for i in v if type(i) is int and 0 <= i < lay.size(L, p)})
    return out


def claim_alive(store, h):
    """Is the run that claimed agent hold h live (store.run_live: its pid and creation time, or
    its run mutex when they cannot be checked)? False when there is no claim or it is malformed."""
    c = h.get("claim")
    try:
        return bool(c) and int(c["pid"]) > 0 and store.run_live(c)
    except (TypeError, KeyError, ValueError, AttributeError):
        return False


def end(store, h, how, **extra):
    """Moves hold h to the ended folder (how, ended and `extra` added). Call it under the holds
    mutex, so a half hold's first pick cannot write it back."""
    with contextlib.suppress(OSError):
        write_json(store.path("ended", h["id"] + ".json"),
                   dict(public(h), how=how, ended=iso(), **extra))
    remove(_path(store, h["id"]))
    store.bump()


def cost_pick(L, size, costs):
    """For each pool, the `size[pool]` slots with the lowest summed cost, lowest slot first among
    equals; sorted."""
    return {p: sorted(sorted(range(lay.size(L, p)), key=lambda i: (costs.get(p, {}).get(i, 0), i))
                      [:size[p]]) for p in POOLS}


def add_cost(costs, pool, slots, what):
    for i in slots:
        costs.setdefault(pool, {})
        costs[pool][i] = costs[pool].get(i, 0) + PICK_COST[what]


def run_slots(r, pool):
    """The slots of `pool` live run record r holds."""
    if r.get("state") not in ("held", "waiting", "bg"):
        return []
    return [i for i in (r.get("slots") or {}).get(pool, []) if isinstance(i, int)]


def place(L, h, earlier, agents, runs=None):
    """The slots half hold h would pick: around the earlier strong holds overlapping it, then the
    runs (None: a placement that ignores them), then agent holds overlapping it, then background
    children."""
    costs = {}
    for w in earlier:
        if w["_start"] < h["_end"] and w["_end"] > h["_start"]:
            for p in POOLS:
                add_cost(costs, p, w["_slots"][p], "strong")
    for b in agents:
        if b["_start"] < h["_end"] and b["_end"] > h["_start"]:
            for p, sl in b["slots"].items():
                add_cost(costs, p, sl, "agent")
    for r in runs or ():
        for p in POOLS:
            add_cost(costs, p, run_slots(r, p), "background" if r.get("state") == "bg" else "run")
    return cost_pick(L, L["half"], costs)


def resolve_slots(store, L, h, earlier, agents):
    """A half hold live now with no recorded slots: picks them with the runs live now and writes
    them, under the holds mutex (re-read there: another process may have picked them, or the
    owner ended it). (slots, fixed); on a mutex or write failure, (the pick unwritten, False)."""
    try:
        with store.lock("holds"):
            d = read_json(_path(store, h["id"]))
            if not d or d.get("id") != h["id"]:
                return place(L, h, earlier, agents, store.live_runs()), False
            got = recorded_slots(L, d)
            if got is not None:
                return got, True
            sl = place(L, h, earlier, agents, store.live_runs())
            store.write("holds/%s.json" % h["id"], dict(d, slots=sl, slots_at=iso()))
            return sl, True
    except OSError:
        return place(L, h, earlier, agents, store.live_runs()), False


def lost(h, strongs):
    """({pool: [slot]}, [hold id]): the slots of agent hold h that strong holds overlapping its
    window cover, and those holds. A strong hold always wins, for h's whole window."""
    out, to = {}, []
    for v in strongs:
        if v["_start"] < h["_end"] and v["_end"] > h["_start"]:
            hit = False
            for pool, slots in h["slots"].items():
                x = [i for i in slots if i in v["_slots"].get(pool, ())]
                if x:
                    out.setdefault(pool, set()).update(x)
                    hit = True
            if hit:
                to.append(v["id"])
    return {p: sorted(s) for p, s in sorted(out.items())}, to


def records(store, L, prune=False):
    """The live holds, by start, each with _start, _end, _slots ({pool: [slot]} it covers, an
    agent hold's less what strong holds take), _fixed (its slots are recorded), and for agent
    holds _lapse and _claimed. Never written. One past its end, or an agent hold past its lapse
    unclaimed, is ignored; prune (under the holds mutex): moved to ended (expired, lapsed)."""
    now = utc_now()
    grace = datetime.timedelta(seconds=store.settings.hold_grace_s)
    strongs, agents = [], []
    for d in _read_all(store):
        if d["owner"] == "agent":
            sl = recorded_slots(L, d)
            if sl is None or not any(sl.values()) or not isinstance(d.get("shares"), dict):
                continue
            d["slots"] = sl
            d["_lapse"] = d["_start"] + grace
            d["_claimed"] = claim_alive(store, d)
            if d["_end"] > now and (d["_claimed"] or now < d["_lapse"]):
                agents.append(d)
            elif prune:
                end(store, d, "expired" if d["_end"] <= now else "lapsed")
        elif d["_end"] > now:
            strongs.append(d)
        elif prune:
            _end_expired(store, d)
    strongs.sort(key=lambda d: (d["_start"], d["id"]))
    agents.sort(key=lambda d: (d["_start"], d["id"]))
    for k, v in enumerate(strongs):
        v["_fixed"] = True
        if v["scope"] == "whole":
            v["_slots"] = {p: list(range(lay.size(L, p))) for p in POOLS}
        elif v["scope"] in ("slots", "domain"):
            v["_slots"] = recorded_slots(L, v) or {p: [] for p in POOLS}
        else:
            got = recorded_slots(L, v) if v["_start"] <= now else None
            if got is not None:
                v["_slots"] = got
            elif v["_start"] <= now:
                v["_slots"], v["_fixed"] = resolve_slots(store, L, v, strongs[:k], agents)
            else:
                v["_slots"], v["_fixed"] = place(L, v, strongs[:k], agents), False
    for b in agents:
        gone = lost(b, strongs)[0]
        b["_fixed"] = True
        b["_slots"] = {p: [i for i in b["slots"].get(p, []) if i not in gone.get(p, ())]
                       for p in POOLS}
    return sorted(strongs + agents, key=lambda d: (d["_start"], d["id"]))


def find(store, L, hid, prune=False):
    return next((h for h in records(store, L, prune) if h["id"] == hid), None)


# ------------------------------------------------------------------ what a run must avoid
def covered(L, pool, span_s, hs, now=None, max_span_s=10 * 365 * 86400):
    """{slot: hold} of `pool` for the holds live at some instant of the next span_s seconds,
    strong holds winning."""
    now = now or utc_now()
    t1 = now + datetime.timedelta(seconds=min(span_s, max_span_s))
    out = {}
    for h in sorted(hs, key=lambda h: not strong(h)):
        if h["_end"] > now and (h["_start"] <= now or h["_start"] < t1):
            for i in h["_slots"][pool]:
                out.setdefault(i, h)
    return out


def cover_plan(store, L, need, span_s, own=None, hs=None, within=None):
    """({pool: {slot: hold}}, [blocking strong hold], [blocking agent hold]) for a run needing
    `need` that expects to run span_s seconds from now: the slots it must not take (every hold's
    but its own, `own`), and the holds that leave it too few of a pool: strong ones first, then,
    for a pool strong ones alone leave enough of, agent ones. within: {pool: the only slots the
    run may take} (its --within); for such a pool only those slots and the holds over them count
    toward too few, while the cover stays every hold's."""
    hs = [h for h in (records(store, L) if hs is None else hs) if h["id"] != own]
    cover, blocking, ablocking = {}, [], []
    for pool in POOLS:
        if not need.get(pool):
            continue
        s = covered(L, pool, span_s, [h for h in hs if strong(h)],
                    max_span_s=store.settings.max_span_s)
        c = covered(L, pool, span_s, hs, max_span_s=store.settings.max_span_s)
        cover[pool] = c
        room = set(range(lay.size(L, pool)))
        if (within or {}).get(pool):
            room &= set(within[pool])
            s = {i: x for i, x in s.items() if i in room}
            c = {i: x for i, x in c.items() if i in room}
        if len(room) - len(s) < need[pool]:
            hit, into = s.values(), blocking
        elif len(room) - len(c) < need[pool]:
            hit, into = [x for x in c.values() if not strong(x)], ablocking
        else:
            continue
        for v in hit:
            if v not in into:
                into.append(v)
    return cover, blocking, ablocking


def clear_at(h):
    """When hold h stops covering its slots: its end, or an unclaimed agent hold's lapse."""
    if h["owner"] == "agent" and not h.get("_claimed"):
        return min(h["_lapse"], h["_end"])
    return h["_end"]


# ------------------------------------------------------------------ text
def slots_text(slots):
    return ", ".join(lay.slots_text(p, slots[p]) for p in POOLS if slots.get(p))


def text(h):
    """'owner hold h-... (half: cpu slots 0-2, gpu slot 0; 14:00-16:00 local: note)'"""
    sl = h.get("_slots") or recorded_slots_raw(h)
    what = h["scope"]
    if h["owner"] == "agent":
        what = "%s/%s %s" % (h.get("repo"), h.get("slug"), h.get("kind"))
    elif h["owner"] == "game":
        what = "game %s" % ((h.get("game") or {}).get("name") or (h.get("game") or {}).get("exe"))
    elif h["scope"] == "whole":
        what = "whole, the owner's runs only"
    when = ""
    if h.get("_start") is not None:
        when = "; %s-%s local" % (local_text(h["_start"]), local_text(h["_end"]))
    return "%s hold %s (%s: %s%s%s%s)" % (
        h["owner"], h["id"], what, slots_text(sl) or "no slot",
        "" if h.get("_fixed", True) else " for now, picked again at its start", when,
        ": " + h["note"] if h.get("note") else "")


def recorded_slots_raw(h):
    sl = h.get("slots")
    return sl if isinstance(sl, dict) else {}


def refusal_text(rid, need, expect_s, blocking, why):
    """The exit-11 line. It keeps the legacy words "quiet hours" (an older caller's script
    matches them) until its callers read "owner hold"."""
    now = utc_now()
    later = [v for v in blocking if v["_start"] > now]
    return "refused: %s (quiet hours) leaves run %s too few of the %s it needs%s; %s. Exit 11." % (
        "; ".join(text(v) for v in blocking), rid, lay.need_text(need),
        "; it expects to run %d min (--expect) and would overlap %s" % (
            int(expect_s // 60), "it" if len(later) == 1 else "them") if later else "", why)


def agent_wait_text(rid, need, expect_s, blocking):
    """Why agent holds leave run rid too few slots, and the --expect that would fit before them."""
    now = utc_now()
    ahead = [b for b in blocking if b["_start"] > now]
    fit = ""
    if ahead and len(ahead) == len(blocking):
        mins = int((min(b["_start"] for b in ahead) - now).total_seconds() // 60)
        fit = "; it expects to run %d min (--expect) and would overlap %s; %s" % (
            int(expect_s // 60), "it" if len(ahead) == 1 else "them",
            "an --expect under %d min would fit before it" % mins if mins >= 1
            else "it starts within a minute")
    return "%s leaves run %s too few of the %s it needs%s" % (
        "; ".join(text(b) for b in blocking), rid, lay.need_text(need), fit)


def entry(h, now=None):
    """Hold h for status and the API: its record (ancestors left out) plus local times, its
    slots, whether they are fixed, and for agent holds its state (upcoming, open, claiming) and
    when it lapses; for strong holds, nothing more."""
    now = now or utc_now()
    d = public(h)
    d.pop("ancestors", None)
    d.update(start_local=local_text(h["_start"], now), end_local=local_text(h["_end"], now),
             covers=h["_slots"], slots_fixed=h["_fixed"], live=h["_start"] <= now)
    if h["owner"] == "agent":
        d.update(state="claiming" if h["_claimed"] else "upcoming" if h["_start"] > now else "open",
                 lapses_at=None if h["_claimed"] else iso(min(h["_lapse"], h["_end"])))
    return d


# ------------------------------------------------------------------ windows
def local_at(day, hhmm):
    """The aware UTC datetime of local `hhmm` (datetime.time) on `day`."""
    return datetime.datetime.combine(day, hhmm).astimezone().astimezone(datetime.timezone.utc)


def window(now, start=None, until=None, dur_s=None, date=None):
    """(start, end) aware UTC datetimes from the CLI's options, or LockError(2).
    dur_s alone: now for that long. until alone: now until the next such local time.
    start: that local time on `date` (default today; tomorrow when it is already over), until
    `until` (the next day when not after start) or for dur_s."""
    def bad(why):
        return LockError(EXIT_USAGE, "usage: machine_lock.py hold: %s" % why)
    if start is None:
        if date:
            raise bad("--date needs --from")
        if (dur_s is None) == (until is None):
            raise bad("give --for D, --until HH:MM, or --from HH:MM with --until or --for")
        if dur_s is not None:
            return now, now + datetime.timedelta(seconds=dur_s)
        e = local_at(now.astimezone().date(), until)
        return now, e if e > now else local_at(now.astimezone().date()
                                              + datetime.timedelta(days=1), until)
    if (dur_s is None) == (until is None):
        raise bad("--from needs one of --until HH:MM or --for D")
    day = date or now.astimezone().date()
    for _ in range(2):
        s = local_at(day, start)
        if dur_s is not None:
            e = s + datetime.timedelta(seconds=dur_s)
        else:
            e = local_at(day, until)
            if e <= s:
                e = local_at(day + datetime.timedelta(days=1), until)
        if e > now or date:
            break
        day += datetime.timedelta(days=1)
    return s, e


# ------------------------------------------------------------------ making, editing, ending
def check_window(store, owner, start, end_, now):
    """LockError(2) unless start/end make a window a hold of `owner` may have."""
    try:
        local_ok(start, end_)
    except (OverflowError, ValueError, OSError):
        raise LockError(EXIT_USAGE, "that window ends past what this machine's local time can "
                                    "show (about year 3000)")
    if end_ <= now:
        raise LockError(EXIT_USAGE, "that window ended at %s" % local_text(end_, now))
    if end_ <= start:
        raise LockError(EXIT_USAGE, "a hold must end after it starts")
    if owner == "agent":
        s = store.settings
        if (end_ - max(start, now)).total_seconds() > s.hold_max_s:
            raise LockError(EXIT_USAGE, "an agent hold lasts at most %d h" % (s.hold_max_s // 3600))
        if (start - now).total_seconds() > s.hold_ahead_s:
            raise LockError(EXIT_USAGE, "an agent hold starts at most %d days ahead"
                            % (s.hold_ahead_s // 86400))


def _agent_slots(store, L, need, start, end_, hs, runs):
    """The slots an agent hold over [start, end_) takes: the lowest no hold covers, preferring
    those whose holder expects to be done by its start. LockError 11 when strong holds leave too
    few, 6 when agent holds do; it never takes from either. Returns (slots, notes)."""
    over_s = [h for h in hs if strong(h) and h["_start"] < end_ and h["_end"] > start]
    over_a = [h for h in hs if not strong(h) and h["_start"] < end_ and h["_end"] > start]
    cov = {}
    for pool in POOLS:
        if not need.get(pool):
            continue
        rv = {i: v for v in over_s for i in v["_slots"][pool]}
        rb = {}
        for b in over_a:
            for i in b["_slots"].get(pool, []):
                rb.setdefault(i, b)
        cov[pool] = (rv, rb, [i for i in range(lay.size(L, pool)) if i not in rv and i not in rb])
    for pool, (rv, rb, free) in cov.items():
        if need[pool] > lay.size(L, pool) - len(rv):
            raise LockError(EXIT_RESERVED, "NOT HELD: %s covers the %s it needs; exit 11" % (
                "; ".join(text(v) for v in over_s if set(rv) & set(v["_slots"][pool])),
                lay.need_text({pool: need[pool]})))
    for pool, (rv, rb, free) in cov.items():
        if need[pool] > len(free):
            who = list({x["id"]: x for x in rb.values()}.values())
            raise LockError(EXIT_BUSY, "NOT HELD: %s %s the %s it needs; exit 6" % (
                "; ".join(text(b) for b in who), "has" if len(who) == 1 else "have",
                lay.need_text({pool: need[pool]})))
    slots, notes = {}, []
    for pool, (rv, rb, free) in cov.items():
        held = {}
        for r in runs:
            for i in run_slots(r, pool):
                held[i] = r

        def busy_at_start(i):
            r = held.get(i)
            if r is None or r.get("state") == "bg":
                return False
            try:
                e = parse_iso(r.get("held_since") or r.get("since")) + datetime.timedelta(
                    seconds=float(r.get("expect_s")))
            except (TypeError, ValueError, OverflowError):
                return True
            return e > start
        slots[pool] = sorted(sorted(free, key=lambda i: (busy_at_start(i), i))[:need[pool]])
        notes += ["%s slot %d is held by run %s; the claim waits for it" % (pool, i, held[i]["id"])
                  for i in slots[pool] if busy_at_start(i)]
    for pool in POOLS:
        slots.setdefault(pool, [])
    return slots, notes


def create(store, L, spec, actor, kinds_table=None, now=None):
    """Makes a hold from spec {owner, scope, start, end (datetimes), note, job (optional: it must
    be the one job_of names), and by owner/scope: slots: slots {pool: [slot]}; agent:
    repo, slug, kind, gpu_share; game: slots, game}. A whole hold names nothing more.
    Returns (the hold as records() gives it, notes). Raises LockError: 2 for a spec it cannot use,
    11 or 6 for an agent hold the holds leave too little."""
    now = now or utc_now()
    owner, scope = spec.get("owner"), spec.get("scope")
    if owner not in OWNERS:
        raise LockError(EXIT_USAGE, "owner must be one of %s" % ", ".join(OWNERS))
    if owner == "agent":
        scope = "slots"
    if scope not in SCOPES[owner]:
        raise LockError(EXIT_USAGE, "a %s hold's scope is one of %s" % (owner, ", ".join(SCOPES[owner])))
    start, end_ = max(spec["start"], now) if owner == "agent" else spec["start"], spec["end"]
    check_window(store, owner, start, end_, now)
    for k in STR_KEYS + ("job",):
        if spec.get(k) is not None and not isinstance(spec[k], str):
            raise LockError(EXIT_USAGE, "%s must be text" % k)
    if scope == "whole" and any(spec.get(k) is not None for k in ("repo", "slug", "open")):
        raise LockError(EXIT_USAGE, "a whole hold is the owner's own (job %s): it names no repo or "
                                    "slug, and open is gone" % OWNER_JOB)
    job = job_of(dict(spec, owner=owner))
    if not job:
        raise LockError(EXIT_USAGE, "every hold names its job: an agent hold its repo "
                                    "and slug, a game hold its game")
    if spec.get("job") not in (None, job):
        raise LockError(EXIT_USAGE, "a %s hold's job is %s, not %s"
                        % (owner, job, spec["job"]))
    h = {"id": new_id("h"), "owner": owner, "scope": scope, "job": job,
         "start": iso(start), "end": iso(end_),
         "note": spec.get("note"), "created": iso(now), "by": (actor or {}).get("name"),
         "session": (actor or {}).get("session"), "ancestors": (actor or {}).get("ancestors") or []}
    notes = []
    if scope in ("slots", "domain") and owner != "agent":
        sl = spec.get("slots")
        got = recorded_slots(L, {"slots": sl}) if isinstance(sl, dict) else None
        if not got or not any(got.values()) or any(
                len(got[p]) != len(set(i for i in (sl.get(p) or []))) for p in POOLS):
            raise LockError(EXIT_USAGE, "slots must name slots of this layout: cpu 0-%d, gpu 0-%d"
                            % (L["cpu_slots"] - 1, L["gpu_slots"] - 1))
        h["slots"] = got
        if owner == "game":
            h["game"] = spec.get("game") or {}
    if owner == "agent":
        table = kinds_table or {}
        kind = spec.get("kind")
        if not spec.get("repo") or not spec.get("slug") or kind not in table:
            raise LockError(EXIT_USAGE, "an agent hold names its repo, slug and a kind (%s)"
                            % ", ".join(sorted(table)))
        share = spec.get("gpu_share")
        if share not in (None, "training"):
            raise LockError(EXIT_USAGE, "gpu_share is training or nothing")
        if share and not table[kind]["share"]:
            raise LockError(EXIT_USAGE, "--gpu-share training needs a kind whose gpu is shared")
        need = lay.need_for(L, table[kind], share)
        h.update(repo=spec["repo"], slug=spec["slug"], kind=kind, gpu_share=share,
                 shares=shares_for(table[kind], share), claim=None)
    try:
        with store.lock("holds"):
            hs = records(store, L, prune=True)
            # An id is a second stamp and a short token: one drawn twice in a second would
            # replace a live hold here, or an ended one's record when this hold ends.
            while (os.path.exists(_path(store, h["id"]))
                   or os.path.exists(store.path("ended", h["id"] + ".json"))):
                h["id"] = new_id("h")
            if owner == "agent":
                h["slots"], notes = _agent_slots(store, L, need, start, end_, hs, store.live_runs())
            store.write("holds/%s.json" % h["id"], h)
    except OSError as e:
        raise LockError(1, "the holds could not be read or written (%s)" % e)
    got = find(store, L, h["id"])
    if got is None:
        got = dict(h, _start=start, _end=end_, _fixed=scope != "half",
                   _slots=h.get("slots") or {p: list(range(L["half"][p] if scope == "half"
                                                           else lay.size(L, p))) for p in POOLS},
                   _claimed=False, _lapse=start)
    if strong(got):
        for b in records(store, L):
            if b["owner"] == "agent":
                gone = lost(b, [got])[0]
                if gone:
                    notes.append("%s loses %s to %s" % (text(b), slots_text(gone), got["id"]))
    return got, notes


def shares_for(kind_entry, gpu_share=None):
    """{pool: 'whole' | 'training' | None}: how a run of this kind uses each pool, compared when
    a run claims an agent hold."""
    return {"cpu": kind_entry["cpu"],
            "gpu": None if kind_entry["gpu"] != "pool" else (
                "training" if gpu_share == "training" and kind_entry["share"] else "whole")}


def edit(store, L, hid, changes, now=None):
    """Changes hold hid's editable fields (owner: start before it starts, end, note; agent and
    game: end, note). Returns the hold. LockError 2 for a change it cannot make,
    1 when no such hold is live."""
    now = now or utc_now()
    try:
        with store.lock("holds"):
            h = find(store, L, hid, prune=True)
            if h is None:
                raise LockError(1, "no live hold %s" % hid)
            bad = sorted(k for k in changes if k not in EDITABLE[h["owner"]])
            if bad:
                raise LockError(EXIT_USAGE, "a %s hold's %s cannot be changed (only %s)" % (
                    h["owner"], ", ".join(bad), ", ".join(EDITABLE[h["owner"]])))
            start, end_ = h["_start"], h["_end"]
            if "start" in changes:
                if h["_start"] <= now:
                    raise LockError(EXIT_USAGE, "%s has started; its start cannot move" % hid)
                start = changes["start"]
            if "end" in changes:
                end_ = changes["end"]
            check_window(store, h["owner"], max(start, now) if "start" not in changes else start,
                         end_, now)
            d = public(h)
            d.update(start=iso(start), end=iso(end_))
            if "note" in changes:
                if changes["note"] is not None and not isinstance(changes["note"], str):
                    raise LockError(EXIT_USAGE, "note must be text")
                d["note"] = changes["note"]
            if h["owner"] == "agent":
                d["slots"] = h["slots"]
            store.write("holds/%s.json" % hid, d)
    except OSError as e:
        raise LockError(1, "the holds could not be read or written (%s)" % e)
    return find(store, L, hid) or h


def end_hold(store, L, hid, how="released"):
    """Ends hold hid now (moved to ended, how). Returns the hold; LockError 1 when no such hold is
    live or the holds mutex was not had."""
    try:
        with store.lock("holds", 30):
            h = find(store, L, hid, prune=True)
            if h is None:
                old = read_json(store.path("ended", hid + ".json")) if ID_RE.fullmatch(hid) else None
                raise LockError(1, "no live hold %s%s" % (hid, " (it ended: %s)" % old["how"]
                                                          if old and old.get("how") else ""))
            end(store, h, how)
            return h
    except OSError as e:
        raise LockError(1, "%s is not ended: %s; try again" % (hid, e))


# ------------------------------------------------------------------ a run inside a hold
def claim(store, L, rec, hid, shares):
    """run --hold hid: (hold, None) once run record rec may use it, else (None, why: exit 12).
    An agent hold must be live, made for rec's repo, slug, kind (a retired name of it, an alias
    in the kinds table, matches) and shares, and not claimed by
    another live run; its claim {id, pid, ctime, at} is written into it and dies with the run's
    wrapper. An owner whole hold must admit rec (admits: the owner's own runs); it is not
    claimed (it may hold several of the owner's runs, one after another). No other hold can be run inside."""
    try:
        with store.lock("holds"):
            h = find(store, L, hid, prune=True)
            if h is None:
                old = read_json(store.path("ended", hid + ".json")) if ID_RE.fullmatch(hid) else None
                if old and old.get("how"):
                    return None, "hold %s is over (%s%s)" % (hid, old["how"], (
                        " by run %s" % old["run"]) if old.get("run") else "")
                return None, "no hold %s" % hid
            if h["owner"] == "owner" and h["scope"] == "whole":
                ok, why = admits(h, rec)
                return (h, None) if ok else (None, why)
            if h["owner"] != "agent":
                return None, ("%s cannot be run inside: only an owner whole hold or an agent hold "
                              "can" % text(h))
            want = {"repo": rec.get("repo"), "slug": rec.get("slug"), "kind": rec.get("kind"),
                    "shares": shares}
            al = kinds_mod.aliases(store.kinds_path)    # a kind under its retired name matches

            def same(k, v):
                if k == "kind":
                    return kinds_mod.canonical(h.get(k), table_aliases=al) == \
                        kinds_mod.canonical(v, table_aliases=al)
                return h.get(k) == v
            diff = [k for k, v in want.items() if not same(k, v)]
            if diff:
                return None, "%s was made for %s, not this run's %s" % (
                    text(h), ", ".join("%s %s" % (k, h.get(k)) for k in diff),
                    ", ".join("%s %s" % (k, want[k]) for k in diff))
            c = h.get("claim") or {}
            if h["_claimed"] and c.get("id") != rec["id"]:
                return None, "%s is claimed by run %s, which is still live" % (text(h), c.get("id"))
            h["claim"] = {"id": rec["id"], "pid": rec["pid"], "ctime": rec["ctime"], "at": iso()}
            d = public(h)
            store.write("holds/%s.json" % hid, d)
            h["_claimed"] = True
            return h, None
    except OSError as e:
        return None, "the holds could not be read or written (%s)" % e


def claimed(store, rid, hid):
    """Run rid holds its slots: the agent hold it claimed moves to ended (how: claimed)."""
    try:
        with store.lock("holds"):
            d = read_json(_path(store, hid))
            if d and d.get("owner") == "agent" and (d.get("claim") or {}).get("id") == rid:
                end(store, d, "claimed", run=rid)
    except OSError:
        pass        # the claim dies with the wrapper; the hold then lapses or expires


def code_for(h):
    """The exit code a hold that refuses a run gives: 11 for a strong hold, else 12."""
    return EXIT_RESERVED if strong(h) else EXIT_HOLD
