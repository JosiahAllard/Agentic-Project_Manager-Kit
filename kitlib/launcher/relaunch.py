"""Relaunching a running PM under a new name (docs/launcher.md "PM relaunch").

For a PM that still runs but can no longer be reached (its Remote Control link died, so the web
app no longer lists it): after-reboot refuses while it runs and stop refuses it. pm-relaunch opens
a new window, the way the after-reboot PM resume does (reboot.resume_pm: coordinators.json's
pm_folder and pm_account, the `pm` session mark), running

  claude --resume '<pm_session_id>' --fork-session --agent pm-kit:pm --remote-control '<new>' -n '<new>' '<prompt>'

The fork's session id is learned from the peer registry record of that name (pipes.await_record),
polled for up to `wait_s` seconds (WAIT_S by default: a long transcript resumes slowly) until its
sessionId differs from the old id, and written to pipes.json. Once it is known, the old PM is told
to stand down (the hub's push delivery to the PM, which coordinators.json still resolves to the old
session), then coordinators.json's pm_session_id becomes the fork's and pm_name its name (stop
refuses that name, as it refuses pm-session; the hub's peer resolution counts it a PM name). That
write is retried; when it still fails, the lines so far are kept and the manual step is said. The
old session is never stopped: closing its window is the owner's step.

The fork's first message (fork_prompt) has it act as the PM only once coordinators.json names its
own session id, so an unconfirmed fork waits or asks the owner instead of being a second PM.

Refused (LaunchError, nothing started or written): no PM recorded in coordinators.json, the recorded
PM not running, a --name that is empty, a PM name (stop's test), or names a running session or a
sessions.json entry.

PM crossover (crossover; docs/launcher.md "PM crossover"): the hub's usage crossover moves the PM
to another account. A conversation cannot resume on another account, so the new PM starts fresh in
pm_folder with that account's config folder and reads the hand-off file (`state_path`, the PM's
HANDOFF.md, else the one its start-up routine finds):

  claude --agent pm-kit:pm --remote-control '<new>' -n '<new>' '<prompt>'

No --permission-mode: the PM's own starts (the desktop app, the after-reboot resume) pass none, so
neither does this one. Then the same confirm as pm-relaunch: the new session id from the registry,
the stand-down pushed to the old PM, and coordinators.json's pm_session_id, pm_name and pm_account
written. Unlike pm-relaunch, an unconfirmed start or a failed write is a LaunchError (exit 1), the
lines so far kept: the hub job reads the exit code. Refused as pm-relaunch is, and also for the
account the PM is on and an account with no sign-in. The default name is pm-a<N>, pm-a<N>-<k> when
that one is taken.
"""
import json
import os

from kitlib.fs import replace as fsreplace
from kitlib.hubsite import push
from kitlib.launcher import accounts as A
from kitlib.launcher import lookup, pipes, state, stop, window
from kitlib.launcher import start as S
from kitlib.launcher.settings import LaunchError

PM_AGENT = 'pm'
WAIT_S = 120                    # seconds pm-relaunch waits for the fork's own session id
WRITE_TRIES = 3                 # tries of the coordinators.json write
WRITE_WAIT = fsreplace.WAIT     # seconds each try's replace waits out a reader holding the file
FORK_PROMPT = ("You are a fork of the PM conversation, started by launch.py pm-relaunch as '%s' (Remote Control name "
               "'%s') because the old PM session could no longer be reached. Act as the PM only once %s has "
               "pm_session_id equal to your own session id (the launcher writes it, with pm_name '%s', once it sees "
               "your id, and then tells the old session to stand down). Until then send nothing, dispatch nothing "
               "and write nothing: check that file again every minute, and if it still names another session after "
               "10 minutes, ask the owner whether you are the PM. Once it names you: the owner closes the old "
               "window by hand, so never send it work; run your start-up routine, re-arm the usage watch and any "
               "dead-man checks, then tell the owner briefly that you are the PM under Remote Control name '%s'.")
STAND_DOWN = ("Stand down: launch.py pm-relaunch started '%s' (session %s), a fork of this conversation with Remote "
              "Control name '%s', and coordinators.json now names it the PM. From now on do nothing: no sends, "
              "dispatches, landings or hub writes; delete your crons and stop your monitors and loops, then stay idle. "
              "The owner closes this window by hand.")
CROSS_PROMPT = ("You are the new PM, started fresh by the hub's usage crossover (launch.py pm-crossover) as '%s' on "
                "account %s (Remote Control name '%s') because the old PM's account neared its weekly limit; a "
                "conversation cannot move between accounts, so you start from the hand-off file. Act as the PM "
                "only once %s has pm_session_id equal to your own session id (the launcher writes it, with pm_name "
                "'%s' and pm_account %s, once it sees your id, and then tells the old session to stand down). Until "
                "then send nothing, dispatch nothing and write nothing: check that file again every minute, and if "
                "it still names another session after 10 minutes, ask the owner whether you are the PM. Once it "
                "names you: run your start-up routine, %s; the owner closes the old window by hand, so never send it "
                "work. Re-arm the usage watch and any dead-man checks, then tell the owner briefly that you are the "
                "PM under Remote Control name '%s' on account %s.")
CROSS_READ = 'reading the hand-off file %s at its hand-off step'
CROSS_READ_DEFAULT = 'reading the hand-off file it names'
CROSS_STAND_DOWN = ("Stand down: the hub's usage crossover started '%s' (session %s), a fresh PM on account %s that "
                    "starts from your hand-off file, and coordinators.json now names it the PM. From now on do "
                    "nothing: no sends, dispatches, landings or hub writes; delete your crons and stop your monitors "
                    "and loops, then stay idle. The owner closes this window by hand.")
CROSS_NAME = 'pm-a%s'
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
    for p in lookup.launched(procs):
        cmd = p.get('CommandLine') or ''
        if sid in cmd or lookup.rc_name(cmd) == S.PM_NAME:
            return True
    return False


def check_name(settings, procs, records, name):
    """LaunchError when `name` is not a usable new session name: empty, spaced, or the name of a
    running launched session, of a live registry record, or of a sessions.json entry."""
    if not name or name.strip() != name or '\n' in name or '\r' in name:
        raise LaunchError('--name must be a session name on one line, no surrounding spaces; nothing was started.')
    if stop._pm_named(name):
        raise LaunchError("--name '%s' is a PM name (it holds 'pm-session' or 'project manager'): coordinators "
                          'would take it for the old PM; nothing was started.' % name)
    window.check_name(name)
    live = lookup.find_exact(lookup.launched(procs), name)
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


def fork_prompt(name, coordinators_path):
    """The fork's first message: it acts as the PM only once coordinators.json names its session."""
    return FORK_PROMPT % (name, name, coordinators_path, name, name)


def fork_command(sid, name, coordinators_path):
    """The fork's claude command line."""
    return window.session_command(name, fork_prompt(name, coordinators_path), S.run_as(PM_AGENT), resume=sid,
                                  fork=True)


def await_fork(settings, host, name, after_ms, old_sid, wait_s):
    """(record, line): the fork's live registry record once its sessionId differs from old_sid,
    its pipes.json entry written, and None; or (None, why) when none came in wait_s seconds."""
    tries = max(1, int(round(wait_s / pipes.POLL_S)))
    rec = pipes.await_record(settings, name, after_ms, host.ctime, host.sleep, tries, pipes.POLL_S,
                             accept=lambda r: isinstance(r.get('sessionId'), str) and r['sessionId']
                             and r['sessionId'] != old_sid)
    if rec is None:
        seen = pipes.find(pipes.registry_dirs(settings), name, after_ms, host.ctime)
        why = ('the fork kept the old session id for %s s' % wait_s if seen is not None
               else 'no session of that name came up in %s s' % wait_s)
        return None, why
    return rec, pipes.write_entry(settings, name, rec, host.now)


def _write_coordinators(p, cfg):
    tmp = p + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(cfg, indent=4, ensure_ascii=False) + '\n')
        fsreplace.replace(tmp, p, wait=WRITE_WAIT)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _account_value(account):
    """pm_account as coordinators.json keeps it: a number for an account number."""
    return int(account) if str(account).isdigit() else account


def record_pm(settings, host, new_sid, name, account=None):
    """Set coordinators.json's pm_session_id and pm_name (and pm_account when `account` is given),
    read again so only those keys change; retried WRITE_TRIES times. None when written, else the
    error's text."""
    err = None
    for i in range(WRITE_TRIES):
        try:
            p = state.path(settings, state.COORDINATORS)
            cfg = state.read_coordinators(settings) or {}
            cfg['pm_session_id'], cfg['pm_name'] = new_sid, name
            if account is not None:
                cfg['pm_account'] = _account_value(account)
            _write_coordinators(p, cfg)
            return None
        except (LaunchError, ValueError, OSError) as e:
            err = '%s: %s' % (type(e).__name__, e)
        if i < WRITE_TRIES - 1:
            host.sleep(1)
    return err


def relaunch(settings, host, name, delivery=None, wait_s=WAIT_S):
    """Fork the running PM into a new window named `name`; the lines it says. `delivery` is the
    host's hub push delivery (an object with deliver(recipient, text) -> push.Sent), or None when
    the host has none: the stand-down is then the owner's to send. wait_s: how long to wait for
    the fork's own session id."""
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
    coords = state.path(settings, state.COORDINATORS)
    text = window.script(name, window.PM, folder, fork_command(sid, name, coords), config_dir,
                         host.env('AGENT_TEMP'))
    after_ms = pipes.launch_ms(host.now())
    kw = window.open_args(name, settings)
    host.open_window(text, **kw)
    out = ['Started %s (account %s), a fork of the PM session %s, in %s. Remote Control name: %s'
           % (name, acct, sid, window.opened_in(name, kw.get('terminal')), name)]
    try:            # the window is open: a fault here must not read as a failed launch
        rec, said = await_fork(settings, host, name, after_ms, sid, wait_s)
    except (OSError, ValueError, LaunchError) as e:
        rec, said = None, 'the registry could not be read (%s)' % e
    if rec is None:
        return out + ['The fork was not confirmed (%s): coordinators.json still names %s and the old PM was not '
                      "told to stand down; the fork waits until coordinators.json names it. Check the '%s' window; "
                      'if it runs as its own session, tell the old PM to stand down and set pm_session_id to the '
                      "fork's session id and pm_name to %s in %s." % (said, sid, name, name, coords), OWNER_STEP % name]
    new_sid = rec['sessionId']
    if said:
        out.append(said)
    out.append(stand_down(delivery, name, new_sid, sid))
    err = record_pm(settings, host, new_sid, name)
    if err:
        out.append('coordinators.json was NOT updated (%s). Manual step: set pm_session_id to %s and pm_name to %s '
                   'in %s.' % (err, new_sid, name, coords))
    else:
        out.append('coordinators.json: pm_session_id %s -> %s, pm_name %s' % (sid, new_sid, name))
    return out + [OWNER_STEP % name]


def stand_down(delivery, name, new_sid, old_sid, text=None):
    """Tell the old PM to stand down; the line to say. Sent while coordinators.json still names
    the old session, so the hub's PM resolution reaches it. `text`: the message, by default
    pm-relaunch's."""
    text = text or STAND_DOWN % (name, new_sid, name)
    if delivery is None:
        return 'Stand-down not sent: this host has no push delivery. Tell the old PM yourself: %s' % text
    try:
        sent = delivery.deliver(push.PM_KEY, text)
    except Exception as e:                          # noqa: BLE001 - the fork runs; say it and go on
        return 'Stand-down not sent (%s: %s): tell the old PM to stand down yourself.' % (type(e).__name__, e)
    if sent.state != push.WRITTEN:
        return ('Stand-down not sent (%s): tell the old PM (session %s) to stand down yourself.'
                % (sent.state, old_sid))
    if sent.session and sent.session != old_sid:
        return ('WARNING: the stand-down reached %s (session %s), not the old PM session %s: tell the old PM to '
                'stand down yourself.' % (sent.name, sent.session, old_sid))
    return 'Told the old PM (%s, session %s) to stand down.' % (sent.name, sent.session or old_sid)


def _taken(settings, procs, records):
    """The names a new session may not take: running sessions, live records, sessions.json entries."""
    out = {lookup.rc_name(p.get('CommandLine')) for p in lookup.launched(procs)}
    out.update(r.get('name') for r in records)
    try:
        sessions = state.read_sessions(settings)
    except LaunchError:
        sessions = {}
    out.update(entry.get('name') for _key, entry in state.entries(sessions))
    return {n for n in out if n}


def crossover_name(account, taken):
    """pm-a<N>, or pm-a<N>-<k> (k from 2) when that name is taken."""
    base = CROSS_NAME % account
    if base not in taken:
        return base
    k = 2
    while '%s-%d' % (base, k) in taken:
        k += 1
    return '%s-%d' % (base, k)


def crossover_prompt(name, account, coordinators_path, state_path=None):
    """The new PM's first message: it acts as the PM only once coordinators.json names its session."""
    read = CROSS_READ % state_path if state_path else CROSS_READ_DEFAULT
    return CROSS_PROMPT % (name, account, name, coordinators_path, name, account, read, name, account)


def crossover_command(name, prompt):
    """The new PM's claude command line: a fresh session, no --resume and no --permission-mode."""
    return ' '.join(['claude', '--agent', S.run_as(PM_AGENT), '--remote-control', window.q(name),
                     '-n', window.q(name), window.q(prompt)])


def crossover(settings, host, account, name=None, state_path=None, delivery=None, wait_s=WAIT_S,
              what_if=False):
    """Start a fresh PM on `account` from the hand-off file and make it the PM; the lines it says
    (module docstring "PM crossover"). LaunchError, with the lines so far, when it was not
    confirmed or coordinators.json was not written."""
    account = str(account)
    sid, folder, acct = recorded_pm(settings)
    if str(acct) == account:
        raise LaunchError('the PM is already on account %s; nothing was started.' % account)
    if not os.path.isdir(folder):
        raise LaunchError('pm_folder %s in coordinators.json is not a folder; nothing was started.' % folder)
    if state_path is not None and not os.path.isfile(state_path):
        raise LaunchError('--state %s is not a file; nothing was started.' % state_path)
    d = A.launch_dir(settings, account)
    procs, records = host.processes(), _live_records(settings, host)
    if not pm_running(procs, records, sid):
        raise LaunchError('the PM session %s that coordinators.json records is not running; nothing was started. '
                          'Resume it with launch.py after-reboot.' % sid)
    if name is None:
        name = crossover_name(account, _taken(settings, procs, records))
    check_name(settings, procs, records, name)
    config_dir = None if settings.is_default(d) else d
    coords = state.path(settings, state.COORDINATORS)
    cmd = crossover_command(name, crossover_prompt(name, account, coords, state_path))
    if what_if:
        return ['Would start %s, a fresh PM on account %s (the PM session %s is on account %s), in %s with config '
                'folder %s: %s' % (name, account, sid, acct, folder, config_dir or 'the default', cmd)]
    text = window.script(name, window.PM, folder, cmd, config_dir, host.env('AGENT_TEMP'))
    after_ms = pipes.launch_ms(host.now())
    kw = window.open_args(name, settings)
    host.open_window(text, **kw)
    out = ['Started %s, a fresh PM on account %s (the PM session %s is on account %s), in %s. Remote Control '
           'name: %s' % (name, account, sid, acct, window.opened_in(name, kw.get('terminal')), name)]
    try:            # the window is open: a fault here must not read as a failed launch
        rec, said = await_fork(settings, host, name, after_ms, sid, wait_s)
    except (OSError, ValueError, LaunchError) as e:
        rec, said = None, 'the registry could not be read (%s)' % e
    if rec is None:
        raise LaunchError("the new PM was not confirmed (%s): coordinators.json still names %s and the old PM was not "
                          "told to stand down; the new PM waits until coordinators.json names it. Check the '%s' "
                          "window; if it runs as its own session, tell the old PM to stand down and set pm_session_id "
                          "to its session id, pm_name to %s and pm_account to %s in %s."
                          % (said, sid, name, name, account, coords), out + [OWNER_STEP % name])
    new_sid = rec['sessionId']
    if said:
        out.append(said)
    out.append(stand_down(delivery, name, new_sid, sid, CROSS_STAND_DOWN % (name, new_sid, account)))
    err = record_pm(settings, host, new_sid, name, account)
    if err:
        raise LaunchError('coordinators.json was NOT updated (%s). Manual step: set pm_session_id to %s, pm_name to %s '
                          'and pm_account to %s in %s.' % (err, new_sid, name, account, coords),
                          out + [OWNER_STEP % name])
    out.append('coordinators.json: pm_session_id %s -> %s, pm_name %s, pm_account %s -> %s'
               % (sid, new_sid, name, acct, account))
    return out + [OWNER_STEP % name]
