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

from kitlib import kitroot, platform
from kitlib.config import user as kuser
from kitlib.ledger import files as ledger_files, rulebook
from kitlib.host import registry
from kitlib.pmstart import configcheck, consoletasks, fglock
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
    subprocess.TimeoutExpired after `timeout` seconds (the session start's TIMEOUT by default).
    On Linux the rows are read from /proc (kitlib.platform.linux.process.rows), with no timeout;
    OSError when /proc cannot be listed."""
    if platform.NAME == 'linux':
        from kitlib.platform.linux import process as linux_process
        return linux_process.rows()
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
# With no pm_watch_cmd, the watcher line names the kit's own pm_watch.py, run in the background
# without --stream: it exits on an event and the PM starts it again (docs/pm-watch.md).
PM_WATCH_DEFAULT_CMD = 'python -u "%s"' % os.path.join(KIT_ROOT, "tools", "pm_watch.py")
BEAT_STALE = 300      # seconds: a heartbeat older than this is a watcher that stopped polling


def machine_config(path=None):
    """The whole machine config as a dict ({} when the file is missing or is not a JSON object),
    read as load_config reads it: unchecked, so one bad key elsewhere does not stop the hub line.
    With no `path`, a PM repository's local.json is read too (its keys over config.json's: `hub`
    and `launcher` live there, kitlib.config.user.LOCAL_KEYS). ConfigError when its location
    cannot be told (kitlib.config.user.readable_file)."""
    def read(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    if path:
        return read(os.path.expanduser(path))
    data = read(kuser.readable_file())
    loc = kuser.locate()
    if loc.state == kuser.OK and loc.local:
        data.update(read(loc.local))
    return data


def current_account():
    """This session's account label (kitlib.launcher.accounts.current_account over the machine
    config and the environment); "default" for a machine with no launcher accounts."""
    from kitlib.launcher import accounts
    return accounts.current_account(machine_config())


HUB_SITE_TIMEOUT_S = 1.5    # seconds each ask of the hub server waits (three asks at most)
HUB_TOOL = os.path.join(KIT_ROOT, "tools", "hub.py")
HUB_ERROR_MAX = 400         # characters of a rejected `hub` key's message shown in its line


# How kitlib.config and kitlib.pmdata end a rule when they refuse a value: `, not ` and the value
# as Python writes it (repr): a string quoted, a quote or backslash inside it escaped; a number,
# true/false or null as one word, ended by the `; ` that joins problems or by the text's end.
_ECHOED = re.compile(r""", not (?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|[-+\w.]+(?=;|\Z))""", re.S)
_ECHO_LEFT = re.compile(r""", not """)


def _without_values(text):
    """The configuration error `text` with every rejected value taken out: each `, not <value>`
    goes, so a problem still names its key and its rule (`hub.access.team must be an Access team
    name (one DNS label)`) and holds nothing the owner wrote as a value: no email, audience tag
    or path. A `, not ` whose value is neither a whole written string nor one word (a list, an
    object, a string cut short) is cut with all that follows it. Key names stay, an unknown one
    the config named too."""
    text = _ECHOED.sub("", text)
    left = _ECHO_LEFT.search(text)
    return text[:left.start()] if left else text


class HubSettingsError(Exception):
    """The machine config has a `hub` key that cannot be used."""


class NoHubToken(Exception):
    """Neither token file of the hub (agent or owner) is there to read."""


class NoHubPointer(Exception):
    """No PM-repo pointer is set, so the hub has no token folder to look in."""


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
    from kitlib.config.types import ConfigError
    from kitlib.hubsite import auth, client
    try:
        folder = auth.folder()
    except ConfigError as e:
        if str(e) == auth.NO_POINTER:
            raise NoHubPointer()
        raise
    if not any(auth.read_token(folder, role) for role in auth.ROLES):
        raise NoHubToken()
    return client.Client(port, tokens=folder, timeout=HUB_SITE_TIMEOUT_S)


_PUSH_STATES = ("up", "down")
_PUSH_LAST = ("written", "not-written", "uncertain", "no-live-session")
_WATCHER_STATES = ("up", "down", "none")


def _known(value, allowed):
    """`value` when it is one of `allowed`, else "?": no server text reaches a line."""
    return value if isinstance(value, str) and value in allowed else "?"


def _count(value):
    """A non-negative whole number as text, else "?"."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else "?"


def push_line(health):
    """[the push and watcher line] of the hub site, from GET health's `push` ({"state", "last",
    "undelivered"}, null with no push worker) and `watcher` ({"state", "age"}, null with no worker;
    docs/action-hub.md "Push notifications"). Sessions run no background hub wait: the hub pushes
    each answer and done to-do, so push down or off is a WARN that nothing reaches the PM by
    itself. Every value is allow-listed (`?` otherwise)."""
    push, w = health.get("push"), health.get("watcher")
    if isinstance(w, dict):
        state = _known(w.get("state"), _WATCHER_STATES)
        age = _count(w.get("age")) if w.get("age") is not None else None
        watch = "watcher %s" % state + (" (beat %ss ago)" % age if age is not None else "")
    else:
        watch = "watcher not read (no push worker)"
    read = 'read python "%s" pending --for pm now and at each start' % HUB_TOOL
    if not isinstance(push, dict):
        return "hub: WARN push notifications are off on this server, so no answer or done to-do " \
               "reaches a session by itself; %s; %s" % (watch, read)
    state = _known(push.get("state"), _PUSH_STATES)
    last = push.get("last")
    last = "none yet" if last is None else _known(last, _PUSH_LAST)
    said = "push %s (last delivery to the PM: %s, %s undelivered); %s" % (
        state, last, _count(push.get("undelivered")), watch)
    if state == "up":
        return "hub: " + said
    return "hub: WARN %s; %s" % (said, read)


def hub_site(load=None, make_client=_hub_client, stamp=None, now=None):
    """The hub site's lines, on the `hub` key's settings or, with no key, on the defaults (port
    8790): the site is the hub whether or not the key is written. Asks only the loopback server,
    with HUB_SITE_TIMEOUT_S per ask; starts nothing. A machine config that cannot be read is one
    line naming the exception's class (the test home fence is not caught). A failure of the
    server side is one line naming no more than the port and the exception's class, so no token,
    URL or message of the server reaches the text. The one message shown is a rejected `hub`
    key's own, without the value it refused (_without_values): key names, the rules, and the
    machine config's path when the settings name it; never a value written in the key. With a
    `stamp` (the checklist gives filed_stamp_file), the up server's lines carry the filed-answers
    digest (filed_digest, one more ask) after the pending ones; with none, no digest."""
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
    except NoHubPointer:
        return ["hub: no PM repo pointer is set, so the hub has no token folder: set it with "
                "tools/machine_setup.py --pm-repo <PM repository> (an elevated owner step)"]
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
        # the PM's items only: one routed to a reachable creator is that session's to ack, and a PM
        # ack would hide it from the creator's wait (docs/action-hub.md "Who handles a pending item")
        items = hub.get("/pending", query={"for": "pm"}, timeout=HUB_SITE_TIMEOUT_S).get("items")
        items = items if isinstance(items, list) else []
    except Exception as e:
        return ["hub: server up on port %d, pending not asked (%s)" % (port, type(e).__name__)]
    lines = ["hub: server up on port %d, %d pending for the PM" % (port, len(items))]
    from kitlib.hubsite import codestale
    old = codestale.notice(health.get("code"), HUB_TOOL)      # never raises; None: current or no field
    if old:
        lines.append("hub: WARN " + old)
    if items:
        lines.append("hub: process the %d pending item(s): python \"%s\" pending --for pm, then "
                     "handle, ack or log each" % (len(items), HUB_TOOL))
    if stamp is not None:
        lines.extend(filed_digest(hub, stamp, now))
    lines.append(push_line(health))
    return lines


FILED_STAMP_NAME = "pm-start-last.json"
FILED_FALLBACK = datetime.timedelta(hours=24)


def filed_stamp_file():
    """The PM-start stamp: pm-start-last.json beside the pm_watch state file (the `state_file` of
    the pm_watch config, else pmwatch.default_state_file)."""
    from kitlib import pmwatch
    state = pmwatch.load_config().get("state_file") or pmwatch.default_state_file()
    return os.path.join(os.path.dirname(os.path.abspath(state)), FILED_STAMP_NAME)


def _read_stamp(path):
    """The time the stamp at `path` holds ({"at": ISO time with its offset}); None when the file
    is missing or unreadable or its time is not one."""
    try:
        with open(path, encoding="utf-8") as f:
            at = datetime.datetime.fromisoformat(json.load(f)["at"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return at if at.tzinfo is not None else None


def _write_stamp(path, at):
    """Writes {"at": at} to `path`, replacing it whole; False when it cannot be written."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": at.isoformat(timespec="seconds")}, f)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def filed_digest(hub, stamp, now=None):
    """The PM-start digest of the owner's answers filed into a record since the last PM start
    (docs/pm-start.md "Filed answers"): at most one line, and no push. `stamp` is the stamp file's
    path, or a function giving it (asked here, so a checklist whose hub is down reads no pm_watch
    config). The time since is the stamp's, or `now` less FILED_FALLBACK when the stamp is missing
    or unreadable; the stamp is rewritten with `now` once the hub has answered, so a start whose
    ask failed leaves it for the next start. One ask (GET /questions?status=filed&since=T): the
    questions whose filed_at is at or after T are counted, and none is no line."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        path = stamp() if callable(stamp) else stamp
    except Exception as e:
        return ["hub: answers filed since the last start not counted (%s)" % type(e).__name__]
    since = _read_stamp(path) or now - FILED_FALLBACK
    text = since.isoformat(timespec="seconds")
    try:
        items = hub.get("/questions", query={"status": "filed", "since": text},
                        timeout=HUB_SITE_TIMEOUT_S).get("items")
    except Exception as e:
        return ["hub: answers filed since %s not asked (%s)" % (text, type(e).__name__)]
    _write_stamp(path, now)
    count = 0
    for d in items if isinstance(items, list) else []:
        try:
            at = datetime.datetime.fromisoformat((d or {}).get("filed_at"))
        except (TypeError, ValueError, AttributeError):
            continue
        if at.tzinfo is not None and at >= since:
            count += 1
    if not count:
        return []
    return ["hub: %d answer(s) filed into records since %s, for your information: python \"%s\" "
            "questions --status filed --since %s" % (count, text, HUB_TOOL, text)]


def read_heartbeat():
    """The pm_watch heartbeat ({pid, beat, stream, started}) at the state file pm_watch uses, or None
    (kitlib.pmwatch.heartbeat_file, read_heartbeat)."""
    from kitlib import pmwatch
    state = pmwatch.load_config().get("state_file") or pmwatch.default_state_file()
    return pmwatch.read_heartbeat(pmwatch.heartbeat_file(state))


def _pm_watch_cmd(cfg):
    """cfg["pm_watch_cmd"] as configured, else the kit's own command (no --stream)."""
    return cfg.get("pm_watch_cmd") or PM_WATCH_DEFAULT_CMD


_STREAM_FLAG = re.compile(r"(?:^|\s)--stream(?:\s|$)")
START_SLACK = 2       # seconds: heartbeat and process list may round the same start time apart


def _created(p):
    """A process dict's Created (list_processes, ISO 8601) as epoch seconds; None when absent or unreadable."""
    try:
        return datetime.datetime.fromisoformat(p.get("Created") or "").timestamp()
    except (TypeError, ValueError):
        return None


def watcher(cfg, procs, beat=None, now=None, job=None):
    """Steps: empty while pm_watch is alive and not streaming, else the step that (re)starts it.

    Alive means all of: the heartbeat pm_watch writes every pass (beat(), default read_heartbeat)
    is younger than BEAT_STALE, its pid is a running python process whose command line has
    pm_watch.py, and that process started when the heartbeat says (within START_SLACK; skipped
    when either start time is unknown), so a reused pid does not pass. A pid alone accepts a
    hung watcher or a --once check; a heartbeat alone stays fresh for a while after a watcher has
    exited on an event. The PM runs pm_watch in the background without --stream: it exits with
    its events, and the PM starts it again at once, then handles them (docs/pm-watch.md). A live
    watcher whose heartbeat says it streams, and a pm_watch_cmd with --stream, each add a line:
    a streaming watcher never exits, so its events reach no one. The configured command is
    printed as written, never changed. The hub's session-watch job stands in for pm_watch: while
    its health block (job(), default kitlib.pmwatch.hub_session_job_block) covers
    (kitlib.pmwatch.session_job_healthy: not failing, a success at most 300 s old) the watcher
    counts as up and no (re)start step is given (docs/pm-watch.md "Hub session-watch job")."""
    running = {}
    for p in procs:
        name = (p.get("Name") or "").lower()
        if name.startswith("python") and "pm_watch.py" in (p.get("CommandLine") or ""):
            running[p.get("ProcessId")] = _created(p)
    hb = (beat or read_heartbeat)()
    now = time.time() if now is None else now
    cmd = _pm_watch_cmd(cfg)
    lines = _watcher_state(hb, running, now, cmd)
    if lines:                       # the hub is asked only when pm_watch alone is not up
        from kitlib import pmwatch
        if pmwatch.session_job_healthy((job or pmwatch.hub_session_job_block)(), now):
            lines = []
    if _STREAM_FLAG.search(cmd):
        lines.append("pm_start.pm_watch_cmd has --stream: a watcher started with it never exits, so "
                     "a background run reports no event; remove --stream from pm_watch_cmd")
    return lines


def _watcher_state(hb, running, now, cmd):
    if hb is not None and hb["pid"] in running:
        started, created = hb.get("started"), running[hb["pid"]]
        if started is not None and created is not None and abs(started - created) > START_SLACK:
            hb = None              # the pid now belongs to another pm_watch.py
    if hb is not None and hb["pid"] in running:
        age = now - hb["beat"]
        if age > BEAT_STALE:
            return ["pm_watch.py (pid %d) has not polled for %dm: stop that pid and start it in the "
                    "background: %s" % (hb["pid"], age // 60, cmd)]
        if hb.get("stream") is True:
            return ["pm_watch.py (pid %d) runs with --stream: it never exits, so a background run "
                    "reports no event; stop that pid and start it in the background without "
                    "--stream: %s" % (hb["pid"], cmd)]
        return []
    if running:
        return ["pm_watch.py is running without a heartbeat (an older copy, or another --state): "
                "restart it in the background: %s" % cmd]
    if hb is not None:
        return ["pm_watch.py is not running (it last polled %ds ago): start it in the background "
                "(it exits with its events; start it again at once, then handle them): %s"
                % (max(0, now - hb["beat"]), cmd)]
    return ["pm_watch.py is not running: start it in the background (it exits with its events; "
            "start it again at once, then handle them): %s" % cmd]


HANDOFF_DEFAULT = "HANDOFF.md"
HANDOFF_STRAY = "pm-handoff*.md"


def handoff(cfg, globber=glob.glob, exists=os.path.isfile, pm_repo=None):
    """The PM's one hand-off file, edited in place (docs/pm-start.md "Hand-off file"): pm_start.handoff_file
    (default HANDOFF.md; a relative name is in the PM repository's main checkout, `pm_repo`, default
    pm_memory_repo). Its line is "read first: <file>", or a line saying it is missing. Any pm-handoff*.md
    left in pm_start.memory_dir (when set) is one more line: merge it into the file and delete it."""
    name = cfg.get("handoff_file") or HANDOFF_DEFAULT
    if os.path.isabs(name):
        path = name
    else:
        top = (pm_repo or pm_memory_repo)(cfg)
        if not top:
            raise KeyError("handoff_file %s is relative and the PM repository is not found "
                           "(set pm_start.sessions_file, or an absolute pm_start.handoff_file)" % name)
        path = os.path.normpath(os.path.join(top, name))
    lines = ["read first: %s" % path] if exists(path) else [
        "no hand-off file %s: create it (the PM keeps one, edited in place)" % path]
    mem = cfg.get("memory_dir")
    stray = sorted(globber(os.path.join(mem, HANDOFF_STRAY))) if mem else []
    if stray:
        lines.append("hand-off notes belong in %s: merge and delete %s" % (path, ", ".join(stray)))
    return lines


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
    session_process; claude.exe on Claude), matched exactly against a process's Name. On Linux the
    running platform's process_names over every host (registry.process_names): claude, node, codex."""
    if platform.NAME == 'linux':
        return frozenset(registry.process_names(path))
    return frozenset(registry.union('session_process', path))


def rc_name(cmd):
    """The --remote-control value of a session's command line, or None (kitlib.launcher.lookup uses it too)."""
    m = _RC.search(cmd or "")
    return next((g for g in m.groups() if g), None) if m else None


def cli_names(path=None):
    """((cli_program, cli_package), ...) of every host in hosts.json, for runs_cli."""
    return tuple((p.cli_program, p.cli_package) for p in registry.profiles(path))


def _in_package(parts, package):
    """True when the path parts hold node_modules followed directly by the scoped package's two
    parts (@scope, name), with the file below them."""
    seg = package.split("/")
    return any(parts[i] == "node_modules" and parts[i + 1:i + 1 + len(seg)] == seg
               for i in range(len(parts) - len(seg) - 1))


def runs_cli(cmd, clis):
    """True when a Linux command line (/proc's arguments joined by spaces) runs one of `clis`,
    ((program, scoped npm package), ...): its program, or the first word after it that is not an
    option (node's script), has the program as its last path part or lies in the package
    directly under a node_modules folder. `node app.js --remote-control x` runs none, nor
    `node ~/src/claude-code/x.js`."""
    words = (cmd or "").split()
    script = next((w for w in words[1:] if not w.startswith("-")), "")
    for word in words[:1] + [script]:
        parts = word.split("/")
        if any(parts[-1] == program or _in_package(parts, package) for program, package in clis):
            return True
    return False


def linux_clis(path=None):
    """cli_names() on Linux, for runs_a_cli; None elsewhere (a Windows image is the CLI's own)."""
    return cli_names(path) if platform.NAME == 'linux' else None


def runs_a_cli(row, clis):
    """A process row runs a host's CLI: always when clis is None (linux_clis off Linux)."""
    return clis is None or runs_cli(row.get("CommandLine"), clis)


def live_session_names(procs):
    """Remote Control names of launched sessions (session_images); the Store app (under WindowsApps) is not one,
    nor on Linux a process that runs no host's CLI (runs_cli)."""
    names = set()
    images = session_images()
    clis = linux_clis()
    for p in procs:
        if (p.get("Name") in images and p.get("CommandLine")
                and STORE_DIR not in (p.get("ExecutablePath") or "") and runs_a_cli(p, clis)):
            n = rc_name(p["CommandLine"])
            if n:
                names.add(n)
    return names


def liveness(cfg, procs, read=None, account=None, repos=None):
    path = cfg.get("sessions_file")
    if not path:
        raise KeyError("sessions_file not configured")
    if read is None:
        read = _read
    sessions = json.loads(read(path))
    alive = live_session_names(procs)
    label = None
    out = []
    for key, e in down_entries(sessions, alive):
        if label is None:     # read once, and only when something is down
            from kitlib.launcher import accounts
            label = (account or current_account)()
            # an unidentified account is left to the launcher's pick-account
            label = "auto" if label == accounts.DEFAULT_LABEL else label
        out.append("coordinator %s (%s) is not running: launch.py start --session %s --account %s"
                   % (key, e["name"], key, label))
    return out + _consultants_missing(sessions, alive, repos)


def down_entries(sessions, alive):
    """[(key, entry)] of the sessions.json entries that should run and do not: an object with a
    name, `autostart` not false, and no live session of exactly that name in `alive` (exact,
    case-sensitive, as kitlib.launcher.lookup.find_exact). The PM checklist's coordinator lines
    and the hub's coordinator restart job (kitlib.hubjobs.coord_restart) both read it."""
    return [(key, e) for key, e in sessions.items()
            if isinstance(e, dict) and e.get("name") and e.get("autostart") is not False
            and e["name"] not in alive]


def _consultants_missing(sessions, alive, repos=None):
    """One line per repository in sessions (an entry's "repo") with no live consultant-<repo> session
    (launch.py consultant names it so, kitlib.launcher.start.consultant). A repository with a
    consultant entry of its own is left to the coordinator lines above. The line names the repository's
    folder from launcher.repos (`repos`, launcher_repos by default, read only when a line is needed);
    a repository not there, or a read that fails, keeps the placeholder."""
    entries = [e for k, e in sessions.items() if not k.startswith("_") and isinstance(e, dict)]
    covered = {e.get("repo") for e in entries if str(e.get("agent") or "").endswith("consultant")}
    out = []
    folders = None
    for repo in sorted({e["repo"] for e in entries if isinstance(e.get("repo"), str) and e["repo"]}, key=str.lower):
        if repo not in covered and "consultant-" + repo not in alive:
            if folders is None:
                try:
                    folders = launcher_repos() if repos is None else repos
                except Exception:
                    folders = {}
            out.append("consultant-%s is not running: launch.py consultant --repo %s --repo-path %s"
                       % (repo, repo, folders.get(repo) or "<its checkout>"))
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


def pm_memory_repo(cfg, main_top=None):
    """The main checkout of the PM's own repository (the one holding sessions_file), or None when
    sessions_file is not set or not there. `main_top` is kitlib.git.repo.main_checkout_toplevel
    unless given."""
    path = cfg.get("sessions_file")
    if not path or not os.path.isfile(path):
        return None
    if main_top is None:
        from kitlib.git.repo import main_checkout_toplevel as main_top
    return main_top(os.path.dirname(os.path.abspath(path)))


def _same_folder(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _pm_memory_folder(top):
    """The memory folder the PM repository's settings name: its autoMemoryDirectory when it is there
    (the kit's value or one the owner chose), else the kit's value for it (kitlib.stubs.memory.wanted).
    Looked up at call time, so a test can replace either reader."""
    from kitlib.stubs import memory as kmemory
    m = kmemory.survey(top)
    if m.state in (kmemory.OK, kmemory.OTHER):
        return m.value
    return m.want or kmemory.wanted(top)


def memory(cfg, repos=None, check=None, pm_repo=None, folder=None):
    """The agent memory folder (kitlib.stubs.memory; docs/pm-start.md): one line per repository in
    launcher.repos, and for the PM's own repository, whose .claude/settings.json has no
    autoMemoryDirectory (an owner step: the command that adds it) or cannot be read; then one line
    when pm_start.memory_dir is not the folder the PM repository's settings name (`folder`, default
    _pm_memory_folder: a value the owner chose is the one compared). None when all match. A folder
    that is not there, or a check that raises, is that repo's own "could not check memory" line."""
    from kitlib.stubs import memory as kmemory
    if check is None:
        check = kmemory.problem_line
    folder = folder or _pm_memory_folder
    repos = launcher_repos() if repos is None else repos
    lines = []
    for name in sorted(repos):
        path = repos[name]
        if not os.path.isdir(path):
            lines.append("could not check memory (%s): no repository folder %s" % (name, path))
            continue
        try:
            line = check(path, label=name)
        except Exception as e:
            line = "could not check memory (%s): %s: %s" % (name, type(e).__name__, e)
        if line:
            lines.append(line)
    top = (pm_repo or pm_memory_repo)(cfg)
    if not top:
        return lines
    if not any(_same_folder(top, p) for p in repos.values()):     # a launcher repo has its line already
        try:
            line = check(top, label="PM repo")
        except Exception as e:
            line = "could not check memory (PM repo): %s: %s" % (type(e).__name__, e)
        if line:
            lines.append(line)
    try:
        want = folder(top)
    except Exception as e:
        lines.append("could not check pm_start.memory_dir: %s: %s" % (type(e).__name__, e))
        return lines
    have = cfg.get("memory_dir")
    if want and not (have and _same_folder(have, want)):
        lines.append("pm_start.memory_dir %s; the PM repo's memory folder is %s: set pm_start.memory_dir to %s"
                     % ("is %s" % have if have else "is not set", want, want))
    return lines


def allow_rules(cfg, repos=None, pm_repo=None, settings=None):
    """Permission rules naming a plugin export other than the current one (kitlib.allowrules;
    docs/pm-start.md "Allow rules"): for each repository in launcher.repos and the PM's own, one
    line per settings file (.claude/settings.local.json, .claude/settings.json) and state (gone,
    not current), with the command that prints the land lines to paste (any other rule's hash the
    owner changes by hand). None when every rule names the
    current export. Nothing is read when there is no repository; a loader (`settings`, default
    plugin_settings) that gives None checks nothing."""
    from kitlib import allowrules, kitroot, pluginsource
    from kitlib.stubs import memory as kmemory
    repos = launcher_repos() if repos is None else repos
    targets = [(name, repos[name]) for name in sorted(repos)]
    top = (pm_repo or pm_memory_repo)(cfg)
    if top and not any(_same_folder(top, p) for p in repos.values()):
        targets.append(("PM repo", top))
    if not targets:
        return []
    s = plugin_settings() if settings is None else settings
    if s is None:
        return []
    root = s.export_dir
    current = pluginsource.current_folder(root)
    lines = []
    for label, path in targets:
        if not os.path.isdir(path):
            lines.append("could not check allow_rules (%s): no repository folder %s" % (label, path))
            continue
        for rel, f in ((kmemory.local_rel(), kmemory.local_path(path)),
                       (kmemory.settings_rel(), kmemory.settings_path(path))):
            try:
                found = allowrules.named_exports(f, root) or {}
            except (OSError, ValueError) as e:
                lines.append("could not check allow_rules (%s): %s: %s" % (label, rel, e))
                continue
            by_state = {}
            for folder in sorted(found):
                state = allowrules.export_state(root, folder, current)
                if state != allowrules.CURRENT:
                    by_state.setdefault(state, []).append(folder)
            for state in (allowrules.GONE, allowrules.NOT_CURRENT):
                if state in by_state:
                    folders = by_state[state]
                    lines.append("allow rules (%s): %s names plugin export %s (%s) in %d rule(s); the current "
                                 "export is %s: %s prints the land lines to paste; any other rule naming the "
                                 "old export needs its hash changed by hand (owner)"
                                 % (label, rel, ", ".join(folders), state, sum(found[x] for x in folders),
                                    current or "none", kitroot.command("allow_rules", path)))
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


def plugin_enabled(settings=None, folders=None):
    """One line naming the account folders whose settings.json does not enable the pm-kit plugin
    (enabledPlugins["pm-kit@pm-kit"] absent), with the machine setup command that installs it
    (docs/machine-setup.md "Default mode"); false is the owner's choice and is silent. A settings.json
    that is not a JSON object is its own "could not check" line. None for `settings` reads the machine
    config (plugin_settings); a loader that gives None checks nothing. None for `folders`: this
    machine's account folders (kitlib.pluginsource.account_folders)."""
    from kitlib import pluginsource
    from kitlib.machinesetup import plugin
    s = plugin_settings() if settings is None else settings
    if s is None:
        return []
    lines, absent = [], []
    for d in pluginsource.account_folders() if folders is None else folders:
        have = plugin.enabled(os.path.join(d, "settings.json"), pluginsource.PLUGIN_ID)
        if have is None:
            lines.append("could not check the pm-kit plugin in %s: its settings.json is not a JSON object" % d)
        elif have is plugin.ABSENT:
            absent.append(d)
    if absent:
        lines.insert(0, 'pm-kit plugin: not enabled in %s: python "%s/tools/machine_setup.py" --apply '
                        '(docs/machine-setup.md "Default mode")'
                     % (", ".join(absent), pluginsource.live_root().replace("\\", "/")))
    return lines


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


def _cdrive_items(get, realpath):
    """(label, path or None, env var that moves it) for each folder the C: drive warning covers; `get`
    reads one environment variable, `realpath` resolves links (the Claude folder may be a junction to
    another drive). A default that needs an unset base variable is None."""
    from kitlib.launcher.accounts import CONFIG_DIR_ENV     # the host's config folder variable and default
    from kitlib.launcher.settings import DEFAULT_FOLDER
    local, home = get("LOCALAPPDATA"), get("USERPROFILE")

    def under(base, *parts):
        return os.path.join(base, *parts) if base else None
    config = get(CONFIG_DIR_ENV) or under(home, DEFAULT_FOLDER)
    config = realpath(config) if config else None
    return [
        ("TEMP", get("TEMP"), "TEMP"),
        ("TMP", get("TMP"), "TMP"),
        ("pip cache", get("PIP_CACHE_DIR") or under(local, "pip", "Cache"), "PIP_CACHE_DIR"),
        ("npm cache", get("npm_config_cache") or under(local, "npm-cache"), "npm_config_cache"),
        ("nuget packages", get("NUGET_PACKAGES") or under(home, ".nuget", "packages"), "NUGET_PACKAGES"),
        ("nuget http cache", get("NUGET_HTTP_CACHE_PATH") or under(local, "NuGet", "v3-cache"),
         "NUGET_HTTP_CACHE_PATH"),
        ("dotnet CLI home", get("DOTNET_CLI_HOME"), "DOTNET_CLI_HOME"),
        ("gradle home", get("GRADLE_USER_HOME") or under(home, ".gradle"), "GRADLE_USER_HOME"),
        ("uv cache", get("UV_CACHE_DIR") or under(local, "uv", "cache"), "UV_CACHE_DIR"),
        ("agent transcripts", under(config, "projects"), CONFIG_DIR_ENV),
    ]


def cdrive_lines(environ=None, realpath=None):
    """One warning line naming each of TEMP, TMP, the pip, npm, nuget and dotnet tool caches and the
    Claude transcript folder (CLAUDE_CONFIG_DIR, else ~/.claude, projects/) that resolves to the C:
    drive, with the variable that would move it; none when nothing is on C:. Also the nuget http
    cache, gradle home and uv cache. Reads this process's environment only (`environ`, default
    os.environ; never the machine-scope TEMP of system services) and runs no subprocess, no `npm
    config get`. The Claude folder is resolved through links (`realpath`, default os.path.realpath).
    A relative or unset path is skipped (its drive is unknown)."""
    from kitlib.launcher.accounts import CONFIG_DIR_ENV
    env = {k.upper(): v for k, v in (os.environ if environ is None else environ).items()}
    hits = []
    for label, path, var in _cdrive_items(lambda k: env.get(k.upper()), realpath or os.path.realpath):
        if path and os.path.splitdrive(os.path.normpath(path))[0].upper() == "C:":
            # the Claude folder variable already set: move the folder it names; unset: set it
            if var == CONFIG_DIR_ENV and env.get(var):
                hint = "move the folder %s names" % var
            else:
                hint = "set %s" % var
            hits.append("%s (%s; %s)" % (label, path, hint))
    return ["on the C: drive: " + ", ".join(hits)] if hits else []


def worktree_sweep_lines(repos=None, read=None, plugin=None):
    """One line per repository in launcher.repos whose `<repo>-worktree-sweep` task is missing, or
    runs from a removed plugin export (kitlib.machinesetup.worktreesweep, docs/machine-setup.md
    "Extras"); none when every task is current. A task list that cannot be read is one "could not
    check" line; a repository that cannot be read is its own; repositories that take one task name
    (machine setup registers it for none) are one line naming each. `read`: the task list
    (consoletasks.read_tasks); `plugin`: the plugin Settings, None or a raising loader checks
    presence only."""
    from kitlib.machinesetup import worktreesweep
    from kitlib.worktree.task import task_name
    repos = launcher_repos() if repos is None else repos
    if not repos:
        return []
    try:
        tasks = (read or consoletasks.read_tasks)()
    except Exception as e:      # access denied, a timeout, no schtasks: unknown, never "not registered"
        return ["could not check the worktree sweep tasks: %s: %s" % (type(e).__name__, e)]
    try:
        s = (plugin or plugin_settings)()
    except Exception:           # the plugin_current check names a bad plugin config
        s = None
    export = s.export_dir if s is not None else None
    lines, resolved = [], []
    for label in sorted(repos):
        try:
            if not os.path.isdir(repos[label]):
                raise OSError("no repository folder %s" % repos[label])
            main, name = worktreesweep.repo_task(repos[label])
        except Exception as e:
            lines.append("could not check the worktree sweep task (%s): %s: %s"
                         % (label, type(e).__name__, _failure_text(e)))
            continue
        resolved.append((label, os.path.normpath(main), name))
    for (label, _, name), group, shared in worktreesweep.by_task(resolved):
        name = task_name(name)
        if shared:
            lines.append("worktree sweep task %s is shared: %s take that one name, so machine_setup.py "
                         "registers it for none; give each its own repo.name in .kit.json"
                         % (name, worktreesweep.sharers(group)))
            continue
        have = worktreesweep.find(tasks, name)
        if have is None:
            lines.append("worktree sweep task %s (%s) is not registered: machine_setup.py --apply registers "
                         "it (the owner's act)" % (name, label))
            continue
        gone = worktreesweep.removed_export(have, export) if export else None
        if gone:
            lines.append("worktree sweep task %s (%s) runs from a removed plugin export %s: machine_setup.py "
                         "--apply registers it again (the owner's act)" % (name, label, gone))
    return lines


def _failure_text(e):
    """The reason in a "could not check" line: the exception's own text, with any rejected value
    taken out (_without_values) when it is a machine config refusal (kitlib.config or
    kitlib.pmdata), so a malformed pm_data value is named by key and rule and never shown."""
    from kitlib import pmdata
    from kitlib.config.types import ConfigError
    text = str(e)
    if isinstance(e, (ConfigError, pmdata.PmDataError)):
        text = _without_values(text)
    return text


def _user_config():
    """The machine config, None when it cannot be read (configcheck reports that)."""
    try:
        return kuser.user_config()
    except kuser.ConfigError:
        return None


def rulebook_lines(machine, repo=None):
    """[the rulebook line] from the machine config `machine` (None: it cannot be read, which the
    line says apart from "nothing configured"): the rules folder, each rulebook
    present with its rule count, and the `tools/ledger.py rules` command to read them
    (`--repo REPO`, or `--all` without `repo`). Always one line: no ledger configured, or no
    rulebook in the folder, says so (docs/ledger.md "Rulebooks"). The rulebooks are loaded for
    their counts only; a parse error is `ledger.py check`'s to report."""
    cmd = kitroot.command("ledger") + (" rules --repo %s" % repo if repo else " rules --all")
    if machine is None:
        return ["rulebooks (standing rules): unknown - the machine config cannot be read (see the machine config line)"]
    found = ledger_files.of_config(machine)
    if found is None:
        return ["rulebooks (standing rules): none - no ledger_dir or ledger_path is set"]
    books, _ = rulebook.load(found.rules)
    if not books:
        return ["rulebooks (standing rules): none in %s" % found.rules]
    counts = ", ".join("%s %d" % ("shared" if n == rulebook.SHARED else n, len(b.rules))
                       for n, b in books.items())
    return ["rulebooks (standing rules, read before deciding) in %s: %s rules; read with: %s"
            % (found.rules, counts, cmd)]


def checklist(cfg, now=None, procs=None, **fakes):
    """The full session-start text. A failed check is one "could not check X: reason" line."""
    today = (now or datetime.datetime.now()).date()
    lines = []
    box = {}
    repos = {"repos": fakes["repos"]} if "repos" in fakes else {}

    def procs_once():
        if "p" not in box:
            box["p"] = procs if procs is not None else list_processes()
        return box["p"]

    checks = [
        ("standing", lambda: standing(cfg)),
        ("machine_config", lambda: configcheck.lines(**fakes.get("machine_config", {}))),
        ("eng_review", lambda: eng_review(cfg)),
        ("rulebooks", lambda: rulebook_lines(fakes["machine"] if "machine" in fakes else _user_config())),
        ("hub", lambda: hub_site(**dict({"stamp": filed_stamp_file},
                                                     **fakes.get("hub_site", {})))),
        ("watcher", lambda: watcher(cfg, procs_once(), **fakes.get("watcher", {}))),
        ("handoff", lambda: handoff(cfg, **fakes.get("handoff", {}))),
        ("reminders", lambda: reminders(cfg, today, **fakes.get("reminders", {}))),
        ("liveness", lambda: liveness(cfg, procs_once(), **fakes.get("liveness", {}))),
        ("kit_sync", lambda: kit_sync(fakes.get("pm_data", pm_data_settings)())),
        ("plugin_current", lambda: plugin_current(**fakes.get("plugin_current", {}))),
        # A plugin_current fake's settings and folders stand for this check's too.
        ("plugin_enabled", lambda: plugin_enabled(**fakes.get("plugin_enabled", {
            k: v for k, v in fakes.get("plugin_current", {}).items() if k in ("settings", "folders")}))),
        # A repos fake stands for launcher.repos in each check that reads it.
        ("memory", lambda: memory(cfg, **fakes.get("memory", repos))),
        ("allow_rules", lambda: allow_rules(cfg, **fakes.get("allow_rules", repos))),
        ("repo_prep", lambda: repo_prep(**fakes.get("repo_prep", {}))),
        ("agent_temp", lambda: agent_temp_lines(**fakes.get("agent_temp", {}))),
        ("cdrive", lambda: cdrive_lines(**fakes.get("cdrive", {}))),
        ("worktree_sweep", lambda: worktree_sweep_lines(**fakes.get("worktree_sweep", repos))),
        ("console_tasks", lambda: consoletasks.check(cfg, **fakes.get("console_tasks", {}))),
        ("foreground_lock", lambda: fglock.check(cfg, **fakes.get("foreground_lock", {}))),
    ]
    for name, fn in checks:
        try:
            lines.extend(fn())
        except Exception as e:
            lines.append("could not check %s: %s: %s" % (name, type(e).__name__, _failure_text(e)))
    return "PM session start checklist:\n" + "\n".join("- " + l for l in lines)
