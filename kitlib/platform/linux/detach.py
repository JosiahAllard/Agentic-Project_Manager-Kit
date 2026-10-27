"""A run started again outside its caller on Linux (machine_lock.py run|background --detach, the
gate's detached landing): kitlib.lock.detach's Linux backend (docs/linux-support-plan.md
section 4, slice 7).

Windows asks WMI, so the run is a child of WMI's provider host. Here the caller starts a
trampoline (`python -I -c TRAMPOLINE`) in a session of its own (setsid), and the trampoline
starts the run and exits, so the run is reparented away from the caller: it is in neither the
caller's process tree, nor its process group, nor its session. Where the user's systemd can be
reached (cgroup v2, systemd-run found, a user bus: kitlib.platform.linux.contain bus_env), the
run starts as `systemd-run --user --scope` in a unit of its own, `<prefix>-detach-<tag>.scope`,
so it is in no cgroup of the caller's either (a terminal's or an agent host's scope, which its
end may kill as a whole); systemd-run execs the command, so the scope's process is the run. The
trampoline says the run's pid once the run is in its unit (or at once, with no unit), and says
why when it is not. Without systemd it is the setsid double fork alone (plan step 10).

The units here are named `-detach-`; a kit job's are `-job-` (contain), and nothing here stops
or cleans up a unit: the run's scope goes when its last process ends (--collect).

Nothing here prints. It imports on Windows (its tests run there): nothing POSIX-only at import.
"""
import json
import os
import subprocess
import sys

from kitlib.platform.linux import contain

CREATE_TIMEOUT_S = 60
READY_S = 30.0              # how long the trampoline waits for the run to be in its unit

# Reads {"argv", "unit", "wait_s"} on stdin. Starts argv, stdio closed. Prints "ok <pid>" once the
# run is in unit's scope (no unit: at once), or "x <why>". Then exits: the run is reparented.
TRAMPOLINE = r'''import json, os, subprocess, sys, time
d = json.load(sys.stdin)
def say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    os._exit(0)
try:
    p = subprocess.Popen(d["argv"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
except OSError as e:
    say("x could not start: %s" % e)
unit = d.get("unit")
if not unit:
    say("ok %d" % p.pid)
want = "/%s.scope" % unit
end = time.monotonic() + d["wait_s"]
while True:
    try:
        with open("/proc/%d/cgroup" % p.pid) as f:
            text = f.read()
    except OSError:
        text = ""
    if any(l.startswith("0::") and (l.rstrip() + "/").find(want + "/") >= 0
           for l in text.splitlines()):
        say("ok %d" % p.pid)
    rc = p.poll()
    if rc is not None:
        say("x systemd-run exited %d before the run was in %s.scope" % (rc, unit))
    if time.monotonic() >= end:
        p.kill()
        say("x the run was not in %s.scope within %d s; it was ended" % (unit, d["wait_s"]))
    time.sleep(0.02)
'''


def command(python, tool, head, rid, tail):
    """The detached run's argv: `python -B tool`, `head` (the caller's arguments before `--`),
    `--detached-id rid` and `tail` (`--` and the command). No carrier: Linux opens no console."""
    return tool_command(python, tool, list(head) + ['--detached-id', rid] + list(tail))


def tool_command(python, tool, args):
    """A detached tool's argv: `python -B tool args`."""
    return [os.path.abspath(python), '-B', os.path.abspath(tool)] + list(args)


def missing(argv):
    """Why `command`'s argv cannot start (its python or its tool is not a file), or None."""
    for what, path in (('python', argv[0]), ('the tool', argv[2] if len(argv) > 2 else '')):
        if not os.path.isfile(path):
            return '%s is not a file: %s' % (what, path)
    return None


def scope(env):
    """(the systemd-run argv head, its unit, its environment) for a run in a scope of its own, or
    None where the user's systemd cannot be reached (no cgroup v2, no systemd-run, no user bus)."""
    run = contain._tool(contain.SYSTEMD_RUN)
    benv = contain.bus_env(env)
    if run is None or benv is None or not contain.cgroup2():
        return None
    unit = '%s-detach-%s' % (contain.prefix(), contain._tag())
    head = ([run, '--user', '--scope', '--quiet', '--collect'] + contain.no_expand(run)
            + ['--unit', unit, '--'])
    return head, unit, benv


def create(argv, cwd, env, popen=None, timeout=CREATE_TIMEOUT_S, ready_s=READY_S):
    """(pid, None) of the detached run of `argv` in `cwd` with `env` (a dict, marked as a detached
    start by the caller: kitlib.lock.detach marked), else (None, why). popen: the tests' stand-in
    for subprocess.Popen (looked up at call time)."""
    sc = scope(env)
    if sc is None:
        data, penv = {'argv': list(argv), 'unit': None, 'wait_s': ready_s}, dict(env)
    else:
        head, unit, penv = sc
        data = {'argv': head + list(argv), 'unit': unit, 'wait_s': ready_s}
    try:
        t = (popen or subprocess.Popen)(
            [sys.executable, '-I', '-c', TRAMPOLINE], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=penv, text=True,
            start_new_session=True, close_fds=True)
    except (OSError, ValueError) as e:
        return None, 'failed: %s' % e
    try:
        out, err = t.communicate(json.dumps(data), timeout=timeout)
    except subprocess.TimeoutExpired:
        t.kill()
        t.communicate()
        return None, 'failed: the trampoline did not answer within %d s' % timeout
    except OSError as e:
        return None, 'failed: %s' % e
    parts = (out or '').split(None, 1)
    if t.returncode == 0 and len(parts) == 2 and parts[0] == 'ok' and parts[1].strip().isdigit():
        return int(parts[1]), None
    if parts[:1] == ['x']:
        return None, parts[1].strip() if len(parts) > 1 else 'failed'
    return None, 'got %r (exit %s): %s' % ((out or '').strip(), t.returncode,
                                            (err or '').strip()[-300:])


def wrapper_pid(pid, **_):
    """The run's wrapper: the pid `create` gave is the run itself (no carrier here)."""
    return pid
