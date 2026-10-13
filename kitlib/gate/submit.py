"""A submitted landing (docs/gate.md "Submitting a landing"): `land.py --submit` and `gate.py land
--submit` start the same landing as a detached, windowless process and return at once.

  submit    check the arguments (USAGE), write each branch's `running` land record, start the
            tool again, detached, with `--submitted <payload>`, and give the one line
            `SUBMITTED <branch>[+<branch>...] tip <sha>[,<sha>] wait: python <kit>/tools/gate.py
            wait-result <branch> --tip <sha> --kind land`
  restore   the payload, read back by the detached tool: the result.Run of each branch (the same
            run_id and started_at), the output file and the land records those runs replaced,
            for kitlib.gate.land.Lander(submitted=...)
  redirect_output  the detached tool's stdout and stderr, appended to the output file

The detached tool is started the way `machine_lock.py run --detach` starts its wrapper:
Win32_Process.Create through PowerShell's CIM cmdlets with ShowWindow 0, so it is outside this
process tree (it outlives the shell and the agent that submitted it) and has no window. Its
environment is this one, less the machine lock's MACHINE_LOCK_RUN (the landing books its own slot,
never one nested in the submitter's run) and with MACHINE_LOCK_ORIGIN, this process's ancestry, so
the machine lock gives the detached slot the submitter's role. A tool run from an export of pushed
main (kitlib.gate.pin) keeps KIT_PINNED, so the detached tool runs from the same export, and
pin.lease_child leases the export for it.

Every outcome of the detached landing ends in a final land record per branch, refusals included:
the records were written `running` before the start, and Lander.run finishes them whatever the
landing does (an exception: `error`). A start that fails finishes them `refused` here. Files, in
the result folder (<git common dir>/kit-gate-result): <stem>.land.json per branch (the record),
and for the batch's first branch <stem>.land.submit (the payload) and <stem>.land.log (the
detached tool's output, the records' `log` until a merged gate names its own). Nothing here
prints or exits.
"""
import json
import os
import subprocess
import sys

from kitlib import gate as G
from kitlib import pluginsource
from kitlib.gate import pin
from kitlib.gate import result
from kitlib.lock import run as lockrun
from kitlib.proc import process, spawn

FLAG = '--submitted'
SCHEMA = 1
START_TIMEOUT_S = 60
# The detach of tools/machine_lock.py, the same script: the command line, folder and environment
# arrive as JSON on stdin; it prints "<ReturnValue> <ProcessId>".
DETACH_PS = ("$d = [Console]::In.ReadToEnd() | ConvertFrom-Json; "
             "$si = New-CimInstance -ClassName Win32_ProcessStartup -Property "
             "@{ShowWindow=[UInt16]0; EnvironmentVariables=[string[]]$d.env} -ClientOnly; "
             "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
             "@{CommandLine=$d.cmd; CurrentDirectory=$d.cwd; ProcessStartupInformation=$si}; "
             "\"$($r.ReturnValue) $($r.ProcessId)\"")


class SubmitError(Exception):
    """A payload that cannot be read back."""


def files(root, branches):
    """(payload, output): the submission's files, named by the batch's first branch."""
    folder = result.result_dir(root)
    stem = result.file_stem(branches[0])
    return (os.path.join(folder, '%s.land.submit' % stem),
            os.path.join(folder, '%s.land.log' % stem))


def detach(argv, cwd, env, run=None, timeout=START_TIMEOUT_S):
    """Start `argv` detached and windowless: (pid, None), or (None, why)."""
    run = run or spawn.run
    data = {'cmd': subprocess.list2cmdline(argv), 'cwd': cwd,
            'env': ['%s=%s' % kv for kv in env.items()]}
    try:
        r = run(['powershell', '-NoProfile', '-NonInteractive', '-Command', DETACH_PS],
                input=json.dumps(data), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, 'the detached start failed: %s' % e
    parts = (r.stdout or '').split()
    if r.returncode or len(parts) != 2 or parts[0] != '0' or not parts[1].isdigit():
        return None, 'the detached start got %r (exit %s): %s' % (
            (r.stdout or '').strip(), r.returncode, ' '.join((r.stderr or '').split())[-300:])
    return int(parts[1]), None


def child_env(environ=None):
    """The detached tool's environment: this one, without MACHINE_LOCK_RUN, with this process's
    ancestry in MACHINE_LOCK_ORIGIN and its output in UTF-8."""
    env = dict(os.environ if environ is None else environ)
    env.pop(lockrun.NEST_ENV, None)
    try:
        env[lockrun.ORIGIN_ENV] = ';'.join('%d:%d' % pc for pc in sorted(process.own_ancestors()))
    except Exception:       # noqa: BLE001 - the slot then has the detached process's own role
        env.pop(lockrun.ORIGIN_ENV, None)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    return env


def wait_line(branches, tips, kit=None):
    kit = kit or pluginsource.live_root()
    script = os.path.join(kit, 'tools', 'gate.py')
    if ' ' in script:
        script = '"%s"' % script
    return 'SUBMITTED %s tip %s wait: python %s wait-result %s --tip %s --kind land' % (
        '+'.join(branches), ','.join(tips), script, branches[0], tips[0])


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(data, sort_keys=True, indent=1) + '\n')
    os.replace(tmp, path)


def _refused(runs, why):
    out = G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
    for rr in runs:
        rr.finish(out)
    return out


def submit(root, tool_file, child_args, branches, tips, expect_main=None, environ=None,
           detach_fn=None, kit=None, now=None):
    """Start the landing of `branches` at `tips` detached: `tool_file` (tools/land.py or
    tools/gate.py) again with `child_args` (the landing's own arguments, -C absolute) and
    `--submitted <payload>`. An Outcome: OK with the SUBMITTED line, USAGE for arguments the
    landing would refuse as usage (nothing written), REFUSED when the records or the payload
    cannot be written or the start fails (records written are finished `refused`)."""
    from kitlib.gate import land        # land imports nothing from here; late, as runner does
    branches, tips = list(branches), list(tips)
    why = land.Lander(root, branches, expect_main, tips).usage()
    if why:
        return G.Outcome(G.USAGE, ['USAGE: %s' % why])
    try:
        payload, output = files(root, branches)
    except result.ResultError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % e])
    replaced = []
    for b in branches:
        rec, _ = result.read(root, b, 'land')
        if isinstance(rec, dict):
            replaced.append(rec)
    runs = []
    for b, t in zip(branches, tips):
        rr = result.Run(root, 'land', b, t, base=expect_main)
        path, line = rr.start(log=output)
        if path is None:
            return _refused(runs, 'the submission: %s' % line)
        runs.append(rr)
    try:
        _write_json(payload, {'schema': SCHEMA, 'output': output, 'replaced': replaced,
                              'runs': [{'branch': rr.branch, 'tip': rr.tip, 'base': rr.base,
                                        'run_id': rr.run_id, 'started_at': rr.started_at}
                                       for rr in runs]})
        with open(output, 'a', encoding='utf-8', newline='\n') as f:
            f.write('=== landing %s at %s submitted %s\n' % (
                '+'.join(branches), ','.join(t[:10] for t in tips), runs[0].started_at))
    except OSError as e:
        return _refused(runs, 'the submission could not write %s: %s' % (payload, e))
    argv = [sys.executable, '-B', os.path.abspath(tool_file)] + list(child_args) + [FLAG, payload]
    env = child_env(environ)
    pid, why = (detach_fn or detach)(argv, root, env)
    if pid is None:
        return _refused(runs, 'the landing could not be started: %s' % why)
    try:
        pin.lease_child(tool_file, pid, env)
    except OSError as e:        # the landing runs; a later pin may prune its export under it
        _note(output, 'WARNING: the pinned export could not be leased for pid %d: %s' % (pid, e))
    return G.Outcome(G.OK, [wait_line(branches, tips, kit)], facts={'pid': pid, 'payload': payload,
                                                                    'output': output})


def _note(path, line):
    try:
        with open(path, 'a', encoding='utf-8', newline='\n') as f:
            f.write(line + '\n')
    except OSError:
        pass


def restore(root, payload):
    """The `submitted` dict for Lander from the payload file: runs (result.Run with the
    submission's run_id and started_at), output, replaced. SubmitError when it cannot be read."""
    try:
        with open(payload, encoding='utf-8') as f:
            data = json.loads(f.read())
        runs = []
        for item in data['runs']:
            rr = result.Run(root, 'land', item['branch'], item['tip'], base=item.get('base'))
            rr.run_id, rr.started_at = item['run_id'], item['started_at']
            runs.append(rr)
        return {'runs': runs, 'output': data.get('output'),
                'replaced': [r for r in data.get('replaced') or [] if isinstance(r, dict)]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SubmitError('cannot read the submission %s: %s' % (payload, e))


def redirect_output(path):
    """This process's stdout and stderr, and the standard handles the programs it starts inherit,
    appended to `path` (line-buffered UTF-8). The stream; OSError when `path` cannot be opened."""
    stream = open(path, 'a', encoding='utf-8', buffering=1, newline='\n')
    for fd in (1, 2):
        try:
            os.dup2(stream.fileno(), fd)
        except OSError:
            pass
    sys.stdout = sys.stderr = stream
    return stream
