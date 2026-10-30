"""The result record: how a branch gate or a landing is going or ended, for a session waiting on it
(docs/gate.md "Result record").

`<dev_drive>/pm-kit/kit-gate-result/<repo key>/<stem>.<kind>.json`, stem = file_stem(branch):
`dev_drive` is the machine config's Dev Drive root (kitlib.config.user.dev_drive), and the repo
key (repo_key) names the repository's git common dir, so every worktree of a repository shares
one folder and no two repositories do. The session that gates in its worktree and the session
that waits in another (or in the main checkout) read the same file. The folder is outside every
checkout and every agent's temp folder, where agents' own file writes do not reach. With no
`dev_drive` set, or a machine config that cannot be read, there is no result folder: a write is
a ResultError (a branch gate says it as a WARNING; a direct landing and a submission refuse) and
nothing is written anywhere else.

The old folder, `<git common dir>/kit-gate-result/` (legacy_dir), is read, and written only when
finish_dead finishes a dead old-folder record in place (under that file's lock, which it makes
there): read() and wait() take a record there when the new folder has none of that branch and
kind, or when it started strictly later than the new folder's (an older kit's run; pick). The readers a landing trusts
(kitlib.gate.submit's claim and restore) read the new folder only.

A Run writes the file twice: a `running` record when the work starts (it replaces any earlier
record of that branch and kind, at that tip or another), then the final record with the same
run_id and started_at. Each write is a temp file in the same folder, then os.replace, so a reader
sees one whole record or the other. Who writes when (kitlib.gate.runner.branch_gate,
kitlib.gate.land.Lander.run) is in docs/gate.md "Result record".

A record has several writers (a landing's own run, its submitter's claim, a waiter's finish_dead),
so every write takes the record's lock first: an OS lock (kitlib.fs.treelock.TreeLock) on
`.<record file name>.lock` beside it, held from the re-read a conditional write decides on to its
os.replace, so no writer replaces a record another wrote after that re-read. write(when=...) and
finish_dead re-read under it; an unconditional write (a run writing its own outcome) only waits
its turn. The lock file stays (TreeLock never removes one), and its leading dot keeps it out of
every scan of the folder. A lock not had within LOCK_WAIT_S is a ResultError, like any write that
fails. kitlib.fs.writelock is not used: it places its lock files by asking git which checkout the
file is in, and the result folder is in none.

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
  gate_once       a landing's final and error records: whether the base's config sets
                  "landing".gate_once (Lander.gate_once); null for a branch gate, a running
                  record, a landing that ended before it read the base's config, a record
                  another writer finished (kitlib.gate.submit, finish_dead) and one written
                  before the key. Added after the schema, like source
  escalated       the same records: whether gate_once made the merged gate run --full
                  (Lander.once_escalate); null where gate_once is

The land ledger, `<result folder>/land-ledger.jsonl` (ledger_path): one JSON line per final or
error land record Lander.run writes, with LEDGER_FIELDS, appended and never rewritten, so a
retry that replaces the land record leaves the earlier outcome's line (append_ledger).

wait() polls for the final record of a branch and kind at a tip. It writes one thing: a running
record at that tip whose process has died (running_state 'dead') is finished `error`, "the
<landing|gate> process died: ..." (finish_dead), so a killed run is never waited for forever.
"""
import calendar
import contextlib
import hashlib
import json
import math
import os
import secrets
import time

from kitlib import gate as G
from kitlib.config import user as kuser
from kitlib.fs import replace as fsreplace
from kitlib.fs.treelock import TreeBusy, TreeLock, TreeLockError
from kitlib.records import gate_ok
from kitlib.git import refs, repo
from kitlib.proc import process

DIR_NAME = 'kit-gate-result'
KIT_DIR = 'pm-kit'      # <dev_drive>/pm-kit/kit-gate-result: the machine's kit folder
KEY_NAME_MAX = 40       # the readable part of a repo key; its hash keeps keys apart
NO_DEV_FOR = ('the machine config sets no dev_drive (the Dev Drive\'s root folder), so there is no '
              '%s (<dev_drive>\\pm-kit\\%s); the owner sets it (docs/kit-json.md "Machine config '
              'keys")')
NO_DEV = NO_DEV_FOR % ('result folder', DIR_NAME)
SCHEMA = 1
KINDS = ('gate', 'land')
RUNNING = 'running'
ERROR = 'error'
TIMEOUT = 7         # gate.py wait-result: no final record at that tip within --timeout
POLL_S = 2.0
RETRIES = 5         # tries of a record's os.replace while it fails with PermissionError
RETRY_SLEEP = 0.2   # seconds between them
# RETRIES as a wait for kitlib.fs.replace: try k starts at least k-1 pauses in, so the last (at
# 0.8 s or later) is past this bound and the one before it (at 0.6 s) is not; the half pause is
# the margin for a slow try.
REPLACE_WAIT = (RETRIES - 1.5) * RETRY_SLEEP
LOCK_WAIT_S = 10.0  # a record's lock is held for one read and one replace; past this, ResultError
LOCK_PAUSE = 0.02
MAX_STEM = 120      # longer stems are cut and given a hash, so the file name stays short
# A running record with no pid this young is taken as live: a submitted landing's, whose detached
# process has not recorded itself yet (kitlib.gate.submit). An older one is 'unknown'.
CLAIM_S = 120
WHAT = {'land': 'landing', 'gate': 'gate'}
SAFE = frozenset('abcdefghijklmnopqrstuvwxyz0123456789._-')
DEVICES = frozenset(['con', 'prn', 'aux', 'nul'] + ['com%d' % i for i in range(10)]
                    + ['lpt%d' % i for i in range(10)])
LEDGER = 'land-ledger.jsonl'
LEDGER_FIELDS = ('branch', 'tip', 'run_id', 'started_at', 'finished_at', 'verdict', 'code',
                 'gate_once', 'escalated')
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


def kit_folder(name, what):
    """`<dev_drive>/pm-kit/<name>`: one of the machine's kit folders, each holding one folder per
    repository (repo_key): kit-gate-result, kitlib.records.gate_ok's kit-gate-ok and
    kitlib.gate.budget's kit-gate-budget. ResultError when the machine config cannot be read or
    sets no dev_drive; `what` names the folder in it ("result folder")."""
    try:
        cfg = kuser.user_config()
    except Exception as e:      # noqa: BLE001 - ConfigError, OSError: no folder, and why
        raise ResultError('the machine config cannot be read, so there is no %s: %s' % (what, e))
    dev = kuser.dev_drive(cfg)
    if not dev:
        raise ResultError(NO_DEV_FOR % (what, name))
    return os.path.join(os.path.abspath(dev), KIT_DIR, name)


def machine_root():
    """`<dev_drive>/pm-kit/kit-gate-result`: the folder every repository's result folder is in.
    ResultError when the machine config cannot be read or sets no dev_drive."""
    return kit_folder(DIR_NAME, 'result folder')


def repo_key(common):
    """The result folder name of the repository whose git common dir is `common`: its folder's
    name (the common dir's parent, for a `.git`), lower case, [a-z0-9_-] only and at most
    KEY_NAME_MAX long, then `-` and 12 hex of the sha256 of the common dir's real path
    (normcase): one name for every worktree of a repository, another for every other one."""
    real = os.path.normcase(os.path.realpath(common)).rstrip('\\/')
    head, base = os.path.split(real)
    name = os.path.basename(head) if base == '.git' else base
    slug = ''.join(c for c in name.lower() if c in SAFE and c != '.')[:KEY_NAME_MAX] or 'repo'
    return '%s-%s' % (slug, hashlib.sha256(real.encode('utf-8')).hexdigest()[:12])


def _common(root):
    common, why = repo.common_dir(root)
    if not common:
        raise ResultError('no git common dir for %s: %s' % (root, why))
    return common


def result_dir(root):
    """The result folder of the repository `root` is in (the module docstring): ResultError
    when it cannot be worked out."""
    return os.path.join(machine_root(), repo_key(_common(root)))


def legacy_dir(root):
    """The old result folder, `<git common dir>/kit-gate-result`: read, and written only when
    finish_dead finishes a dead old-folder record in place."""
    return os.path.join(_common(root), DIR_NAME)


def record_dirs(root):
    """[result_dir, legacy_dir] of `root`: the folders a reader of every record lists, the new
    one first. ResultError when the new one cannot be worked out."""
    common = _common(root)
    return [os.path.join(machine_root(), repo_key(common)), os.path.join(common, DIR_NAME)]


def _file_name(branch, kind):
    if kind not in KINDS:
        raise ResultError('a result kind is one of %s, not %r' % (', '.join(KINDS), kind))
    return '%s.%s.json' % (file_stem(branch), kind)


def result_path(root, branch, kind='gate'):
    return os.path.join(result_dir(root), _file_name(branch, kind))


def legacy_path(root, branch, kind='gate'):
    """The record's path in the old folder (legacy_dir): read, and written only when finish_dead
    finishes a dead old-folder record in place."""
    return os.path.join(legacy_dir(root), _file_name(branch, kind))


def source_of(rec):
    """A record's `source`, None when it has none (a record written before the key)."""
    return rec.get('source')


def build(branch, tip, base, kind, code, log=None, summary=None, gate_ok_record=None,
          finished_at=None, run_id=None, started_at=None, verdict=None, cleanup=None,
          source=None, pid=None, ctime=None, gate_once=None, escalated=None):
    """A record. `verdict` defaults to verdict_of(code); RUNNING and ERROR carry no code."""
    verdict = verdict or verdict_of(code)
    now = gate_ok.utc()
    return {'schema': SCHEMA, 'kind': kind, 'branch': branch, 'tip': tip, 'base': base,
            'run_id': run_id or secrets.token_hex(8), 'started_at': started_at or now,
            'finished_at': None if verdict == RUNNING else (finished_at or now),
            'verdict': verdict, 'code': None if verdict in (RUNNING, ERROR) else code,
            'log': log, 'summary': summary or '', 'gate_ok_record': gate_ok_record,
            'cleanup': cleanup, 'source': source, 'pid': pid, 'ctime': ctime,
            'gate_once': gate_once, 'escalated': escalated}


def own_process():
    """(pid, ctime) of this process; ctime None when it cannot be read."""
    try:
        return os.getpid(), process.own_ctime()
    except Exception:       # noqa: BLE001 - the pid alone still names it
        return os.getpid(), None


def lock_path(path):
    """The lock file of the record at `path`: `.<its file name>.lock` beside it."""
    folder, name = os.path.split(path)
    return os.path.join(folder, '.%s.lock' % name)


@contextlib.contextmanager
def _held(path):
    """Hold the lock of the record at `path` (lock_path) for the block, waiting up to LOCK_WAIT_S
    for another writer to let it go. ResultError when it cannot be had. One writer holds it at a
    time, a thread of this process included (a TreeLock is per handle)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as e:
        raise ResultError('cannot write %s: %s' % (path, e))
    lock = TreeLock(lock_path(path), what='result record %s' % os.path.basename(path))
    end = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            lock.acquire()
            break
        except TreeBusy as e:
            if time.monotonic() >= end:
                raise ResultError('cannot write %s: its lock %s is still %s after %ss'
                                  % (path, lock.path, e, LOCK_WAIT_S))
            time.sleep(LOCK_PAUSE)
        except TreeLockError as e:
            raise ResultError('cannot write %s: %s' % (path, e))
    try:
        yield
    finally:
        lock.release()


def _put(path, record):
    """Replace the record at `path` with `record`: a temp file, then os.replace, retried while a
    reader holds the old file open (PermissionError; kitlib.fs.replace for REPLACE_WAIT). The
    caller holds the record's lock."""
    folder = os.path.dirname(path)
    try:
        tmp = os.path.join(folder, '.%s.tmp' % secrets.token_hex(6))
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(record, sort_keys=True, indent=1) + '\n')
            fsreplace.replace(tmp, path, wait=REPLACE_WAIT, pause=RETRY_SLEEP)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        raise ResultError('cannot write %s: %s' % (path, e))


def write(root, record, when=None):
    """Write `record` (from build()) atomically under the record's lock; returns its path.
    `when(current)`, given: the record on disk now (None when there is none or it cannot be read)
    is read under the lock, and a false answer leaves it as it is and returns None."""
    path = result_path(root, record['branch'], record['kind'])
    with _held(path):
        if when is not None and not when(read_path(path)[0]):
            return None
        _put(path, record)
    return path


def ledger_path(root):
    """`<result folder>/land-ledger.jsonl`: the land ledger of the repository `root` is in.
    ResultError when the result folder cannot be worked out."""
    return os.path.join(result_dir(root), LEDGER)


def append_ledger(root, record):
    """Append `record`'s LEDGER_FIELDS to the land ledger as one JSON line, in one write call
    (landings are serialized by the landing lock; one write keeps a line whole all the same).
    `(path, None)`, or `(None, warning line)` when it could not be appended: nothing here raises,
    so a ledger that cannot be written never changes the landing's outcome."""
    try:
        path = ledger_path(root)
        data = (json.dumps(dict((k, record.get(k)) for k in LEDGER_FIELDS)) + '\n').encode('utf-8')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_BINARY', 0))
        try:
            wrote = os.write(fd, data)
        finally:
            os.close(fd)
        if wrote != len(data):
            raise OSError('wrote %d of %d bytes' % (wrote, len(data)))
    except Exception as e:      # noqa: BLE001 - the landing's outcome stands whatever happens here
        return None, ('WARNING: the land ledger line could not be appended: %s: %s; the outcome '
                      'stands' % (type(e).__name__, e))
    return path, None


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
        self.last = None

    @classmethod
    def maybe(cls, root, kind, branch, tip, base=None):
        """A Run, or None when there is no branch or no full tip to name the record by."""
        if not branch or not refs.is_sha(tip or ''):
            return None
        return cls(root, kind, branch, tip, base)

    def _write(self, when=None, **kw):
        self.last = None    # the record this write built (written or not), for the land ledger
        try:
            rec = self.last = build(self.branch, self.tip, kw.pop('base', self.base), self.kind,
                                    kw.pop('code', None), run_id=self.run_id,
                                    started_at=self.started_at, pid=self.pid, ctime=self.ctime,
                                    **kw)
            path = write(self.root, rec, when=when)
        except Exception as e:      # noqa: BLE001 - the run's outcome stands whatever happens here
            return None, 'WARNING: the result record could not be written: %s' % e
        if path is None:
            return None, 'result record of %s %s left as it is: it changed before this write' % (
                self.kind, self.branch)
        return path, 'result record: %s' % path

    def start(self, log=None, when=None):
        """`when`: write only if when(the record on disk) is true, read under the record's lock."""
        return self._write(when=when, verdict=RUNNING, log=log,
                           summary='%s %s at %s started' % (self.kind, self.branch, self.tip[:10]))

    def finish(self, outcome, base=None, log=None, source=None, when=None, gate_once=None,
               escalated=None):
        """`gate_once`, `escalated`: a landing's (Lander.run); None for a branch gate."""
        try:
            facts = outcome.facts
            kw = dict(code=outcome.code, base=base or facts.get('base') or self.base,
                      log=log or facts.get('log_dir'), summary=_summary(outcome),
                      gate_ok_record=facts.get('record'),
                      cleanup=(facts.get('cleanup') or {}).get(self.branch),
                      source=source or facts.get('source'), gate_once=gate_once,
                      escalated=escalated)
        except Exception as e:      # noqa: BLE001
            self.last = None
            return None, 'WARNING: the result record could not be written: %s' % e
        return self._write(when=when, **kw)

    def error(self, exc, log=None, gate_once=None, escalated=None):
        """`log`: the run's log folder, when it was made before `exc` ended the run."""
        return self._write(verdict=ERROR, log=log, summary='the %s stopped: %s: %s' % (
            self.kind, type(exc).__name__, exc), gate_once=gate_once, escalated=escalated)


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


def _stamp(rec):
    try:
        return calendar.timegm(time.strptime(rec.get('started_at'), '%Y-%m-%dT%H:%M:%SZ'))
    except (AttributeError, TypeError, ValueError):
        return None


def newer_old(new, old):
    """True when the old folder's record `old` is the one to take over the new folder's `new`:
    there is no `new`, or `old` started strictly later (an older kit's run since). A started_at
    that cannot be read never wins over the new folder's record."""
    if not isinstance(old, dict):
        return False
    if not isinstance(new, dict):
        return True
    a, b = _stamp(new), _stamp(old)
    return a is not None and b is not None and b > a


def pick(new_got, old_got):
    """`((record, why), index)`: what read() answers from read_path's `(record, why)` of the new
    path and of the old one, and which it is (0 new, 1 old). A new path that cannot be read is
    the answer, whatever the old one holds; else the old one when newer_old, or when neither
    path has a record and only the old one's why is more than 'none'; else the new one."""
    (new, new_why), (old, old_why) = new_got, old_got
    if new is None and new_why != 'none':
        return new_got, 0
    if newer_old(new, old) or (new is None and old is None and old_why != 'none'):
        return old_got, 1
    return new_got, 0


def read(root, branch, kind='gate', legacy=True):
    """`(record, None)`, `(None, 'none')` or `(None, why)`: the record of `branch` and `kind`
    in the new folder, or in the old one (pick); `legacy` False: the new folder only."""
    try:
        path = result_path(root, branch, kind)
        old = legacy_path(root, branch, kind) if legacy else None
    except ResultError as e:
        return None, str(e)
    if old is None:
        return read_path(path)
    return pick(read_path(path), read_path(old))[0]


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


def finish_dead(root, rec, why=None, unclaimed=False, path=None):
    """Finish the running record `rec`, whose process died, as `error`: the record as it is
    now, or None when it cannot be read. Only when the record on disk is still `rec`'s run and
    running (`unclaimed`: and has no pid yet); anything else (finished, or a newer run's) is
    returned untouched. The final record keeps the run's run_id, started_at, tip, base, log, pid
    and ctime. The record is read, judged and replaced under its lock, so a record another writer
    put there meanwhile is judged, never overwritten unread. `path`: the file `rec` was read
    from (wait finishes an old-folder record where it is, under that file's lock); default the
    record's path in the new folder."""
    if path is None:
        try:
            path = result_path(root, rec['branch'], rec['kind'])
        except (KeyError, ResultError):
            return None
    try:
        with _held(path):
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
            _put(path, final)
    except ResultError:
        return read_path(path)[0]
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
    record at that tip whose process died is finished `error` (finish_dead, in the folder it was
    read from) and returned; any other running record, one at another tip or of another branch,
    a missing one, or one that cannot be read is waited past. Each poll reads the record's file
    in the new folder and in the old one and takes the one read() would (pick). ResultError when
    the record path cannot be worked out (not a repository, or no result folder: result_dir)."""
    if not valid_timeout(timeout_s):
        raise ValueError('timeout_s must be None or a finite number >= 0, not %r' % (timeout_s,))
    paths = (result_path(root, branch, kind), legacy_path(root, branch, kind))
    end = None if timeout_s is None else clock() + timeout_s
    while True:
        (rec, _), at = pick(read_path(paths[0]), read_path(paths[1]))
        if (rec is not None and rec.get('verdict') == RUNNING and at_tip(rec, branch, kind, tip)
                and running_state(rec) == 'dead'):
            rec = finish_dead(root, rec, path=paths[at])
        if rec is not None and answers(rec, branch, kind, tip):
            return rec, None
        if end is not None and clock() >= end:
            return None, 'timeout'
        sleep(poll_s if end is None else max(0.0, min(poll_s, end - clock())))
