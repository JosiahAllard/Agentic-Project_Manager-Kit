"""After a restart (docs/launcher.md "After a reboot"): run by the hub's coord-restart job once
after a boot (kitlib.hubjobs.coord_restart), or by hand. It starts every plain sessions.json entry
(`agent` false) on the account pick-account chooses, then resumes the PM session with Remote
Control and tells it to re-sync. An agent entry (a coordinator) is the hub's: coord-restart starts
it, so after-reboot leaves it alone. Anything already running is left alone, so a second run never
starts duplicates.

An entry's brief is passed whenever it has one; a failed start, of any kind, is logged FAILED and
the run goes on to the next entry. The run has no console, so the failures around the entries (no
signed-in account, sessions.json or coordinators.json missing or bad, the PM account not signed
in) are logged FAILED too, never raised.

Skipped: a "_" key, an entry without a stem, "autostart": false, an agent entry, and an entry
whose stem is already running under any account or recycle. The PM resume is skipped when
pm-session, the PM session id or coordinators.json's pm_name (pm-relaunch's fork) is running, when
the Claude desktop app is running (resuming there too would fork the conversation) unless
launcher.pm_resume_beside_desktop is true, and, more than 15 minutes after boot, when the PM
transcript was written in the last 15 minutes. --test starts only a throwaway "autostart-test"
session.
"""
import glob
import os

from kitlib import platform
from kitlib.launcher import accounts as A
from kitlib.launcher import lookup, state, window
from kitlib.launcher import start as S
from kitlib.launcher.settings import LaunchError

LOG = 'after-reboot.log'
TEST_NAME = 'autostart-test'
FRESH_MINUTES = 15
PM_AGENT = 'pm'    # the kit's PM agent (content/roles/pm.md), resumed as --agent pm-kit:pm
WAIT_SECONDS = 60
PROMPT = ('Start up now: the machine restarted and after-reboot launched you. Run your start-up routine, read your '
          'state note %s, check in with %s and say the machine restarted, then continue '
          'your queue.')
PM_PROMPT = ('The machine restarted and the hub resumed you through after-reboot; its coord-restart job brings the '
             'coordinators back one at a time, about 5 minutes apart. Re-arm the usage watch and any dead-man checks, '
             'find the coordinators with ListAgents as they come back, and send each a sync (main state, what was in '
             'flight, anything from the owner). Then tell the owner briefly what came back and what did not, and that '
             'this %s must be stopped before the Project Manager conversation is opened in the desktop app, so it '
             'does not fork.' % S.PM_NAME)


def why(e):
    """A failure as the log says it: a LaunchError's own text, anything else with its type."""
    return str(e) if isinstance(e, LaunchError) else '%s: %s' % (type(e).__name__, e)


def desktop_app_running(procs):
    """True when a Claude process runs that no launcher started: the desktop app (the Store
    install under WindowsApps) or a session with no Remote Control name."""
    for p in procs:
        if (p.get('Name') or '').lower() != 'claude.exe':
            continue
        if lookup.pmstart.STORE_DIR in (p.get('ExecutablePath') or '') or not lookup.rc_name(p.get('CommandLine')):
            return True
    return False


def running(procs, text):
    """A claude.exe whose command line contains text (the old after-reboot test, for the PM)."""
    return any((p.get('Name') or '').lower() == 'claude.exe' and text in (p.get('CommandLine') or '') for p in procs)


def newest_transcript_mtime(settings, session_id):
    best = None
    roots = {settings.default_folder()}
    for n in settings.account_numbers():
        roots.update(settings.account_dirs(n))
    for root in roots:
        for p in glob.glob(os.path.join(root, 'projects', '*', session_id + '.jsonl')):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            best = m if best is None or m > best else best
    return best


def run(settings, host, test=False, dry_run=False, say=None):
    """The lines a run says (and logs). `say`, when given, takes each line as it happens (the
    caller's console) and the run returns none of them: the wait before anything starts is
    WAIT_SECONDS, and a caller that prints only the returned list says nothing until it is over."""
    out = []

    def tell(line):
        if say is None:
            out.append(line)
        else:
            say(line)

    def log(line):
        tell(line)
        if not dry_run:
            state.log(settings, LOG, line)

    log('start (test=%s, dry_run=%s)' % (test, dry_run))
    if not dry_run:
        log('waiting %d s for the network before starting anything' % WAIT_SECONDS)
        host.sleep(WAIT_SECONDS)                     # let the network come up after a boot
    procs = host.processes()
    if test:
        if running(procs, TEST_NAME):
            log('test session already running')
            return out
        folder = host.home
        cmd = 'claude --remote-control %s -n %s' % (window.q(TEST_NAME), window.q(TEST_NAME))
        text = window.script(TEST_NAME, window.PLAIN, folder, cmd, None, host.env('AGENT_TEMP'))
        if dry_run:
            tell('would start %s in %s' % (TEST_NAME, folder))
            return out
        host.open_window(text, **window.open_args(TEST_NAME, settings))
        if platform.NAME == 'linux':
            log('started %s in %s' % (TEST_NAME, window.opened_in(TEST_NAME, settings.terminal)))
        else:
            log('started %s: its window opens minimized and unfocused (window.open_window), so look '
                'for it on the taskbar' % TEST_NAME)
        return out
    try:
        cfg = state.read_coordinators(settings) or {}
    except Exception as e:                           # a bad coordinators.json still lets the coordinators start
        log('FAILED to read coordinators.json : %s' % why(e))
        cfg = {}
    try:
        acct, _ = A.pick(settings, host.now())
        sessions = state.read_sessions(settings)
    except Exception as e:                           # no account signed in, sessions.json missing or bad
        log('FAILED, no session started : %s' % why(e))
        sessions = None
    changed = False
    for key, entry in (state.entries(sessions) if sessions is not None else []):
        if not entry.get('stem'):
            continue
        if 'autostart' in entry and not entry['autostart']:
            log('%s autostart off, skipped' % key)
            continue
        if entry.get('agent'):
            log('%s a coordinator: the hub restarts it (coord-restart), skipped' % key)
            continue
        if lookup.running_stem(procs, entry['stem']):
            log('%s already running, skipped' % key)
            continue
        args = S.entry_start_args(settings, key, entry, acct, PROMPT % (entry.get('state'), S.PM_REF),
                                  any_brief=True)
        if dry_run:
            tell('would start %s (%s): %s' % (args['name'], key, '; '.join(
                '%s=%s' % (k, v) for k, v in sorted(args.items()) if v not in (None, '', False))))
            continue
        try:
            S.start(settings, host, **args)
        except Exception as e:                       # one failed window never skips the rest, sessions.json or the PM
            log('FAILED to start %s : %s' % (key, why(e)))
            continue
        log('started %s (%s) on account %s' % (args['name'], key, acct))
        entry['account'], entry['name'], changed = int(acct), args['name'], True
    if changed and not dry_run:
        try:
            state.write_json(state.path(settings, state.SESSIONS), sessions)
        except Exception as e:
            log('FAILED to write sessions.json : %s' % why(e))
    out += resume_pm(settings, host, cfg, log, dry_run)
    return out


def resume_pm(settings, host, cfg, log, dry_run=False):
    """Resume the PM session unless a check skips it. dry_run: every check runs and the decision
    is said ('would resume pm-session ...'), but no window opens."""
    sid, folder, pm_account = cfg.get('pm_session_id'), cfg.get('pm_folder'), cfg.get('pm_account')
    if not sid or not folder or not pm_account or str(sid).startswith('<'):
        log('coordinators.json names no PM session (pm_session_id, pm_folder, pm_account), pm-session skipped')
        return []
    procs = host.processes()
    pm_name = cfg.get('pm_name')                     # pm-relaunch's fork: its command line carries the old id
    named = isinstance(pm_name, str) and pm_name and lookup.find_exact(lookup.launched(procs), pm_name)
    if running(procs, 'pm-session') or running(procs, sid) or named:
        log('pm-session already running, skipped')
        return []
    if desktop_app_running(procs):
        if not settings.pm_resume_beside_desktop:
            log('desktop app running, pm-session skipped')
            return []
        log('desktop app running, pm-session not skipped (launcher.pm_resume_beside_desktop)')
    m = newest_transcript_mtime(settings, sid)
    if host.uptime_minutes() > FRESH_MINUTES and m is not None and host.now().timestamp() - m < FRESH_MINUTES * 60:
        log('pm transcript active, pm-session skipped')
        return []
    try:
        d = A.launch_dir(settings, str(pm_account))
        config_dir = None if settings.is_default(d) else d
        cmd = window.claude_command('pm-session', PM_PROMPT, S.run_as(PM_AGENT), resume=sid)
        text = window.script('pm-session', window.PM, folder, cmd, config_dir, host.env('AGENT_TEMP'))
        if dry_run:
            log('would resume pm-session (session %s, account %s) in %s' % (sid, pm_account, folder))
            return []
        host.open_window(text, **window.open_args('pm-session', settings))
    except Exception as e:                           # the PM account not signed in, or the window failed
        log('FAILED to resume pm-session : %s' % why(e))
        return []
    log('started pm-session')
    if cfg.get('pm_name') is not None:
        clear_pm_name(settings, log)
    return []


def clear_pm_name(settings, log):
    """Drop coordinators.json's pm_name: the PM runs as pm-session again, so a name pm-relaunch
    gave it no longer names it. Read again so only that key changes; a fault is logged."""
    try:
        p = state.path(settings, state.COORDINATORS)
        cfg = state.read_json(p) or {}
        if cfg.pop('pm_name', None) is not None:
            state.write_json(p, cfg)
            log('cleared pm_name in coordinators.json (the PM runs as pm-session)')
    except (LaunchError, OSError, ValueError) as e:
        log('FAILED to clear pm_name in coordinators.json : %s' % why(e))
