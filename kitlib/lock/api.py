"""The machine lock's one API: every UI reads and writes the lock through it.

Plain functions, JSON-ready dicts in and out, no transport: the CLI (`status --json` is
snapshot()) and anything later (a web page, an MCP server) call these and
nothing else. docs/lock-api.md is the contract; SCHEMA_VERSION changes when a key's meaning
changes or a key goes (added keys do not change it).

Reads:  snapshot(), revision(), wait_change(revision, timeout_s), stats(...), history(...).
Writes: set_division, hold_create, hold_edit, hold_end, set_games, set_game_mode, end_run,
        move_to_top.
Each write takes `actor` and returns
    {"ok": true,  "result": ..., "snapshot": snapshot()}  or
    {"ok": false, "error": {"code": invalid | forbidden | not_found | conflict | busy | store,
                            "message": text, "field": the input key at fault or null},
     "snapshot": snapshot() or null}
and never raises for a refusal. Who may do what is decided in one place, authorize(), whose owner
check is the one hook (set_owner_check). On the machine's store the writes read who is asking from
the calling process's tree (who()); `actor` gives only a name for the records.
"""
import datetime
import subprocess
import time

import kitlib.platform
from kitlib.lock import game
from kitlib.lock import holds as hl
from kitlib.lock import kinds as kinds_mod
from kitlib.lock import layout as lay
from kitlib.lock import queue as qm
from kitlib.lock import stale as stale_mod
from kitlib.lock import status as st
from kitlib.lock.run import expected_end, request_stop
from kitlib.lock.store import (EXIT_BUSY, EXIT_HOLD, EXIT_REQUIRE, EXIT_RESERVED, EXIT_USAGE,
                               ID_RE, LockError, Store, actor_host, caller, iso, owns, parse_iso,
                               public, read_json, read_record, utc_now)
from kitlib.proc import process

SCHEMA_VERSION = 1
POOLS = lay.POOLS
ERROR_CODES = ("invalid", "forbidden", "not_found", "conflict", "busy", "store")
OWNER_ACTIONS = ("set_division", "set_games", "set_game_mode", "move_to_top")


class ApiError(Exception):
    def __init__(self, code, message, field=None):
        Exception.__init__(self, message)
        self.code, self.message, self.field = code, message, field


# ------------------------------------------------------------------ who may (the one hook)
def who(actor, store):
    """Who is asking. The machine's store: store.caller() from this process's tree,
    whatever `actor` says (only its name is kept), so no caller can claim the owner's role,
    another session or force. A scratch store (tests) takes `actor` as given."""
    if store.testing and isinstance(actor, dict):
        return actor
    return caller(name=actor.get("name") if isinstance(actor, dict) else None)


def default_owner_check(actor):
    """The owner: role "owner" with no agent host above it (store.caller). Swap it with
    set_owner_check."""
    return (isinstance(actor, dict) and actor.get("role") == "owner"
            and not actor_host(actor))


_owner_check = [default_owner_check]


def set_owner_check(fn):
    """Replaces the owner check (fn(actor) -> bool); None restores the default. Returns the old."""
    old = _owner_check[0]
    _owner_check[0] = fn or default_owner_check
    return old


def is_owner(actor):
    try:
        return bool(_owner_check[0](actor))
    except Exception:
        return False                    # fail closed


def authorize(actor, action, target=None):
    """None when actor may do `action` to `target` (a hold or run record, or a hold spec), else
    the reason; actor is who() it is. The owner may do everything. An agent may make agent
    holds, edit and end its own agent holds (store.owns), and end its own runs. Ending another
    session's run, owner and game holds, the division and the games list are the owner's (a run
    whose wrapper has died is reaped by the lock itself, not ended by anyone)."""
    if is_owner(actor):
        return None
    if not isinstance(actor, dict):
        return "no actor"
    if action in OWNER_ACTIONS:
        return "%s is the owner's" % action
    if action == "hold_create":
        return None if (target or {}).get("owner") == "agent" else "only the owner makes %s holds" % (
            (target or {}).get("owner"))
    if action in ("hold_edit", "hold_end"):
        if (target or {}).get("owner") != "agent":
            return "%s holds are the owner's" % (target or {}).get("owner")
        return None if owns(actor, target) else "hold %s belongs to another session" % target["id"]
    if action == "end_run":
        if owns(actor, target or {}):
            return None
        return ("run %s (%s/%s) belongs to another session; only the owner ends another "
                "session's run" % (
                    target["id"], target.get("repo"), target.get("slug")))
    return "unknown action %s" % action


def _check(actor, action, target=None):
    why = authorize(actor, action, target)
    if why:
        raise ApiError("forbidden", why)


# ------------------------------------------------------------------ live gpu use (NVIDIA only)
def gpu_use(store):
    """{pid: MB of gpu memory} of the processes using an NVIDIA gpu now, or None when it cannot
    be known (no nvidia-smi, another vendor). Cached settings.gpu_use_cache_s. A test store uses
    store.gpu_probe (a callable) or gets None."""
    probe = getattr(store, "gpu_probe", None)
    if probe is None and store.testing:
        return None
    c = store._gpu_cache
    if c is not None and time.monotonic() - c[0] < store.settings.gpu_use_cache_s:
        return c[1]
    out = None
    try:
        if probe is not None:
            out = probe()
        else:
            r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True,
                               timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                out = {}
                for line in r.stdout.splitlines():
                    parts = [x.strip() for x in line.split(",")]
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        out[int(parts[0])] = out.get(int(parts[0]), 0) + int(parts[1])
    except (OSError, subprocess.SubprocessError, ValueError):
        out = None
    store._gpu_cache = (time.monotonic(), out)
    return out


# ------------------------------------------------------------------ reading
def _store(store):
    return store if store is not None else Store()


def _epoch_iso(t):
    return iso(datetime.datetime.fromtimestamp(t, datetime.timezone.utc)) if t is not None else None


def _hardware(store, L, hw):
    """The machine: threads, groups, cores, CCDs (L3 domains) with their cpu slots and whether
    they hold the preferred cores, memory, gpus."""
    ranks = {c["thread"]: (c.get("efficiency", 0), c.get("sched", 0)) for c in hw.get("cpu_sets") or ()}
    top = max(ranks.values()) if ranks else None
    preferred = sorted(t for t, r in ranks.items() if r == top) if ranks and len(set(ranks.values())) > 1 else []
    game_pick = game.domain_slots(L, hw)
    ccds = []
    for i, dom in enumerate(hw.get("l3") or []):
        slots = [k for k, s in enumerate(L["slots_cpu"]) if set(s) & set(dom)]
        ccds.append({"index": i, "threads": dom, "cpu_slots": slots,
                     "preferred": bool(set(preferred) & set(dom)),
                     "game_pick": bool(slots) and set(slots) == set(game_pick)})
    mem = kitlib.platform.memory_mb() if not store.testing else None   # the running platform's
    return {"threads": len(hw["threads"]), "thread_ids": hw["threads"], "groups": hw.get("groups") or [],
            "cores": [{"threads": c["threads"], "smt": c["smt"], "efficiency": c["efficiency"]}
                      for c in hw.get("cores") or []],
            "ccds": ccds, "preferred_threads": preferred,
            "memory_mb": {"total": hw.get("memory_mb"), "free": (mem or {}).get("free_mb")},
            "gpus": [{k: g.get(k) for k in ("index", "name", "vendor", "vendor_id", "vram_mb",
                                            "shared_mb", "source", "default_share")}
                     for g in hw.get("gpus") or []],
            "source": hw.get("source"), "problems": hw.get("problems") or []}


def _layout(store, lf, hw, runs):
    L, want = lf["layout"], lf["wanted"]
    try:
        default = lay.default_division(hw)
    except ValueError:
        default = None
    return {"key": L["key"], "threads": L["threads"], "cpu_slots": L["cpu_slots"],
            "cpu_slot_threads": L["cpu_slot_threads"], "slots_cpu": L["slots_cpu"],
            "gpu_slots": L["gpu_slots"], "gpu_cards": L["gpu_cards"], "gpus": L["gpus"],
            "half": L["half"], "agent_cap": L["agent_cap"], "medium_cores": L["medium_cores"],
            "l3_slots": L["l3_slots"], "division": L["division"], "default_division": default,
            "wanted_division": want["division"], "wanted_key": want["key"],
            "owner_division": lay.read_division(store)[0] or {},
            "source": lf["source"], "warning": lf["warning"],
            "hardware_change": {"pending": lf["pending"], "reason": st.pending_reason(lf["pending"]),
                                "changed": want["key"] != L["key"]},
            "other_labels": lay.other_labels(runs, L)}


def _usage(r):
    keys = ("cpu_s", "peak_cpu_cores", "peak_mem_mb", "peak_commit_mb", "cpu_cores_now", "mem_mb_now")
    return {k: r.get(k) for k in keys}


def snapshot(store=None, history_days=7, since=None, until=None):
    """Everything a UI shows, at one revision (see docs/lock-api.md, Snapshot)."""
    store = _store(store)
    rev = store.revision()
    now = utc_now()
    hw = lay.hardware(store)
    lf = lay.in_force(store, adopt=False)
    L = lf["layout"]
    runs = store.live_runs()
    try:
        q, qerr = qm.load(store), None
    except qm.QueueUnreadable as e:
        q, qerr = {"ticket": 0, "entries": []}, str(e)
    dead = {e["id"] for e in q["entries"] if store.run_dead(e)}
    lq = dict(q, entries=[e for e in q["entries"] if e["id"] not in dead])
    planned = qm.plan(L, lq)[0]
    ents = {e["id"]: e for e in lq["entries"]}
    try:
        hs, herr = hl.records(store, L), None
    except OSError as e:            # HoldsUnreadable among them: shown, never read as none
        hs, herr = [], str(e)
    try:
        snap = process.snapshot()
    except OSError:
        snap = None
    gpu = gpu_use(store)
    run_rows = []
    for r in runs:
        e = ents.get(r["id"])
        tree = [r["pid"]]
        if snap is not None:
            tree += [p for p, _c in process.descendants(r["pid"], r.get("ctime"), snap)]
        info = planned.get(r["id"]) or {}
        start_at = r.get("held_since") or r.get("since")
        row = {k: r.get(k) for k in ("id", "state", "repo", "slug", "kind", "note", "pid", "ctime",
                                     "since", "held_since", "expect_s", "expect_from", "hold",
                                     "parent", "need", "has", "shares", "pinned", "pin", "pin_note",
                                     "threads", "child", "blocked_by", "detached", "session",
                                     "releasing", "log", "moved", "resource", "priority",
                                     "within")}
        row.update(cores=qm.entry_holds(L, e, "cpu") if e else r.get("cores") or [],
                   gpus=qm.entry_holds(L, e, "gpu") if e else r.get("gpus") or [],
                   tree=tree, usage=_usage(r),
                   gpu_mem_mb=None if gpu is None else sum(gpu.get(p, 0) for p in tree),
                   expected_end=expected_end(start_at, r.get("expect_s")),
                   background=bool(r.get("background")),
                   stale=stale_mod.reason(store, r, now),
                   overrun=None)
        if r.get("state") == "held" and r.get("held_since") and r.get("expect_s"):
            try:
                late = (now - parse_iso(r["held_since"])).total_seconds() - float(r["expect_s"])
                row["overrun"] = {"s": int(late),
                                  "planned_end": _epoch_iso(qm.holder_end(
                                      (e or {}).get("held_at"), r["expect_s"], time.time()))} if late > 0 else None
            except (TypeError, ValueError):
                pass
        if r.get("state") == "waiting":
            row.update(position=info.get("position"), est_start=_epoch_iso(info.get("start")),
                       backfill=bool(info.get("backfill")), moved=(e or {}).get("moved"))
        run_rows.append(row)
    rows_by = {x["id"]: x for x in run_rows}
    held = qm.holders(L, lq) if qerr is None else {
        p: {i: {"id": x["id"], "state": x["state"]} for x in run_rows for i in x[qm.PICK[p]]}
        for p in POOLS}
    cover = {p: hl.covered(L, p, 0, hs) for p in POOLS}
    slots = {}
    for p in POOLS:
        slots[p] = []
        for i in range(lay.size(L, p)):
            x = held[p].get(i)
            h = cover[p].get(i)
            d = {"slot": i, "holder": x["id"] if x else None,
                 "background": bool(x and x.get("state") == "bg"),
                 "hold": h["id"] if h else None, "hold_owner": h["owner"] if h else None}
            if p == "cpu":
                d["threads"] = L["slots_cpu"][i]
                d["pinned"] = (rows_by.get(d["holder"]) or {}).get("pinned")
            else:
                d["card"] = L["gpu_cards"][i]
            slots[p].append(d)
    queue_rows = []
    for e in sorted((x for x in lq["entries"] if x["state"] == "waiting"),
                    key=lambda x: ((planned.get(x["id"]) or {}).get("position") or 1 << 30,
                                   x.get("ticket") or 0)):
        info = planned.get(e["id"]) or {}
        if e.get("parked"):
            ids = e.get("blocked_by") or []
            why = "parked: %s keeps it waiting" % "; ".join(
                hl.text(h) for h in hs if h["id"] in ids) if ids else "parked behind a hold"
        else:
            why = qm.busy_text(L, lq, e, planned)
        queue_rows.append({
            "id": e["id"], "position": info.get("position"), "ticket": e.get("ticket"),
            "repo": e.get("repo"), "slug": e.get("slug"), "kind": e.get("kind"),
            "needs": {p: qm.entry_need(e, p) for p in POOLS}, "since": e.get("since"),
            "expect_s": e.get("expect_s"), "est_start": _epoch_iso(info.get("start")),
            "backfill": bool(info.get("backfill")), "parked": bool(e.get("parked")),
            "blocked_by": e.get("blocked_by"), "hold": e.get("hold"), "moved": e.get("moved"),
            "resource": qm.entry_resource(e), "priority": qm.entry_priority(e),
            "within_slots": sorted(qm.entry_within(e, "cpu") or []) or None, "why": why})
    try:
        kinds, kerr = kinds_mod.load(store.kinds_path, agent_cap=L["agent_cap"]), None
    except kinds_mod.KindsError as e:
        kinds, kerr = {}, str(e)
    gs = game.state(store, L, hs)
    return {"schema_version": SCHEMA_VERSION, "revision": rev, "at": iso(now), "store": store.root,
            "hardware": _hardware(store, L, hw), "layout": _layout(store, lf, hw, runs),
            "slots": slots, "runs": run_rows,
            "queue": {"entries": queue_rows if qerr is None else None, "error": qerr},
            "holds": [hl.entry(h, now) for h in hs], "holds_error": herr, "game": gs,
            "kinds": kinds, "kinds_error": kerr,
            "stats": stats(store, days=history_days, since=since, until=until)}


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def stats(store=None, days=7, since=None, until=None):
    """History per kind and repo over [since, until) (ISO times; default the last `days`):
    {range: {since, until}, rows: [{kind, repo, runs, done (exit 0), failed (the command's exit
    not 0), interrupted (stopped, cancelled, killed), held_s: {median, p90, max},
    wait_s: {median, max}, cpu_s, peak_cpu_cores, peak_mem_mb}]}."""
    store = _store(store)
    t0 = _time(since, "since", None) or utc_now() - datetime.timedelta(days=days)
    t1 = _time(until, "until", None)
    groups = {}
    for d in store.history_records(since=t0, until=t1):
        key = (d.get("kind") or "background", d.get("repo"))
        groups.setdefault(key, []).append(d)
    rows = []
    for (kind, repo), ds in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        held = sorted(float(d["held_s"]) for d in ds if isinstance(d.get("held_s"), (int, float)))
        wait = [float(d["wait_s"]) for d in ds if isinstance(d.get("wait_s"), (int, float))]
        num = lambda k: [d[k] for d in ds if isinstance(d.get(k), (int, float))]
        rows.append({"kind": kind, "repo": repo, "runs": len(ds),
                     "done": sum(1 for d in ds if d.get("how") == "done" and d.get("code") == 0),
                     "failed": sum(1 for d in ds if d.get("how") == "done" and d.get("code") != 0),
                     "interrupted": sum(1 for d in ds if d.get("how") != "done"),
                     "held_s": {"median": _median(held),
                                "p90": held[min(len(held) - 1, int(len(held) * 0.9))] if held else None,
                                "max": held[-1] if held else None},
                     "wait_s": {"median": _median(wait), "max": max(wait) if wait else None},
                     "cpu_s": round(sum(num("cpu_s")), 1),
                     "peak_cpu_cores": max(num("peak_cpu_cores"), default=None),
                     "peak_mem_mb": max(num("peak_mem_mb"), default=None)})
    return {"range": {"since": iso(t0), "until": iso(t1) if t1 else None}, "rows": rows}


def history(store=None, days=7, since=None, until=None):
    """One line per run that ended in [since, until) (ISO times; default the last `days`), oldest
    first: the store's history lines (store.Store.HISTORY_KEYS plus held_s, wait_s, partial,
    background). stats() aggregates the same lines; this is for a view that draws each run."""
    if isinstance(days, bool) or not isinstance(days, (int, float)):
        raise ApiError("invalid", "days must be a number", "days")
    store = _store(store)
    try:
        t0 = _time(since, "since", None) or utc_now() - datetime.timedelta(days=days)
    except (OverflowError, ValueError):
        raise ApiError("invalid", "days is out of range", "days")
    t1 = _time(until, "until", None)
    return store.history_records(since=t0, until=t1)


def revision(store=None):
    """The store's revision alone (snapshot()'s `revision`, which every write moves on): what a
    client that polls for a change reads, without building a snapshot."""
    return _store(store).revision()


def wait_change(revision, timeout_s=30.0, store=None, **snapshot_kw):
    """The snapshot once the store's revision differs from `revision`, or after timeout_s (its
    revision then equal: nothing was written; live use may still have moved)."""
    store = _store(store)
    end = time.monotonic() + max(0.0, float(timeout_s))
    while store.revision() == revision and time.monotonic() < end:
        time.sleep(min(store.settings.wait_change_poll_s, max(0.0, end - time.monotonic())))
    return snapshot(store, **snapshot_kw)


# ------------------------------------------------------------------ writing
_CODES = {EXIT_USAGE: "invalid", EXIT_RESERVED: "conflict", EXIT_HOLD: "conflict",
          EXIT_BUSY: "busy", EXIT_REQUIRE: "store"}


def _write(store, fn):
    """Runs fn() -> result; wraps it as {ok, result, snapshot} or {ok: false, error, snapshot}."""
    store = _store(store)
    try:
        result = fn(store)
    except ApiError as e:
        return _fail(store, e.code, e.message, e.field)
    except LockError as e:
        code = _CODES.get(e.code)
        if code is None:
            code = "not_found" if str(e).startswith("no live") else "store"
        return _fail(store, code, str(e))
    except OSError as e:
        return _fail(store, "store", "the lock store could not be read or written: %s" % e)
    return {"ok": True, "result": result, "snapshot": _safe_snapshot(store)}


def _safe_snapshot(store):
    try:
        return snapshot(store)
    except Exception:
        return None


def _id(v, prefix):
    """v when it is a run (prefix r) or hold (prefix h) id as the lock makes them (store.ID_RE,
    whole string); else ApiError invalid. An id names a file (active/<id>.json, ended/<id>.json),
    so nothing else reaches a path."""
    if not (isinstance(v, str) and v.startswith(prefix + "-") and ID_RE.fullmatch(v)):
        raise ApiError("invalid", "id must be a %s id like %s-20261006T120000Z-abcd" % (
            "run" if prefix == "r" else "hold", prefix), "id")
    return v


def _fail(store, code, message, field=None):
    return {"ok": False, "error": {"code": code, "message": message, "field": field},
            "snapshot": _safe_snapshot(store)}


def _time(v, field, now):
    """An ISO time with its offset (Z or +hh:mm) or None -> an aware UTC datetime."""
    if v is None:
        return None
    try:
        t = datetime.datetime.fromisoformat(v.replace("Z", "+00:00")) if isinstance(v, str) else None
    except ValueError:
        t = None
    if t is None or t.tzinfo is None:
        raise ApiError("invalid", "%s must be an ISO time with its offset, like "
                                  "2026-10-06T14:00:00Z or 2026-10-06T16:00:00+02:00" % field, field)
    return t.astimezone(datetime.timezone.utc)


def _layout_now(store):
    return lay.in_force(store)["layout"]


def set_division(actor, division, store=None):
    """The owner's division (machine.json): a dict of kitlib.lock.layout.DIVISION_KEYS (keys left
    out take their default), or None for every default. It is checked against this machine first;
    it applies at once when nothing is live or queued, else once nothing is (the snapshot's
    layout.hardware_change says which). Keys starting with _ (notes such as _comment) are not
    division keys: those already in machine.json are kept (one the division also names takes the
    division's value), and machine.json is removed only when it would hold neither division keys
    nor notes. result: {applied, pending, reason} (reason: kitlib.lock.status.pending_reason)."""
    def go(store):
        me = who(actor, store)
        _check(me, "set_division")
        if division is not None and not isinstance(division, dict):
            raise ApiError("invalid", "the division is an object or null", "division")
        keys = {k: v for k, v in (division or {}).items() if not str(k).startswith("_")}
        try:
            lay.derive(lay.hardware(store), keys)
        except ValueError as e:
            raise ApiError("invalid", "division: %s" % e, "division")
        notes = lay.division_notes(store)
        notes.update((k, v) for k, v in (division or {}).items() if str(k).startswith("_"))
        if keys or notes:
            store.write("machine.json", dict(notes, **keys))
        else:
            store.drop("machine.json")
        lf = lay.in_force(store)
        return {"applied": lf["layout"]["key"] == lf["wanted"]["key"], "pending": lf["pending"],
                "reason": st.pending_reason(lf["pending"])}
    return _write(store, go)


def _hold_spec(spec, now):
    if not isinstance(spec, dict):
        raise ApiError("invalid", "a hold spec is an object", "spec")
    out = dict(spec)
    start = _time(spec.get("start"), "start", now) or now
    end = _time(spec.get("end"), "end", now)
    if end is None and spec.get("for_s") is not None:
        try:
            end = start + datetime.timedelta(seconds=float(spec["for_s"]))
        except (TypeError, ValueError, OverflowError):
            raise ApiError("invalid", "for_s must be a number of seconds", "for_s")
    if end is None:
        raise ApiError("invalid", "a hold needs end (an ISO time) or for_s", "end")
    out.update(start=start, end=end)
    out.pop("for_s", None)
    return out


def hold_create(actor, spec, store=None, kinds_table=None, checkout=None):
    """Makes a hold. spec: {owner: owner | agent, scope (owner: whole | half | slots), start
    (ISO, default now), end (ISO) or for_s, note, job (optional; holds.job_of names it: OWNER for an
    owner hold, repo/slug for an agent hold), and by scope: slots: slots {cpu: [...], gpu:
    [...]}; agent: repo, slug, kind, gpu_share}. A whole hold names nothing more: it is the
    owner's, for the owner's own runs. Game holds are made by game mode only. checkout: the
    folder the hold is made from (the CLI's current directory): an agent hold's kind may then be
    a repository kind of its main checkout's `.kit.json` (run.book's checkout). result:
    {hold, notes}."""
    def go(store):
        me = who(actor, store)
        now = utc_now()
        if not isinstance(spec, dict):
            raise ApiError("invalid", "a hold spec is an object", "spec")
        if spec.get("owner") == "game":
            raise ApiError("invalid", "game holds are made by game mode", "owner")
        _check(me, "hold_create", spec)
        s = _hold_spec(spec, now)
        table = kinds_table
        L = _layout_now(store)
        if s.get("owner") == "agent" and table is None:
            try:
                table = kinds_mod.load(store.kinds_path, agent_cap=L["agent_cap"])
            except kinds_mod.KindsError as e:
                raise ApiError("store", str(e))
        if table and s.get("kind"):
            try:
                s["kind"] = kinds_mod.resolve(s["kind"], table, store.kinds_path)
                if s.get("owner") == "agent":
                    merged = kinds_mod.with_repo(table, s["kind"], checkout, store.kinds_path)
                    if merged is not table:     # the repo kind's size against the layout
                        kinds_mod.check_cap({s["kind"]: merged[s["kind"]]}, L["agent_cap"])
                    table = merged
            except kinds_mod.KindsError as e:
                raise ApiError("store", str(e))
        h, notes = hl.create(store, L, s, me, table, now)
        return {"hold": hl.entry(h, now), "notes": notes}
    return _write(store, go)


def _live_hold(store, L, hid):
    _id(hid, "h")
    h = hl.find(store, L, hid)
    if h is None:
        old = read_json(store.path("ended", hid + ".json"))
        raise ApiError("not_found", "no live hold %s%s" % (
            hid, " (it ended: %s)" % old["how"] if old and old.get("how") else ""), "id")
    return h


def hold_edit(actor, hid, changes, store=None):
    """Changes a live hold: owner holds start (before it starts), end, note; agent and game
    holds end, note. Times are ISO. result: {hold}."""
    def go(store):
        me = who(actor, store)
        now = utc_now()
        L = _layout_now(store)
        h = _live_hold(store, L, hid)
        _check(me, "hold_edit", h)
        if not isinstance(changes, dict) or not changes:
            raise ApiError("invalid", "changes is a non-empty object", "changes")
        c = dict(changes)
        for k in ("start", "end"):
            if k in c:
                c[k] = _time(c[k], k, now)
        got = hl.edit(store, L, hid, c, now)
        return {"hold": hl.entry(got, now)}
    return _write(store, go)


def hold_end(actor, hid, store=None):
    """Ends a live hold now. A game hold ends, but game mode makes it again while the game runs
    and game mode is on (switch game mode off instead). result: {hold}."""
    def go(store):
        me = who(actor, store)
        L = _layout_now(store)
        h = _live_hold(store, L, hid)
        _check(me, "hold_end", h)
        got = hl.end_hold(store, L, hid, "released" if h["owner"] != "owner" else "ended by owner")
        return {"hold": public(got)}
    return _write(store, go)


def set_games(actor, games, store=None):
    """Replaces the games list: [{exe, pin?, name?}]; pin left out is off for a title known to
    use Vanguard, EasyAntiCheat or BattlEye, else on. result: {games}."""
    def go(store):
        me = who(actor, store)
        _check(me, "set_games")
        try:
            got = game.write_list(store, games=games)
        except LockError as e:
            raise ApiError("invalid", str(e), "games")
        L = _layout_now(store)
        game.sync(store, L, force=True)
        return {"games": got["games"]}
    return _write(store, go)


def set_game_mode(actor, enabled, store=None):
    """Game mode on or off. Off ends a live game hold at once. result: {enabled}."""
    def go(store):
        me = who(actor, store)
        _check(me, "set_game_mode")
        if type(enabled) is not bool:
            raise ApiError("invalid", "enabled must be true or false", "enabled")
        got = game.write_list(store, enabled=enabled)
        L = _layout_now(store)
        game.sync(store, L, force=True)
        return {"enabled": got["enabled"]}
    return _write(store, go)


def end_run(actor, rid, store=None):
    """Asks a live run to end (its stop request: a waiting run leaves the queue, a held run's
    tree is ended, a background run gets settings.bg_stop_grace_s to checkpoint). It does not
    wait. result: {id, requested}. A record that cannot be read (_active_record) is the store
    error, never "no live run"."""
    def go(store):
        me = who(actor, store)
        _id(rid, "r")
        d = _active_record(store, rid)
        if not d or not store.run_live(d):
            raise ApiError("not_found", "no live run %s" % rid, "id")
        _check(me, "end_run", d)
        by = "owner" if is_owner(me) else me.get("name") or me.get("session")
        if not request_stop(store, rid, by, me.get("session")):
            raise ApiError("not_found", "no live run %s" % rid, "id")
        return {"id": rid, "requested": True}
    return _write(store, go)


def move_to_top(actor, rid, store=None):
    """Moves a waiting run to the front of the queue: it starts as soon as what it needs is free,
    ahead of every other waiting run except a hold's claimer or a nested run (queue.plan), and a
    later move goes ahead of an earlier one. Runs holding slots keep them: nothing is stopped or
    preempted. The owner's. result: {id, moved: {by, name, at, seq}, position}."""
    def go(store):
        me = who(actor, store)
        _check(me, "move_to_top")
        _id(rid, "r")
        L = _layout_now(store)
        with store.lock("queue", store.settings.queue_lock_s):
            q = qm.load(store)
            e = qm.entry_of(q, rid)
            if e is None or store.run_dead(e):
                old = read_json(store.path("ended", rid + ".json"))
                raise ApiError("not_found", (
                    "run %s has ended (%s); only a waiting run moves to the top" % (
                        rid, old.get("how")) if old else "no waiting run %s" % rid), "id")
            if e.get("state") != "waiting":
                raise ApiError("conflict", "run %s has started (it holds slots); only a waiting "
                                           "run moves to the top" % rid, "id")
            m = qm.move_to_top(q, e, "owner", me.get("name"), iso())
            qm.save(store, q)
            live = dict(q, entries=[x for x in q["entries"] if not store.run_dead(x)])
        pos = (qm.plan(L, live)[0].get(rid) or {}).get("position")
        return {"id": rid, "moved": dict(m), "position": pos}
    return _write(store, go)


def _active_record(store, rid):
    """Run rid's record in active/, or None when it has none. One that is there but cannot be
    read (its wrapper's os.replace is swapping it) is read again for settings.queue_read_s,
    then ApiError store: never taken for no run (a live run read as ended)."""
    path = store.path("active", rid + ".json")
    d, there = read_record(path, store.settings.queue_read_s)
    if there and d is None:
        raise ApiError("store", "run %s's record %s could not be read for %g s"
                       % (rid, path, store.settings.queue_read_s), "id")
    return d


def run_record(rid, store=None):
    """One run's record, live (with live: true) or ended, or None. An id the lock does not make
    (store.ID_RE, r-) is the invalid error result, never a path; a live record that cannot be
    read (_active_record) is the store error result, never None."""
    store = _store(store)
    try:
        _id(rid, "r")
        d = _active_record(store, rid)
    except ApiError as e:
        return _fail(store, e.code, e.message, e.field)
    if d and store.run_live(d):
        return dict(d, live=True)
    return read_json(store.path("ended", rid + ".json"))


def hold_record(hid, store=None):
    """One hold, live (as the snapshot shows it, live_record: true) or ended, or None. Holds that
    cannot be read (HoldsUnreadable) are never read as no hold: the answer is the write error
    result, {ok: false, error: {code: store}, snapshot}. An id the lock does not make
    (store.ID_RE, h-) is the invalid error result."""
    store = _store(store)
    try:
        _id(hid, "h")
    except ApiError as e:
        return _fail(store, e.code, e.message, e.field)
    L = lay.in_force(store, adopt=False)["layout"]
    try:
        h = hl.find(store, L, hid)
    except OSError as e:            # HoldsUnreadable among them (fail closed)
        return _fail(store, "store", str(e))
    if h is not None:
        return dict(hl.entry(h), live_record=True)
    return read_json(store.path("ended", hid + ".json"))

