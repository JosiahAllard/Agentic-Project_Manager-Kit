"""Finding a running Claude session by its Remote Control name (docs/launcher.md "Lookup").

A launched session is a claude.exe with a command line, not the Store app's (under WindowsApps).
Its name is the --remote-control argument, matched exactly and case-sensitively as a whole token:
"x-a1" never matches "x-a1-r2". Process rows are dicts with Name, CommandLine, ExecutablePath,
ProcessId and ParentProcessId (kitlib.pmstart.list_processes, or a test's list).
"""
from kitlib import pmstart

rc_name = pmstart.rc_name
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
