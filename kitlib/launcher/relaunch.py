"""Relaunching a running PM under a new name (docs/launcher.md "PM relaunch").

For a PM that still runs but can no longer be reached (its Remote Control link died, so the web
app no longer lists it): after-reboot refuses while it runs and stop refuses it. pm-relaunch opens
a new window, the way the after-reboot PM resume does (reboot.resume_pm: coordinators.json's
pm_folder and pm_account, the `pm` session mark), running

  claude --resume '<pm_session_id>' --fork-session --agent pm-kit:pm --remote-control '<new>' -n '<new>' '<prompt>'

The fork's session id is learned as every start learns a new session's: the peer registry record
of that name, written to pipes.json (pipes.record through Host.record_pipe). Once it is known and
differs from the old id, the old PM is told to stand down (the hub's push delivery to the PM, which
coordinators.json still resolves to the old session), then coordinators.json's pm_session_id
becomes the fork's and pm_name its name (stop refuses that name, as it refuses pm-session). The old
session is never stopped: closing its window is the owner's step.

Refused (LaunchError, nothing started or written): no PM recorded in coordinators.json, the recorded
PM not running, a --name that is empty or names a running session or a sessions.json entry.
"""
import os

from kitlib.hubsite import push
from kitlib.launcher import accounts as A
from kitlib.launcher import lookup, pipes, state, window
from kitlib.launcher import start as S
from kitlib.launcher.settings import LaunchError

PM_AGENT = 'pm'
FORK_PROMPT = ("You are a fork of the PM conversation, started by launch.py pm-relaunch as '%s' (Remote Control name "
               "'%s') because the old PM session could no longer be reached. You are the PM now: the launcher records "
               "your session id in coordinators.json and tells the old session to stand down; the owner closes its "
               "window by hand, so never send it work. Run your start-up routine, re-arm the usage watch and any "
               "dead-man checks, then tell the owner briefly that you are the PM under Remote Control name '%s'.")
STAND_DOWN = ("Stand down: launch.py pm-relaunch started '%s' (session %s), a fork of this conversation with Remote "
              "Control name '%s', and coordinators.json now names it the PM. From now on do nothing: no sends, "
              "dispatches, landings or hub writes; delete your crons and stop your monitors and loops, then stay idle. "
              "The owner closes this window by hand.")
OWNER_STEP = ("Owner step: close the old PM window by hand once '%s' has checked in; the launcher never stops the "
              "PM (launch.py stop refuses it).")


def recorded_pm(settings):
    """(session id, folder, account) of coordinators.json's PM, or LaunchError when it names none."""
    cfg = state.read_coordinators(settings) or {}
    sid, folder, acct = cfg.get('pm_session_id'), cfg.get('pm_folder'), cfg.get('pm_account')
    if not isinstance(sid, str) or not sid or sid.startswith('<') or not folder or not acct:
        raise LaunchError('coordinators.json (%s) names no running PM (pm_session_id, pm_folder, pm_account); '
                          'nothing was started. A PM that is not running is resumed by after-reboot.'
                          % state.path(settings, state.COORDINATORS))
    return sid, folder, acct


def _live_records(settings, host):
    return [r for _d, r in pipes.records(pipes.registry_dirs(settings)) if pipes.is_live(r, host.ctime)]


def pm_running(procs, records, sid):
    """True when the PM session `sid` runs: a live registry record with that sessionId (a resumed,
    forked or desktop-app PM), or a launched process whose command line carries the id or the name
    pm-session (reboot.running's test)."""
    if any(r.get('sessionId') == sid for r in records):
        return True
    for p in lookup.claude_sessions(procs):
        cmd = p.get('CommandLine') or ''
        if sid in cmd or lookup.rc_name(cmd) == S.PM_NAME:
            return True
    return False


def check_name(settings, procs, records, name):
    """LaunchError when `name` is not a usable new session name: empty, spaced, or the name of a
    running launched session, of a live registry record, or of a sessions.json entry."""
    if not name or name.strip() != name or '\n' in name or '\r' in name:
        raise LaunchError('--name must be a session name on one line, no surrounding spaces; nothing was started.')
    window.check_name(name)
    live = lookup.find_exact(lookup.claude_sessions(procs), name)
    if live:
        raise LaunchError("--name '%s' is already taken: a session of that name is running (PID %s); nothing was "
                          'started.' % (name, lookup.pids(live)))
    if any(r.get('name') == name for r in records):
        raise LaunchError("--name '%s' is already taken: a live session has that name; nothing was started." % name)
    try:
        sessions = state.read_sessions(settings)
    except LaunchError:
        sessions = {}
    for key, entry in state.entries(sessions):
        if entry.get('name') == name:
            raise LaunchError("--name '%s' is already taken: sessions.json entry '%s' has that name; nothing was "
                              'started.' % (name, key))


def fork_command(sid, name):
    """The fork's claude command line."""
    return window.claude_command(name, FORK_PROMPT % (name, name, name), S.run_as(PM_AGENT), resume=sid, fork=True)


def relaunch(settings, host, name, delivery=None):
    """Fork the running PM into a new window named `name`; the lines it says. `delivery` is the
    host's hub push delivery (an object with deliver(recipient, text) -> push.Sent), or None when
    the host has none: the stand-down is then the owner's to send."""
    sid, folder, acct = recorded_pm(settings)
    if not os.path.isdir(folder):
        raise LaunchError('pm_folder %s in coordinators.json is not a folder; nothing was started.' % folder)
    procs, records = host.processes(), _live_records(settings, host)
    if not pm_running(procs, records, sid):
        raise LaunchError('the PM session %s that coordinators.json records is not running; nothing was started. '
                          'Resume it with launch.py after-reboot.' % sid)
    check_name(settings, procs, records, name)
    d = A.launch_dir(settings, str(acct))
    config_dir = None if settings.is_default(d) else d
    text = window.script(name, window.PM, folder, fork_command(sid, name), config_dir, host.env('AGENT_TEMP'))
    after_ms = pipes.launch_ms(host.now())
    kw = window.open_args(name, settings)
    host.open_window(text, **kw)
    out = ['Started %s (account %s), a fork of the PM session %s, in %s. Remote Control name: %s'
           % (name, acct, sid, window.opened_in(name, kw.get('terminal')), name)]
    try:            # the window is open: a fault here must not read as a failed launch
        said = host.record_pipe(settings, name, after_ms)
    except (OSError, ValueError, LaunchError) as e:
        said = 'pipe not recorded (%s)' % e
    entry = pipes.read(settings).get(name) if said is None else None
    new_sid = entry.get('session_id') if isinstance(entry, dict) else None
    if not isinstance(new_sid, str) or not new_sid or new_sid == sid:
        why = said or ('the fork kept the old session id' if new_sid == sid else 'no session id was recorded')
        return out + ['The fork was not confirmed (%s): coordinators.json still names %s and the old PM was not '
                      "told to stand down. Check the '%s' window; if it runs, tell the old PM to stand down and set "
                      'pm_session_id by hand.' % (why, sid, name), OWNER_STEP % name]
    out.append(stand_down(delivery, name, new_sid, sid))
    cfg = state.read_coordinators(settings) or {}          # read again: write only the two PM keys
    cfg['pm_session_id'], cfg['pm_name'] = new_sid, name
    state.write_json(state.path(settings, state.COORDINATORS), cfg)
    out.append('coordinators.json: pm_session_id %s -> %s, pm_name %s' % (sid, new_sid, name))
    return out + [OWNER_STEP % name]


def stand_down(delivery, name, new_sid, old_sid):
    """Tell the old PM to stand down; the line to say. Sent while coordinators.json still names
    the old session, so the hub's PM resolution reaches it."""
    if delivery is None:
        return ('Stand-down not sent: this host has no push delivery. Tell the old PM yourself: %s'
                % (STAND_DOWN % (name, new_sid, name)))
    try:
        sent = delivery.deliver(push.PM_KEY, STAND_DOWN % (name, new_sid, name))
    except Exception as e:                          # noqa: BLE001 - the fork runs; say it and go on
        return 'Stand-down not sent (%s: %s): tell the old PM to stand down yourself.' % (type(e).__name__, e)
    if sent.state != push.WRITTEN:
        return ('Stand-down not sent (%s): tell the old PM (session %s) to stand down yourself.'
                % (sent.state, old_sid))
    if sent.session and sent.session != old_sid:
        return ('WARNING: the stand-down reached %s (session %s), not the old PM session %s: tell the old PM to '
                'stand down yourself.' % (sent.name, sent.session, old_sid))
    return 'Told the old PM (%s, session %s) to stand down.' % (sent.name, sent.session or old_sid)
