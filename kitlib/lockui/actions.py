"""The schedule window's actions: Client maps each one to one kitlib.lock.api call (the only way
the window writes the lock), and the parsers turn its dialogs' text into API input. Pure apart
from Client's calls; parsers raise ValueError with a message for the window to show.

Who acts is decided by the API (kitlib.lock.api.who): on the machine's store from this
process's tree, whatever the actor says (only its name is kept), so the window acts as the owner
only when the owner runs it. A scratch store (tests, the screenshot demo) takes the actor as given.
"""
import datetime as dt
import os
import re

from kitlib.lock import api as lock_api
from kitlib.lock.layout import DIVISION_KEYS

UTC = dt.timezone.utc
NOW_SLACK = dt.timedelta(seconds=30)    # a window starting this close to now starts now
DEFAULT_NOTE = "reserve window"
SCOPES = ("whole", "half", "slots")
MAX_SLOT = 1023                         # a slot number above this is a typo, not a slot
NO_STATS = "1970-01-01T00:00:00Z"       # since and until of the snapshot's stats: no history file
BARE = re.compile(r"\d{1,2}:\d{2}")


class Client:
    """The window's one door to the lock. store None is the machine's: one Store, made by
    make_store (default kitlib.lock.store.Store) on first use and kept for every later call."""

    def __init__(self, store=None, actor=None, api_mod=None, make_store=None):
        self._store = store
        self.actor = actor or {"name": "reserve_ui"}
        self.api = api_mod or lock_api
        if make_store is None and api_mod is None:
            from kitlib.lock.store import Store as make_store
        self._make_store = make_store

    @property
    def store(self):
        if self._store is None and self._make_store is not None:
            self._store = self._make_store()
        return self._store

    # reads
    def snapshot(self):
        """The snapshot with an empty stats range: the window shows no stats, and stats over the
        default 7 days would read and parse every history line of the month on each refresh."""
        return self.api.snapshot(store=self.store, since=NO_STATS, until=NO_STATS)

    def history(self, days=7):
        return self.api.history(store=self.store, days=days)

    def history_stamp(self):
        """(name, size, mtime_ns) of each file in the store's history folder, or None when the
        store has no folder to look at (a stand-in): what changes when a history line is
        written, read without parsing a line."""
        path = getattr(self.store, "path", None)
        if path is None:
            return None
        try:
            folder = path("history")
            return tuple(sorted((n, st.st_size, st.st_mtime_ns) for n, st in
                                ((n, os.stat(os.path.join(folder, n))) for n in os.listdir(folder))))
        except OSError:
            return None

    # writes: each returns the API's {ok, result | error, snapshot}
    def reserve(self, scope, start, end, note, now=None):
        """A whole or half owner hold over the dragged window [start, end) (aware datetimes)."""
        return self.hold(reserve_spec(scope, start, end, note, now or dt.datetime.now(UTC)))

    def hold(self, spec):
        return self.api.hold_create(self.actor, spec, store=self.store)

    def edit(self, hid, changes):
        return self.api.hold_edit(self.actor, hid, changes, store=self.store)

    def release(self, hid):
        return self.api.hold_end(self.actor, hid, store=self.store)

    def move_to_top(self, rid):
        return self.api.move_to_top(self.actor, rid, store=self.store)

    def game_mode(self, enabled):
        return self.api.set_game_mode(self.actor, bool(enabled), store=self.store)

    def games(self, rows):
        return self.api.set_games(self.actor, rows, store=self.store)

    def division(self, division):
        return self.api.set_division(self.actor, division, store=self.store)


class HistoryCache:
    """The history lines the window last read, read again only when the history files changed
    (Client.history_stamp: they are only appended to) or when forced (the Refresh button).
    Parsing them is most of a refresh's reading. One caller at a time."""

    def __init__(self):
        self.lines, self.stamp = None, None

    def due(self, stamp, force=False):
        return force or self.lines is None or stamp is None or stamp != self.stamp

    def get(self, stamp, fetch, force=False):
        """The lines while the history files have `stamp` (taken before fetch, so a line written
        meanwhile is read again next time); fetch() reads them. A fetch that raises leaves the
        cache as it was."""
        if self.due(stamp, force):
            self.lines, self.stamp = fetch(), stamp
        return self.lines


# ------------------------------------------------------------------ specs
def iso_utc(t):
    """An aware datetime as the API's ISO UTC text ('2026-10-08T12:00:00Z')."""
    if t.tzinfo is None:
        raise ValueError("a time needs its offset")
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note(note):
    return (note or "").strip() or DEFAULT_NOTE


def reserve_spec(scope, start, end, note, now):
    """The hold_create spec of a dragged window: no start when it starts within NOW_SLACK of now
    (the hold then starts at once), the note or 'reserve window'."""
    if scope not in ("whole", "half"):
        raise ValueError("pick whole or half")
    if end <= start:
        raise ValueError("the window ends before it starts")
    spec = {"owner": "owner", "scope": scope, "end": iso_utc(end), "note": _note(note)}
    if start > now + NOW_SLACK:
        spec["start"] = iso_utc(start)
    return spec


def parse_ranges(text):
    """'0-2, 5' -> [0, 1, 2, 5]; '' -> []. A slot above MAX_SLOT is refused."""
    out = []
    text = (text or "").strip()
    if not text:
        return out
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not m:
            raise ValueError("slots are numbers and ranges like 0-2, 5 (not %r)" % part.strip())
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        if b < a:
            raise ValueError("the range %s runs backwards" % part.strip())
        if b > MAX_SLOT:
            raise ValueError("a slot is at most %d (not %d)" % (MAX_SLOT, b))
        out += range(a, b + 1)
    return sorted(set(out))


def is_bare(text):
    """True for a bare 'HH:MM' (no date, no offset)."""
    return bool(BARE.fullmatch((text or "").strip()))


def parse_local(text, ref, day=None):
    """A local time from a dialog: 'HH:MM' (on `day`'s local day, default ref's),
    'YYYY-MM-DD HH:MM', or '+90m' / '+2h' / '+1.5h' after ref (an aware datetime). Returns a
    local aware datetime; anything it cannot read or represent raises ValueError."""
    t = (text or "").strip()
    try:
        m = re.fullmatch(r"\+(\d+(?:\.\d+)?)\s*([mh])", t)
        if m:
            n = float(m.group(1))
            return ref + (dt.timedelta(minutes=n) if m.group(2) == "m" else dt.timedelta(hours=n))
        if BARE.fullmatch(t):
            r = (day or ref).astimezone()
            hh, mm = map(int, t.split(":"))
            return dt.datetime(r.year, r.month, r.day, hh, mm).astimezone()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}", t):
            return dt.datetime.strptime(t, "%Y-%m-%d %H:%M").astimezone()
    except (ValueError, OverflowError, OSError):
        pass
    raise ValueError("a time is HH:MM, YYYY-MM-DD HH:MM, +90m or +2h (not %r)" % t)


def _end(text, start, ref, day=None):
    """An end time: a bare HH:MM not after start is the next day's (22:00 to 02:00)."""
    end = parse_local(text, ref, day)
    if is_bare(text) and end <= start:
        try:
            end = (end.replace(tzinfo=None) + dt.timedelta(days=1)).astimezone()
        except (OverflowError, OSError):
            raise ValueError("the end %r is out of range" % text.strip())
    return end


def _iso(t):
    try:
        return iso_utc(t)
    except (OverflowError, OSError):
        raise ValueError("the time is out of range")


def hold_spec(scope, start_text, end_text, note, cpu_text, gpu_text, ref):
    """The hold_create spec of the Hold dialog. Blank start: now. End: a time (a bare HH:MM not
    after the start is the next day's), or +90m / +2h after the start. scope slots names its cpu
    and gpu slots; whole and half pick their own."""
    if scope not in SCOPES:
        raise ValueError("pick whole, half or slots")
    start = parse_local(start_text, ref) if (start_text or "").strip() else None
    if not (end_text or "").strip():
        raise ValueError("a hold needs an end")
    end = _end(end_text, start or ref, start or ref)
    if end <= (start or ref):
        raise ValueError("the hold ends before it starts")
    spec = {"owner": "owner", "scope": scope, "end": _iso(end), "note": _note(note)}
    if start is not None:
        spec["start"] = _iso(start)
    if scope == "slots":
        slots = {"cpu": parse_ranges(cpu_text), "gpu": parse_ranges(gpu_text)}
        if not (slots["cpu"] or slots["gpu"]):
            raise ValueError("a slots hold names some cpu or gpu slot")
        spec["slots"] = slots
    return spec


def edit_changes(hold, start_text, end_text, note, ref):
    """The hold_edit changes of the Edit dialog: only fields that differ. A bare HH:MM is on the
    hold's own local day (an end not after the start rolls to the next day); +90m / +2h count from
    ref (now). An owner hold's start moves (the API refuses once it has started); agent and game
    holds change end and note."""
    out = {}
    try:
        day = dt.datetime.fromisoformat((hold.get("start") or "").replace("Z", "+00:00"))
    except ValueError:
        day = None
    if day is not None and day.tzinfo is None:
        day = None
    start = day
    if (start_text or "").strip():
        if hold.get("owner") != "owner":
            raise ValueError("only an owner hold's start moves")
        start = parse_local(start_text, ref, day)
        out["start"] = _iso(start)
    if (end_text or "").strip():
        out["end"] = _iso(_end(end_text, start or ref, ref, day))
    if (note or "").strip() != (hold.get("note") or ""):
        out["note"] = (note or "").strip()
    return out


def division_from_fields(fields):
    """The Division dialog's text fields -> set_division's division: numbers (gpu_share: one
    number or a list per card, '4, 2'); a blank field takes its default; all blank -> None (every
    default)."""
    out = {}
    for k, v in fields.items():
        if k not in DIVISION_KEYS:
            raise ValueError("%s is not a division key" % k)
        v = (v or "").strip()
        if not v:
            continue
        try:
            if k == "gpu_share" and "," in v:
                out[k] = [int(x) for x in v.split(",")]
            else:
                out[k] = int(v)
        except ValueError:
            raise ValueError("%s is a whole number%s (not %r)" % (
                k, " or one per card like 4, 2" if k == "gpu_share" else "", v))
    return out or None


def division_fields(division):
    """A division -> the Division dialog's text, one field per DIVISION_KEYS ('' when unset)."""
    division = division or {}
    out = {}
    for k in DIVISION_KEYS:
        v = division.get(k)
        out[k] = "" if v is None else ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    return out


def games_from_text(text):
    """The Games dialog: one game a line, 'Exe.exe [pin|nopin] [name...]'; blank lines and #
    comments skipped. pin left out lets the lock decide (off for a known anti-cheat
    title). An exe name holds no spaces."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        row = {"exe": words[0]}
        rest = words[1:]
        if rest and rest[0].lower() in ("pin", "nopin"):
            row["pin"] = rest.pop(0).lower() == "pin"
        if rest:
            row["name"] = " ".join(rest)
        rows.append(row)
    return rows


def games_text(games):
    """The games list (the snapshot's game.games) as the Games dialog's text."""
    out = []
    for g in games or []:
        words = [g.get("exe") or "", "pin" if g.get("pin") else "nopin"]
        if g.get("name") and g["name"] != g.get("exe"):
            words.append(g["name"])
        out.append("  ".join(words) + "\n")
    return "".join(out)


def attempt(call):
    """call()'s result, or an error result for any exception it raises (code invalid for a
    ValueError from a parser, else error, naming the exception), so a dialog always has
    something to show."""
    try:
        return call()
    except ValueError as e:
        return {"ok": False, "error": {"code": "invalid", "message": str(e)}}
    except Exception as e:                      # the window shows it rather than losing it
        return {"ok": False, "error": {"code": "error", "message": "%s: %s" % (type(e).__name__, e)}}


def result_text(res):
    """What to tell the owner after a write: the error ('forbidden: ...'), the result's notes, or
    None when there is nothing to say."""
    if not res.get("ok"):
        e = res.get("error") or {}
        return "%s: %s" % (e.get("code") or "error", e.get("message") or "")
    notes = (res.get("result") or {}).get("notes") or []
    return "\n".join(notes) or None
