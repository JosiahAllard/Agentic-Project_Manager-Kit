"""CLAUDE-SPECIFIC SEAM of kitlib.pmwatch (ARCHITECTURE "Host split").

Everything here knows how Claude Code lays out its files: the ~/.claude* config folders, their
projects/<folder>/<id>.jsonl transcripts with subagent transcripts under <id>/, the
"custom-title" record that names a session, and the machine-lock alerts folder under ~/.claude.
Moved here from kitlib/pmwatch by the host split (step 1); step 2 puts it behind SessionHost.
Nothing in kitlib.pmwatch reads these paths or formats.
"""
import json
import os

# The kit modules of Claude's own that the watcher's checks import late (kitlib.pmwatch.preload):
# the PLUGIN check's account folder refresh, after the first difference.
PRELOAD = ("kitlib.pluginsource.claude",)


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
                paths = [real]
                sub = os.path.join(os.path.dirname(real), fn[:-6])
                for dp, _, fs in os.walk(sub):
                    paths += [os.path.join(dp, x) for x in fs if x.endswith(".jsonl")]
                for q in paths:
                    try:
                        m = os.path.getmtime(q)
                    except OSError:
                        continue
                    newest = m if newest is None or m > newest else newest
    return newest


def newest_finder(roots=None):
    """A name -> newest-mtime-or-None function over these roots (default: every account's
    projects folder, listed again on each call so a new account folder is picked up)."""
    if roots is None:
        return lambda name: newest_transcript(name, project_roots())
    return lambda name: newest_transcript(name, roots)
