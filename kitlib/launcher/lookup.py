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


def legacy_stems(key, entry):
    """The old-name stems a session of the sessions.json entry `key` may still run under: its stem
    with coordinator said as conductor, and for a repo's main coordinator (an agent entry keyed by
    its repo) `<repo>-conductor`; none that the entry's own stem starts with (running_stem covers
    those). A stem renamed by `handoff --stem` while the old session runs would otherwise not see
    it, and start --session or after-reboot would run a second coordinator in the repo.
    One-release guard for the conductor-to-coordinator rename: removed after the switch (W2b)."""
    stem = entry.get('stem') or ''
    out = []
    old = stem.replace('coordinator', 'conductor')
    if old != stem:
        out.append(old)
    if entry.get('agent') and entry.get('repo') and key == entry.get('repo'):
        out.append('%s-conductor' % str(entry['repo']).lower())
    return [s for s in dict.fromkeys(out) if not stem.startswith(s)]


def legacy_running(procs, key, entry):
    """The first of legacy_stems(key, entry) a launched session runs under, or None."""
    for s in legacy_stems(key, entry):
        if running_stem(procs, s):
            return s
    return None


def pids(rows):
    return ', '.join(str(p.get('ProcessId')) for p in rows)
