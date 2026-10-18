"""A submitted landing (docs/gate.md "Submitting a landing"): `land.py --submit` and `gate.py land
--submit` start the same landing as a detached, windowless process and return at once.

  submit    check the arguments (USAGE); refuse, writing nothing, while a branch's landing runs
            in a live process or once a branch landed at its tip (blocked); write each branch's
            `running` land record (one run_id for the batch, no pid yet), start the tool again,
            detached, with `--submitted <payload>`, wait for it to record itself (claim), and
            give a `REVIEW <branch> <tip>: ...` line for each branch with a verdict queued at
            its tip (review_lines), then the last line `SUBMITTED <branch>[+<branch>...] tip
            <sha>[,<sha>] wait: python <kit>/tools/gate.py wait-result <branch> --tip <sha>
            --kind land -C <main checkout>` (wait_line; a batch's last branch and tip), and
            prune old submissions' files (prune)
  prune     delete the payload and output files of submissions older than KEEP_S whose run no
            land record has running
  restore   the payload, read back by the detached tool: the result.Run of each branch (the same
            run_id and started_at), the output file and the land records those runs replaced,
            for kitlib.gate.land.Lander(submitted=...); it writes its own pid into the records
  abandon   the detached tool that cannot read its payload finishes the submission's records
            `refused`, found by the run_id in the payload's name
  redirect_output  the detached tool's stdout and stderr, appended to the output file

The detached tool is started the way `machine_lock.py run --detach` starts its wrapper
(kitlib.lock.detach, "Why WMI" and "Why the carrier"): `create` asks Win32_Process.Create, through
PowerShell's CIM cmdlets, to start `command`'s argv: the pythonw.exe beside this interpreter
running the carrier tools/no_window.py beside the tool, which starts the python.exe beside it
running the tool with CREATE_NO_WINDOW and waits for it. WMI's process is outside this process
tree and its job (it outlives the shell and the agent that submitted it); pythonw has no console,
so the tool gets a console with no window, and nothing rests on ShowWindow. The pid `detach` gives
is the carrier's: it ends with the tool's exit code, and it runs from the same folder (a pinned
export). The tool is the python.exe the carrier started (kitlib.lock.detach.wrapper_pid finds it),
which outlives a carrier that is killed: the export is leased to both pids, and the claim watches
and records the tool's pid (the carrier's when the tool was not found). A missing pythonw.exe,
carrier or python.exe starts nothing. Its
environment is this one, less the machine lock's MACHINE_LOCK_RUN (the landing books its own slot,
never one nested in the submitter's run) and with MACHINE_LOCK_ORIGIN, this process's ancestry, so
the machine lock gives the detached slot the submitter's role. A tool run from an export of pushed
main (kitlib.gate.pin) keeps KIT_PINNED, so the detached tool runs from the same export, and
pin.lease_child leases the export for the carrier and for the tool, one lease each.

Every outcome of the detached landing ends in a final land record per branch, refusals included:
the records were written `running` before the start, and Lander.run finishes them whatever the
landing does (an exception: `error`). A start that fails finishes them `refused` here; a payload
the detached tool cannot read, `refused` there (abandon); a detached tool that dies before it
records itself, `error` here (claim); one that dies later, `error` by the first result.wait that
sees it gone. Files, in the result folder (<git common dir>/kit-gate-result): <stem>.land.json
per branch (the record), and, named by the batch's first branch and the run_id,
<stem>.<run_id>.land.submit (the payload) and <stem>.<run_id>.land.log (the detached tool's
output, the records' `log` until a merged gate names its own); each submission deletes the old
ones (prune). Nothing here prints or exits.
"""
import json
import os
import re
import secrets
import sys
import time

from kitlib import gate as G
from kitlib import pluginsource
from kitlib.gate import pin
from kitlib.gate import result, verdicts
from kitlib.git import repo
from kitlib.lock import detach as lockdetach
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
LANDED = ('pass', 'landed-ungated')    # landed-ungated: an old record's (exit 9 is retired)
# How old a finished submission's payload and output are before prune deletes them: 14 days, 14
# times the longest a landing may wait for its turn ("landing".wait_s at most 86400 s,
# kitlib/config/schema.py), so the age alone never reaches a landing still queued; prune also
# keeps every run a land record has running whatever its age.
KEEP_S = 14 * 86400


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


def prune(root, now=None, keep_s=KEEP_S):
    """Delete the payload and output files (files()) in the result folder modified more than
    `keep_s` seconds before `now` (default: the time now) whose run_id no land record names with
    a verdict that is not final (running: a submitted landing's records are running from the
    submission, through its wait in the queue, until it ends; a record with no verdict counts as
    running). A land record that cannot be read may be such a run, so then nothing is deleted. A
    file that cannot be deleted (in use: the detached landing holds its output open) is left.
    The paths deleted; never raises."""
    try:
        folder = result.result_dir(root)
        names = os.listdir(folder)
    except (OSError, result.ResultError):
        return []
    running = set()
    for name in names:
        if not name.endswith('.land.json'):
            continue
        rec, why = result.read_path(os.path.join(folder, name))
        if rec is None:
            if why == 'none':       # replaced or removed since the listing
                continue
            return []
        if rec.get('verdict') in (None, result.RUNNING):
            running.add(rec.get('run_id'))
    now = time.time() if now is None else now
    gone = []
    for name in names:
        ext = next((e for e in (PAYLOAD_EXT, OUTPUT_EXT) if name.endswith(e)), None)
        if ext is None:
            continue
        rid = name[:-len(ext)].rsplit('.', 1)[-1]
        if not RUN_ID.fullmatch(rid) or rid in running:
            continue
        path = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(path) <= keep_s:
                continue
            os.remove(path)
        except OSError:
            continue
        gone.append(path)
    return gone


def command(tool_file, args, python=None):
    """The detached tool's argv: the pythonw.exe beside `python` (default: this interpreter), the
    carrier beside `tool_file`, the python.exe beside `python`, then `-B tool_file` and `args`.
    kitlib.lock.detach.command's shape, without the lock's --detached-id."""
    pythonw, console = lockdetach.pythons(python or sys.executable)
    tool = os.path.abspath(tool_file)
    carrier = os.path.join(os.path.dirname(tool), spawn.CARRIER)
    return [pythonw, carrier, console, '-B', tool] + list(args)


def detach(argv, cwd, env, run=None, timeout=START_TIMEOUT_S):
    """Start `command`'s argv detached and windowless (kitlib.lock.detach.create): (the carrier's
    pid, None), or (None, why), with nothing started when its pythonw.exe, carrier or python.exe
    is not a file. run: the tests' stand-in for spawn.run."""
    why = lockdetach.missing(argv)
    if why:
        return None, 'the detached start: %s' % why
    pid, why = lockdetach.create(argv, cwd, env, run=run, timeout=timeout)
    if pid is None:
        return None, 'the detached start %s' % ' '.join(why.split())
    return pid, None


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
    """The SUBMITTED line. Its wait command waits on the batch's last branch: each branch has its
    own land record, but a batch has one outcome, which Lander.run writes into the records in the
    batch's order, so the last branch's record is final only once every branch's is. The command
    names the repository with -C: the main checkout of `root` (where the landing runs, and a
    checkout no cleanup removes), or `root` itself when git cannot place it. The record is in the
    git common dir, so any checkout of the repository reads the same one."""
    kit = kit or pluginsource.live_root()
    main = repo.main_checkout_toplevel(root) or root
    return 'SUBMITTED %s tip %s wait: python %s wait-result %s --tip %s --kind land -C %s' % (
        '+'.join(branches), ','.join(tips), _arg(os.path.join(kit, 'tools', 'gate.py')),
        branches[-1], tips[-1], _arg(main))


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
    count), or a branch already landed at its tip (verdict pass, or an old record's landed-ungated).
    Read only."""
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
           clock=time.monotonic, sleep=time.sleep, landing_of=None):
    """Start the landing of `branches` at `tips` detached: `tool_file` (tools/land.py or
    tools/gate.py) again with `child_args` (the landing's own arguments, -C absolute) and
    `--submitted <payload>`. An Outcome: OK with the REVIEW lines (review_lines) and the
    SUBMITTED line last, once old submissions' files are pruned (prune), USAGE for arguments the
    landing would refuse as usage (nothing written), REFUSED with nothing written when a
    branch's landing is running or it landed at its tip already (blocked), REFUSED when the
    records or the payload cannot be written or the start fails (records written are finished
    `refused`), or when the detached landing died before it recorded itself (claim: its
    records are finished `error`). The export a pinned tool runs from is leased to the carrier
    `detach` started and to the landing (`landing_of(carrier)`, default
    kitlib.lock.detach.wrapper_pid; the tests' stand-in), and the claim watches the landing, or
    the carrier when no landing was found."""
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
    argv = command(tool_file, list(child_args) + [FLAG, payload])
    env = child_env(environ)
    pid, why = (detach_fn or detach)(argv, root, env)
    if pid is None:
        return _refused(runs, 'the landing could not be started: %s' % why)
    landing = _landing(pid, landing_of)
    for p in (pid, landing):
        if p is None:
            continue
        try:
            pin.lease_child(tool_file, p, env)
        except OSError as e:    # the landing runs; a later pin may prune its export under it
            _note(output, 'WARNING: the pinned export could not be leased for pid %d: %s' % (p, e))
    died = claim(root, runs, landing or pid, output, claim_wait_s, clock=clock, sleep=sleep)
    if died:
        return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed; its output: %s' % (
            died, output)], pid=pid, payload=payload, output=output)
    try:
        prune(root, now=now)
    except Exception:   # noqa: BLE001 - the landing is submitted whatever the prune meets
        pass
    return G.Outcome(G.OK, review_lines(root, branches, tips) + [wait_line(branches, tips, root, kit)],
                     pid=pid, payload=payload, output=output)


def review_lines(root, branches, tips):
    """`REVIEW <branch> <tip>: <verdict> by <reviewer>/<model> (<id>)[, carried from <sha> over N
    main merge(s)]` for each branch with a verdict queued at the tip submitted
    (kitlib.gate.verdicts), so the submission shows the review on record. Read only, and only for
    the reader: a branch with no entry at its tip, or a queue that cannot be read, has no line
    (the landing's own checks decide; this decides nothing)."""
    common, _ = repo.common_dir(root)
    if not common:
        return []
    lines = []
    for b, t in zip(branches, tips):
        try:
            e = verdicts._load(verdicts.entry_path(common, b), b)
        except verdicts.VerdictError:
            continue
        if e is None or e['state'] != verdicts.QUEUED or e['tip'].lower() != t.lower():
            continue
        c = e.get(verdicts.CARRIED)
        more = ''
        if c:
            n = len(c['merges'])
            more = ', carried from %s over %d main merge%s' % (c['from'], n, '' if n == 1 else 's')
        lines.append('REVIEW %s %s: %s by %s/%s (%s)%s' % (
            b, e['tip'], e['verdict'], e['reviewer'], e['reviewer_model'], e['reviewer_id'], more))
    return lines


def _landing(carrier, landing_of=None):
    """The pid of the landing (the python.exe) the carrier `carrier` started, or None: not seen
    (it and the carrier ended already) or the lookup failed. The carrier ends with the landing,
    but a carrier that is killed leaves it running."""
    try:
        return (landing_of or lockdetach.wrapper_pid)(carrier)
    except Exception:       # noqa: BLE001 - the carrier's pid still names the submission
        return None


def claim(root, runs, pid, output, wait_s=CLAIM_WAIT_S, poll_s=CLAIM_POLL_S, clock=time.monotonic,
          sleep=time.sleep, alive=None):
    """Wait up to `wait_s` for the detached landing `pid` (submit: the landing's own pid, or the
    carrier's when the landing was not found) to record itself in `runs`' records
    (restore writes its pid into them). Once every record is claimed, or finished: None. When
    the process died first: its unclaimed records are finished `error` and the reason is
    returned. When the wait ends with the process alive: its pid (and creation time) is written
    into the records still unclaimed, each read again just before, so one the landing claimed or
    finished meanwhile is kept, and a death after this is seen by result.wait."""
    alive = alive or process.alive
    try:
        ctime = process.proc_ctime(pid)
    except Exception:       # noqa: BLE001 - alive() then checks the pid alone
        ctime = None
    end = clock() + max(0.0, wait_s)
    while True:
        open_ = []
        for rr in runs:
            rec = _unclaimed(root, rr)
            if rec:
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
                if _unclaimed(root, rr):
                    rr.pid, rr.ctime = pid, ctime
                    rr.start(log=output)
            return None
        sleep(max(0.0, min(poll_s, end - clock())))


def _unclaimed(root, rr):
    """The land record of `rr` when it is still `rr`'s run, running and with no pid; else None."""
    rec, _ = result.read(root, rr.branch, 'land')
    if (isinstance(rec, dict) and rec.get('verdict') == result.RUNNING
            and rec.get('run_id') == rr.run_id and rec.get('pid') is None):
        return rec
    return None


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
