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
  started_at      when the run started; finished_at when it ended, or null while running
                  (UTC, YYYY-MM-DDTHH:MM:SSZ)
  verdict         running; or final: pass, fail, refused, stuck, busy, usage, landed-ungated (a
                  landing that landed with commits on top no gate saw; exit LANDED_UNGATED),
                  not-run (a machine lock refusal), error (an exception or Ctrl+C ended the run)
  code            the exit code gate.py gives that outcome; null for running and error
  log             the run's log folder, or null
  summary         the outcome's verdict line, or the exception for error
  gate_ok_record  the gate-OK record written for the tip, or null
  cleanup         a landing that landed: the branch cleanup's result for this branch
                  (kitlib.gate.cleanup: remote_branch, worktree, worktree_path, local_branch,
                  kept); null for a branch gate, a landing that did not land, and one whose
                  cleanup did not run

wait() is read-only: it polls for the final record of a branch and kind at a tip.
"""
import hashlib
import json
import math
import os
import secrets
import time

from kitlib import gate as G
from kitlib.records import gate_ok
from kitlib.git import refs, repo

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


def build(branch, tip, base, kind, code, log=None, summary=None, gate_ok_record=None,
          finished_at=None, run_id=None, started_at=None, verdict=None, cleanup=None):
    """A record. `verdict` defaults to verdict_of(code); RUNNING and ERROR carry no code."""
    verdict = verdict or verdict_of(code)
    now = gate_ok.utc()
    return {'schema': SCHEMA, 'kind': kind, 'branch': branch, 'tip': tip, 'base': base,
            'run_id': run_id or secrets.token_hex(8), 'started_at': started_at or now,
            'finished_at': None if verdict == RUNNING else (finished_at or now),
            'verdict': verdict, 'code': None if verdict in (RUNNING, ERROR) else code,
            'log': log, 'summary': summary or '', 'gate_ok_record': gate_ok_record,
            'cleanup': cleanup}


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
    keys = ('LANDED ', 'NOT LANDED', 'STUCK:', 'REFUSED:', 'BUSY:', 'USAGE:', 'NOT RUN:')
    for line in outcome.lines:      # a landing's verdict line (docs/gate.md "Exit codes")
        if line.startswith(keys):
            return line
    return outcome.lines[0] if outcome.lines else ''


class Run(object):
    """One gate or landing's records: start() writes `running`, finish() or error() the final
    record with the same run_id and started_at. Each returns `(path, line)`: the line to say as
    progress, `result record: <path>` or a WARNING (path None). Nothing here raises: a record
    that cannot be written never changes the run's outcome."""

    def __init__(self, root, kind, branch, tip, base=None):
        self.root, self.kind, self.branch, self.tip, self.base = root, kind, branch, tip, base
        self.run_id = secrets.token_hex(8)
        self.started_at = gate_ok.utc()

    @classmethod
    def maybe(cls, root, kind, branch, tip, base=None):
        """A Run, or None when there is no branch or no full tip to name the record by."""
        if not branch or not refs.is_sha(tip or ''):
            return None
        return cls(root, kind, branch, tip, base)

    def _write(self, **kw):
        try:
            rec = build(self.branch, self.tip, kw.pop('base', self.base), self.kind,
                        kw.pop('code', None), run_id=self.run_id, started_at=self.started_at, **kw)
            path = write(self.root, rec)
        except Exception as e:      # noqa: BLE001 - the run's outcome stands whatever happens here
            return None, 'WARNING: the result record could not be written: %s' % e
        return path, 'result record: %s' % path

    def start(self):
        return self._write(verdict=RUNNING, summary='%s %s at %s started' % (
            self.kind, self.branch, self.tip[:10]))

    def finish(self, outcome, base=None, log=None):
        try:
            facts = outcome.facts
            kw = dict(code=outcome.code, base=base or facts.get('base') or self.base,
                      log=log or facts.get('log_dir'), summary=_summary(outcome),
                      gate_ok_record=facts.get('record'),
                      cleanup=(facts.get('cleanup') or {}).get(self.branch))
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
    got = rec.get('tip')
    return (rec.get('branch') == branch and rec.get('kind') == kind
            and rec.get('verdict') not in (None, RUNNING)
            and isinstance(got, str) and got.lower().startswith(tip.lower()))


def valid_timeout(timeout_s):
    return timeout_s is None or (math.isfinite(timeout_s) and timeout_s >= 0)


def wait(root, branch, tip, kind='gate', timeout_s=None, poll_s=POLL_S, clock=time.monotonic,
         sleep=time.sleep):
    """Poll until the final `kind` record of `branch` names a tip starting with `tip`:
    `(record, None)`, or `(None, 'timeout')` once `timeout_s` passed (None: never). A running
    record, one at another tip or of another branch, a missing one, or one that cannot be read
    is waited past. ResultError when the record path cannot be worked out (not a repository)."""
    if not valid_timeout(timeout_s):
        raise ValueError('timeout_s must be None or a finite number >= 0, not %r' % (timeout_s,))
    path = result_path(root, branch, kind)
    end = None if timeout_s is None else clock() + timeout_s
    while True:
        rec, _ = read_path(path)
        if rec is not None and answers(rec, branch, kind, tip):
            return rec, None
        if end is not None and clock() >= end:
            return None, 'timeout'
        sleep(poll_s if end is None else max(0.0, min(poll_s, end - clock())))
