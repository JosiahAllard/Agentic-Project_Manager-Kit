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
import time

from kitlib.config import user as kuser
from kitlib.pmstart import fglock

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


# This kit's checkout: the PM session's directory is not the kit, so kit paths are made absolute.
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HUB_SOURCE = "claude/hub/action-hub.html"
HUB_MIRROR_TOOL = os.path.join(KIT_ROOT, "tools", "hub_mirror.py")
# With no pm_watch_cmd, the watcher line names the kit's own pm_watch.py in stream mode.
PM_WATCH_DEFAULT_CMD = 'python -u "%s" --stream' % os.path.join(KIT_ROOT, "tools", "pm_watch.py")
BEAT_STALE = 300      # seconds: a heartbeat older than this is a watcher that stopped polling
# The capabilities docs/action-hub.md "For the PM" declares on every publish of the hub page.
HUB_CAPABILITIES = ('{db: {rules: [{path: "parked", read: "view", write: "admin"}]}, '
                    'user: {}, comments: {}}')


def machine_config(path=None):
    """The whole machine config as a dict ({} when the file is missing or is not a JSON object),
    read as load_config reads it: unchecked, so one bad key elsewhere does not stop the hub line.
    ConfigError when its location cannot be told (kitlib.config.user.readable_file)."""
    p = os.path.expanduser(path) if path else kuser.readable_file()
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def current_account():
    """This session's account label (kitlib.launcher.accounts.current_account over the machine
    config and the environment); "default" for a machine with no launcher accounts."""
    from kitlib.launcher import accounts
    return accounts.current_account(machine_config())


def hub_urls(cfg):
    """cfg["hub_urls"] ({account label: artifact URL}), or None when unset. TypeError for
    anything but an object of non-empty strings."""
    urls = cfg.get("hub_urls")
    if urls is None:
        return None
    if not isinstance(urls, dict) or not all(isinstance(k, str) and k.strip() and isinstance(v, str)
                                             and v.strip() for k, v in urls.items()):
        raise TypeError("hub_urls must be an object of account label: artifact URL")
    return urls


def hub_source(cfg, isfile=os.path.isfile):
    """(absolute path, exists) of the hub page source: cfg["hub_source"] or HUB_SOURCE, a relative
    path taken from KIT_ROOT (the PM session's directory is not the kit). TypeError for a value
    that is not a non-empty string."""
    src = cfg.get("hub_source") or HUB_SOURCE
    if not isinstance(src, str):
        raise TypeError("hub_source must be a path string")
    path = os.path.normpath(src if os.path.isabs(src) else os.path.join(KIT_ROOT, src))
    return path, bool(isfile(path))


def _hub_watch_line(url):
    """The ArtifactComments watch step, the same text in both hub branches."""
    return ("hub: start ArtifactComments watch on %s (the owner's sends reach only a session that "
            "watches it), or start it again after a resume or compact" % (url or "the new copy's URL"))


HUB_PROCESS_LINE = "hub: then process the hub (answers, stories, to-dos, pm_ack)"


def hub(cfg, account=None, isfile=os.path.isfile):
    """The hub lines. The URL is hub_urls[<this account>] when hub_urls holds any entry (then
    hub_url is not read, and a set hub_url gets a line saying so); with no hub_urls (or an empty
    one) it is hub_url, the single-copy setting of an install that has not moved to the map.
    A URL: republish the page to it, the watch step, the refill check and the process step.
    None: publish a copy on this account, record it (hub_mirror.py set-url), fill it from the mirror
    (hub_mirror.py restore) and refill it, then the watch and process steps. A page source that
    is not there, or a hub_source that is not a string, replaces the republish or publish step
    with a line saying so; the watch and process steps still print. `account` is a callable giving the label (default
    current_account); it is called only when the label is needed. The hook cannot see the
    session's watch or the live hub, so watch and refill are steps the PM runs, not checks."""
    from kitlib import hubmirror
    try:
        src, exists = hub_source(cfg, isfile)
        bad_src = None
    except TypeError as e:
        src, exists, bad_src = None, False, e
    urls = hub_urls(cfg)
    label = None
    if urls:
        label = (account or current_account)()
        url = urls.get(label)
    else:
        url = cfg.get("hub_url")
    lines = []
    if urls and cfg.get("hub_url"):
        lines.append("hub: pm_start.hub_url is ignored because hub_urls is set; remove it from the "
                     "machine config")
    default_src = os.path.join(KIT_ROOT, os.path.normpath(HUB_SOURCE))
    if bad_src is not None:
        lines.append("hub: pm_start.hub_source is %s, not a path string: fix it in the machine "
                     "config, or remove it to use %s" % (type(cfg.get("hub_source")).__name__, default_src))
    elif not exists:
        lines.append("hub: the page source %s does not exist (pm_start.hub_source); point it at %s"
                     % (src, default_src))
    elif url:
        lines.append("hub: republish the kit's Action Hub page %s to %s with the Artifact tool, "
                     "restating the capabilities in docs/action-hub.md (For the PM), to arm the alert"
                     % (src, url))
    else:
        if label is None:
            label = (account or current_account)()
        lines.append("hub: no Action Hub copy is recorded for account %s: publish the kit's page %s "
                     "with the Artifact tool declaring capabilities %s (docs/action-hub.md, For the PM) "
                     "and pin it; record its URL with `python \"%s\" set-url --account %s <url>`; fill "
                     "it from the mirror with `python \"%s\" restore --out <file>` and send "
                     "every batch file it names with ArtifactData (an empty mirror gives none); refill "
                     "the regenerated collections (%s)"
                     % (label, src, HUB_CAPABILITIES, HUB_MIRROR_TOOL, label, HUB_MIRROR_TOOL,
                        hubmirror.regenerate_text()))
    lines.append(_hub_watch_line(url))
    if url:
        lines.append("hub: refill check: ArtifactData list backlog on %s; empty means a new or "
                     "unfilled copy: refill the regenerated collections (%s)"
                     % (url, hubmirror.regenerate_text()))
    lines.append(HUB_PROCESS_LINE)
    return lines


def read_heartbeat():
    """The pm_watch heartbeat ({pid, beat, stream, started}) at the state file pm_watch uses, or None
    (kitlib.pmwatch.heartbeat_file, read_heartbeat)."""
    from kitlib import pmwatch
    state = pmwatch.load_config().get("state_file") or pmwatch.default_state_file()
    return pmwatch.read_heartbeat(pmwatch.heartbeat_file(state))


def _pm_watch_cmd(cfg):
    """cfg["pm_watch_cmd"] as configured, else the kit's own stream-mode command."""
    return cfg.get("pm_watch_cmd") or PM_WATCH_DEFAULT_CMD


_STREAM_FLAG = re.compile(r"(?:^|\s)--stream(?:\s|$)")
START_SLACK = 2       # seconds: heartbeat and process list may round the same start time apart


def _created(p):
    """A process dict's Created (list_processes, ISO 8601) as epoch seconds; None when absent or unreadable."""
    try:
        return datetime.datetime.fromisoformat(p.get("Created") or "").timestamp()
    except (TypeError, ValueError):
        return None


def watcher(cfg, procs, beat=None, now=None):
    """Steps: empty while pm_watch is alive and streaming, else the step that (re)starts it.

    Alive means all of: the heartbeat pm_watch writes every pass (beat(), default read_heartbeat)
    is younger than BEAT_STALE, its pid is a running python process whose command line has
    pm_watch.py, and that process started when the heartbeat says (within START_SLACK; skipped
    when either start time is unknown), so a reused pid does not pass. A pid alone accepts a
    hung watcher or a --once check; a heartbeat alone stays fresh for a while after a watcher has
    exited on an event. A live watcher whose heartbeat says it is not streaming, and a
    pm_watch_cmd without --stream, each add a line: such a watcher exits after one batch. The
    configured command is printed as written, never changed."""
    running = {}
    for p in procs:
        name = (p.get("Name") or "").lower()
        if name.startswith("python") and "pm_watch.py" in (p.get("CommandLine") or ""):
            running[p.get("ProcessId")] = _created(p)
    hb = (beat or read_heartbeat)()
    now = time.time() if now is None else now
    cmd = _pm_watch_cmd(cfg)
    lines = _watcher_state(hb, running, now, cmd)
    if not _STREAM_FLAG.search(cmd):
        lines.append("pm_start.pm_watch_cmd has no --stream: a watcher started with it exits after "
                     "one batch of events; add --stream to run it under Monitor")
    return lines


def _watcher_state(hb, running, now, cmd):
    if hb is not None and hb["pid"] in running:
        started, created = hb.get("started"), running[hb["pid"]]
        if started is not None and created is not None and abs(started - created) > START_SLACK:
            hb = None              # the pid now belongs to another pm_watch.py
    if hb is not None and hb["pid"] in running:
        age = now - hb["beat"]
        if age > BEAT_STALE:
            return ["pm_watch.py (pid %d) has not polled for %dm: stop that pid and start it under "
                    "Monitor: %s" % (hb["pid"], age // 60, cmd)]
        if hb.get("stream") is False:
            return ["pm_watch.py (pid %d) runs without --stream: it exits after one batch of events; "
                    "stop that pid and start it under Monitor with --stream: %s" % (hb["pid"], cmd)]
        return []
    if running:
        return ["pm_watch.py is running without a heartbeat (an older copy, or another --state): "
                "restart it under Monitor: %s" % cmd]
    if hb is not None:
        return ["pm_watch.py is not running (it last polled %ds ago): start it under Monitor: %s"
                % (max(0, now - hb["beat"]), cmd)]
    return ["pm_watch.py is not running: start it under Monitor: %s" % cmd]


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


def liveness(cfg, procs, read=None, account=None):
    path = cfg.get("sessions_file")
    if not path:
        raise KeyError("sessions_file not configured")
    if read is None:
        read = _read
    sessions = json.loads(read(path))
    alive = live_session_names(procs)
    label = None
    out = []
    for key, e in sessions.items():
        if not isinstance(e, dict) or not e.get("name") or e.get("autostart") is False:
            continue
        if e["name"] not in alive:  # exact, case-sensitive, as kitlib.launcher.lookup.find_exact
            if label is None:     # read once, and only when something is down
                from kitlib.launcher import accounts
                label = (account or current_account)()
                # an unidentified account is left to the launcher's pick-account
                label = "auto" if label == accounts.DEFAULT_LABEL else label
            out.append("coordinator %s (%s) is not running: launch.py start --session %s --account %s"
                       % (key, e["name"], key, label))
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


def launcher_repos():
    """{name: folder} of the machine config's launcher.repos (kitlib.launcher.settings)."""
    from kitlib.launcher import settings
    return settings.load().repos


def stubs(repos=None, check=None):
    """One line per repository in launcher.repos whose stubs are missing or stale (kitlib.stubs.problem_line),
    none when all are current. A folder that is not there, or a check that raises, is that repo's
    own "could not check stubs" line."""
    from kitlib import stubs as kstubs
    check = check or kstubs.problem_line
    repos = launcher_repos() if repos is None else repos
    lines = []
    for name in sorted(repos):
        path = repos[name]
        if not os.path.isdir(path):
            lines.append("could not check stubs (%s): no repository folder %s" % (name, path))
            continue
        try:
            line = check(path)
        except Exception as e:
            line = "could not check stubs (%s): %s: %s" % (name, type(e).__name__, e)
        if line:
            lines.append(line)
    return lines


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
        ("hub", lambda: hub(cfg, **fakes.get("hub", {}))),
        ("watcher", lambda: watcher(cfg, procs_once(), **fakes.get("watcher", {}))),
        ("handoff", lambda: handoff(cfg, **fakes.get("handoff", {}))),
        ("reminders", lambda: reminders(cfg, today, **fakes.get("reminders", {}))),
        ("liveness", lambda: liveness(cfg, procs_once(), **fakes.get("liveness", {}))),
        ("kit_sync", lambda: kit_sync(fakes.get("pm_data", pm_data_settings)())),
        ("stubs", lambda: stubs(**fakes.get("stubs", {}))),
        ("foreground_lock", lambda: fglock.check(cfg, **fakes.get("foreground_lock", {}))),
    ]
    for name, fn in checks:
        try:
            lines.extend(fn())
        except Exception as e:
            lines.append("could not check %s: %s: %s" % (name, type(e).__name__, e))
    return "PM session start checklist:\n" + "\n".join("- " + l for l in lines)
