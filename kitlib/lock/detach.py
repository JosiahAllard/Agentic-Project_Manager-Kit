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

`command` builds the argv, `create` asks WMI, `wrapper_pid` finds the python the carrier
started (the run's wrapper, whose pid the run record holds). Nothing here prints.

`note_carrier_failure` is the carrier's one line when it cannot start its program: under pythonw
it has no stderr, so without the line a detached run (or a scheduled task) that never started
would leave nothing behind.
"""
import json
import os
import subprocess
import time

from kitlib.proc import process, spawn

INCIDENT_LOG = 'kit-console-incidents.log'  # in the kit's work temp folder (kitlib.config.user.work_temp)
INCIDENT_LOG_ENV = 'KIT_CONSOLE_INCIDENT_LOG'   # a path that replaces it: the tests' own file
KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    folder = os.path.dirname(os.path.abspath(python))
    return os.path.join(folder, 'pythonw.exe'), os.path.join(folder, 'python.exe')


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


def create(argv, cwd, env, run=None, timeout=CREATE_TIMEOUT_S):
    """(pid, None) of the process Win32_Process.Create started for `argv` in `cwd` with `env`
    (a dict), else (None, why). run: the tests' stand-in for spawn.run (looked up at call time)."""
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


def incident_log(environ=None):
    """The file `note_carrier_failure` appends to: INCIDENT_LOG_ENV when set, else INCIDENT_LOG in
    the kit's work temp folder. None when that folder cannot be worked out (a fenced test process:
    HomeFenced, a BaseException; a bad machine config; no LOCALAPPDATA): the line is skipped, as
    kitlib/proc/console.py skips its own, rather than kept in a second folder under the same name."""
    env = os.environ if environ is None else environ
    if env.get(INCIDENT_LOG_ENV):
        return env[INCIDENT_LOG_ENV]
    try:
        from kitlib.config import user
        try:
            return os.path.join(user.work_temp(KIT), INCIDENT_LOG)
        except user.HomeFenced:
            return None
    except Exception:
        return None


def note_carrier_failure(program, why, path=None, now=None, pid=None):
    """Append one JSON line to `path` (default `incident_log()`), in the shape of
    kitlib/proc/console.py's lines in the same log: {"time" (UTC), "how": "carrier-fail", "pid"
    (the carrier's), "exe" (the file name of `program`, never its arguments), "why"}. Returns the
    path written, or None when there is no log or it could not be written; never raises, a
    HomeFenced included: it runs on the carrier's way out."""
    try:
        from kitlib.config import user
        try:
            path = path or incident_log()
            if not path:
                return None
            name = str(program).strip().strip('"').replace('/', '\\').split('\\')[-1]
            line = json.dumps({
                'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', now or time.gmtime()),
                'how': 'carrier-fail', 'pid': os.getpid() if pid is None else pid,
                'exe': name or '?', 'why': ' '.join(str(why).split())}) + '\n'
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line)
            return path
        except user.HomeFenced:
            return None
    except Exception:
        return None
