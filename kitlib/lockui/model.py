"""The schedule window's view of the lock: build() turns one kitlib.lock.api.snapshot() and the
api.history() lines into what reserve_ui.pyw draws (schedule boxes, the agent strip, the
slot map, memory) and says (running runs, queue, agent holds, warnings). Pure: no tkinter, no
store; times in the result are local aware datetimes.

The agent strip holds agent work: runs now (to their expected end), queued runs where the
queue plans them, agent holds ("bookings": an owner or game hold over the same time takes
the slots it covers, shown as lost) and past runs from history (a bar for when the run held its
slots, _x the end it was expected to have; late when it took longer).
"""
import datetime as dt

from kitlib.lock import status as st

COLOURS = {"whole": "#d9534f", "half": "#f0ad4e", "slots": "#e5c07b", "game": "#c678dd",
           "sel": "#5b9bd5", "now": "#4caf50", "agent": "#7f8fa6", "booked": "#9b7fd4",
           "ran": "#3fa796", "late": "#e06c75", "expect": "#e8e8e8", "warn": "#f0ad4e"}
# one per running run, on the slot map, in its details and on the strip
RUN_COLOURS = ("#6f9fd8", "#d8b365", "#8fbf6a", "#c97fb0", "#56b6c2", "#d08770", "#b48ead",
               "#a3be8c")
MAX_LANES = 4               # runs that overlap share the strip side by side, up to this many
OPEN_FOR = dt.timedelta(minutes=30)     # how long a run with no expected end is drawn ahead
DAYS, ROWS = 7, 48          # the schedule: 7 day columns of 48 half-hour rows
BACK_DAYS = 14              # how far back < Earlier goes
ROW = dt.timedelta(minutes=30)


# ------------------------------------------------------------------ the grid
def day_start(now, offset=0):
    """Local midnight of now's day, `offset` days later: the schedule's first day."""
    today = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return today + dt.timedelta(days=offset)


def clamp_offset(offset):
    """The schedule's start in days from today: between BACK_DAYS ago and today."""
    return min(0, max(-BACK_DAYS, offset))


def cell_time(day0, day, row):
    """The start of half-hour row `row` of day column `day` (row ROWS is the next midnight)."""
    return day0 + dt.timedelta(days=day, minutes=30 * row)


def segments(day0, s, e):
    """[(day column, first row, end row)] of [s, e) clipped to the DAYS shown from day0, one
    piece per day; rows are fractional (23:15 is 46.5)."""
    out = []
    for d in range(DAYS):
        ds, de = day0 + dt.timedelta(days=d), day0 + dt.timedelta(days=d + 1)
        a, b = max(s, ds), min(e, de)
        if a < b:
            out.append((d, (a - ds) / ROW, (b - ds) / ROW))
    return out


def drag_window(day0, sel, now):
    """The [start, end) of a drag over rows a..b of day column d (sel (d, a, b)): from the first
    row's start, or now when that has passed, to the last row's end."""
    d, a, b = sel
    return max(cell_time(day0, d, a), now), cell_time(day0, d, b + 1)


# ------------------------------------------------------------------ text
def to_local(s):
    """An API time (ISO with its offset) as a local aware datetime; None when s is not one."""
    if not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone() if t.tzinfo else None


def when(t, now):
    """'14:05' on now's day, else 'Tue 14:05'."""
    t, now = t.astimezone(), now.astimezone()
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%a %H:%M")


def day_time(t):
    return t.astimezone().strftime("%a %H:%M")


def span(seconds):
    """'40 s', '12 min' or '2.5 h'."""
    return ("%d s" % round(seconds) if seconds < 90 else "%d min" % round(seconds / 60)
            if seconds < 5400 else "%.1f h" % (seconds / 3600))


def ranges(nums):
    """[0, 1, 2, 5] -> '0-2,5'"""
    out = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ",".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in out)


def holds_text(cpu, gpu):
    """'slots 0-3 + gpu 1': the cpu and gpu slots a run holds."""
    parts = ["slot%s %s" % ("" if len(cpu) == 1 else "s", ranges(cpu))] if cpu else []
    if gpu:
        parts.append("gpu %s" % ranges(gpu))
    return " + ".join(parts) or "no slots"


def needs_text(cpu, gpu):
    """'6 slots + 1 gpu'"""
    parts = ["%d slot%s" % (cpu, "" if cpu == 1 else "s")] if cpu else []
    if gpu:
        parts.append("%d gpu" % gpu)
    return " + ".join(parts) or "nothing"


def pools_text(d, short=False):
    """{cpu: [0, 1], gpu: [0]} -> 'cpu 0/1, gpu 0' (short: 'c0/1 g0'); '' when empty."""
    d = d or {}
    return (" " if short else ", ").join(
        "%s%s%s" % (k[0] if short else k, "" if short else " ", "/".join(map(str, d[k])))
        for k in ("cpu", "gpu") if d.get(k))


def slots_text(hold, short=False):
    """The slots hold covers, and "for now" while a half hold's are not yet picked
    (picked when it starts; slots_fixed false)."""
    return ((pools_text(hold.get("covers"), short) or "no slots")
            + ("" if hold.get("slots_fixed", True) else " for now"))


def hold_line(h):
    """'owner whole hold h-.. Tue 14:00-Tue 16:00 (cpu 0/1, gpu 0) (note)' for an owner or game
    hold on the schedule."""
    who = ("game %s" % ((h.get("game") or {}).get("name") or (h.get("game") or {}).get("exe") or "")
           if h.get("owner") == "game" else "owner %s" % h.get("scope"))
    return "%s hold %s %s-%s (%s)%s" % (
        who, h.get("id"), day_time(h["_s"]), day_time(h["_e"]), slots_text(h),
        " (%s)" % h["note"] if h.get("note") else "")


def release_prompt(h):
    """The question Release asks; a game hold says game mode makes it again while the game runs."""
    q = "End %s?" % hold_line(h)
    if h.get("owner") == "game":
        q += ("\n\nWhile game mode is on, game mode makes it again while the game runs; turn "
              "Game mode off to keep the slots free.")
    return q


def assign_lanes(items):
    """Puts strip items whose times overlap side by side: sets each item's _lane (first free
    lane, by start) over [_s, max(_e, _x)) and returns the lane count."""
    ends = []
    for it in sorted(items, key=lambda i: i["_s"]):
        end = max(it["_e"], it.get("_x") or it["_e"])
        for k, e in enumerate(ends):
            if e <= it["_s"]:
                ends[k], it["_lane"] = end, k
                break
        else:
            ends.append(end)
            it["_lane"] = len(ends) - 1
    return len(ends)


# ------------------------------------------------------------------ the view
def _timed(h):
    s, e = to_local(h.get("start")), to_local(h.get("end"))
    return dict(h, _s=s, _e=e) if s and e else None


def lost(agent_hold, strong):
    """(lost {pool: [slots]}, [ids of the owner or game holds over the same time covering them])
    of an agent hold: the slots it asked for that it does not cover."""
    asked, has = agent_hold.get("slots") or {}, agent_hold.get("covers") or {}
    gone = {p: sorted(set(asked.get(p) or []) - set(has.get(p) or [])) for p in ("cpu", "gpu")}
    gone = {p: v for p, v in gone.items() if v}
    by = [h["id"] for h in strong
          if h["_s"] < agent_hold["_e"] and agent_hold["_s"] < h["_e"]
          and any(set(v) & set((h.get("covers") or {}).get(p) or []) for p, v in gone.items())]
    return gone, by


def build(snap, history_rows=(), now=None):
    """The window's view of snapshot `snap` and history lines at `now` (aware; default now):
    holds (owner and game, the schedule's boxes), agent_holds, colour {run id: colour}, strip,
    lanes, details / queue / bookings ([(text, tags)] for the side panels), cores {head, cpu,
    gpu} (the slot map), memory {free_mb, total_mb} or None, warnings [text]."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone()
    snap = snap or {}
    holds = [x for x in (_timed(h) for h in snap.get("holds") or []) if x]
    strong = [h for h in holds if h.get("owner") in ("owner", "game")]
    agent_holds = [h for h in holds if h.get("owner") == "agent"]
    for h in agent_holds:
        h["_lost"], h["_lost_to"] = lost(h, strong)
    runs = [r for r in snap.get("runs") or [] if r.get("state") != "waiting"]
    runs.sort(key=lambda r: (r.get("since") or "", r.get("id") or ""))
    colour = {r.get("id"): RUN_COLOURS[k % len(RUN_COLOURS)] for k, r in enumerate(runs)}
    qd = snap.get("queue") or {}
    queue = qd.get("entries") if isinstance(qd.get("entries"), list) else []
    strip = _strip(runs, queue, agent_holds, history_rows or (), now)
    v = {"now": now, "holds": strong, "agent_holds": agent_holds, "runs": runs, "colour": colour,
         "strip": strip, "lanes": min(MAX_LANES, max(1, assign_lanes(strip)))}
    v["details"] = _details(runs, colour, now)
    v["queue"] = _queue(queue, now)
    v["bookings"] = _bookings(agent_holds)
    v["cores"] = _cores(snap, {r.get("id"): r for r in runs}, {h["id"]: h for h in holds})
    mem = (snap.get("hardware") or {}).get("memory_mb") or {}
    v["memory"] = {"free_mb": mem.get("free"), "total_mb": mem["total"]} if mem.get("total") else None
    v["warnings"] = _warnings(snap)
    return v


def _strip(runs, queue, agent_holds, history_rows, now):
    out = []
    for r in runs:
        s = to_local(r.get("held_since") or r.get("since")) or now
        e = to_local(r.get("expected_end"))
        out.append({"type": "run", "id": r.get("id"), "_s": s, "_e": max(e, now) if e else now + OPEN_FOR,
                    "open": e is None, "text": "%s%s/%s (%s, %s) since %s, %s" % (
                        "background " if r.get("background") else "", r.get("repo") or "",
                        r.get("slug") or "", r.get("kind") or "?",
                        holds_text(r.get("cores") or [], r.get("gpus") or []), when(s, now),
                        "expected end " + when(e, now) if e else "end unknown")})
    for q in queue:
        est = to_local(q.get("est_start"))
        if not est:
            continue
        s, exp = max(est, now), q.get("expect_s")
        e = s + dt.timedelta(seconds=exp) if exp else s + OPEN_FOR
        n = q.get("needs") or {}
        out.append({"type": "wait", "id": q.get("id"), "_s": s, "_e": e, "open": not exp,
                    "text": "queued %s/%s (%s, needs %s), #%s in line, planned %s-%s%s" % (
                        q.get("repo") or "", q.get("slug") or "", q.get("kind") or "?",
                        needs_text(n.get("cpu") or 0, n.get("gpu") or 0), q.get("position") or "?",
                        when(s, now), when(e, now) if exp else "?",
                        ", backfill" if q.get("backfill") else "")})
    for h in agent_holds:
        out.append({"type": "booking", "id": h["id"], "_s": h["_s"], "_e": h["_e"], "open": False,
                    "text": "hold %s %s/%s (%s) (%s) %s-%s%s" % (
                        h["id"], h.get("repo") or "", h.get("slug") or "", h.get("state") or "",
                        pools_text(h.get("slots")) or "no slots", day_time(h["_s"]), day_time(h["_e"]),
                        "; lost %s to %s" % (pools_text(h["_lost"]), ", ".join(h["_lost_to"]) or "?")
                        if h["_lost"] else "")})
    for d in history_rows:
        s, e = to_local(d.get("held_since") or d.get("since")), to_local(d.get("ended"))
        if not (s and e):
            continue
        took, exp = (e - s).total_seconds(), d.get("expect_s")
        late = bool(exp) and took > exp
        vs = ("expected %s (%s %s)" % (span(exp), span(abs(took - exp)), "over" if late else "under")
              if exp else "no expected time")
        out.append({"type": "hist", "id": d.get("id"), "_s": s, "_e": max(e, s),
                    "_x": s + dt.timedelta(seconds=exp) if exp else None, "late": late, "open": False,
                    "text": "ran %s/%s (%s, %s) %s-%s: %s, %s%s%s%s" % (
                        d.get("repo") or "", d.get("slug") or "", d.get("kind") or "?",
                        holds_text(d.get("cores") or [], d.get("gpus") or []), when(s, now), when(e, now),
                        span(took), vs,
                        "; peak %.1f cores, %d MB" % (d["peak_cpu_cores"], d["peak_mem_mb"])
                        if d.get("peak_cpu_cores") is not None and d.get("peak_mem_mb") is not None else "",
                        "; its docker work was not measured" if d.get("partial") else "",
                        "; exit %s" % d["code"] if d.get("code") not in (None, 0) else "")})
    return out


def holders(view, s, e, owner=True):
    """Everything over [s, e): owner and game holds (unless owner is False), then agent work."""
    hits = [hold_line(h) for h in view["holds"] if h["_s"] < e and s < h["_e"]] if owner else []
    return hits + [it["text"] for it in sorted(view["strip"], key=lambda i: i["_s"])
                   if it["_s"] < e and s < it["_e"]]


def _details(runs, colour, now):
    """Each running run: what it is, what it holds, start, expected end, OVERRUN, its
    measured peaks. The marker's tag is 'c:<run id>' (its colour)."""
    parts = []
    for r in runs:
        rid = r.get("id") or "?"
        s = to_local(r.get("held_since") or r.get("since"))
        e = to_local(r.get("expected_end"))
        parts += [("■ ", ("c:" + rid,)), ("%s/%s  %s%s  run %s\n" % (
            r.get("repo") or "", r.get("slug") or "", r.get("kind") or "?",
            "  background" if r.get("background") else "", rid), ()),
            ("   holds %s; started %s, %s\n" % (
                holds_text(r.get("cores") or [], r.get("gpus") or []), when(s, now) if s else "?",
                ("expected end %s%s" % (when(e, now), " (from its history)"
                                        if r.get("expect_from") == "history" else ""))
                if e else "no expected end"), ())]
        u = r.get("usage") or {}
        pc, pm = u.get("peak_cpu_cores"), u.get("peak_mem_mb")
        parts.append(("   peak %.1f cores, %d MB" % (pc, pm) if pc is not None and pm is not None
                      else "   peak not measured yet", ()))
        over = r.get("overrun")
        if over and over.get("s") is not None:
            parts.append(("  OVERRUN %s past its expected end" % span(over["s"]), ("bad",)))
        elif e and now > e:
            parts.append(("  %s past its expected end" % span((now - e).total_seconds()), ("warn",)))
        parts.append(("\n", ()))
    return parts


def _queue(queue, now):
    """The queue in order: place, needs, since, planned start, backfill, a move to the top, and
    why it waits. Each run starts with an empty part tagged "move:<run id>", where the window
    puts its Move to top button."""
    parts = []
    for q in queue:
        if q.get("id"):
            parts.append(("", ("move:%s" % q["id"],)))
        since, est = to_local(q.get("since")), to_local(q.get("est_start"))
        n = q.get("needs") or {}
        parts.append(("#%s %s/%s  %s  needs %s, waiting since %s; %s" % (
            q.get("position") or "-", q.get("repo") or "", q.get("slug") or "", q.get("kind") or "?",
            needs_text(n.get("cpu") or 0, n.get("gpu") or 0), when(since, now) if since else "?",
            "starts about " + when(est, now) if est else "start not planned"), ()))
        if q.get("backfill"):
            parts.append((", backfill (it ends before the runs ahead of it can start)", ("warn",)))
        if q.get("parked"):
            parts.append((", parked", ()))
        mv = q.get("moved")
        if mv:
            t = to_local(mv.get("at"))
            parts.append((", moved to the top by %s at %s" % (
                "the owner" if mv.get("by") == "owner" else mv.get("name") or mv.get("by") or "?",
                when(t, now) if t else "?"), ("warn",)))
        if q.get("why"):
            parts.append(("; %s" % q["why"], ("dim",)))
        parts.append(("\n", ()))
    return parts


def _bookings(agent_holds):
    parts = []
    for h in agent_holds:
        parts.append(("%s/%s  %s  %s-%s %s" % (
            h.get("repo") or "", (h.get("slug") or "")[:26], pools_text(h.get("slots")) or "-",
            h.get("start_local") or day_time(h["_s"]), h.get("end_local") or day_time(h["_e"]),
            h.get("state") or ""), ()))
        if h["_lost"]:
            parts.append(("; LOST %s to %s" % (pools_text(h["_lost"]), ", ".join(h["_lost_to"]) or "?"),
                          ("bad",)))
        parts.append(("\n", ()))
    return parts


def _cores(snap, runs, holds):
    """The slot map: each cpu and gpu slot with its holder (a run id), label, and outline: the
    covering hold's scope (whole, half, slots), 'game' or 'agent', or None."""
    sl = snap.get("slots") or {}
    lay = snap.get("layout") or {}

    def box(d, title):
        rid = d.get("holder")
        r = runs.get(rid)
        h = holds.get(d.get("hold")) or {}
        outline = {"owner": h.get("scope") or "whole", "game": "game", "agent": "agent"}.get(
            d.get("hold_owner"))
        return {"slot": d.get("slot"), "title": title, "holder": rid, "background": bool(d.get("background")),
                "hold": d.get("hold"), "outline": outline,
                "label": ((r.get("slug") or r.get("repo") or rid) if r else rid) or "free"}

    cpu = [box(d, "slot %s  t%s" % (d.get("slot"), ranges(d.get("threads") or [])) if d.get("threads")
               else "slot %s" % d.get("slot")) for d in sl.get("cpu") or []]
    gpu = [box(d, "gpu slot %s (card %s)" % (d.get("slot"), d.get("card"))) for d in sl.get("gpu") or []]
    head = "Slots now: %d cpu slot%s, %d gpu slot%s%s" % (
        len(cpu), "" if len(cpu) == 1 else "s", len(gpu), "" if len(gpu) == 1 else "s",
        "; an agent run takes at most %d" % lay["agent_cap"] if lay.get("agent_cap") else "")
    return {"head": head, "cpu": cpu, "gpu": gpu}


_LAYOUT_WAITS = {
    st.IDLE: "a new division waits until nothing runs or queues",
    st.UNWRITTEN: "layout.json is missing or not valid to this kit; it is written once nothing "
                  "runs or queues",
    st.FAILED_LOOK: "a look at the hardware failed (the hardware lines say what): the layout in "
                    "force stays until a look succeeds",
    st.PROVISIONAL: "the layout in force is provisional: it came from a look at the hardware that "
                    "failed (the hardware lines say what), and the first look that succeeds "
                    "replaces it once nothing runs or queues"}


def layout_wait(lay):
    """What the window says about the snapshot layout `lay`'s hardware_change, or None: its
    `reason` (from an older snapshot without one, kitlib.lock.status.pending_reason of its pending
    text), as a sentence here or else the pending text itself; the idle wait when only `changed`
    is set."""
    hc = lay.get("hardware_change") or {}
    reason = hc["reason"] if "reason" in hc else st.pending_reason(hc.get("pending"))
    if reason in _LAYOUT_WAITS:
        return _LAYOUT_WAITS[reason]
    if reason and hc.get("pending"):
        return hc["pending"]
    if hc.get("pending") or hc.get("changed"):
        return _LAYOUT_WAITS[st.IDLE]
    return None


def _warnings(snap):
    out = []
    lay = snap.get("layout") or {}
    if lay.get("warning"):
        out.append(lay["warning"])
    wait = layout_wait(lay)
    if wait:
        out.append(wait)
    if (snap.get("queue") or {}).get("error"):
        out.append(snap["queue"]["error"])
    for k in ("holds_error", "kinds_error"):
        if snap.get(k):
            out.append(snap[k])
    out += ["hardware: %s" % p for p in (snap.get("hardware") or {}).get("problems") or []]
    return out
