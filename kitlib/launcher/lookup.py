"""Finding a running Claude session by its Remote Control name (docs/launcher.md "Lookup").

A launched session is a claude.exe with a command line, not the Store app's (under WindowsApps).
Its name is the --remote-control argument (else -n), matched exactly and case-sensitively as a whole token:
"x-a1" never matches "x-a1-r2". Process rows are dicts with Name, CommandLine, ExecutablePath,
ProcessId and ParentProcessId (kitlib.pmstart.list_processes, or a test's list).
"""
import re

from kitlib import pmstart

_N = re.compile(r"""(?:^|\s)-n\s+(?:"([^"]*)"|'([^']*)'|(\S+))""")


def rc_name(cmd):
    """The session's name: its --remote-control value, else, for a session launched without it
    (launch.py start --no-remote-control), its -n value; None for neither."""
    n = pmstart.rc_name(cmd)
    if n:
        return n
    m = _N.search(cmd or '')
    return next((g for g in m.groups() if g), None) if m else None
list_processes = pmstart.list_processes


def claude_sessions(procs):
    return [p for p in procs
            if p.get('Name') == 'claude.exe' and p.get('CommandLine')
            and pmstart.STORE_DIR not in (p.get('ExecutablePath') or '')]


def find_exact(sessions, name):
    return [p for p in sessions if rc_name(p.get('CommandLine')) == name]


def running_stem(procs, stem):
    """True when a launched session's name starts with `stem` (any account, any recycle)."""
    return any((rc_name(p.get('CommandLine')) or '').startswith(stem) for p in claude_sessions(procs))


def pids(rows):
    return ', '.join(str(p.get('ProcessId')) for p in rows)
