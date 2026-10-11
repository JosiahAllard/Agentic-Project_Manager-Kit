"""kitlib.pmstart: the PM session-start checklist.

Pure functions that return data; the hook (claude/hooks/pm_session_start.py) prints it. Every check
takes its process list or filesystem reads as parameters so tests can fake them.
"""
import datetime
import glob
import json
import os
import re
import subprocess

from kitlib.config import user as kuser

KEY = "pm_start"
TIMEOUT = 4

_PS = ("Get-CimInstance Win32_Process | Select-Object Name,CommandLine,ExecutablePath,"
       "ProcessId,ParentProcessId,@{n='Created';e={$_.CreationDate.ToString('o')}}"
       " | ConvertTo-Json -Compress")


def load_config(path=None):
    """The "pm_start" dict from the user-profile config, or None when the file or key is missing.
    With no `path`, a machine config location that cannot be told (kitlib.config.user.locate BAD)
    raises kitlib.config.ConfigError: never read as "no config"."""
    p = os.path.expanduser(path) if path else kuser.readable_file()
    try:
        with open(p, encoding="utf-8-sig") as f:
            cfg = json.load(f).get(KEY)
    except (OSError, ValueError, AttributeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _read(path):
    """A JSON file's text; utf-8-sig because PowerShell writes the file with a BOM."""
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def list_processes():
    """All processes as dicts with Name, CommandLine, ExecutablePath, ProcessId, ParentProcessId, Created (PowerShell Get-CimInstance)."""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
                       capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError("powershell exit %d: %s" % (r.returncode, r.stderr.strip()[:200]))
    data = json.loads(r.stdout or "[]")
    return [data] if isinstance(data, dict) else data


def standing(cfg):
    """One "standing rule: <text>" line per string in cfg["standing"]; no lines when the key is missing."""
    return ["standing rule: %s" % t for t in cfg.get("standing") or []]


def eng_review(cfg):
    """One line on cfg["eng_review"] (items, areas or record prefixes the owner reviews).

    No line while the key is absent or the list is empty: nothing is flagged, so nothing changes for the PM.
    Anything but a list of non-empty strings raises (the checklist prints it as "could not check")."""
    if "eng_review" not in cfg:
        return []
    flags = cfg["eng_review"]
    if not isinstance(flags, list) or not all(isinstance(f, str) and f.strip() for f in flags):
        raise TypeError("eng_review must be a list of non-empty strings")
    if not flags:
        return []
    return ["eng_review: ENG questions matching %s go to the hub, not decided; "
            "check the list is current" % ", ".join(flags)]


def hub(cfg):
    url = cfg.get("hub_url")
    if not url:
        raise KeyError("hub_url not configured")
    src = cfg.get("hub_source", "claude/hub/action-hub.html")
    return ["hub: republish the kit's Action Hub page %s to %s with the Artifact tool, "
            "restating the capabilities in docs/action-hub.md (For the PM), to arm the alert; "
            "then process the hub (answers, stories, to-dos, pm_ack)" % (src, url)]


def watcher(cfg, procs):
    """Steps: empty when a pm_watch.py python process is running, else the start step."""
    for p in procs:
        name = (p.get("Name") or "").lower()
        if name.startswith("python") and "pm_watch.py" in (p.get("CommandLine") or ""):
            return []
    cmd = cfg.get("pm_watch_cmd")
    if not cmd:
        raise KeyError("pm_watch_cmd not configured")
    return ["pm_watch.py is not running: start it in the background: %s" % cmd]


def handoff(cfg, globber=glob.glob, mtime=os.path.getmtime):
    mem = cfg.get("memory_dir")
    if not mem:
        raise KeyError("memory_dir not configured")
    pattern = os.path.join(mem, cfg.get("handoff_glob", "pm-handoff-*.md"))
    files = globber(pattern)
    if not files:
        return ["no handoff file matches %s" % pattern]
    newest = max(files, key=mtime)
    return ["read first: %s" % newest]


def reminders(cfg, today, read=None):
    path = cfg.get("reminders_file")
    if not path:
        raise KeyError("reminders_file not configured")
    if read is None:
        read = _read
    items = json.loads(read(path))
    out = []
    for it in sorted(items, key=lambda i: i["due"]):
        if it["due"] <= today.isoformat():
            out.append("reminder due %s: %s" % (it["due"], it["text"]))
    return out


STORE_DIR = chr(92) + "WindowsApps" + chr(92)
_RC = re.compile(r"""(?:^|\s)--remote-control(?:\s+|=)(?:"([^"]*)"|'([^']*)'|(\S+))""")


def rc_name(cmd):
    """The --remote-control value of a claude.exe command line, or None (kitlib.launcher.lookup uses it too)."""
    m = _RC.search(cmd or "")
    return next((g for g in m.groups() if g), None) if m else None


def live_session_names(procs):
    """Remote Control names of launched claude.exe sessions; the Store app (under WindowsApps) is not one."""
    names = set()
    for p in procs:
        if (p.get("Name") == "claude.exe" and p.get("CommandLine")
                and STORE_DIR not in (p.get("ExecutablePath") or "")):
            n = rc_name(p["CommandLine"])
            if n:
                names.add(n)
    return names


def liveness(cfg, procs, read=None):
    path = cfg.get("sessions_file")
    if not path:
        raise KeyError("sessions_file not configured")
    if read is None:
        read = _read
    sessions = json.loads(read(path))
    alive = live_session_names(procs)
    out = []
    for key, e in sessions.items():
        if not isinstance(e, dict) or not e.get("name") or e.get("autostart") is False:
            continue
        if e["name"] not in alive:  # exact, case-sensitive, as kitlib.launcher.lookup.find_exact
            out.append("coordinator %s (%s) is not running: launch.py start --session %s"
                       % (key, e["name"], key))
    return out


def _toplevel(path):
    """The basename of the git repository holding path, or None outside one."""
    from kitlib.git import cmd as gitcmd     # here, not at the top: only this line needs git
    try:
        r = gitcmd.subprocess_run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    top = r.stdout.strip() if r.returncode == 0 else ""
    return os.path.basename(top.rstrip("/")) or None


def known_repos(cfg, read=None, toplevel=_toplevel, kit_dir=None):
    """Every repository the PM knows, sorted: each session's "repo" in sessions_file,
    the PM's own repository (the one holding sessions_file), this kit's repository, and any names
    in the optional "repos" list. The Action Hub builds every repo menu from these; nothing is
    hard-coded, so a new coordinator's repository appears on its own."""
    path = cfg.get("sessions_file")
    if not path:
        raise KeyError("sessions_file not configured")
    if read is None:
        read = _read
    names = {e["repo"] for e in json.loads(read(path)).values()
             if isinstance(e, dict) and isinstance(e.get("repo"), str) and e["repo"]}
    for d in (os.path.dirname(os.path.abspath(path)),
              kit_dir or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))):
        n = toplevel(d)
        if n:
            names.add(n)
    extra = cfg.get("repos") or []
    if not isinstance(extra, list) or not all(isinstance(x, str) and x for x in extra):
        raise ValueError("pm_start.repos must be a list of names")
    names.update(extra)
    folded = {}
    for n in sorted(names):  # one entry per name, whatever its case
        folded.setdefault(n.lower(), n)
    return sorted(folded.values(), key=str.lower)


FETCH_TIMEOUT = 3     # seconds: the kit_sync fetch must not hold the session start up
REMOTE, MAIN = "origin", "main"


def pm_data_settings():
    """The machine config's pm_data settings dict, or None when unset (kitlib.pmdata.machine_settings)."""
    from kitlib import pmdata
    return pmdata.machine_settings()


# git's markers of an operation in progress besides a merge, with the phrase for the refusal line.
_OPERATIONS = (("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
               ("REVERT_HEAD", "a revert is in progress"),
               ("rebase-merge", "a rebase is in progress"),
               ("rebase-apply", "a rebase or am is in progress"))


def kit_sync(settings):
    """Lines for the kit-currency check (docs/pm-data-branch.md "Session start: keep the kit current").

    `settings` is the pm_data dict; None gives no line. The kit checkout is the main checkout of the
    repository holding settings["path"]. It is left alone, with one line saying why, when it is not
    on main, has a merge, cherry-pick, revert or rebase in progress, is not clean or the landing lock is held. Otherwise origin's
    main is fetched (explicit refspec, FETCH_TIMEOUT) and local main is fast-forwarded when it is
    behind; a diverged main is reported, never merged. No config, rebase, reset or force."""
    if not settings:
        return []
    from kitlib.git import cmd as gitcmd, refs, repo
    from kitlib.records import landing
    kit = repo.main_checkout_toplevel(settings["path"])
    if not kit:
        return ["kit not updated: no git checkout found for %s" % settings["path"]]
    branch, why = refs.current_branch(kit)
    if branch is None:
        return ["kit not updated: cannot read the kit's branch: %s" % why]
    if branch != MAIN:
        return ["kit not updated: the kit checkout is on %s, not %s" % (branch or "a detached HEAD", MAIN)]
    merging, why = refs.merge_in_progress(kit)
    if merging is None:
        return ["kit not updated: cannot tell whether a merge is in progress: %s" % why]
    if merging:
        return ["kit not updated: a merge is in progress in the kit checkout"]
    for name, what in _OPERATIONS:
        path, why = repo.git_path(kit, name)
        if path is None:
            return ["kit not updated: cannot look for %s: %s" % (what, why)]
        if os.path.exists(path):
            return ["kit not updated: %s in the kit checkout" % what]
    clean, why = refs.is_clean(kit)
    if clean is None:
        return ["kit not updated: cannot read the kit's status: %s" % why]
    if not clean:
        return ["kit not updated: the kit checkout has uncommitted changes"]
    try:
        held = landing.LandingLock(kit).read()
    except landing.LockError as e:
        return ["kit not updated: the landing lock cannot be read: %s" % e]
    if held:
        return ["kit not updated: the landing lock is held (%s)" % landing.describe(held)]
    tracking = "refs/remotes/%s/%s" % (REMOTE, MAIN)
    _out, why = gitcmd.run(["fetch", "--no-tags", REMOTE, "refs/heads/%s:%s" % (MAIN, tracking)], kit,
                           timeout=FETCH_TIMEOUT)
    if _out is None:
        return ["kit not updated: %s" % why]
    current, why = refs.is_ancestor(tracking, "refs/heads/%s" % MAIN, kit)
    if current is None:
        return ["kit not updated: %s" % why]
    if current:
        return []
    behind, why = refs.is_ancestor("refs/heads/%s" % MAIN, tracking, kit)
    if behind is None:
        return ["kit not updated: %s" % why]
    if not behind:
        return ["kit main has diverged from %s/%s; not merged" % (REMOTE, MAIN)]
    _out, why = gitcmd.run(["merge", "--ff-only", tracking], kit)
    if _out is None:
        return ["kit not updated: %s" % why]
    sha, why = refs.resolve("refs/heads/%s" % MAIN, kit)
    return ["kit updated to %s" % (sha or "?")[:9]]


def checklist(cfg, now=None, procs=None, **fakes):
    """The full session-start text. A failed check is one "could not check X: reason" line."""
    today = (now or datetime.datetime.now()).date()
    lines = []
    box = {}

    def procs_once():
        if "p" not in box:
            box["p"] = procs if procs is not None else list_processes()
        return box["p"]

    checks = [
        ("standing", lambda: standing(cfg)),
        ("eng_review", lambda: eng_review(cfg)),
        ("hub", lambda: hub(cfg)),
        ("watcher", lambda: watcher(cfg, procs_once())),
        ("handoff", lambda: handoff(cfg, **fakes.get("handoff", {}))),
        ("reminders", lambda: reminders(cfg, today, **fakes.get("reminders", {}))),
        ("liveness", lambda: liveness(cfg, procs_once(), **fakes.get("liveness", {}))),
        ("kit_sync", lambda: kit_sync(fakes.get("pm_data", pm_data_settings)())),
    ]
    for name, fn in checks:
        try:
            lines.extend(fn())
        except Exception as e:
            lines.append("could not check %s: %s: %s" % (name, type(e).__name__, e))
    return "PM session start checklist:\n" + "\n".join("- " + l for l in lines)
