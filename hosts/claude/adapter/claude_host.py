"""CLAUDE-SPECIFIC SEAM of kitlib.pmwatch (ARCHITECTURE "Host split").

Everything here knows how Claude Code lays out its files: the ~/.claude* config folders, their
projects/<folder>/<id>.jsonl transcripts with subagent transcripts under <id>/, the
"custom-title" record that names a session, the session registry's bridgeSessionId field (the
Remote Control link), the CLI's usage-limit API error entry, and the machine-lock alerts folder
under ~/.claude.
Moved here from kitlib/pmwatch by the host split (step 1); step 2 puts it behind SessionHost.
Nothing in kitlib.pmwatch reads these paths or formats.
"""
import functools
import glob
import json
import os

# The kit modules of Claude's own that the watcher's checks import late (kitlib.pmwatch.preload):
# the PLUGIN check's account folder refresh, after the first difference.
PRELOAD = ("kitlib.pluginsource.claude",)

TAIL_BYTES = 64 * 1024              # newest_entry reads this much: a limit entry is about 1.5 KB
# The entry types that are the session's own work. Others (title, prompt and index records) can
# follow a limit entry in the file while the session stands still.
CONVERSATION = ("user", "assistant")
# The only window that counts as a stall (the owner's rule: the five-hour window is ignored). Only
# "seven_day" has been seen in a real entry; no real five-hour entry has been, so its value is not
# known, and any other value, unknown ones too, reads as no stall.
WEEKLY = "seven_day"


def default_alerts_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "machine-lock", "alerts")


def project_roots(home=None):
    """The projects folder of every ~/.claude* config folder (one per signed-in account)."""
    h = home or os.path.expanduser("~")
    return [os.path.join(h, d, "projects") for d in sorted(os.listdir(h)) if d.startswith(".claude")]


_TITLES = {}


def session_title(path):
    """The session's name: the custom-title record in the first lines of its main transcript
    (the same name --remote-control gives it), or None. Cached per file; a title never changes."""
    if path not in _TITLES:
        title = None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(60), f):
                    if '"custom-title"' in line:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("type") == "custom-title":
                            title = r.get("customTitle")
                            break
        except OSError:
            return None
        _TITLES[path] = title
    return _TITLES[path]


def subagent_transcripts(main):
    """The subagent transcripts of the main transcript <id>.jsonl, all under the folder <id>/."""
    return [os.path.join(dp, x) for dp, _, fs in os.walk(main[:-len(".jsonl")])
            for x in fs if x.endswith(".jsonl")]


def newest_transcript(name, roots):
    """Newest mtime across the named session's own transcripts: each main <id>.jsonl titled name
    (in any project folder of any account; a junction reached twice counts once) and the
    subagent transcripts under <id>/. None if no main transcript carries the name."""
    seen, newest = set(), None
    for root in roots:
        try:
            folders = os.listdir(root)
        except OSError:
            continue
        for fo in folders:
            d = os.path.join(root, fo)
            try:
                files = os.listdir(d)
            except OSError:
                continue
            for fn in files:
                p = os.path.join(d, fn)
                if not fn.endswith(".jsonl"):
                    continue
                real = os.path.normcase(os.path.realpath(p))
                if real in seen or session_title(real) != name:
                    continue
                seen.add(real)
                for q in [real] + subagent_transcripts(real):
                    try:
                        m = os.path.getmtime(q)
                    except OSError:
                        continue
                    newest = m if newest is None or m > newest else newest
    return newest


def newest_entry(path):
    """The newest conversation entry (CONVERSATION) of a transcript, reading only its last
    TAIL_BYTES, or None: no such entry there, a file that cannot be read, or a line that does not
    parse before one is found (one being written says nothing about the session yet)."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read(TAIL_BYTES)
    except OSError:
        return None
    lines = tail.split(b"\n")
    for raw in reversed(lines[1:] if size > TAIL_BYTES else lines):     # the first was cut
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            return None
        if isinstance(entry, dict) and entry.get("type") in CONVERSATION:
            return entry
    return None


def weekly_reset(entry):
    """resetsAt (epoch seconds) of the CLI's usage-limit API error at the weekly window, else None.
    Reads the structured fields only: the display text is free, and apiErrorStatus is absent from a
    subagent's entry. A resetsAt that is no number gives None."""
    if not isinstance(entry, dict) or entry.get("isApiErrorMessage") is not True \
            or entry.get("apiError") != "usage_limit_reached":
        return None
    params = entry.get("apiErrorParams")
    info = params.get("rate_limit_info") if isinstance(params, dict) else None
    if not isinstance(info, dict):
        info = entry.get("quotaLimits")
    if not isinstance(info, dict) or info.get("rateLimitType") != WEEKLY:
        return None
    reset = info.get("resetsAt")
    return reset if type(reset) in (int, float) else None


def _when(entry):
    stamp = entry.get("timestamp")
    return stamp if isinstance(stamp, str) else ""


def weekly_stop(registry, session_id):
    """resets_at when the newest thing the session did is a weekly usage-limit error, else None.
    `registry` is its <config folder>/sessions folder, `session_id` the record's sessionId. A
    subagent's entries live in its own file under <id>/, not in the main transcript, so a main
    session waiting on a stalled subagent shows nothing in its own file: the subagent file written
    last is read too, and the later of the two newest entries (by their ISO timestamp, the same
    form in both; the main one on a tie) decides. So a subagent's limit entry counts only while
    neither the main session nor a later-written subagent has done anything since."""
    if not isinstance(session_id, str) or not session_id:
        return None
    mains = glob.glob(os.path.join(glob.escape(os.path.dirname(registry)), "projects", "*",
                                   glob.escape(session_id) + ".jsonl"))
    if not mains:
        return None
    subs = []
    for p in subagent_transcripts(mains[0]):
        try:
            subs.append((os.path.getmtime(p), p))
        except OSError:
            continue
    found = [e for e in (newest_entry(mains[0]), newest_entry(max(subs)[1]) if subs else None) if e]
    return weekly_reset(max(found, key=_when)) if found else None


def rc_records(dirs):
    """Claude's session registry records (<config folder>/sessions/<pid>.json, read by
    kitlib.launcher.pipes) in the form kitlib.pmwatch.rc_events and limit_events read: {name, pid,
    proc_start, started_ms, linked, registry, weekly_stop}. linked is None when the record has no
    bridgeSessionId key, else whether it holds a Remote Control bridge session id. registry is the
    record's folder. weekly_stop() reads the transcript (weekly_stop), so only when asked."""
    from kitlib.launcher import pipes
    return [{"name": rec.get("name"), "pid": rec.get("pid"), "proc_start": pipes.proc_start(rec),
             "started_ms": rec.get("startedAt"),
             "linked": bool(rec["bridgeSessionId"]) if "bridgeSessionId" in rec else None,
             "registry": d, "weekly_stop": functools.partial(weekly_stop, d, rec.get("sessionId"))}
            for d, rec in pipes.records(dirs)]


def newest_finder(roots=None):
    """A name -> newest-mtime-or-None function over these roots (default: every account's
    projects folder, listed again on each call so a new account folder is picked up)."""
    if roots is None:
        return lambda name: newest_transcript(name, project_roots())
    return lambda name: newest_transcript(name, roots)
