"""The result record: how a branch gate or a landing is going or ended, for a session waiting on it
(docs/gate.md "Result record").

`<git common dir>/kit-gate-result/<stem>.<kind>.json`, stem = file_stem(branch). The common dir is
where the gate-OK records (kit-gate-ok/) and the landing lock already live: one folder per
repository, shared by every worktree of it and never tracked, so the session that gates in its
worktree and the session that waits in another (or in the main checkout) read the same file.

A Run writes the file twice: a `running` record when the work starts (it replaces any earlier
record of that branch and kind, at that tip or another), then the final record with the same
run_id and started_at. Each write is a temp file in the same folder, then os.replace, so a reader
sees one whole record or the other. Who writes when (kitlib.gate.runner.branch_gate,
kitlib.gate.land.Lander.run) is in docs/gate.md "Result record".

The record (SCHEMA 1), every key always present:

  schema          1
  kind            "gate" (a branch gate) or "land" (a landing)
  branch          the branch name
  tip             the full hash gated (the branch gate: HEAD; a landing: --expect-tip)
  base            the base commit: the merge base gated against, or --expect-main; or null
  run_id          one run's id: the running record and its final record share it
  started_at      when the run started (a landing: when it joined the landing queue, before the
                  machine lock gave it its turn, whether run directly or submitted); finished_at
                  when it ended, or null while running (UTC, YYYY-MM-DDTHH:MM:SSZ)
  verdict         running; or final: pass, fail, refused, stuck, busy, usage, landed-ungated (an
                  old record's only: exit LANDED_UNGATED is retired, no landing writes it),
                  not-run (a machine lock refusal), error (an exception or Ctrl+C ended the run)
  code            the exit code gate.py gives that outcome; null for running and error
  log             the run's log folder, or null
  summary         the outcome's verdict line, or the exception for error
  gate_ok_record  the gate-OK record written for the tip, or null
  cleanup         a landing that landed: the branch cleanup's result for this branch
                  (kitlib.gate.cleanup: remote_branch, worktree, worktree_path, local_branch,
                  kept); null for a branch gate, a landing that did not land, and one whose
                  cleanup did not run
  source          where the gate's steps came from (kitlib.gate.detect): "gate.json" (the
                  configured "gate" section) or "auto" (detected from the tree); null while
                  running, for error, and when the run ended before its gate config was read
                  (a landing: before its merged gate). Added after the schema: a reader takes a
                  record without the key as null (source_of)
  pid, ctime      the process writing the run's records (its creation time, FILETIME), or null:
                  a submitted landing's running records until the detached landing records
                  itself (kitlib.gate.submit restore), and a record written before the keys.
                  Added after the schema, like source

wait() polls for the final record of a branch and kind at a tip. It writes one thing: a running
record at that tip whose process has died (running_state 'dead') is finished `error`, "the
<landing|gate> process died: ..." (finish_dead), so a killed run is never waited for forever.
"""
import calendar
import hashlib
import json
import math
import os
import secrets
import time

from kitlib import gate as G
from kitlib.records import gate_ok
from kitlib.git import refs, repo
from kitlib.proc import process

DIR_NAME = 'kit-gate-result'
SCHEMA = 1
KINDS = ('gate', 'land')
RUNNING = 'running'
ERROR = 'error'
TIMEOUT = 7         # gate.py wait-result: no final record at that tip within --timeout
POLL_S = 2.0
RETRIES = 5
RETRY_SLEEP = 0.2
MAX_STEM = 120      # longer stems are cut and given a hash, so the file name stays short
# A running record with no pid this young is taken as live: a submitted landing's, whose detached
# process has not recorded itself yet (kitlib.gate.submit). An older one is 'unknown'.
CLAIM_S = 120
WHAT = {'land': 'landing', 'gate': 'gate'}
SAFE = frozenset('abcdefghijklmnopqrstuvwxyz0123456789._-')
DEVICES = frozenset(['con', 'prn', 'aux', 'nul'] + ['com%d' % i for i in range(10)]
                    + ['lpt%d' % i for i in range(10)])
VERDICTS = {G.OK: 'pass', G.FAILED: 'fail', G.USAGE: 'usage', G.STUCK: 'stuck',
            G.REFUSED: 'refused', G.BUSY: 'busy', G.LANDED_UNGATED: 'landed-ungated'}


class ResultError(Exception):
    """A result record could not be written."""


def verdict_of(code):
    return VERDICTS.get(code, 'not-run')


def file_stem(branch):
    """`branch` as one file name that no other branch maps to, even where file names ignore case
    (Windows): every character outside [a-z0-9._-], capital letters included, is %XX (UTF-8
    bytes, upper-case hex; `%` itself is %25), so `kit/A` is `kit%2F%41`. A stem whose first part
    is a Windows device name (con, nul, ...) has its first letter encoded too. A stem longer than
    MAX_STEM is cut and ends `~` plus a hash of the branch (`~` is never left unencoded)."""
    stem = ''.join(c if c in SAFE else ''.join('%%%02X' % b for b in c.encode('utf-8'))
                   for c in branch)
    if stem.split('.')[0] in DEVICES:
        stem = '%%%02X' % ord(stem[0]) + stem[1:]
    if len(stem) > MAX_STEM:
        stem = '%s~%s' % (stem[:MAX_STEM - 17], hashlib.sha256(branch.encode('utf-8')).hexdigest()[:16])
    return stem


def result_dir(root):
    common, why = repo.common_dir(root)
    if not common:
        raise ResultError('no git common dir for %s: %s' % (root, why))
    return os.path.join(common, DIR_NAME)


def result_path(root, branch, kind='gate'):
    if kind not in KINDS:
        raise ResultError('a result kind is one of %s, not %r' % (', '.join(KINDS), kind))
    return os.path.join(result_dir(root), '%s.%s.json' % (file_stem(branch), kind))


def source_of(rec):
    """A record's `source`, None when it has none (a record written before the key)."""
    return rec.get('source')


def build(branch, tip, base, kind, code, log=None, summary=None, gate_ok_record=None,
          finished_at=None, run_id=None, started_at=None, verdict=None, cleanup=None,
          source=None, pid=None, ctime=None):
    """A record. `verdict` defaults to verdict_of(code); RUNNING and ERROR carry no code."""
    verdict = verdict or verdict_of(code)
    now = gate_ok.utc()
    return {'schema': SCHEMA, 'kind': kind, 'branch': branch, 'tip': tip, 'base': base,
            'run_id': run_id or secrets.token_hex(8), 'started_at': started_at or now,
            'finished_at': None if verdict == RUNNING else (finished_at or now),
            'verdict': verdict, 'code': None if verdict in (RUNNING, ERROR) else code,
            'log': log, 'summary': summary or '', 'gate_ok_record': gate_ok_record,
            'cleanup': cleanup, 'source': source, 'pid': pid, 'ctime': ctime}


def own_process():
    """(pid, ctime) of this process; ctime None when it cannot be read."""
    try:
        return os.getpid(), process.own_ctime()
    except Exception:       # noqa: BLE001 - the pid alone still names it
        return os.getpid(), None


def _retry(fn, *args):
    for i in range(RETRIES):
        try:
            return fn(*args)
        except PermissionError:     # a reader holding the old file open (Windows)
            if i == RETRIES - 1:
                raise
            time.sleep(RETRY_SLEEP)


def write(root, record):
    """Write `record` (from build()) atomically; returns its path."""
    path = result_path(root, record['branch'], record['kind'])
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, '.%s.tmp' % secrets.token_hex(6))
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(record, sort_keys=True, indent=1) + '\n')
            _retry(os.replace, tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        raise ResultError('cannot write %s: %s' % (path, e))
    return path


def _summary(outcome):
    keys = ('LANDED ', 'NOT LANDED', 'CONFLICT ', 'STUCK:', 'REFUSED:', 'BUSY:', 'USAGE:',
            'NOT RUN:')
    for line in outcome.lines:      # a landing's verdict line (docs/gate.md "Exit codes")
        if line.startswith(keys):
            return line
    return outcome.lines[0] if outcome.lines else ''


class Run(object):
    """One gate or landing's records: start() writes `running`, finish() or error() the final
    record with the same run_id and started_at. Each returns `(path, line)`: the line to say as
    progress, `result record: <path>` or a WARNING (path None). Nothing here raises: a record
    that cannot be written never changes the run's outcome. Its records name this process (pid,
    ctime), the one doing the run; a submission clears them (kitlib.gate.submit)."""

    def __init__(self, root, kind, branch, tip, base=None):
        self.root, self.kind, self.branch, self.tip, self.base = root, kind, branch, tip, base
        self.run_id = secrets.token_hex(8)
        self.started_at = gate_ok.utc()
        self.pid, self.ctime = own_process()

    @classmethod
    def maybe(cls, root, kind, branch, tip, base=None):
        """A Run, or None when there is no branch or no full tip to name the record by."""
        if not branch or not refs.is_sha(tip or ''):
            return None
        return cls(root, kind, branch, tip, base)

    def _write(self, **kw):
        try:
            rec = build(self.branch, self.tip, kw.pop('base', self.base), self.kind,
                        kw.pop('code', None), run_id=self.run_id, started_at=self.started_at,
                        pid=self.pid, ctime=self.ctime, **kw)
            path = write(self.root, rec)
        except Exception as e:      # noqa: BLE001 - the run's outcome stands whatever happens here
            return None, 'WARNING: the result record could not be written: %s' % e
        return path, 'result record: %s' % path

    def start(self, log=None):
        return self._write(verdict=RUNNING, log=log, summary='%s %s at %s started' % (
            self.kind, self.branch, self.tip[:10]))

    def finish(self, outcome, base=None, log=None, source=None):
        try:
            facts = outcome.facts
            kw = dict(code=outcome.code, base=base or facts.get('base') or self.base,
                      log=log or facts.get('log_dir'), summary=_summary(outcome),
                      gate_ok_record=facts.get('record'),
                      cleanup=(facts.get('cleanup') or {}).get(self.branch),
                      source=source or facts.get('source'))
        except Exception as e:      # noqa: BLE001
            return None, 'WARNING: the result record could not be written: %s' % e
        return self._write(**kw)

    def error(self, exc):
        return self._write(verdict=ERROR, summary='the %s stopped: %s: %s' % (
            self.kind, type(exc).__name__, exc))


def read_path(path):
    """`(record, None)`, `(None, 'none')` when there is none, or `(None, why)`."""
    try:
        with open(path, encoding='utf-8') as f:
            rec = json.loads(f.read())
    except FileNotFoundError:
        return None, 'none'
    except (OSError, ValueError) as e:
        return None, 'cannot read %s: %s' % (path, e)
    if not isinstance(rec, dict):
        return None, '%s is not a result record' % path
    return rec, None


def read(root, branch, kind='gate'):
    try:
        path = result_path(root, branch, kind)
    except ResultError as e:
        return None, str(e)
    return read_path(path)


def answers(rec, branch, kind, tip):
    """True when `rec` is the final record of `branch` and `kind` at a tip starting with `tip`."""
    return rec.get('verdict') not in (None, RUNNING) and at_tip(rec, branch, kind, tip)


def _age_s(stamp, now):
    try:
        return now - calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError):
        return None


def running_state(rec, now=None, alive=None):
    """Whether the process of a running record still runs: 'live', 'dead' (surely gone:
    process.alive), or 'unknown' (no pid, and started CLAIM_S or more ago: a record written
    before the pid key). A record with no pid younger than CLAIM_S is 'live' (a submission whose
    detached landing has not recorded itself yet). None for a record that is not running."""
    if not isinstance(rec, dict) or rec.get('verdict') != RUNNING:
        return None
    pid = rec.get('pid')
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        ctime = rec.get('ctime')
        try:
            up = (alive or process.alive)(pid, ctime if isinstance(ctime, int) else None)
        except Exception:   # noqa: BLE001 - not surely gone
            return 'unknown'
        return 'live' if up else 'dead'
    age = _age_s(rec.get('started_at'), time.time() if now is None else now)
    return 'live' if age is not None and age < CLAIM_S else 'unknown'


def finish_dead(root, rec, why=None, unclaimed=False):
    """Finish the running record `rec`, whose process died, as `error`: the record as it is
    now, or None when it cannot be read. Only when the record on disk is still `rec`'s run and
    running (`unclaimed`: and has no pid yet); anything else (finished, or a newer run's) is
    returned untouched. The final record keeps the run's run_id, started_at, tip, base, log, pid
    and ctime."""
    try:
        path = result_path(root, rec['branch'], rec['kind'])
    except (KeyError, ResultError):
        return None
    now, _ = read_path(path)
    if (not isinstance(now, dict) or now.get('verdict') != RUNNING
            or now.get('run_id') != rec.get('run_id')
            or (unclaimed and now.get('pid') is not None)):
        return now
    why = why or 'the %s process died: pid %s ended without finishing its record' % (
        WHAT.get(now.get('kind'), now.get('kind')), now.get('pid'))
    final = build(now['branch'], now.get('tip'), now.get('base'), now['kind'], None,
                  log=now.get('log'), summary=why, run_id=now.get('run_id'),
                  started_at=now.get('started_at'), verdict=ERROR, pid=now.get('pid'),
                  ctime=now.get('ctime'))
    try:
        write(root, final)
    except ResultError:
        return now
    return final


def running_records(root, kind, run_id):
    """The running `kind` records of run `run_id`, every branch: a submitted batch shares one."""
    try:
        folder = result_dir(root)
        names = sorted(os.listdir(folder))
    except (OSError, ResultError):
        return []
    found = []
    for name in names:
        if not name.endswith('.%s.json' % kind):
            continue
        rec, _ = read_path(os.path.join(folder, name))
        if (isinstance(rec, dict) and rec.get('kind') == kind and rec.get('verdict') == RUNNING
                and rec.get('run_id') == run_id):
            found.append(rec)
    return found


def at_tip(rec, branch, kind, tip):
    got = rec.get('tip')
    return (rec.get('branch') == branch and rec.get('kind') == kind
            and isinstance(got, str) and got.lower().startswith(tip.lower()))


def valid_timeout(timeout_s):
    return timeout_s is None or (math.isfinite(timeout_s) and timeout_s >= 0)


def wait(root, branch, tip, kind='gate', timeout_s=None, poll_s=POLL_S, clock=time.monotonic,
         sleep=time.sleep):
    """Poll until the final `kind` record of `branch` names a tip starting with `tip`:
    `(record, None)`, or `(None, 'timeout')` once `timeout_s` passed (None: never). A running
    record at that tip whose process died is finished `error` (finish_dead) and returned; any
    other running record, one at another tip or of another branch, a missing one, or one that
    cannot be read is waited past. ResultError when the record path cannot be worked out (not a
    repository)."""
    if not valid_timeout(timeout_s):
        raise ValueError('timeout_s must be None or a finite number >= 0, not %r' % (timeout_s,))
    path = result_path(root, branch, kind)
    end = None if timeout_s is None else clock() + timeout_s
    while True:
        rec, _ = read_path(path)
        if (rec is not None and rec.get('verdict') == RUNNING and at_tip(rec, branch, kind, tip)
                and running_state(rec) == 'dead'):
            rec = finish_dead(root, rec)
        if rec is not None and answers(rec, branch, kind, tip):
            return rec, None
        if end is not None and clock() >= end:
            return None, 'timeout'
        sleep(poll_s if end is None else max(0.0, min(poll_s, end - clock())))
