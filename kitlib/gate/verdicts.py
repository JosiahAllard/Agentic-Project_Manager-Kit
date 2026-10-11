"""The verdict queue: reviewed branches waiting to land, one file per branch.

A coordinator queues a branch after reading its review (tools/verdict.py add); a reviewer never
does, so no reviewer can queue its own verdict. A landing takes what is ready (ready()) and drops
what it landed.

Where: `<git common dir>/kit-verdicts/<branch>.json`, the branch name percent-encoded
(urllib.parse.quote with no safe characters: '/' is '%2F', '%' is '%25'), so the file name gives
the branch back and holds no character Windows refuses. The common dir is shared by every work
tree of the repository and never tracked. Every write is a temp file in the same folder, then
os.replace; a re-add for the same branch replaces its entry.

The queue lock: add, drop, mark_failed and scan each run whole (read, check, then write or
remove) holding `<queue folder>/.queue.lock`, a file made with O_CREAT|O_EXCL and removed when
the call ends, so none of them acts on an entry another replaced between its read and its write.
A call waits up to LOCK_WAIT_S for the lock, then raises VerdictError and does nothing. A
PermissionError making the lock file is waited out the same way (on Windows a lock file being
removed refuses a new one for a moment), the VerdictError then naming it; any other OSError is a
VerdictError at once, naming the cause. Stale-lock
rule: a holder keeps the lock for milliseconds, so a lock file last written more than
LOCK_STALE_S ago was left by a holder that died (a killed process skips its finally) and the
next caller removes it and takes the lock. A lock file that cannot be removed when a call ends is
left to that rule. Temp files are made only under the lock, so one that scan (holding it) sees
was left by a write that died: scan reports it among the files that are not entries, which stops
entries(); nothing here deletes it.

An entry (every key always present):

  branch          the branch name, as `git branch` names it (kit/x)
  tip             the full commit hash reviewed; add() checks it is `refs/heads/<branch>` now
  verdict         LAND | LAND WITH FOLLOW-UP
  risk            Low | Medium | High
  reviewer        the reviewer's agent type
  reviewer_model  the model the reviewer ran on
  reviewer_id     the reviewer's agent or session id
  author          the change's author: commit author and agent type
  range           the range reviewed, `<base>..<tip>`; its right side is the tip or a prefix of
                  it (7 or more hex digits), so the review covered the commit queued
  session         the coordinator session that queued it
  records         [backlog record ids] the branch carries
  queued_utc      set by add(): YYYY-MM-DDTHH:MM:SSZ
  state           queued | failed (mark_failed)
  last_result     None, or why it was marked failed

Errors (all VerdictError): BadEntry for a malformed entry or branch name given, nothing read or
written; TipMismatch when the tip is not the branch's head, or there is no such branch;
OtherBranch when the file for the branch named holds another branch's entry (names that differ
only in case share one file on Windows); VerdictError itself for a queue file that cannot be read
or written, or is not an entry, the queue lock not had in time, or git giving no answer. Nothing
here prints.

Residual race (inference, not tested): two callers that find the same stale lock at once can
both remove it, the second removing the lock the first just made; that needs a holder to have
died first. A wall-clock jump of more than LOCK_STALE_S can make a held lock look stale.
"""
import collections
import contextlib
import datetime
import json
import os
import re
import secrets
import time
import urllib.parse

from kitlib.git import refs

DIR_NAME = 'kit-verdicts'
SUFFIX = '.json'
VERDICTS = ('LAND', 'LAND WITH FOLLOW-UP')
RISKS = ('Low', 'Medium', 'High')
QUEUED, FAILED = 'queued', 'failed'
STATES = (QUEUED, FAILED)
# What the coordinator states; add() sets the rest.
GIVEN_KEYS = ('branch', 'tip', 'verdict', 'risk', 'reviewer', 'reviewer_model', 'reviewer_id',
              'author', 'range', 'session', 'records')
SET_KEYS = ('queued_utc', 'state', 'last_result')
KEYS = GIVEN_KEYS + SET_KEYS
TEXT_KEYS = ('reviewer', 'reviewer_model', 'reviewer_id', 'author', 'session')
UTC_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
RANGE_TIP_RE = re.compile(r'^[0-9a-f]{7,64}$')
# DOS device names: a file called CON.json or nul.x.json is the device, not a file.
DEVICES = frozenset(['CON', 'PRN', 'AUX', 'NUL'] + ['COM%d' % i for i in range(1, 10)]
                    + ['LPT%d' % i for i in range(1, 10)])
RETRIES = 5
RETRY_SLEEP = 0.2
LOCK_NAME = '.queue.lock'
LOCK_WAIT_S = 10.0          # how long add, drop and mark_failed wait for the queue lock
LOCK_STALE_S = 60.0         # a lock file older than this was left by a crash
LOCK_POLL_S = 0.05
BY_HAND = 'remove it by hand'

# Why ready() leaves an entry out of the batch (Skip.reason).
SKIP_FAILED = 'failed'      # a landing marked it failed; a re-add queues it again
SKIP_STALE = 'stale'        # the branch's head is not the queued tip (or the branch is gone)
SKIP_HIGH = 'high'          # High risk lands alone
SKIP_STRICT = 'strict'      # its diff touches a strict path (or that cannot be told): lands alone
SKIP_LATER = 'later'        # ready, but the batch is full (batch_max)
SKIP_INVALID = 'invalid'    # not a well-formed entry

Skip = collections.namedtuple('Skip', 'branch reason why entry')


class VerdictError(Exception):
    """An entry is malformed, its file cannot be read or written, or git gave no answer."""


class BadEntry(VerdictError):
    """The entry (or branch name) given is malformed: nothing was read or written."""


class TipMismatch(VerdictError):
    """The tip to queue is not the branch's head (or the branch does not exist)."""


class OtherBranch(VerdictError):
    """The file for the branch named holds another branch's entry."""


def utc(when=None):
    return (when or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _branch_why(branch):
    if not isinstance(branch, str) or not branch.strip():
        return 'the branch is a non-empty name, not %r' % (branch,)
    why = refs.bad_rev(branch)
    if why:
        return 'the branch %s' % why
    if branch != branch.strip() or any(c.isspace() for c in branch):
        return 'the branch name %r holds white space' % branch
    if branch.startswith('refs/'):
        return 'name the branch as git branch does (kit/x), not the ref %s' % branch
    return None


def file_name(branch):
    """The queue file name for `branch`: percent-encoded, then .json."""
    why = _branch_why(branch)
    if why:
        raise BadEntry(why)
    name = urllib.parse.quote(branch, safe='') + SUFFIX
    if name.split('.')[0].upper() in DEVICES:
        raise BadEntry('the branch %s would be the file %s, a Windows device name' % (branch, name))
    return name


def branch_of(name):
    """The branch a queue file name holds, or None when `name` is not one file_name() makes."""
    if not name.endswith(SUFFIX) or name.startswith('.'):
        return None
    branch = urllib.parse.unquote(name[:-len(SUFFIX)])
    try:
        return branch if file_name(branch) == name else None
    except VerdictError:
        return None


def queue_dir(common_dir):
    return os.path.join(common_dir, DIR_NAME)


def entry_path(common_dir, branch):
    return os.path.join(queue_dir(common_dir), file_name(branch))


def check(entry, stored=True):
    """Why `entry` is not a well-formed entry, or None. `stored`: it must carry add()'s keys too
    (an entry read back); False: only the keys the coordinator states (an entry given to add)."""
    if not isinstance(entry, dict):
        return 'an entry is a JSON object, not %s' % type(entry).__name__
    keys = KEYS if stored else GIVEN_KEYS
    missing = [k for k in keys if k not in entry]
    if missing:
        return 'the entry lacks %s' % ', '.join(missing)
    extra = sorted(k for k in entry if k not in keys)
    if extra:
        return 'the entry has keys it may not set: %s' % ', '.join(extra)
    why = _branch_why(entry['branch'])
    if why:
        return why
    tip = entry['tip']
    if not refs.is_sha(tip):
        return 'tip is a full commit hash, not %r' % (tip,)
    if entry['verdict'] not in VERDICTS:
        return 'verdict is one of %s, not %r' % (' | '.join(VERDICTS), entry['verdict'])
    if entry['risk'] not in RISKS:
        return 'risk is one of %s, not %r' % (' | '.join(RISKS), entry['risk'])
    for k in TEXT_KEYS:
        v = entry[k]
        if not isinstance(v, str) or not v.strip() or '\n' in v or '\r' in v:
            return '%s is a non-empty one-line text, not %r' % (k, v)
    rng = entry['range']
    base, sep, right = rng.partition('..') if isinstance(rng, str) else ('', '', '')
    if (not sep or not base or base.startswith('-') or any(c.isspace() for c in rng)
            or not RANGE_TIP_RE.match(right) or not tip.startswith(right)):
        return 'range is <base>..<tip> with the queued tip (or 7+ digits of it) on the right, not %r' % (rng,)
    recs = entry['records']
    if not isinstance(recs, list) or any(not isinstance(r, str) or not r or any(c.isspace() for c in r)
                                         for r in recs):
        return 'records is a list of record ids, not %r' % (recs,)
    if stored:
        if not isinstance(entry['queued_utc'], str) or not UTC_RE.match(entry['queued_utc']):
            return 'queued_utc is YYYY-MM-DDTHH:MM:SSZ, not %r' % (entry['queued_utc'],)
        if entry['state'] not in STATES:
            return 'state is one of %s, not %r' % (' | '.join(STATES), entry['state'])
        if entry['last_result'] is not None and not isinstance(entry['last_result'], str):
            return 'last_result is null or a text, not %r' % (entry['last_result'],)
    return None


def _retry(fn, *args):
    for i in range(RETRIES):
        try:
            return fn(*args)
        except PermissionError:          # Windows: another process holds the file a moment
            if i == RETRIES - 1:
                raise
            time.sleep(RETRY_SLEEP)


def _read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _load(path, branch):
    """`entry` read from `path`; None when there is no file. Raises VerdictError when the file
    cannot be read or is not a well-formed entry for `branch`."""
    try:
        text = _retry(_read_text, path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise VerdictError('cannot read %s: %s' % (path, e))
    try:
        entry = json.loads(text)
    except ValueError as e:
        raise VerdictError('%s is not a verdict entry: %s' % (path, e))
    why = check(entry)
    if why:
        raise VerdictError('%s is not a verdict entry: %s' % (path, why))
    if entry['branch'] != branch:
        raise OtherBranch('%s holds the entry of %s, not %s (names that differ only in case share '
                          'one file on Windows)' % (path, entry['branch'], branch))
    return entry


def _clear_stale(path):
    """Remove the lock file `path` when it was last written more than LOCK_STALE_S ago."""
    try:
        if time.time() - os.path.getmtime(path) > LOCK_STALE_S:
            os.remove(path)
    except OSError:                      # gone already, or its holder is removing it
        pass


@contextlib.contextmanager
def _locked(common_dir):
    """Hold the queue lock (the module docstring says how) for the `with` body."""
    folder = queue_dir(common_dir)
    lock = os.path.join(folder, LOCK_NAME)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        raise VerdictError('cannot make %s: %s' % (folder, e))
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        denied = None
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            _clear_stale(lock)
        except PermissionError as e:     # Windows: a lock file being removed, for a moment
            denied = e
        except OSError as e:
            raise VerdictError('cannot make the queue lock %s: %s' % (lock, e))
        if time.monotonic() >= deadline:
            if denied is not None:
                raise VerdictError('cannot make the queue lock %s in %.0f s: %s'
                                   % (lock, LOCK_WAIT_S, denied))
            raise VerdictError('the verdict queue stayed locked for %.0f s (%s); it is removed by '
                               'the next call once %.0f s old' % (LOCK_WAIT_S, lock, LOCK_STALE_S))
        time.sleep(LOCK_POLL_S)
    try:
        try:
            os.write(fd, ('pid %d at %s\n' % (os.getpid(), utc())).encode('ascii'))
        except OSError:                  # the note is for a reader; the file is the lock
            pass
        finally:
            os.close(fd)
        yield
    finally:
        try:
            _retry(os.remove, lock)
        except OSError:                  # left to the stale-lock rule
            pass


def _write(path, entry):
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, '.%s.%s.tmp' % (os.path.basename(path), secrets.token_hex(4)))
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(entry, sort_keys=True, indent=1) + '\n')
            _retry(os.replace, tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        raise VerdictError('cannot write %s: %s' % (path, e))
    return path


def head_of(common_dir, branch):
    """`refs/heads/<branch>`'s commit; '' when there is no such branch. Raises VerdictError when
    git gives no answer."""
    sha, why = refs.ref_commit('refs/heads/%s' % branch, common_dir)
    if sha is None:
        raise VerdictError('git cannot say where %s is: %s' % (branch, why))
    return sha


def add(common_dir, entry, now=None):
    """Queue `entry` (the GIVEN_KEYS; nothing else) for its branch, replacing an entry already
    queued for it: `(path, stored entry)`. The tip must be `refs/heads/<branch>` in the repository
    whose common dir is `common_dir`, else TipMismatch. `now`: a datetime for queued_utc."""
    why = check(entry, stored=False)
    if why:
        raise BadEntry(why)
    branch, tip = entry['branch'], entry['tip']
    path = entry_path(common_dir, branch)
    head = head_of(common_dir, branch)
    if not head:
        raise TipMismatch('there is no branch %s' % branch)
    if head != tip:
        raise TipMismatch('%s is at %s, not the tip given %s' % (branch, head, tip))
    stored = dict(entry, records=list(entry['records']), queued_utc=utc(now), state=QUEUED,
                  last_result=None)
    with _locked(common_dir):
        _load(path, branch)              # an unreadable entry, or another branch's, is not replaced
        return _write(path, stored), stored


def scan(common_dir):
    """`(entries, bad)`: every well-formed entry, oldest first, and `[(path, why)]` for each file
    in the queue folder that is not one, a temp file a dead write left among them; each why ends
    with how to remove that file (tools/verdict.py drop only where drop removes it)."""
    folder = queue_dir(common_dir)
    if not os.path.isdir(folder):
        return [], []
    with _locked(common_dir):
        try:
            names = sorted(os.listdir(folder))
        except OSError as e:
            raise VerdictError('cannot list %s: %s' % (folder, e))
        return _scan(folder, names)


def _scan(folder, names):
    good, bad = [], []
    for name in names:
        path = os.path.join(folder, name)
        if name == LOCK_NAME:                # ours: scan holds it
            continue
        if name.startswith('.') and name.endswith('.tmp'):
            bad.append((path, 'a temp file a write that died left behind; %s' % BY_HAND))
            continue
        branch = branch_of(name)
        if branch is None:
            bad.append((path, 'not a name a verdict entry file has; %s' % BY_HAND))
            continue
        try:
            entry = _load(path, branch)
        except OtherBranch as e:             # drop refuses it
            bad.append((path, '%s; %s' % (e, BY_HAND)))
            continue
        except VerdictError as e:
            bad.append((path, '%s; remove it with tools/verdict.py drop %s, or by hand'
                        % (e, branch)))
            continue
        if entry is not None:
            good.append(entry)
    return order(good), bad


def entries(common_dir):
    """Every queued entry, oldest first. Raises VerdictError naming each file in the queue folder
    that is not a well-formed entry: a landing takes nothing from a queue it cannot read whole."""
    good, bad = scan(common_dir)
    if bad:
        raise VerdictError('the verdict queue holds files that are not entries: %s'
                           % ' | '.join('%s: %s' % pb for pb in bad))
    return good


def order(items):
    """Oldest first; the branch name settles a tie. A malformed item sorts as text, never raises."""
    def key(e):
        e = e if isinstance(e, dict) else {}
        return str(e.get('queued_utc') or ''), str(e.get('branch') or '')
    return sorted(items, key=key)


def drop(common_dir, branch, tip=None):
    """Remove `branch`'s entry: True when one was removed, False when none is queued (or, with
    `tip`, the entry queued is for another tip and is kept). Without `tip` an unreadable entry
    file is removed too. OtherBranch, and nothing removed, when the file holds another branch's
    entry or, unreadable, has another branch's name in another case."""
    path = entry_path(common_dir, branch)
    with _locked(common_dir):
        try:
            entry = _load(path, branch)
        except OtherBranch:
            raise
        except VerdictError:
            if tip is not None:
                raise
            try:
                names = os.listdir(os.path.dirname(path))
            except OSError as e:
                raise VerdictError('cannot list %s: %s' % (os.path.dirname(path), e))
            if os.path.basename(path) not in names:
                raise OtherBranch('%s is not named for %s (names that differ only in case share '
                                  'one file on Windows): nothing removed' % (path, branch))
        else:
            if entry is None or (tip is not None and entry['tip'] != tip):
                return False
        try:
            _retry(os.remove, path)
        except FileNotFoundError:
            return False
        except OSError as e:
            raise VerdictError('cannot remove %s: %s' % (path, e))
        return True


def mark_failed(common_dir, branch, why, tip=None):
    """Mark `branch`'s entry failed with `why` (kept in last_result): True when marked, False when
    none is queued (or, with `tip`, the entry queued is for another tip and is left as is). A
    failed entry is never in a batch; a re-add queues it again."""
    if not isinstance(why, str) or not why.strip():
        raise VerdictError('say why the landing failed')
    path = entry_path(common_dir, branch)
    with _locked(common_dir):
        entry = _load(path, branch)
        if entry is None or (tip is not None and entry['tip'] != tip):
            return False
        entry.update(state=FAILED, last_result=why.strip())
        _write(path, entry)
        return True


def ready(items, tip_of, strict_hits, batch_max):
    """`(batch, skipped)` from queue entries `items`: the batch is the entries that may land
    together, oldest first, at most `batch_max`; skipped is a Skip(branch, reason, why, entry) for
    every other entry, oldest first.

    Checked in this order, the first that holds naming the reason: not well-formed (invalid);
    state failed (failed); tip_of(branch) is not the queued tip (stale: the branch moved, or
    tip_of gave None because it is gone or git could not say); risk High (high); strict_hits(entry)
    is a non-empty list of paths (strict), or not a list at all (None, False, '' ...), which means
    it cannot be told (strict too); the batch already holds batch_max (later). Only an empty list
    lets an entry in. Nothing here calls git: the caller's tip_of and strict_hits do."""
    if isinstance(batch_max, bool) or not isinstance(batch_max, int) or batch_max < 1:
        raise ValueError('batch_max is a whole number of at least 1, not %r' % (batch_max,))
    batch, skipped = [], []
    for e in order(items):
        why = check(e)
        branch = e.get('branch') if isinstance(e, dict) else None
        if why:
            skipped.append(Skip(branch, SKIP_INVALID, why, e))
            continue
        if e['state'] == FAILED:
            skipped.append(Skip(branch, SKIP_FAILED, 'a landing failed: %s' % e['last_result'], e))
            continue
        now = tip_of(branch)
        if now != e['tip']:
            skipped.append(Skip(branch, SKIP_STALE,
                                'queued at %s, but the branch is %s' % (
                                    e['tip'][:10], now[:10] if now else 'gone or unknown'), e))
            continue
        if e['risk'] == 'High':
            skipped.append(Skip(branch, SKIP_HIGH, 'High risk lands alone', e))
            continue
        hits = strict_hits(e)
        if not isinstance(hits, list):
            skipped.append(Skip(branch, SKIP_STRICT,
                                'cannot tell whether it touches a strict path: lands alone', e))
            continue
        if hits:
            skipped.append(Skip(branch, SKIP_STRICT, 'touches strict path %s: lands alone'
                                % ', '.join(sorted(hits)), e))
            continue
        if len(batch) >= batch_max:
            skipped.append(Skip(branch, SKIP_LATER, 'the batch is full (%d)' % batch_max, e))
            continue
        batch.append(e)
    return batch, skipped
