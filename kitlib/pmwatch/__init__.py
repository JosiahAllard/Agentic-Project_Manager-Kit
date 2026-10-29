"""kitlib.pmwatch: the PM watcher's checks (docs/pm-watch.md).

Each check turns the previous state and a fresh reading into event lines. tools/pm_watch.py loops,
prints the lines and exits. Event lines and state keys are those of the PM repo's
launcher/pm_watch.py. Transcript reading is host-specific and lives in hosts/claude/adapter/claude_host.py.
"""
import json
import os
import re
import time

from kitlib.config import user as kuser
from kitlib.git import cmd as gitcmd

KEY = "pm_watch"
WEEK_BANDS = (95, 98, 99)
FIVE_BANDS = (95, 98, 99)         # 95 no new work, 98 landings only; no PM headroom
ACCOUNT = r"account (\d+|codex): "
LINE = re.compile(ACCOUNT + r"five_hour=(reset|\d+|\?)%?.*?seven_day=(reset|\d+|\?)%?")
NO_FILE = re.compile(ACCOUNT + r"no usage (?:file|data)")
AGE = re.compile(r"\[updated (\d+|\?) min ago\]")
STALE_MINUTES = 60               # a reading older than this is shown as stale (docs/launcher.md "Usage")


def load_config(path=None):
    """The "pm_watch" dict from the user-profile config; {} when the file or key is missing.
    With no `path`, a machine config location that cannot be told (kitlib.config.user.locate BAD)
    raises kitlib.config.ConfigError."""
    p = os.path.expanduser(path) if path else kuser.readable_file()
    try:
        with open(p, encoding="utf-8-sig") as f:
            cfg = json.load(f).get(KEY)
    except (OSError, ValueError, AttributeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def default_state_file():
    base = os.environ.get("LOCALAPPDATA") or kuser.home_dir()
    return os.path.join(base, "pm_watch", "state.json")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(path, s):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(tmp, path)


def heartbeat_file(state_path):
    """The heartbeat file beside the state file: separate, so the state keys stay those of the
    PM repo's copy (which shares the state file)."""
    return state_path + ".beat"


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def write_heartbeat(path, pid, now, stream, started=None, yielded=False):
    """Record that pm_watch `pid` polled at `now` (epoch seconds); `stream` is its --stream flag
    (the PM start no longer passes it: docs/pm-watch.md) and `started` its process start time
    (epoch seconds, None when unknown), so a reused pid is not
    taken for the watcher. kitlib.pmstart.watcher reads it to tell a live watcher from a hung or
    exited one. `yielded`: its session checks stepped aside for the hub's session-watch job at
    that pass (Watcher.yielded), so that job does not wait for it (a beat without the field, from
    an older pm_watch, counts as not stepped aside)."""
    save_state(path, {"pid": pid, "beat": now, "stream": bool(stream), "started": started,
                      "yielded": bool(yielded)})


def read_heartbeat(path):
    """The heartbeat dict, or None when the file is missing, unreadable or malformed. "started"
    is a number or None (None also for a heartbeat written without it)."""
    hb = load_state(path)
    if not isinstance(hb, dict) or type(hb.get("pid")) is not int or not _number(hb.get("beat")):
        return None
    if hb.get("started") is not None and not _number(hb["started"]):
        return None
    return dict(hb, started=hb.get("started"))


_EPOCH_AS_FILETIME = 116444736000000000     # 1970-01-01 in 100 ns ticks since 1601-01-01


def own_start_time():
    """This process's start time in epoch seconds (kernel32 GetProcessTimes), None when it cannot
    be read. The same clock as the CreationDate kitlib.pmstart.list_processes reports."""
    import ctypes
    from ctypes import wintypes
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        times = [wintypes.FILETIME() for _ in range(4)]
        ok = k32.GetProcessTimes(wintypes.HANDLE(k32.GetCurrentProcess()),
                                 *[ctypes.byref(t) for t in times])
    except (OSError, AttributeError):
        return None
    if not ok:
        return None
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return (ticks - _EPOCH_AS_FILETIME) / 1e7


POLL_SLEEP = 60       # seconds between passes; well inside kitlib.pmstart BEAT_STALE


def run(w, clock, sleep, save, emit, beat, once=False, stream=False):
    """The watcher loop over Watcher `w`. Each pass polls, saves the state (`save(state)`) and,
    except with `once`, calls `beat(now)`. Events go to `emit` as one text. Without `stream` the
    first events end the run (the PM relaunches it); with `stream` it keeps polling and never
    exits, and the PM start no longer passes --stream (docs/pm-watch.md). `once`: one pass, then 0.
    Returns the exit code. A beat that raises OSError
    (a locked or read-only heartbeat file, os.replace refused) is skipped, never fatal: the
    heartbeat goes stale and the PM start check says so, while the events keep flowing."""
    w.start(clock())
    save(w.s)
    while True:
        events = w.poll(clock())
        save(w.s)
        if not once:
            try:
                beat(clock())
            except OSError:
                pass
        if events:
            emit("\n".join(events))
            if not stream:
                return 0
        if once:
            return 0
        sleep(POLL_SLEEP)


def branch_tip(repo, ref):
    args = ["ls-remote", "origin", ref]
    r = gitcmd.subprocess_run(["git", "-C", repo] + args, capture_output=True, text=True, timeout=60,
                              env=gitcmd.network_env(args, os.environ))
    if r.returncode != 0:
        raise RuntimeError("ls-remote exit %d" % r.returncode)
    return r.stdout.split()[0][:8] if r.stdout.strip() else None


def parse_usage(text):
    """{account: {"five", "week", "age", "stale", "missing"}} from kitlib.launcher.usage's lines.
    five and week are whole percentages, 0 for a window that has reset, and None when the reading
    is unknown (`?`, or no usage file): unknown is never 0. age is the reading's age in minutes
    (None when unknown); stale is True when it is older than STALE_MINUTES or of unknown age;
    missing is True for an account with no usage file. RuntimeError when no account line at all."""
    out = {}
    conv = lambda v: None if v == "?" else 0 if v == "reset" else int(v)
    for line in text.splitlines():
        m = LINE.search(line)
        if m:
            a = AGE.search(line)
            age = int(a.group(1)) if a and a.group(1) != "?" else None
            out[m.group(1)] = {"five": conv(m.group(2)), "week": conv(m.group(3)), "age": age,
                               "stale": age is None or age > STALE_MINUTES, "missing": False}
            continue
        m = NO_FILE.search(line)
        if m:
            out[m.group(1)] = {"five": None, "week": None, "age": None, "stale": True, "missing": True}
    if not out:
        raise RuntimeError("the usage report printed no account lines")
    return out


def launcher_configured(home=None):
    """True when the machine config has a `launcher` key, so in-process usage reads the accounts
    it names; without one it would read only the default ~/.claude account. A config that cannot
    be read counts as no key."""
    from kitlib.config import user as kuser
    from kitlib.config.types import ConfigError
    try:
        return kuser.user_config(home).get("launcher") is not None
    except ConfigError:
        return False


def usage(report=None):
    """Every account's usage, read in this process (kitlib.launcher.usage, from the launcher
    settings of the machine config). `report` replaces that reader (a callable giving the lines)."""
    if report is None:
        from kitlib.launcher import settings as launcher_settings, usage as launcher_usage
        report = lambda: launcher_usage.report(launcher_settings.load())
    return parse_usage("\n".join(report()))


def band(v, bands):
    return max([b for b in bands if v >= b], default=0)


def usage_events(prev, cur):
    """Band and reset events between two parse_usage readings; a window unknown (None) in either
    is skipped, never read as 0."""
    ev = []
    for acct, u in cur.items():
        p = prev.get(acct)
        if p is None:
            continue
        known = lambda k: u.get(k) is not None and p.get(k) is not None
        if known("week"):
            wb, pwb = band(u["week"], WEEK_BANDS), band(p["week"], WEEK_BANDS)
            if wb > pwb:
                ev.append("USAGE account %s seven_day %d%% (now %d%%)" % (acct, wb, u["week"]))
        if known("five"):
            fb, pfb = band(u["five"], FIVE_BANDS), band(p["five"], FIVE_BANDS)
            if fb > pfb:
                ev.append("USAGE account %s five_hour %d%% (now %d%%)" % (acct, fb, u["five"]))
        if known("week") and p["week"] - u["week"] >= 20:
            ev.append("RESET account %s (weekly %d%% -> %d%%)" % (acct, p["week"], u["week"]))
    return ev


def merge_usage(prev, cur):
    """The watcher's stored usage after a reading: cur over prev, but a window cur does not know
    (None) keeps prev's known figure, so a band crossed while a reading was missing still fires."""
    out = dict(prev)
    for acct, u in cur.items():
        p = prev.get(acct) or {}
        out[acct] = {**u, **{k: p[k] for k in ("five", "week") if u.get(k) is None and p.get(k) is not None}}
    return out


def alert_events(seen, alerts_dir):
    """(events, seen now): one BGFAIL per alert file not in seen. An unreadable file (mid-write)
    is left unseen for the next poll; names no longer in the folder drop out of seen."""
    try:
        names = sorted(n for n in os.listdir(alerts_dir) if n.endswith(".json"))
    except FileNotFoundError:
        return [], []
    ev, now = [], []
    for n in names:
        if n in seen:
            now.append(n)
            continue
        try:
            with open(os.path.join(alerts_dir, n), encoding="utf-8") as f:
                a = json.load(f)
        except (OSError, ValueError):
            continue
        reason = " ".join(str(a.get("reason") or a.get("how") or "failed").split())
        ev.append("BGFAIL run %s %s" % (a.get("id") or n[:-5], reason))
        now.append(n)
    return ev, now


# Lock-window notices (kitlib.lock.notice writes them): <dir>/<run id>-start|end.json, each a
# doc with its id; the hub server lists them (kitlib.hubsite.derived). A subfolder is not read.


def configured_notice_dir(path=None):
    """machine_lock.notice.dir of the user-profile config, or None. With no `path`, a machine
    config location that cannot be told (kitlib.config.user.locate BAD) raises
    kitlib.config.ConfigError."""
    p = os.path.expanduser(path) if path else kuser.readable_file()
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)["machine_lock"]["notice"]["dir"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return d if isinstance(d, str) and d else None


def pending_notices(d):
    """[(file name, doc)] of the notices in d not yet posted, oldest first. An unreadable file
    (one being written) is left for the next look."""
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("id"):
            out.append((n, doc))
    out.sort(key=lambda x: (str(x[1].get("at") or ""), x[0]))
    return out


def notice_events(seen, notice_dir):
    """(events, seen now): one LOCKNOTICE per notice in notice_dir not in seen (one that is
    gone drops out of seen)."""
    ev, now = [], []
    for n, doc in pending_notices(notice_dir):
        if n not in seen:
            ev.append("LOCKNOTICE %s %s" % (doc["id"], " ".join(str(doc.get("title") or "").split())))
        now.append(n)
    return ev, now


def watched_sessions(sessions):
    """Sorted names of the sessions.json entries to watch: every entry with a name (coordinator or
    plain), except autostart=false. Empty if the file is unreadable."""
    try:
        with open(sessions, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return sorted({v["name"] for v in d.values() if isinstance(v, dict) and v.get("name")
                   and v.get("autostart") is not False})


_STOP_MARK, _SUCC_MARK = " stopped '", "; successor '"


def _stop_entry(line):
    """(stamp, name, successor) of a stop.log line kitlib.launcher.stop wrote, else None."""
    if line[19:19 + len(_STOP_MARK)] != _STOP_MARK or _SUCC_MARK not in line:
        return None
    name = line[19 + len(_STOP_MARK):].split("'", 1)[0]
    return line[:19], name, line.rsplit(_SUCC_MARK, 1)[1].split("'", 1)[0]


def stop_log_reader(sessions):
    """stopped(name, mtime) -> the epoch time of the launcher stop that ended the session `name`
    with no successor, or None. Reads stop.log beside the sessions file (kitlib.launcher.stop writes
    one line per stop: "<stamp> stopped '<name>' ...; successor '<name or ->'"). Only the newest stop
    of the name counts, and only when it is not older than `mtime`, the session's newest
    activity: later activity means it was started again. A missing or unreadable log gives None."""
    if not sessions:
        return lambda name, mtime: None
    log = os.path.join(os.path.dirname(os.path.abspath(sessions)), "stop.log")

    def stopped(name, mtime):
        last = None
        try:
            with open(log, encoding="utf-8-sig") as f:
                for line in f:
                    e = _stop_entry(line)
                    if e and e[1] == name:
                        last = (e[0], e[2])
        except OSError:
            return None
        if last is None or last[1] != "-":
            return None
        try:
            t = time.mktime(time.strptime(last[0], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            return None
        return t if t + 1 > mtime else None        # the stamp is whole seconds, mtime is not
    return stopped


def idle_baseline(names, newest, now, minutes, stopped=None):
    """{name: marker} for sessions already quiet or stopped, so a relaunch-free first run stays
    silent. The marker is the newest activity time, or [time, "stopped"] for a stopped session.
    newest(name) is the newest activity time of that session, or None (hosts.claude.adapter.claude_host.newest_finder)."""
    out = {}
    for n in names:
        m = newest(n)
        if m is None:
            continue
        if stopped and stopped(n, m) is not None:
            out[n] = [m, "stopped"]
        elif now - m >= minutes * 60:
            out[n] = m
    return out


def idle_events(names, newest, fired, nolog, now, minutes, stopped=None):
    """(events, fired now, nolog now). IDLE once per quiet spell: fired maps name -> the mtime it
    fired at, and a session drops out of it when active again. A session the launcher stopped with
    no successor (`stopped`, stop_log_reader) gives STOPPED once, never IDLE. A name with no
    transcript gives one ERROR, then stays quiet (nolog) until it has a transcript."""
    ev, cur, miss = [], {}, []
    for n in names:
        m = newest(n)
        if m is None:
            miss.append(n)
            if n not in nolog:
                ev.append("ERROR idle: no transcript for %s" % n)
            continue
        t = stopped(n, m) if stopped else None
        if t is not None:
            cur[n] = [m, "stopped"]
            if fired.get(n) != cur[n]:
                ev.append("STOPPED %s at %s" % (n, time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))))
        elif now - m >= minutes * 60:
            cur[n] = m
            if fired.get(n) != m:
                ev.append("IDLE %s %dm since %s" % (n, (now - m) // 60,
                          time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(m))))
    return ev, cur, miss


def hub_jobs(load=None, make_client=None):
    """The loopback hub server's GET health `jobs` ({name: health block}), or None when the hub is
    down, has no token file or no pointer, or answers anything else. Never raises. `load` and
    `make_client` as hub_check."""
    try:
        from kitlib import pmstart
        port = (load or pmstart.hub_settings)().port
        health = (make_client or pmstart._hub_client)(port).get("/health", timeout=pmstart.HUB_SITE_TIMEOUT_S)
        jobs = health.get("jobs") if isinstance(health, dict) else None
        return jobs if isinstance(jobs, dict) else None
    except Exception:                       # noqa: BLE001 - a hub we cannot read is a hub that is not up
        return None


def hub_plugin_job_up(load=None, make_client=None):
    """True when the loopback hub server's GET health `jobs` names its plugin-refresh job
    (kitlib.hubsite.plugin_job): the hub then owns the plugin refresh. A hub that is down, has no
    token file or no pointer, or answers anything else, is False. Never raises."""
    return "plugin-refresh" in (hub_jobs(load, make_client) or {})


SESSION_JOB = "session-watch"     # the hub's job that runs the session checks (kitlib.hubjobs.session_watch)
SESSION_JOB_FAILS = 3             # failures in a row that end its cover (kitlib.hubsite.jobs FAIL_PUSH)
SESSION_JOB_STALE_S = 300         # its last success older than this: not covering (BEAT_STALE's rule)


def _count(v):
    return type(v) is int and v >= 0


def hub_session_job_up(load=None, make_client=None):
    """True when the hub's GET health `jobs` names the session-watch job and it is not failing
    (`failures` below SESSION_JOB_FAILS): pm_watch's session checks then step aside
    (docs/pm-watch.md "Hub session-watch job"). Its `ok_at` is not asked for: the job skips while
    pm_watch is live and not stepped aside, so it has no success before pm_watch steps aside. A hub
    that is down or unreadable is False. Never raises."""
    block = (hub_jobs(load, make_client) or {}).get(SESSION_JOB)
    return isinstance(block, dict) and _count(block.get("failures")) \
        and block["failures"] < SESSION_JOB_FAILS


def session_yield(sessions, cfg, up=None):
    """pm_watch's step-aside test for Watcher(yield_to=...): `up` (default hub_session_job_up), or
    None when the hub's session-watch job watches other sessions than this run. The job reads the
    config's sessions_file (`cfg`, the "pm_watch" keys), so a run whose sessions file is another
    one, or a config with none (the job then skips every run), never steps aside."""
    theirs = cfg.get("sessions_file")
    norm = lambda p: os.path.normcase(os.path.abspath(p))
    if not sessions or not theirs or norm(sessions) != norm(theirs):
        return None
    return up or hub_session_job_up


def session_job_healthy(block, now):
    """True when a session-watch health block (GET health jobs.session-watch) shows the job
    covering pm_watch's session checks: not failing (`failures` below SESSION_JOB_FAILS) and a
    success (`ok_at`) at most SESSION_JOB_STALE_S before `now`. A skipping job (pm_watch live, no
    sessions file) has no fresh success, so it does not count. Anything else is False."""
    if not isinstance(block, dict) or not _count(block.get("failures")) \
            or block["failures"] >= SESSION_JOB_FAILS:
        return False
    ok = block.get("ok_at")
    return _number(ok) and now - ok <= SESSION_JOB_STALE_S


def hub_session_job_block(load=None, make_client=None):
    """The session-watch job's health block from the loopback hub, or None (hub down, no such
    job). Never raises."""
    block = (hub_jobs(load, make_client) or {}).get(SESSION_JOB)
    return block if isinstance(block, dict) else None


def plugin_check(kit=None, settings=None, spawn=None, alive=None, folders=None, hub_job=None):
    """The PLUGIN check for Watcher: check(last) -> (lines, last), over kitlib.pluginsource.poll
    (docs/plugin-source.md "Behind warnings"). `last` is the kit main sha last reported (None when
    current), kept in the state file so a relaunch does not report the same sha again. With no
    `settings` the machine config's plugin key is read on every call, so a bad key is a failed
    check (ERROR on the third), never a dead watcher, and so is an unreadable tracking ref or
    state.json; with no `kit`, the checkout this code is in; with no `folders`, this machine's
    account folders. With auto_refresh on and the hub's plugin-refresh job up (`hub_job()`, default
    hub_plugin_job_up), the hub is the one owner of the refresh: the check reports the behind
    line and starts nothing; with the hub down it still starts the refresh itself."""
    from kitlib import pluginsource

    def check(last):
        s = pluginsource.load() if settings is None else settings
        if s.auto_refresh and (hub_job or hub_plugin_job_up)():
            s = s._replace(auto_refresh=False)
        return pluginsource.poll(kit or pluginsource.kit_root(), s, last, spawn=spawn, alive=alive,
                                 folders=folders)
    return check


def hub_check(load=None, make_client=None):
    """The HUB check for Watcher: check(last) -> (lines, last). Asks the loopback hub server's
    GET health and gives one line, once per stale episode, when its `code` says hub code newer than
    the running server has landed (kitlib.hubsite.codestale.notice: the owner's restart step).
    `last` is True once reported (kept in the state file, so a relaunch does not say it again) and
    None otherwise: a restarted hub, or one that is down, re-arms it. A hub that is down, has no
    token file, or has no pointer to read is no event and no failure; any other error is a failed
    check (ERROR on the third), never a dead watcher. `load` gives the hub Settings and
    `make_client(port)` the client, as kitlib.pmstart.hub_site takes them."""
    def check(last):
        from kitlib import pmstart
        from kitlib.hubsite import codestale, serve
        down = (pmstart.NoHubToken, serve.NotRunning,
                getattr(pmstart, "NoHubPointer", pmstart.NoHubToken))
        try:
            port = (load or pmstart.hub_settings)().port
            health = (make_client or pmstart._hub_client)(port).get(
                "/health", timeout=pmstart.HUB_SITE_TIMEOUT_S)
        except down:
            return [], None
        text = codestale.notice(health.get("code") if isinstance(health, dict) else None,
                                pmstart.HUB_TOOL)
        if text is None:
            return [], None
        return ([] if last else [text]), True
    return check


RC_GRACE_S = 180     # a session this young may not have connected its Remote Control link yet


def rc_key(r):
    """The rc_lost entry of a session record: name, pid and creation time, so a session stopped and
    started again under the same name is a new one."""
    return "%s %s %s" % (r["name"], r.get("pid"), r.get("proc_start"))


def rc_events(names, records, alive, rc_pids, lost, now):
    """(events, lost now) of the Remote Control link check (docs/pm-watch.md "Remote Control link").

    records: the host's session records, dicts {name, pid, proc_start, started_ms, linked}, linked
    None when the host's record says nothing about the link. alive(rec): its pid runs now with that
    creation time. rc_pids(): {pid: --remote-control name} of the running sessions, read only when
    some session looks unlinked. lost: the rc_key of each session already reported. A watched name
    whose newest live record says unlinked, started more than RC_GRACE_S ago, and whose process was
    started with --remote-control under that name gives REMOTE ... lost once; the same process read
    linked again gives REMOTE ... back once. One too young or silent on the link keeps its earlier
    state, so neither line repeats; a session that is gone, or runs as another process now (a stop
    and a relaunch), drops out, so the new one starts clean. A record whose name is not text is
    skipped."""
    newest = {}
    for r in records:
        n, started = r.get("name"), r.get("started_ms")
        if not isinstance(n, str) or n not in names or isinstance(started, bool)                 or not isinstance(started, (int, float)):
            continue
        if (n not in newest or started > newest[n]["started_ms"]) and alive(r):
            newest[n] = r
    ev, cur, unlinked = [], [], []
    for n in sorted(names):
        r = newest.get(n)
        if r is None:
            continue
        key = rc_key(r)
        if r.get("linked") is None or now - r["started_ms"] / 1000.0 < RC_GRACE_S:
            if key in lost:
                cur.append(key)
        elif r["linked"]:
            if key in lost:
                ev.append("REMOTE %s link back" % n)
        else:
            unlinked.append(r)
    pids = rc_pids() if unlinked else {}
    for r in unlinked:
        n, key = r["name"], rc_key(r)
        if pids.get(r["pid"]) != n:                 # started without --remote-control: no link to lose
            continue
        cur.append(key)
        if key not in lost:
            ev.append("REMOTE %s link lost: the session runs, its Remote Control link is down "
                      "(pid %s)" % (n, r["pid"]))
    return ev, sorted(cur)


def rc_check(sessions, reader, dirs=None, ctime=None, rc_pids=None):
    """The REMOTE check for Watcher: check(lost) -> (lines, lost), over rc_events for the names in
    sessions.json (watched_sessions) and the PM's (kitlib.launcher.stop.PM_RC_NAME: the PM has
    no sessions.json entry). reader(dirs) gives the host's session records
    (on Claude, hosts.claude.adapter.claude_host.rc_records); dirs() the registry folders (default:
    every configured account's, kitlib.launcher.pipes.registry_dirs, read again on each call);
    ctime(pid) the process creation time; rc_pids() the running sessions' --remote-control names.
    The imports happen here, when the watcher is built, so a landing cannot mix modules later."""
    from kitlib import pmstart
    from kitlib.launcher import pipes, settings as launcher_settings, stop
    from kitlib.proc import process
    if dirs is None:
        dirs = lambda: pipes.registry_dirs(launcher_settings.load())
    ctime = ctime or process.proc_ctime
    if rc_pids is None:
        # every process: only the pids of live registry records are looked up, so no image filter
        rc_pids = lambda: {p.get("ProcessId"): pmstart.rc_name(p.get("CommandLine"))
                           for p in pmstart.list_processes()}

    def alive(r):
        try:
            now = ctime(r.get("pid"))
        except (OSError, ValueError, TypeError):
            return False
        return now is not None and r.get("proc_start") is not None and str(now) == str(r["proc_start"])

    def check(lost):
        names = set(watched_sessions(sessions)) | {stop.PM_RC_NAME}
        return rc_events(names, reader(dirs()), alive, rc_pids, list(lost or []), time.time())
    return check


def preload(modules=()):
    """Load, at start, every kit module a check imports on its first pass, so a landing that writes
    this checkout later cannot give the running watcher a mix of old and new modules
    (kitlib.launcher.usage at the first usage read). `modules` names the host's own ones, which the
    host's entry point passes in (on Claude, its plugin source module, imported after the first
    PLUGIN difference), so no host is named here. The function-level imports stay where they are;
    they find these loaded."""
    import importlib
    import kitlib.launcher.settings
    import kitlib.launcher.usage
    import kitlib.machinesetup.extras
    import kitlib.hubsite.codestale
    import kitlib.hubsite.serve
    import kitlib.pluginsource
    import kitlib.pmstart.consoletasks      # kitlib.pluginsource's in-use reads (_processes, _tasks)
    import kitlib.launcher.pipes            # the REMOTE check (rc_check)
    import kitlib.launcher.stop
    import kitlib.proc.process
    for name in modules:
        importlib.import_module(name)


class Watcher:
    """One watcher run. start() records baselines; poll(now) runs the checks that are due and
    returns their event lines. The caller saves state after each poll and stops at the first
    non-empty one. Every check gives an ERROR line on its third failure in a row.

    branches: [(repo_dir, ref)]; usage_reader() -> parse_usage's {account: {"five", "week", "age",
    "stale", "missing"}} (five or week None: unknown);
    sessions: path of sessions.json; newest(name) -> mtime or None; tip(repo, ref) -> short sha;
    plugin: check(last) -> (lines, last) (plugin_check), or None for no PLUGIN check. Unlike the
    others it has no baseline: a plugin export already behind at the first run is reported then.
    hub: check(last) -> (lines, last) (hub_check), or None for no HUB check.
    rc: check(lost) -> (lines, lost) (rc_check), or None for no REMOTE check; on the idle cadence.
    preload_modules: the host's own modules its checks import late (preload).
    stopped: stopped(name, mtime) -> time or None (stop_log_reader over stop.log beside `sessions`).
    yield_to: yield_to() -> True while the hub's session-watch job covers the session checks
    (hub_session_job_up), or None to never step aside; asked once each poll (one that raises is
    False). While True the session checks (USAGE, RESET, IDLE, STOPPED, BGFAIL, LOCKNOTICE, REMOTE
    and their ERROR lines) still run and keep the state current, but their lines are dropped, so
    a hub gone down hands back with nothing repeated; BRANCH, PLUGIN and HUB stay. `yielded` is the
    latest poll's answer, which the heartbeat carries (write_heartbeat).
    """

    def __init__(self, state, branches, usage_reader, alerts_dir, sessions, newest,
                 idle_minutes=20, branch_poll=300, usage_poll=600, idle_poll=300, tip=branch_tip,
                 notice_dir=None, plugin=None, preload_modules=(), stopped=None, hub=None, rc=None,
                 yield_to=None):
        preload(preload_modules)
        self.yield_to = yield_to
        self.yielded = False
        self.stopped = stop_log_reader(sessions) if stopped is None else stopped
        self.s = state
        self.branches = branches
        self.usage_reader = usage_reader
        self.alerts_dir = alerts_dir
        self.sessions = sessions
        self.newest = newest
        self.idle_minutes = idle_minutes
        self.branch_poll, self.usage_poll, self.idle_poll = branch_poll, usage_poll, idle_poll
        self.tip = tip
        self.notice_dir = notice_dir
        self.plugin = plugin
        self.hub = hub
        self.rc = rc
        self.fails = {"branch": 0, "usage": 0, "alerts": 0, "idle": 0, "notices": 0, "plugin": 0,
                      "hub": 0, "rc": 0}
        self.next_b = self.next_u = self.next_i = self.next_p = self.next_h = self.next_r = 0.0

    def start(self, now):
        s = self.s
        tips = s.setdefault("branches", {})
        for repo, ref in self.branches:             # a newly watched branch: baseline, no event
            key = "%s %s" % (repo, ref)
            if key not in tips:
                tips[key] = self.tip(repo, ref)
        if "usage" not in s:
            s["usage"] = self.usage_reader()
        if "bg_alerts" not in s:                    # first run: existing alerts are baseline
            s["bg_alerts"] = alert_events([], self.alerts_dir)[1]
        if self.notice_dir and "lock_notices" not in s:
            s["lock_notices"] = notice_events([], self.notice_dir)[1]
        if "idle" not in s:                         # first run: sessions already quiet are baseline
            s["idle"] = idle_baseline(watched_sessions(self.sessions), self.newest, now,
                                      self.idle_minutes, self.stopped)

    def _failed(self, what, e, events):
        self.fails[what] += 1
        if self.fails[what] == 3:
            events.append("ERROR %s check: %s" % (what, e))

    def poll(self, now):
        s, events = self.s, []
        try:
            self.yielded = bool(self.yield_to and self.yield_to())
        except Exception:                           # noqa: BLE001 - an unreadable hub covers nothing
            self.yielded = False
        sev = [] if self.yielded else events        # the session checks' lines: dropped while yielded
        tips = s.setdefault("branches", {})
        if now >= self.next_b:
            self.next_b = now + self.branch_poll
            try:
                for repo, ref in self.branches:
                    key, tip = "%s %s" % (repo, ref), self.tip(repo, ref)
                    if tip and tip != tips.get(key):
                        events.append("BRANCH %s %s %s" % (repo, ref, tip))
                        tips[key] = tip
                self.fails["branch"] = 0
            except Exception as e:
                self._failed("branch", e, events)
        if now >= self.next_u:
            self.next_u = now + self.usage_poll
            try:
                cur = self.usage_reader()
                self.fails["usage"] = 0
                sev += usage_events(s.get("usage", {}), cur)
                s["usage"] = merge_usage(s.get("usage", {}), cur)
            except Exception as e:
                self._failed("usage", e, sev)
        if now >= self.next_i:
            self.next_i = now + self.idle_poll
            try:
                ev, s["idle"], s["idle_nolog"] = idle_events(
                    watched_sessions(self.sessions), self.newest, s.get("idle", {}),
                    s.get("idle_nolog", []), now, self.idle_minutes, self.stopped)
                sev += ev
                self.fails["idle"] = 0
            except Exception as e:
                self._failed("idle", e, sev)
        try:                                        # every loop: a listdir is cheap
            ev, s["bg_alerts"] = alert_events(set(s.get("bg_alerts", [])), self.alerts_dir)
            sev += ev
            self.fails["alerts"] = 0
        except Exception as e:
            self._failed("alerts", e, sev)
        if self.notice_dir:
            try:
                ev, s["lock_notices"] = notice_events(set(s.get("lock_notices", [])),
                                                      self.notice_dir)
                sev += ev
                self.fails["notices"] = 0
            except Exception as e:
                self._failed("notices", e, sev)
        if self.plugin and now >= self.next_p:      # on the branch cadence: a ref read, a json read
            self.next_p = now + self.branch_poll
            try:
                ev, s["plugin_main"] = self.plugin(s.get("plugin_main"))
                events += ["PLUGIN %s" % line for line in ev]
                self.fails["plugin"] = 0
            except Exception as e:
                self._failed("plugin", e, events)
        if self.hub and now >= self.next_h:         # on the branch cadence: one loopback ask
            self.next_h = now + self.branch_poll
            try:
                ev, s["hub_stale"] = self.hub(s.get("hub_stale"))
                events += ["HUB %s" % line for line in ev]
                self.fails["hub"] = 0
            except Exception as e:
                self._failed("hub", e, events)
        if self.rc and now >= self.next_r:          # on the idle cadence: a folder of small files
            self.next_r = now + self.idle_poll
            try:
                events_rc, s["rc_lost"] = self.rc(s.get("rc_lost", []))
                sev += events_rc
                self.fails["rc"] = 0
            except Exception as e:
                self._failed("rc", e, sev)
        return events
