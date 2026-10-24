"""A run started again outside its caller (machine_lock.py run|background --detach), with no
console window.

Why WMI: Win32_Process.Create makes the new process a child of WMI's provider host, not of the
caller, so it is in neither the caller's process tree nor its job object (a Claude Code shell's
job ends everything in it when the shell ends) and outlives the shell that started it.

Why the carrier: WMI gives a console program it starts a console of its own, and whether that
console shows a window rests on Win32_ProcessStartup.ShowWindow and on which terminal hosts it.
So the program WMI starts is `pythonw.exe` (no console) running the carrier tools/no_window.py
(kitlib.proc.spawn.carry), which starts the console python.exe running machine_lock.py with
CREATE_NO_WINDOW: a console with no window, which the run's commands share. The carrier waits
for that python and ends with its exit code. Nothing here relies on ShowWindow.

Why the marker: WMI's provider host exits when idle, and its pid may then be gone or reused
while the run goes on, so the process tree alone stops showing that WMI started it. `create`
marks the process it starts (store.DETACHED_ENV, inherited by everything under it), and
store.caller reads a marked process as an agent's. When the environment it is given is marked
already, the launcher is itself inside such a start: the ancestry it recorded in
MACHINE_LOCK_ORIGIN stops at WMI and tells nothing, so `create` drops it, and the new run's role
from its launcher (store.tree_role, over no ancestry) is agent. Both only ever lower a role.

`command` builds the argv, `create` asks WMI, `wrapper_pid` finds the python the carrier
started (the run's wrapper, whose pid the run record holds). Nothing here prints. The carrier's
line when it cannot start its program is kitlib.proc.console.note_carrier_failure, the one
writer of the kit's console incident log.
"""
import json
import os
import subprocess
import time

from kitlib.lock.run import ORIGIN_ENV
from kitlib.lock.store import DETACHED_ENV
from kitlib.proc import process, spawn


# Reads {"cmd", "cwd", "env"} as JSON on stdin; prints "<ReturnValue> <ProcessId>".
CREATE_PS = ("$d = [Console]::In.ReadToEnd() | ConvertFrom-Json; "
             "$si = New-CimInstance -ClassName Win32_ProcessStartup -Property "
             "@{EnvironmentVariables=[string[]]$d.env} -ClientOnly; "
             "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
             "@{CommandLine=$d.cmd; CurrentDirectory=$d.cwd; ProcessStartupInformation=$si}; "
             "\"$($r.ReturnValue) $($r.ProcessId)\"")
CREATE_TIMEOUT_S = 60
WRAPPER_WAIT_S = 10.0       # how long wrapper_pid looks for the carrier's python
WRAPPER_POLL_S = 0.05


def pythons(python):
    """(pythonw.exe, python.exe) in the folder of `python` (either of the two). Neither is checked
    to exist here. Not the hooks' windowless() (kitlib/host/hookmain.py) or machine setup's
    pythonw(): both fall back to a console python where this must refuse (`missing`), and importing
    either puts the guard, the gate and the backlog in the lock tool's import reach, which a release
    may then not change under a live run (kitlib.lock.release.LOCK_PATHS, tests/lock/test_release.py)."""
    python = os.path.abspath(python)
    return spawn.pythonw_beside(python), os.path.join(os.path.dirname(python), 'python.exe')


def command(python, tool, head, rid, tail):
    """The detached run's argv: the pythonw beside `python`, the carrier beside `tool`, the
    python.exe beside `python`, then `-B tool` with `head` (the caller's arguments before `--`),
    `--detached-id rid` and `tail` (`--` and the command)."""
    pythonw, console = pythons(python)
    carrier = os.path.join(os.path.dirname(os.path.abspath(tool)), spawn.CARRIER)
    return ([pythonw, carrier, console, '-B', os.path.abspath(tool)] + list(head)
            + ['--detached-id', rid] + list(tail))


def missing(argv):
    """Why `command`'s argv cannot start (its pythonw, carrier or python.exe is not a file), or
    None."""
    for what, path in zip(('pythonw.exe', 'the carrier', 'python.exe'), argv[:3]):
        if not os.path.isfile(path):
            return '%s is not a file: %s' % (what, path)
    return None


def marked(env):
    """A copy of `env` (a dict) for a detached start: DETACHED_ENV set, and ORIGIN_ENV dropped
    when `env` was marked already (the launcher is inside a detached start itself)."""
    out = dict(env)
    if out.get(DETACHED_ENV):
        out.pop(ORIGIN_ENV, None)
    out[DETACHED_ENV] = '1'
    return out


def create(argv, cwd, env, run=None, timeout=CREATE_TIMEOUT_S):
    """(pid, None) of the process Win32_Process.Create started for `argv` in `cwd` with `env`
    (a dict, marked as a detached start: `marked`), else (None, why). run: the tests' stand-in for
    spawn.run (looked up at call time)."""
    env = marked(env)
    data = {'cmd': subprocess.list2cmdline(argv), 'cwd': cwd,
            'env': ['%s=%s' % kv for kv in env.items()]}
    try:
        r = (run or spawn.run)(['powershell', '-NoProfile', '-NonInteractive', '-Command', CREATE_PS],
                input=json.dumps(data), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, 'failed: %s' % e
    parts = (r.stdout or '').split()
    if r.returncode or len(parts) != 2 or parts[0] != '0' or not parts[1].isdigit():
        return None, 'got %r (exit %s): %s' % ((r.stdout or '').strip(), r.returncode,
                                                (r.stderr or '').strip()[-300:])
    return int(parts[1]), None


def wrapper_pid(carrier, wait_s=WRAPPER_WAIT_S, poll_s=WRAPPER_POLL_S, snapshot=process.snapshot,
                ctime=process.proc_ctime, sleep=time.sleep, clock=time.monotonic):
    """The pid of the python.exe the carrier `carrier` started, or None when none was seen within
    `wait_s` seconds (the carrier and its python have ended already, or a snapshot failed). A
    process with the carrier's pid as parent but created before the carrier holds a reused pid and
    is not it."""
    start = ctime(carrier)
    if start is None:
        return None
    limit = clock() + wait_s
    while True:
        try:
            for pid, ppid, name in snapshot():
                if ppid == carrier and pid != carrier and name.lower() == 'python.exe':
                    c = ctime(pid)
                    if c is not None and c >= start:
                        return pid
        except OSError:
            return None
        if clock() >= limit or ctime(carrier) != start:
            return None
        sleep(poll_s)
