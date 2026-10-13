"""A submitted landing (docs/gate.md "Submitting a landing"): `land.py --submit` and `gate.py land
--submit` start the same landing as a detached, windowless process and return at once.

  submit    check the arguments (USAGE); refuse, writing nothing, while a branch's landing runs
            in a live process or once a branch landed at its tip (blocked); write each branch's
            `running` land record (one run_id for the batch, no pid yet), start the tool again,
            detached, with `--submitted <payload>`, wait for it to record itself (claim), and
            give the one line `SUBMITTED <branch>[+<branch>...] tip <sha>[,<sha>] wait: python
            <kit>/tools/gate.py wait-result <branch> --tip <sha> --kind land -C <main checkout>`
            (wait_line)
  restore   the payload, read back by the detached tool: the result.Run of each branch (the same
            run_id and started_at), the output file and the land records those runs replaced,
            for kitlib.gate.land.Lander(submitted=...); it writes its own pid into the records
  abandon   the detached tool that cannot read its payload finishes the submission's records
            `refused`, found by the run_id in the payload's name
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
landing does (an exception: `error`). A start that fails finishes them `refused` here; a payload
the detached tool cannot read, `refused` there (abandon); a detached tool that dies before it
records itself, `error` here (claim); one that dies later, `error` by the first result.wait that
sees it gone. Files, in the result folder (<git common dir>/kit-gate-result): <stem>.land.json
per branch (the record), and, named by the batch's first branch and the run_id,
<stem>.<run_id>.land.submit (the payload) and <stem>.<run_id>.land.log (the detached tool's
output, the records' `log` until a merged gate names its own). Nothing here prints or exits.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time

from kitlib import gate as G
from kitlib import pluginsource
from kitlib.gate import pin
from kitlib.gate import result
from kitlib.git import repo
from kitlib.lock import run as lockrun
from kitlib.proc import process, spawn

FLAG = '--submitted'
SCHEMA = 1
START_TIMEOUT_S = 60
PAYLOAD_EXT = '.land.submit'
OUTPUT_EXT = '.land.log'
RUN_ID = re.compile(r'[0-9a-f]{16}')
# How long submit() waits for the detached landing to record itself (its pid in the records).
CLAIM_WAIT_S = 15.0
CLAIM_POLL_S = 0.25
LANDED = ('pass', 'landed-ungated')
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


def files(root, branches, run_id):
    """(payload, output): the submission's files, named by the batch's first branch and the
    submission's run_id (every record of the batch carries it), so no submission overwrites
    another's."""
    folder = result.result_dir(root)
    stem = '%s.%s' % (result.file_stem(branches[0]), run_id)
    return (os.path.join(folder, '%s%s' % (stem, PAYLOAD_EXT)),
            os.path.join(folder, '%s%s' % (stem, OUTPUT_EXT)))


def run_id_of(payload):
    """The run_id a payload file is named by (files()), or None."""
    name = os.path.basename(payload or '')
    if not name.endswith(PAYLOAD_EXT):
        return None
    rid = name[:-len(PAYLOAD_EXT)].rsplit('.', 1)[-1]
    return rid if RUN_ID.fullmatch(rid) else None


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


def _arg(path):
    return '"%s"' % path if ' ' in path else path


def wait_line(branches, tips, root, kit=None):
    """The SUBMITTED line. Its wait command names the repository with -C: the main checkout of
    `root` (where the landing runs, and a checkout no cleanup removes), or `root` itself when git
    cannot place it. The record is in the git common dir, so any checkout of the repository reads
    the same one."""
    kit = kit or pluginsource.live_root()
    main = repo.main_checkout_toplevel(root) or root
    return 'SUBMITTED %s tip %s wait: python %s wait-result %s --tip %s --kind land -C %s' % (
        '+'.join(branches), ','.join(tips), _arg(os.path.join(kit, 'tools', 'gate.py')),
        branches[0], tips[0], _arg(main))


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


def blocked(root, branches, tips):
    """Why the land records of `branches` stop a submission, or None: a branch's landing is
    running and its process lives (result.running_state 'live'; a dead or unknown one does not
    count), or a branch already landed at its tip (verdict pass or landed-ungated). Read only."""
    for b, t in zip(branches, tips):
        rec, _ = result.read(root, b, 'land')
        if not isinstance(rec, dict):
            continue
        if result.running_state(rec) == 'live':
            return 'a landing of %s is already running (run %s, pid %s, tip %s); wait for it ' \
                   'with gate.py wait-result %s --tip %s --kind land' % (
                       b, rec.get('run_id'), rec.get('pid') or 'not recorded yet',
                       str(rec.get('tip'))[:10], b, rec.get('tip'))
        got = rec.get('tip')
        if (rec.get('verdict') in LANDED and isinstance(got, str)
                and got.lower() == t.lower()):
            return '%s at %s already landed (run %s, %s)' % (
                b, t[:10], rec.get('run_id'), rec.get('summary'))
    return None


def submit(root, tool_file, child_args, branches, tips, expect_main=None, environ=None,
           detach_fn=None, kit=None, now=None, claim_wait_s=CLAIM_WAIT_S,
           clock=time.monotonic, sleep=time.sleep):
    """Start the landing of `branches` at `tips` detached: `tool_file` (tools/land.py or
    tools/gate.py) again with `child_args` (the landing's own arguments, -C absolute) and
    `--submitted <payload>`. An Outcome: OK with the SUBMITTED line, USAGE for arguments the
    landing would refuse as usage (nothing written), REFUSED with nothing written when a
    branch's landing is running or it landed at its tip already (blocked), REFUSED when the
    records or the payload cannot be written or the start fails (records written are finished
    `refused`), or when the detached landing died before it recorded itself (claim: its
    records are finished `error`)."""
    from kitlib.gate import land        # land imports nothing from here; late, as runner does
    branches, tips = list(branches), list(tips)
    why = land.Lander(root, branches, expect_main, tips).usage()
    if why:
        return G.Outcome(G.USAGE, ['USAGE: %s' % why])
    run_id = secrets.token_hex(8)
    try:
        payload, output = files(root, branches, run_id)
    except result.ResultError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % e])
    why = blocked(root, branches, tips)
    if why:
        return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
    replaced = []
    for b in branches:
        rec, _ = result.read(root, b, 'land')
        if isinstance(rec, dict):
            replaced.append(rec)
    runs = []
    for b, t in zip(branches, tips):
        rr = result.Run(root, 'land', b, t, base=expect_main)
        # One run_id for the batch: the payload is named by it, so a landing that cannot read
        # its payload still finds its records (abandon). No pid until the landing records itself.
        rr.run_id, rr.pid, rr.ctime = run_id, None, None
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
    died = claim(root, runs, pid, output, claim_wait_s, clock=clock, sleep=sleep)
    if died:
        return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed; its output: %s' % (
            died, output)], pid=pid, payload=payload, output=output)
    return G.Outcome(G.OK, [wait_line(branches, tips, root, kit)], pid=pid, payload=payload,
                     output=output)


def claim(root, runs, pid, output, wait_s=CLAIM_WAIT_S, poll_s=CLAIM_POLL_S, clock=time.monotonic,
          sleep=time.sleep, alive=None):
    """Wait up to `wait_s` for the detached landing `pid` to record itself in `runs`' records
    (restore writes its pid into them). Once every record is claimed, or finished: None. When
    the process died first: its unclaimed records are finished `error` and the reason is
    returned. When the wait ends with the process alive: its pid (and creation time) is written
    into the records still unclaimed, so a death after this is seen by result.wait."""
    alive = alive or process.alive
    try:
        ctime = process.proc_ctime(pid)
    except Exception:       # noqa: BLE001 - alive() then checks the pid alone
        ctime = None
    end = clock() + max(0.0, wait_s)
    while True:
        open_ = []
        for rr in runs:
            rec, _ = result.read(root, rr.branch, 'land')
            if (isinstance(rec, dict) and rec.get('verdict') == result.RUNNING
                    and rec.get('run_id') == rr.run_id and rec.get('pid') is None):
                open_.append((rr, rec))
        if not open_:
            return None
        try:
            up = alive(pid, ctime)
        except Exception:   # noqa: BLE001 - not surely gone
            up = True
        if not up:
            why = 'the landing process died before it started: pid %d ended without ' \
                  'recording itself' % pid
            for _rr, rec in open_:
                result.finish_dead(root, rec, why='%s; its output: %s' % (why, output),
                                   unclaimed=True)
            _note(output, 'ERROR: %s' % why)
            return why
        if clock() >= end:
            for rr, _rec in open_:
                rr.pid, rr.ctime = pid, ctime
                rr.start(log=output)
            return None
        sleep(max(0.0, min(poll_s, end - clock())))


def _note(path, line):
    try:
        with open(path, 'a', encoding='utf-8', newline='\n') as f:
            f.write(line + '\n')
    except OSError:
        pass


def restore(root, payload):
    """The `submitted` dict for Lander from the payload file: runs (result.Run with the
    submission's run_id and started_at, and this process's pid), output, replaced. Each record
    still running for its run is claimed: written again with this process's pid, so a death from
    here on is seen (result.wait). SubmitError when the payload cannot be read."""
    try:
        with open(payload, encoding='utf-8') as f:
            data = json.loads(f.read())
        runs = []
        for item in data['runs']:
            rr = result.Run(root, 'land', item['branch'], item['tip'], base=item.get('base'))
            rr.run_id, rr.started_at = item['run_id'], item['started_at']
            runs.append(rr)
        output = data.get('output')
        replaced = [r for r in data.get('replaced') or [] if isinstance(r, dict)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise SubmitError('cannot read the submission %s: %s' % (payload, e))
    for rr in runs:
        rec, _ = result.read(root, rr.branch, 'land')
        if (isinstance(rec, dict) and rec.get('verdict') == result.RUNNING
                and rec.get('run_id') == rr.run_id):
            rr.start(log=output)
    return {'runs': runs, 'output': output, 'replaced': replaced}


def abandon(root, payload, why):
    """The detached landing could not read its payload: finish the running land records of the
    submission (every branch's record with the run_id the payload is named by, run_id_of) as
    `refused`, note it in the submission's output file, and return the REFUSED Outcome."""
    out = G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
    lines = []
    rid = run_id_of(payload)
    if rid:
        for rec in result.running_records(root, 'land', rid):
            rr = result.Run(root, 'land', rec.get('branch'), rec.get('tip'), base=rec.get('base'))
            rr.run_id, rr.started_at = rid, rec.get('started_at')
            lines.append(rr.finish(out, log=rec.get('log'))[1])
        _note(payload[:-len(PAYLOAD_EXT)] + OUTPUT_EXT, out.lines[-1])
    return G.Outcome(G.REFUSED, lines + out.lines)


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
