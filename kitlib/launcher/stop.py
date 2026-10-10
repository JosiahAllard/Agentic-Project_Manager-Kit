"""Stopping one session and closing its window (docs/launcher.md "Stop").

The PM runs it only after a successor is running and has checked in; handoff --recycle prints the
exact line. The name is matched against claude.exe's --remote-control argument exactly, as a whole
token. Refused (REFUSED: ..., nothing stopped):
  - a name or successor naming the PM session (pm-session, Project Manager);
  - a name with surrounding spaces, or a successor equal to the name;
  - a successor that is not running;
  - no match; several matches, unless --pid picks one; a --pid that is not one of them;
  - a match that is the PM session (its name, or the pm_session_id of conductors.json);
  - a match that is this process's own ancestor (the caller).
It stops the matched claude.exe, then its parent window only when that parent is powershell.exe
whose script (plain or -EncodedCommand) carries the same name quoted as the window writes it, and
that window's conhost.exe when the window is its only child. Never by window title alone. Each
process is stopped by pid and creation time, read when it is chosen, so a reused pid is never hit;
the session's command line is read again at that creation time and must still carry the name.
"""
import re
import sys

from kitlib.launcher import lookup, state, window
from kitlib.launcher.settings import LaunchError

PM_RC_NAME = 'pm-session'
LOG = 'stop.log'
_ENC = re.compile(r'(?:^|\s)-(?:EncodedCommand|enc|ec)\s+([A-Za-z0-9+/=]+)', re.I)


class Refused(LaunchError):
    """A stop refusal: the CLI prints REFUSED: <message> and exits 2."""


def window_script(cmd):
    """The script of a window's command line: decoded from -EncodedCommand, else the line itself."""
    m = _ENC.search(cmd or '')
    if m:
        return window.decode(m.group(1))
    return cmd


def _pm_named(n):
    """Case-insensitive, as stop-session.ps1's -match was."""
    low = (n or '').lower()
    return bool(low) and (PM_RC_NAME.lower() in low or 'project manager' in low)


def plan(procs, name, successor=None, pid=0, own_pid=None, pm_session_id=None):
    """(target, window, conhost, successor rows, lines) for a stop, or Refused."""
    for n in (name, successor):
        if _pm_named(n):
            raise Refused("'%s' names the PM session; never stopped." % n)
    if not name or name.strip() != name:
        raise Refused('--name must be the exact session name, no surrounding spaces.')
    if successor and successor == name:
        raise Refused('--successor equals --name.')
    if pid is not None and int(pid) < 0:
        raise Refused('--pid must be a process id, not %s.' % pid)
    claudes = lookup.claude_sessions(procs)
    succ = []
    if successor:
        succ = lookup.find_exact(claudes, successor)
        if not succ:
            raise Refused("successor '%s' is not running (no claude.exe with --remote-control exactly that name)."
                          % successor)
    hits = lookup.find_exact(claudes, name)
    if not hits:
        raise Refused("no claude.exe with --remote-control exactly '%s'." % name)
    if pid:
        picked = [h for h in hits if int(h.get('ProcessId')) == int(pid)]
        if not picked:
            raise Refused("PID %s is not a claude.exe named '%s' (matches: PIDs %s); stopping none."
                          % (pid, name, lookup.pids(hits)))
        hits = picked
    if len(hits) > 1:
        raise Refused("%d claude.exe match '%s' (PIDs %s); stopping none." % (len(hits), name, lookup.pids(hits)))
    target = hits[0]
    tpid = int(target['ProcessId'])
    if ((lookup.rc_name(target.get('CommandLine')) or '').startswith(PM_RC_NAME)
            or (pm_session_id and pm_session_id in (target.get('CommandLine') or ''))):
        raise Refused('PID %d is the PM session.' % tpid)
    by_pid = {int(p['ProcessId']): p for p in procs if p.get('ProcessId') is not None}
    cur, seen = by_pid.get(int(own_pid)) if own_pid is not None else None, set()
    while cur is not None and int(cur['ProcessId']) not in seen:
        seen.add(int(cur['ProcessId']))
        if int(cur['ProcessId']) == tpid:
            raise Refused("PID %d is this process's own ancestor." % tpid)
        cur = by_pid.get(int(cur.get('ParentProcessId') or 0))
    parent = by_pid.get(int(target.get('ParentProcessId') or 0))
    win = con = None
    quoted = window.q(name)
    if parent and parent.get('Name') == 'powershell.exe' and parent.get('CommandLine'):
        text = window_script(parent['CommandLine'])
        if text and ('--remote-control ' + quoted) in text and ('WindowTitle = ' + quoted) in text:
            win = parent
            gp = by_pid.get(int(parent.get('ParentProcessId') or 0))
            if gp and gp.get('Name') == 'conhost.exe':
                kids = [p for p in procs if int(p.get('ParentProcessId') or 0) == int(gp['ProcessId'])
                        and int(p['ProcessId']) != int(parent['ProcessId'])]
                if not kids:
                    con = gp
    what = 'session PID %d: %s' % (tpid, target.get('CommandLine'))
    if win:
        wtext = 'window PID %s (powershell.exe, same name)' % win['ProcessId']
        if con:
            wtext += ' and its conhost.exe PID %s' % con['ProcessId']
    else:
        wtext = 'no window (parent PID %s %s does not carry the name)' % (
            target.get('ParentProcessId'), parent.get('Name') if parent else 'gone')
    return target, win, con, succ, [what, wtext]


def stop(settings, host, name, successor=None, pid=0, dry_run=False, procs=None):
    """Stop one session; the lines it says. Refused for a refusal; LaunchError when the session
    could not be stopped."""
    conductors = None
    try:
        conductors = state.read_conductors(settings)
    except LaunchError:
        conductors = None
    pm_id = (conductors or {}).get('pm_session_id') if isinstance(conductors, dict) else None
    if pm_id and pm_id.startswith('<'):
        pm_id = None                                # the example file's placeholder
    rows = host.processes() if procs is None else procs
    target, win, con, succ, (what, wtext) = plan(rows, name, successor, pid, host.own_pid(), pm_id)
    if dry_run:
        out = ['DryRun: would stop ' + what, 'DryRun: would stop ' + wtext]
        if successor:
            out.append("DryRun: successor '%s' running as PID %s" % (successor, lookup.pids(succ)))
        return out + ['DryRun: nothing stopped.']
    # Creation times first, all of them, so the window and its host are the ones seen in the list.
    chosen = [(p, host.ctime(int(p['ProcessId']))) for p in (target, win, con) if p]
    tpid, tct = int(target['ProcessId']), chosen[0][1]
    if tct is None or lookup.rc_name(host.command_line(tpid, tct)) != name:
        raise LaunchError("PID %d is no longer the session '%s'; nothing stopped." % (tpid, name))
    result = host.terminate(tpid, tct)
    if result not in ('killed', 'gone'):
        raise LaunchError('could not stop PID %d (%s); nothing else stopped.' % (tpid, result))
    for p, ct in chosen[1:]:
        if ct is not None:
            host.terminate(int(p['ProcessId']), ct)
    line = "stopped '%s' claude PID %d, window PID %s, conhost PID %s; successor '%s'" % (
        name, tpid, win['ProcessId'] if win else '-', con['ProcessId'] if con else '-', successor or '-')
    if pid:
        line += ' (picked by --pid)'
    try:
        state.log(settings, LOG, line)
    except (OSError, LaunchError) as e:              # the stop happened; say the log line is missing
        print('launch.py stop: could not write %s: %s' % (LOG, e), file=sys.stderr)
    return [line]
