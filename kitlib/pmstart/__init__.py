"""kitlib.pmstart: the PM session-start checklist.

Pure functions that return data; the hook (boot/hook.py claude SessionStart pm) prints it. Every check
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
from kitlib.host import registry
from kitlib.pmstart import fglock
from kitlib.proc import spawn

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


def list_processes(timeout=TIMEOUT):
    """All processes as dicts with Name, CommandLine, ExecutablePath, ProcessId, ParentProcessId, Created (PowerShell Get-CimInstance).
    subprocess.TimeoutExpired after `timeout` seconds (the session start's TIMEOUT by default)."""
    r = spawn.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
                  capture_output=True, text=True, timeout=timeout)
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
# With no pm_watch_cmd, the watcher line names the kit's own pm_watch.py in stream mode.
PM_WATCH_DEFAULT_CMD = 'python -u "%s" --stream' % os.path.join(KIT_ROOT, "tools", "pm_watch.py")
BEAT_STALE = 300      # seconds: a heartbeat older than this is a watcher that stopped polling


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


HUB_SITE_TIMEOUT_S = 1.5    # seconds each ask of the hub server waits (two asks at most)
HUB_TOOL = os.path.join(KIT_ROOT, "tools", "hub.py")
HUB_ERROR_MAX = 400         # characters of a rejected `hub` key's message shown in its line


# How kitlib.config ends a rule when it refuses a value: `, not ` and the value as Python writes
# a string (repr: quoted, a quote or backslash inside it escaped).
_ECHOED = re.compile(r""", not (?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""", re.S)
_ECHO_LEFT = re.compile(r""", not ['"]""")


def _without_values(text):
    """The configuration error `text` with every rejected value taken out: each `, not <value>`
    goes, so a problem still names its key and its rule (`hub.access.team must be an Access team
    name (one DNS label)`) and holds nothing the owner wrote as a value: no email, audience tag
    or path. A `, not ` whose value does not end as a written string does is cut with all that
    follows it. Key names stay, an unknown one the config named too."""
    text = _ECHOED.sub("", text)
    left = _ECHO_LEFT.search(text)
    return text[:left.start()] if left else text


class HubSettingsError(Exception):
    """The machine config has a `hub` key that cannot be used."""


class NoHubToken(Exception):
    """Neither token file of the hub (agent or owner) is there to read."""


def hub_settings():
    """The hub Settings (kitlib.hubsite.settings.load): the `hub` key's, or every default (port
    8790) when the machine config has no `hub` key. HubSettingsError when they cannot be had and
    the raw file holds a `hub` key, also when it is the machine config's check that refuses; any
    other error as it is raised: the config could not be read, with no `hub` key in it."""
    from kitlib.config.types import ConfigError
    from kitlib.hubsite import settings as hubsettings
    try:
        return hubsettings.load()
    except Exception as e:
        try:
            has_key = machine_config().get(hubsettings.KEY) is not None
        except ConfigError:
            has_key = False
        if has_key:
            raise HubSettingsError(str(e))
        raise


def _hub_client(port):
    """The real client, after a look that a token file is there: NoHubToken when neither is."""
    from kitlib.hubsite import auth, client
    folder = auth.folder()
    if not any(auth.read_token(folder, role) for role in auth.ROLES):
        raise NoHubToken()
    return client.Client(port, tokens=folder, timeout=HUB_SITE_TIMEOUT_S)


def _wait_running(procs):
    """Whether a `hub.py wait --pending` process is in `procs` (a list, or a function giving
    one: asked only when the server is up; a list that cannot be had counts as none)."""
    try:
        procs = procs() if callable(procs) else procs
    except Exception:
        return False
    for p in procs or []:
        cmd = p.get("CommandLine") or ""
        if (p.get("Name") or "").lower().startswith("python") and "hub.py" in cmd \
                and " wait" in cmd and "--pending" in cmd:
            return True
    return False


def hub_site(procs=(), load=None, make_client=_hub_client):
    """The hub site's lines, on the `hub` key's settings or, with no key, on the defaults (port
    8790): the site is the hub whether or not the key is written. Asks only the loopback server,
    with HUB_SITE_TIMEOUT_S per ask; starts nothing. A machine config that cannot be read is one
    line naming the exception's class (the test home fence is not caught). A failure of the
    server side is one line naming no more than the port and the exception's class, so no token,
    URL or message of the server reaches the text. The one message shown is a rejected `hub`
    key's own, without the value it refused (_without_values): key names, the rules, and the
    machine config's path when the settings name it; never a value written in the key."""
    try:
        site = (load or hub_settings)()      # looked up at the call, so a test can replace it
    except HubSettingsError as e:
        # the values go first: a cut or a joined line could split one and leave half of it
        text = " ".join(_without_values(str(e)).split())[:HUB_ERROR_MAX]
        return ["hub: the hub key of the machine config cannot be used: %s" % text]
    except Exception as e:
        return ["hub: the machine config could not be read (%s)" % type(e).__name__]
    from kitlib.hubsite import serve
    port = site.port
    try:
        hub = make_client(port)
        health = hub.get("/health", timeout=HUB_SITE_TIMEOUT_S)
    except NoHubToken:
        return ["hub: no hub token file is there to read: run python \"%s\" setup" % HUB_TOOL]
    except serve.NotRunning:
        return ["hub: hub server down (nothing listens on port %d): start it with python \"%s\" serve"
                % (port, HUB_TOOL)]
    except Exception as e:
        return ["hub: server did not answer as the hub does (%s)" % type(e).__name__]
    if health.get("ok") is False:
        return ["hub: server did not answer as the hub does (health not ok)"]
    try:
        items = hub.get("/pending", timeout=HUB_SITE_TIMEOUT_S).get("items")
        items = items if isinstance(items, list) else []
    except Exception as e:
        return ["hub: server up on port %d, pending not asked (%s)" % (port, type(e).__name__)]
    lines = ["hub: server up on port %d, %d pending" % (port, len(items))]
    if items:
        lines.append("hub: process the %d pending item(s): python \"%s\" pending, then handle, ack or log "
                     "each" % (len(items), HUB_TOOL))
    if _wait_running(procs):
        lines.append("hub: wait --pending is running")
    else:
        lines.append("hub: start in the background: python \"%s\" wait --pending (it exits with one HUB "
                     "line per item; handle the items, then start it again)" % HUB_TOOL)
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


def session_images(path=None):
    """The process images a host's launched session runs as (each hosts.json profile's
    session_process; claude.exe on Claude), matched exactly against a process's Name."""
    return frozenset(registry.union('session_process', path))


def rc_name(cmd):
    """The --remote-control value of a session's command line, or None (kitlib.launcher.lookup uses it too)."""
    m = _RC.search(cmd or "")
    return next((g for g in m.groups() if g), None) if m else None


def live_session_names(procs):
    """Remote Control names of launched sessions (session_images); the Store app (under WindowsApps) is not one."""
    names = set()
    images = session_images()
    for p in procs:
        if (p.get("Name") in images and p.get("CommandLine")
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
    none when all are current. Stubs are opt-in (decision 21 in docs/multi-host-plan.md section 7),
    so a repository that uses none has no line. A folder that is not there, or a check that raises,
    is that repo's own "could not check stubs" line."""
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


def repo_prep(repos=None, prep=None):
    """The first-use repo prep (kitlib.git.prep.run; docs/new-worktree.md "First-use prep") of each
    repository in launcher.repos, the repositories the kit's workers make worktrees in: its lines,
    none for a repository already prepared. A folder that is not there is that repo's own "could
    not prep" line; prep never raises."""
    if prep is None:
        from kitlib.git import prep as gitprep
        prep = gitprep.run
    repos = launcher_repos() if repos is None else repos
    lines = []
    for name in sorted(repos):
        path = repos[name]
        if not os.path.isdir(path):
            lines.append("could not prep %s: no repository folder %s" % (name, path))
            continue
        lines.extend(prep(path))
    return lines


def plugin_settings():
    """The machine config's plugin Settings (kitlib.pluginsource.load)."""
    from kitlib import pluginsource
    return pluginsource.load()


def plugin_current(settings=None, kit=None, folders=None):
    """Lines when the pm-kit plugin export is missing or behind the kit's
    refs/remotes/<remote>/main (which kit_sync, just before, has fetched), an account folder is not
    migrated, or one's last refresh failed (kitlib.pluginsource.current_lines; docs/plugin-source.md
    "Behind warnings"). None for `settings` reads the machine config; a loader that gives None
    checks nothing. None for `folders`: this machine's account folders."""
    from kitlib import pluginsource
    s = plugin_settings() if settings is None else settings
    if s is None:
        return []
    return pluginsource.current_lines(kit or pluginsource.kit_root(), s, folders=folders)


AGENT_TEMP_ENV = "AGENT_TEMP"
AGENT_TEMP_TASK = "agent-temp-sweep"


def agent_temp_env():
    """AGENT_TEMP from this process's environment, else the user environment in the registry
    (kitlib.launcher.host._user_env: a variable machine_setup.py set after this session started)."""
    v = os.environ.get(AGENT_TEMP_ENV)
    if v:
        return v
    from kitlib.launcher.host import _user_env
    return _user_env(AGENT_TEMP_ENV)


def agent_temp_sweep_key():
    """The machine config's agent_temp_sweep value (kitlib.config.user.user_config), or None."""
    return kuser.user_config().get("agent_temp_sweep")


def agent_temp_task_exists(name=AGENT_TEMP_TASK):
    """Whether the scheduled task `name` is registered (kitlib.machinesetup.agenttemp.task_registered,
    TIMEOUT seconds): False only when schtasks says it does not exist; RuntimeError or OSError when
    schtasks fails another way (access denied, a timeout) or cannot be run."""
    from kitlib.machinesetup.agenttemp import task_registered
    return task_registered(name, TIMEOUT)


def agent_temp_lines(env=None, sweep_key=None, task_exists=None):
    """Warnings for the agents' temp folder set-up (docs/machine-setup.md "Agent temp init"): AGENT_TEMP
    unset, the machine config without agent_temp_sweep, the sweep task not registered. None when all
    three are in place. A task check that fails is its own "could not check the agent temp sweep task"
    line, after the other two; a failing env or config reader is the checklist's "could not check
    agent_temp" line."""
    env, sweep_key = env or agent_temp_env, sweep_key or agent_temp_sweep_key
    task_exists = task_exists or agent_temp_task_exists
    lines = []
    if not env():
        lines.append("%s is not set (machine_setup.py plans it)" % AGENT_TEMP_ENV)
    if not sweep_key():
        lines.append("agent_temp_sweep is not set in the machine config (machine_setup.py plans it)")
    try:
        there = task_exists(AGENT_TEMP_TASK)
    except Exception as e:      # access denied, a timeout, no schtasks: unknown, never "not registered"
        lines.append("could not check the agent temp sweep task %s: %s: %s"
                     % (AGENT_TEMP_TASK, type(e).__name__, e))
        return lines
    if not there:
        lines.append("agent temp sweep task %s is not registered (machine_setup.py --apply registers it, "
                     "the owner's act)" % AGENT_TEMP_TASK)
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
        ("hub", lambda: hub_site(procs_once, **fakes.get("hub_site", {}))),
        ("watcher", lambda: watcher(cfg, procs_once(), **fakes.get("watcher", {}))),
        ("handoff", lambda: handoff(cfg, **fakes.get("handoff", {}))),
        ("reminders", lambda: reminders(cfg, today, **fakes.get("reminders", {}))),
        ("liveness", lambda: liveness(cfg, procs_once(), **fakes.get("liveness", {}))),
        ("kit_sync", lambda: kit_sync(fakes.get("pm_data", pm_data_settings)())),
        ("plugin_current", lambda: plugin_current(**fakes.get("plugin_current", {}))),
        ("stubs", lambda: stubs(**fakes.get("stubs", {}))),
        ("repo_prep", lambda: repo_prep(**fakes.get("repo_prep", {}))),
        ("agent_temp", lambda: agent_temp_lines(**fakes.get("agent_temp", {}))),
        ("foreground_lock", lambda: fglock.check(cfg, **fakes.get("foreground_lock", {}))),
    ]
    for name, fn in checks:
        try:
            lines.extend(fn())
        except Exception as e:
            lines.append("could not check %s: %s: %s" % (name, type(e).__name__, e))
    return "PM session start checklist:\n" + "\n".join("- " + l for l in lines)
