"""The status text, rendered from api.snapshot() alone (the audit: one source for text and JSON).

render(snap) -> [line]; record_lines(d) -> [line] for one run or hold record. Nothing here reads
the store or prints: the CLI prints these lines with its prefix, and `status --json` prints the
snapshot itself.
"""
from kitlib.lock import layout as lay
from kitlib.lock.store import local_text, parse_iso

POOLS = lay.POOLS


def _local(s):
    try:
        return local_text(parse_iso(s)) if s else "?"
    except (TypeError, ValueError):
        return str(s)


def moved_by(mv):
    """Who moved a waiting run to the top, for the status line."""
    return "the owner" if mv.get("by") == "owner" else mv.get("name") or mv.get("by") or "?"


def _minutes(since, at):
    try:
        return max(0, int((parse_iso(at) - parse_iso(since)).total_seconds() // 60))
    except (TypeError, ValueError):
        return 0


def _need(row):
    return lay.need_text({"cpu": len(row.get("cores") or []), "gpu": len(row.get("gpus") or [])})


def _slots(sl):
    return ", ".join(lay.slots_text(p, sl[p]) for p in POOLS if (sl or {}).get(p)) or "no slots"


def hold_line(h):
    """One hold of the snapshot as a line."""
    what = h.get("scope")
    if h.get("owner") == "agent":
        what = "%s/%s %s" % (h.get("repo"), h.get("slug"), h.get("kind"))
    elif h.get("owner") == "game":
        what = "game %s" % ((h.get("game") or {}).get("name") or (h.get("game") or {}).get("exe"))
    elif h.get("scope") == "whole":
        what = "whole, the owner's runs only"
    extra = ""
    if h.get("owner") == "agent":
        extra = "; %s" % h.get("state")
        if h.get("lapses_at"):
            extra += ", lapses unclaimed at %s" % _local(h["lapses_at"])
        elif isinstance(h.get("claim"), dict):
            extra += " by run %s" % h["claim"].get("id")
    return "%s hold %s, %s (%s: %s%s; %s-%s local%s)%s" % (
        h.get("owner"), h.get("id"), "live" if h.get("live") else "upcoming", what,
        _slots(h.get("covers")),
        "" if h.get("slots_fixed", True) else " for now, picked again at its start",
        h.get("start_local"), h.get("end_local"), ": " + h["note"] if h.get("note") else "", extra)


# Why layout.in_force's `pending` is set (pending_reason): its text says the same in words.
IDLE = "idle"                   # a changed layout waits until no run is live or queued
UNWRITTEN = "unwritten"         # layout.json missing or invalid: written once no run is live or queued
FAILED_LOOK = "failed_look"     # a failed look at the hardware: the layout from a good look stays
PROVISIONAL = "provisional"     # the layout in use came from a failed look: a good look replaces it
UNCHECKED = "unchecked"         # the layout could not be checked (the queue or store unreadable)
OTHER = "other"                 # a text none of the above: a newer or older kit's
REASONS = (IDLE, UNWRITTEN, FAILED_LOOK, PROVISIONAL, UNCHECKED, OTHER)

# (reason, how in_force's text for it starts or ends), first match wins. The texts are layout's
# own PENDING_* constants, which in_force builds its `pending` from; the tests drive every branch
# of in_force through pending_reason (tests/lock/test_status_reason.py).
_TEXTS = ((UNCHECKED, "start", lay.PENDING_UNCHECKED),
          (FAILED_LOOK, "end", lay.PENDING_FAILED_LOOK),
          (PROVISIONAL, "end", lay.PENDING_PROVISIONAL),
          (UNWRITTEN, "start", lay.PENDING_UNWRITTEN),
          (IDLE, "start", lay.PENDING_IDLE))


def pending_reason(pending):
    """The reason, one of REASONS, for layout.in_force's `pending` (as the snapshot's
    layout.hardware_change.pending and set_division's result carry it): None when nothing is
    pending, OTHER for a text this kit does not give. Callers state the reason from this, not
    from the words of the text."""
    if not isinstance(pending, str) or not pending:
        return None
    for reason, where, text in _TEXTS:
        if pending.startswith(text) if where == "start" else pending.endswith(text):
            return reason
    return OTHER


def waits_for_idle(pending):
    """Whether layout.in_force's pending text is a change that is adopted once no run is live or
    queued (IDLE, or UNWRITTEN: a layout.json written then). Any other reason (a look at the
    hardware that failed, kept out or in use as provisional, or a layout that could not be
    checked) is not settled by an idle machine alone, so its own reason is shown in place of the
    line that the new layout applies then."""
    return pending_reason(pending) in (IDLE, UNWRITTEN)


def render(snap):
    """The `status` text for snapshot snap."""
    out = []
    hw, L = snap["hardware"], snap["layout"]
    out.append("layout (%s): %d threads, %d cpu slots of %s, %d gpu(s) in %d gpu slot(s); an agent "
               "run takes at most %d cores; half is %d cores + %d gpu slot(s)%s" % (
                   "the owner's division" if L["source"] == "config" else "the default division",
                   L["threads"], L["cpu_slots"],
                   "a physical core" if not L["division"].get("cpu_slot_threads")
                   else "%d threads" % L["cpu_slot_threads"],
                   L["gpus"], L["gpu_slots"], L["agent_cap"], L["half"]["cpu"], L["half"]["gpu"],
                   "; WARNING: " + L["warning"] if L.get("warning") else ""))
    why = L["hardware_change"]["pending"]
    if why and not waits_for_idle(why):
        out.append("layout: %s" % why)
    elif L["hardware_change"]["changed"]:
        out.append("hardware or division changed: the new layout applies once nothing is live or "
                   "queued%s" % (" (waiting for that)" if L["hardware_change"]["pending"] else ""))
    if L.get("other_labels"):
        # Another writer's text: repr keeps a line break or a "; " inside a label from passing
        # for the line's own structure.
        out.append("WARNING: a live run carries another slot numbering (%s): an older lock tool "
                   "that counts this machine's cpu or gpu slots its own way writes to this store, "
                   "and its slot numbers are not this layout's" % "; ".join(map(repr, L["other_labels"])))
    ccds = hw.get("ccds") or []
    if len(ccds) > 1:
        out.append("L3 domains: %s" % "; ".join(
            "%d: %s%s%s" % (c["index"], lay.slots_text("cpu", c["cpu_slots"]),
                            " (preferred cores)" if c["preferred"] else "",
                            " (game mode's pick)" if c["game_pick"] else "") for c in ccds))
    for g in hw.get("gpus") or []:
        out.append("gpu %s: %s (%s, %s MB)" % (g.get("index"), g.get("name"), g.get("vendor"),
                                               g.get("vram_mb")))
    for p in hw.get("problems") or []:
        out.append("hardware: %s" % p)
    m = hw.get("memory_mb") or {}
    if m.get("free") is not None and m.get("total"):
        out.append("memory: %d MB free of %d MB" % (m["free"], m["total"]))
    if snap["queue"]["error"]:
        out.append("%s; the slots below are from the run records. Until it reads again no run "
                   "takes slots, waiting runs keep their places and a new run exits 10"
                   % snap["queue"]["error"])
    runs = {r["id"]: r for r in snap["runs"]}
    holds = {h["id"]: h for h in snap["holds"]}
    for p in POOLS:
        rows = snap["slots"][p]
        out.append("%s: %d %sslot%s" % (p, len(rows), "training " if p == "gpu" else "",
                                        "" if len(rows) == 1 else "s"))
        for d in rows:
            r = runs.get(d["holder"])
            if not d["holder"]:
                text = "free"
            elif not r:
                text = "held by run %s (no record yet: starting or ending)" % d["holder"]
            elif r.get("background"):
                text = "held by %s/%s (background training, run %s, pid %s)" % (
                    r["repo"], r["slug"], r["id"], r["pid"])
            else:
                late = (r.get("overrun") or {}).get("s")
                text = "held by %s/%s (%s, %s, run %s, pid %s, %d min%s%s)" % (
                    r["repo"], r["slug"], r["kind"], _need(r), r["id"], r["pid"],
                    _minutes(r.get("held_since") or r.get("since"), snap["at"]),
                    ", %d min past its guide" % (late // 60) if late else "",
                    ", not pinned" if p == "cpu" and r.get("pinned") is False else "")
            h = holds.get(d["hold"])
            if h:
                text += "; %s hold %s until %s" % (h["owner"], h["id"], h["end_local"])
            where = d.get("threads") if p == "cpu" else "card %s" % d.get("card")
            out.append("  %s slot %d (%s): %s" % (p, d["slot"], where, text))
    for r in snap["runs"]:
        if r.get("resource") and r.get("state") in ("held", "bg"):
            out.append("resource %s: held by %s/%s (run %s%s)" % (
                r["resource"], r["repo"], r["slug"], r["id"],
                ", priority %s" % r["priority"] if r.get("priority") else ""))
    for r in snap["runs"]:
        if r.get("stale"):
            out.append("stale: run %s (%s/%s, %s): %s; the next booking's look asks it to stop "
                       "(docs/lock-api.md \"Stale runs\")" % (
                           r["id"], r["repo"], r["slug"], r["kind"], r["stale"]))
    for w in snap["queue"]["entries"] or []:
        if w.get("position"):
            plan = "queue position %s, expected to start %s%s" % (
                w["position"], _local(w["est_start"]) if w.get("est_start")
                else "once what keeps it waiting ends",
                " (ahead of its turn: it ends before the runs ahead of it start)"
                if w.get("backfill") else "")
        else:
            plan = "not yet planned"
        mv = w.get("moved") or {}
        out.append("waiting: %s/%s (%s, %s, run %s%s%s%s) since %s; %s%s%s" % (
            w["repo"], w["slug"], w["kind"], lay.need_text(w["needs"]), w["id"],
            ", priority %s" % w["priority"] if w.get("priority") else "",
            ", resource %s" % w["resource"] if w.get("resource") else "",
            ", within %s (--within)" % lay.slots_text("cpu", w["within_slots"])
            if w.get("within_slots") else "",
            _local(w["since"]), plan, "; moved to the top by %s at %s" % (
                moved_by(mv), _local(mv.get("at"))) if mv else "",
            "; " + w["why"] if w.get("why") else ""))
    if snap["queue"]["entries"] is None:
        for r in snap["runs"]:
            if r.get("state") == "waiting":
                out.append("waiting: %s/%s (%s, run %s); queue position unknown (queue "
                           "unreadable)" % (r["repo"], r["slug"], r["kind"], r["id"]))
    if not snap["runs"]:
        out.append("lock: free (no run holds cores or gpu slots, waits, or runs in the background)")
    for h in snap["holds"]:
        out.append(hold_line(h))
    if snap.get("holds_error"):
        out.append("%s; until they read again no run takes slots and a new run exits 10"
                   % snap["holds_error"])
    elif not snap["holds"]:
        out.append("holds: none")
    g = snap["game"]
    if g["enabled"] or g["games"]:
        run = g.get("running")
        out.append("game mode: %s, %d game(s) listed%s" % (
            "on" if g["enabled"] else "off", len(g["games"]),
            "; %s runs, hold %s on %s%s" % (
                run.get("name") or run.get("exe"), g["hold"], lay.slots_text("cpu", g["slots"]),
                ", pinned: %s" % g["pinned"] if g.get("pinned") is not None else "")
            if run else ""))
    if snap.get("kinds_error"):
        out.append("kinds: %s" % snap["kinds_error"])
    return out


def record_lines(d):
    """`key: value` lines of one run or hold record, empty values left out."""
    return ["%s: %s" % (k, d[k]) for k in sorted(d) if d[k] not in (None, "", [], {})]
