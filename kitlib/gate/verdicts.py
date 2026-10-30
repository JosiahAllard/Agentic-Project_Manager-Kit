"""The verdict queue: reviewed branches waiting to land, one file per branch.

A coordinator queues a branch after reading its review (tools/verdict.py add); a reviewer never
does, so no reviewer can queue its own verdict. After a main sync of a queued branch the
coordinator carries the verdict to the new head (tools/verdict.py carry) when the sync is all that
changed (kitlib.gate.carry). A landing reads its branch's entry and never removes it;
tools/verdict.py list drops each entry that landed or whose branch is gone (expire()).

Where: `<git common dir>/kit-verdicts/<branch>.json`, the branch name percent-encoded
(urllib.parse.quote with no safe characters: '/' is '%2F', '%' is '%25'), so the file name gives
the branch back and holds no character Windows refuses. The common dir is shared by every work
tree of the repository and never tracked. Every write is a temp file in the same folder, then
os.replace; a re-add for the same branch replaces its entry.

The queue lock: add, drop, mark_failed and scan each run whole (read, check, then write or
remove) holding `<queue folder>/.queue.lock`, a file made with O_CREAT|O_EXCL and removed when
the call ends, so none of them acts on an entry another replaced between its read and its write.
carry() runs its git check between two holds and writes only when the entry and the branch's head
are, in the second, what they were in the first. A call waits up to LOCK_WAIT_S for the lock, then raises VerdictError and does nothing. A
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
  reviewer_model  the model the reviewer ran on; tools/verdict.py add checks it against the
                  reviewer's transcript where the host has a reader (model_mismatch_why)
  reviewer_id     the reviewer's agent or session id
  author          the change's author: commit author and agent type (`Kit Test / worker`); it
                  picks no reviewer model: a strict branch needs a strong-tier reviewer
                  whoever built it, and off the strict paths there is no tier rule
                  (kitlib.gate.land, Lander.review_refusal)
  range           the range reviewed, `<base>..<tip>`; its right side is the tip or a prefix of
                  it (7 or more hex digits), so the review covered the commit queued
  session         the coordinator session that queued it
  records         [backlog record ids] the branch carries
  queued_utc      set by add(): YYYY-MM-DDTHH:MM:SSZ
  state           queued | failed (mark_failed)
  last_result     None, or why it was marked failed

and, only in an entry carry() moved (a re-add drops it):

  carried         {from, merges, main, diff, utc}: the verdict was given at `from` (the commit the
                  range ends at), and tip carries it over `merges` (main merges, oldest first, the
                  last the tip) as kitlib.gate.carry checked against `main` at `utc`; `diff` is the
                  sha256 of the branch's normalized patch. A record of the check, never proof: a
                  landing checks it again (docs/gate.md "Carrying a verdict forward").

Errors (all VerdictError): BadEntry for a malformed entry or branch name given, nothing read or
written; SelfReview (a BadEntry) when add() is given a High-risk verdict the coordinator queueing
it reviewed itself (self_review_why), nothing read or written, or, with "landing".
self_review_max_lines given, a self-read of a branch over that many changed lines or one whose
size cannot be told (self_review_size_why), nothing written; ModelMismatch (a BadEntry) when
add() is given the models the reviewer's transcript shows (seen_models, the host's reader) and one
is not the reviewer_model stated (model_mismatch_why), nothing written; TipMismatch when the tip is not the branch's head, or there is no such branch;
CarryRefused when carry() finds the head does not carry the verdict, nothing written;
OtherBranch when the file for the branch named holds another branch's entry (names that differ
only in case share one file on Windows); VerdictError itself for a queue file that cannot be read
or written, or is not an entry, the queue lock not had in time, or git giving no answer. Nothing
here prints.

Residual race (inference, not tested): two callers that find the same stale lock at once can
both remove it, the second removing the lock the first just made; that needs a holder to have
died first. A wall-clock jump of more than LOCK_STALE_S can make a held lock look stale.
"""
import contextlib
import datetime
import json
import os
import re
import secrets
import time
import urllib.parse

from kitlib.fs import replace as fsreplace
from kitlib.gate import carry as carrymod
from kitlib.git import cmd as gitcmd
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
CARRIED = 'carried'         # set only by carry(): an entry may lack it
CARRIED_KEYS = ('from', 'merges', 'main', 'diff', 'utc')
DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
TEXT_KEYS = ('reviewer', 'reviewer_model', 'reviewer_id', 'author', 'session')
UTC_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
RANGE_TIP_RE = re.compile(r'^[0-9a-f]{7,64}$')
# DOS device names: a file called CON.json or nul.x.json is the device, not a file.
DEVICES = frozenset(['CON', 'PRN', 'AUX', 'NUL'] + ['COM%d' % i for i in range(1, 10)]
                    + ['LPT%d' % i for i in range(1, 10)])
RETRIES = 5                 # tries of a read, replace or remove while it fails with PermissionError
RETRY_SLEEP = 0.2           # seconds between them
# RETRIES as a wait for kitlib.fs.replace: try k starts at least k-1 pauses in, so the last (at
# 0.8 s or later) is past this bound and the one before it (at 0.6 s) is not; the half pause is
# the margin for a slow try.
REPLACE_WAIT = (RETRIES - 1.5) * RETRY_SLEEP
LOCK_NAME = '.queue.lock'
LOCK_WAIT_S = 10.0          # how long add, drop and mark_failed wait for the queue lock
LOCK_STALE_S = 60.0         # a lock file older than this was left by a crash
LOCK_POLL_S = 0.05
BY_HAND = 'remove it by hand'


class VerdictError(Exception):
    """An entry is malformed, its file cannot be read or written, or git gave no answer."""


class BadEntry(VerdictError):
    """The entry (or branch name) given is malformed: nothing was read or written."""


class SelfReview(BadEntry):
    """A High-risk verdict whose reviewer is the coordinator queueing it (self_review_why): its
    text ends with the Instead: line (SELF_REVIEW_INSTEAD). Nothing was read or written."""


class ModelMismatch(BadEntry):
    """The reviewer's transcript shows a model other than the reviewer_model given
    (model_mismatch_why): its text ends with an Instead: line. Nothing was read or written."""


class TipMismatch(VerdictError):
    """The tip to queue is not the branch's head (or the branch does not exist)."""


class OtherBranch(VerdictError):
    """The file for the branch named holds another branch's entry."""


class CarryRefused(VerdictError):
    """The branch's head does not carry its queued verdict (carry()): nothing was written."""


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


def reviewer_types(text):
    """The agent types a verdict's `reviewer` names, casefolded: each part split on '/', ',' or
    ';', stripped, a `plugin:` prefix dropped, empty ones left out (`pm-kit:` names none). Pure."""
    return set(p.strip().rpartition(':')[2].strip().casefold()
               for p in re.split(r'[/,;]', text)) - {''}


SELF_REVIEW_INSTEAD = ('Instead: dispatch a fresh analyst to review the branch (skill branch-review, '
                       'step 3), then queue its verdict with --reviewer pm-kit:analyst '
                       '--reviewer-id <its agent id>')
COORDINATOR_RE = re.compile(r'(?<![a-z0-9])coordinator(?![a-z0-9])')


def self_reader(entry):
    """How `entry`'s reviewer is the coordinator queueing it, or None: an id `reviewer_id` lists
    (split as land.is_author splits it) is `session`, case and outer space aside, or an agent type
    the reviewer names (reviewer_types) holds `coordinator` as a word (`pm-kit:coordinator`,
    `kit-coordinator`). `entry` is well formed (check). Pure."""
    session = entry['session'].strip().casefold()
    ids = [i for i in re.split(r'[/,;\s]+', entry['reviewer_id'].casefold()) if i]
    if session in ids:
        return 'its reviewer id %s is the session queueing it' % entry['reviewer_id'].strip()
    if any(COORDINATOR_RE.search(t) for t in reviewer_types(entry['reviewer'])):
        return 'its reviewer %s is the coordinator role' % entry['reviewer'].strip()
    return None


def self_review_why(entry):
    """Why add() refuses `entry` as a self-review, or None. Refused: a High-risk verdict whose
    reviewer is the coordinator queueing it (self_reader). A Low or Medium one is not: a
    coordinator may read a small branch itself (skill branch-review, step 3), up to
    "landing".self_review_max_lines when that is set (self_review_size_why). `entry` is well
    formed (check). Pure."""
    if entry['risk'] != 'High':
        return None
    who = self_reader(entry)
    if not who:
        return None
    return ('a High-risk verdict needs a reviewer other than the coordinator queueing it, and %s '
            '(skill branch-review, step 3); nothing was queued\n%s' % (who, SELF_REVIEW_INSTEAD))


def diff_lines(cwd, rng):
    """`(added, removed, files)` the range `rng` (`<base>..<tip>`) changes, counted as
    `git diff --numstat --find-renames <base>...<tip>` counts them: from the merge base of the two
    to the tip, so commits the base gained after the branch left it are not counted. A binary file
    is one file and no lines. Git runs in `cwd` (a git dir does). VerdictError when git gives no
    answer or prints a line that is not a numstat row."""
    base, _, tip = rng.partition('..')
    for rev in (base, tip):
        why = refs.bad_rev(rev)
        if why:
            raise VerdictError('the range %s: %s' % (rng, why))
    out, why = gitcmd.run_text(['diff', '--numstat', '--find-renames', '%s...%s' % (base, tip),
                                '--'], cwd)
    if out is None:
        raise VerdictError('git diff --numstat %s...%s: %s' % (base, tip, (why or '').strip()))
    added = removed = files = 0
    for row in out.splitlines():
        a, sep, rest = row.partition('\t')
        d, sep2, _ = rest.partition('\t')
        if not (sep and sep2):
            raise VerdictError('git diff --numstat printed %r, not a numstat row' % row[:80])
        if (a, d) != ('-', '-'):
            if not (a.isdigit() and d.isdigit()):
                raise VerdictError('git diff --numstat printed %r, not a numstat row' % row[:80])
            added, removed = added + int(a), removed + int(d)
        files += 1
    return added, removed, files


def self_review_size_why(entry, max_lines, cwd, main, main_why=None):
    """Why add() refuses `entry` as a self-read too large for the coordinator to give, or None.
    None whenever `max_lines` ("landing".self_review_max_lines) is None or the reviewer is not the
    coordinator queueing it (self_reader). Else the size is the larger of two counts (diff_lines,
    git run in `cwd`): the range given, and `main...<tip>` with `main` main as fetched, so a base
    stated past most of the branch (`<tip>~1..<tip>`) does not shrink it. Refused when that is
    more than `max_lines`, or when it cannot be told (`main` None, `main_why` saying why, or git
    gives no answer): with a cap set, a size that cannot be told is not a small branch. `entry` is
    well formed (check)."""
    if max_lines is None:
        return None
    who = self_reader(entry)
    if not who:
        return None
    head = ('a verdict the coordinator queueing it reviewed itself covers at most %d changed lines '
            '("landing".self_review_max_lines), and ' % max_lines)
    tail = '; %s; nothing was queued\n%s' % (who, SELF_REVIEW_INSTEAD)
    if not main:
        return head + ('main as fetched cannot be read (%s), so the size of %s cannot be told'
                       % (main_why or 'not given', entry['range'])) + tail
    tip = entry['tip'][:10]
    counts = []
    for rng in (entry['range'], '%s..%s' % (main, entry['tip'])):
        try:
            counts.append(diff_lines(cwd, rng))
        except VerdictError as e:
            return head + 'git cannot tell how many lines %s changes (%s)' % (rng, e) + tail
    added, removed, files = max(counts, key=lambda c: c[0] + c[1])
    if added + removed <= max_lines:
        return None
    return head + ('%s changes %d (+%d -%d in %d file%s; the larger of the range given and %s...%s '
                   'from main as fetched)' % (entry['range'], added + removed, added, removed,
                                              files, '' if files == 1 else 's', main[:10],
                                              tip)) + tail


def _reviewer_ids(text):
    """The ids a verdict's `reviewer_id` lists, split as land.is_author splits them. Pure."""
    return [i for i in re.split(r'[/,;\s]+', text) if i]


def seen_models(entry, reader):
    """`{id: [models]}`: for each id `entry`'s reviewer_id lists that `reader` takes as an agent
    id, the models its transcript shows. `reader` is a host's transcript reader (on Claude
    hosts.claude.adapter.agent_models.models): id -> sorted models, [] for an agent id with no
    transcript, None for an id that is not an agent id (a session name), which is left out. {}
    when there is no reader (a host with none, Codex) or the reviewer is the coordinator queueing
    it (self_reader): a self-review is no subagent. `entry` is well formed (check)."""
    if reader is None or self_reader(entry):
        return {}
    out = {}
    for rid in _reviewer_ids(entry['reviewer_id']):
        got = reader(rid)
        if got is not None:
            out[rid] = list(got)
    return out


def model_mismatch_why(entry, seen):
    """Why add() refuses `entry` because its reviewer ran on another model than its reviewer_model
    says, or None. `seen` is seen_models(): a model seen matches when it is reviewer_model (case
    and outer space aside) or names the same tier model of hosts.json (land.named_model:
    `claude-haiku-5-5` names haiku). Refused when any model seen for any id does not match; an id
    with none seen (no transcript) is not checked. `entry` is well formed (check)."""
    from kitlib.gate import land          # land imports this module: a late import
    strong, weak, _ = land.tier_models()
    strong, weak = strong or set(), weak or set()
    typed = entry['reviewer_model'].strip()
    want = land.named_model(typed, strong, weak)

    def same(model):
        return (model.casefold() == typed.casefold()
                or (want is not None and land.named_model(model, strong, weak) == want))
    off = [(rid, models) for rid, models in sorted(seen.items())
           if models and not all(same(m) for m in models)]
    if not off:
        return None
    ran = sorted(set(m for _, models in off for m in models))
    said = '; '.join('the reviewer %s ran on %s' % (rid, ' and '.join(models))
                     for rid, models in off)
    if len(ran) == 1:
        use = 'queue it with --reviewer-model %s, the model it ran on' % (
            land.named_model(ran[0], strong, weak) or ran[0])
    else:
        use = 'queue it with --reviewer-model naming the model the review ran on (%s)' % ', '.join(ran)
    tier = ' or '.join(sorted(strong - weak)) or 'a strong-tier model'
    return ('--reviewer-model %s, but %s (its transcript): an agent dispatched with no model '
            'override runs on its role\'s default tier; nothing was queued\nInstead: %s; a branch '
            'land.py --check-strict calls STRICT needs a review on %s: dispatch a fresh analyst '
            'with model %s (skill branch-review)' % (typed, said, use, tier, tier))


def check(entry, stored=True):
    """Why `entry` is not a well-formed entry, or None. `stored`: it must carry add()'s keys too
    (an entry read back; it may hold `carried`); False: only the keys the coordinator states (an
    entry given to add)."""
    if not isinstance(entry, dict):
        return 'an entry is a JSON object, not %s' % type(entry).__name__
    keys = KEYS if stored else GIVEN_KEYS
    missing = [k for k in keys if k not in entry]
    if missing:
        return 'the entry lacks %s' % ', '.join(missing)
    extra = sorted(k for k in entry if k not in keys and not (stored and k == CARRIED))
    if extra:
        return 'the entry has keys it may not set: %s' % ', '.join(extra)
    why = _branch_why(entry['branch'])
    if why:
        return why
    tip = entry['tip']
    if not refs.is_sha(tip):
        return 'tip is a full commit hash, not %r' % (tip,)
    reviewed = tip
    if CARRIED in entry:
        why = _carried_why(entry[CARRIED], tip)
        if why:
            return why
        reviewed = entry[CARRIED]['from']
    if entry['verdict'] not in VERDICTS:
        return 'verdict is one of %s, not %r' % (' | '.join(VERDICTS), entry['verdict'])
    if entry['risk'] not in RISKS:
        return 'risk is one of %s, not %r' % (' | '.join(RISKS), entry['risk'])
    for k in TEXT_KEYS:
        v = entry[k]
        if not isinstance(v, str) or not v.strip() or '\n' in v or '\r' in v:
            return '%s is a non-empty one-line text, not %r' % (k, v)
    if not reviewer_types(entry['reviewer']):
        # Fail closed: with no type, land.is_author could not tell the reviewer from the author.
        return ('reviewer names no agent type: it is the reviewer\'s agent type '
                '(pm-kit:analyst), not %r' % entry['reviewer'])
    rng = entry['range']
    base, sep, right = rng.partition('..') if isinstance(rng, str) else ('', '', '')
    if (not sep or not base or base.startswith('-') or any(c.isspace() for c in rng)
            or not RANGE_TIP_RE.match(right) or not reviewed.startswith(right)):
        return ('range is <base>..<tip> with the %s (or 7+ digits of it) on the right, not %r'
                % ('reviewed commit the verdict was carried from' if reviewed != tip
                   else 'queued tip', rng))
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


def _carried_why(c, tip):
    """Why `c` is not the `carried` record of an entry queued at `tip`, or None."""
    if not isinstance(c, dict) or sorted(c) != sorted(CARRIED_KEYS):
        return 'carried is an object of %s, not %r' % (', '.join(CARRIED_KEYS), c)
    for k in ('from', 'main'):
        if not refs.is_sha(c[k]):
            return 'carried.%s is a full commit hash, not %r' % (k, c[k])
    if c['from'] == tip:
        return 'carried.from is the commit reviewed, not the tip %s it was carried to' % tip[:10]
    merges = c['merges']
    if (not isinstance(merges, list) or not merges or any(not refs.is_sha(m) for m in merges)
            or merges[-1] != tip):
        return 'carried.merges is the main merges carried over, the last the tip, not %r' % (merges,)
    if not isinstance(c['diff'], str) or not DIGEST_RE.match(c['diff']):
        return 'carried.diff is a sha256 in hex, not %r' % (c['diff'],)
    if not isinstance(c['utc'], str) or not UTC_RE.match(c['utc']):
        return 'carried.utc is YYYY-MM-DDTHH:MM:SSZ, not %r' % (c['utc'],)
    return None


def _retry(fn, *args):
    """fn(*args), tried RETRIES times RETRY_SLEEP apart while it fails with PermissionError. Kept
    for the entry read (kitlib.fs.replace has no read) and drop()'s remove (a file already gone
    is False there, where kitlib.fs.replace.remove returns as on success)."""
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
            fsreplace.remove(lock, wait=REPLACE_WAIT, pause=RETRY_SLEEP)
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
            fsreplace.replace(tmp, path, wait=REPLACE_WAIT, pause=RETRY_SLEEP)
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


def add(common_dir, entry, now=None, self_review_max_lines=None, main=None, main_why=None,
        seen=None):
    """Queue `entry` (the GIVEN_KEYS; nothing else) for its branch, replacing an entry already
    queued for it: `(path, stored entry)`. The tip must be `refs/heads/<branch>` in the repository
    whose common dir is `common_dir`, else TipMismatch. A High-risk self-review is SelfReview
    (self_review_why); so is a self-read of a branch over `self_review_max_lines` changed lines, or
    one whose size cannot be told, when that cap ("landing".self_review_max_lines) is given
    (self_review_size_why; `main`: main as fetched, a commit, or None with `main_why`). `seen`:
    seen_models() for the entry; a model seen that is not its reviewer_model is ModelMismatch
    (model_mismatch_why). Nothing seen is stored. `now`: a datetime for queued_utc."""
    why = check(entry, stored=False)
    if why:
        raise BadEntry(why)
    why = self_review_why(entry)
    if why:
        raise SelfReview(why)
    why = model_mismatch_why(entry, seen) if seen else None
    if why:
        raise ModelMismatch(why)
    branch, tip = entry['branch'], entry['tip']
    path = entry_path(common_dir, branch)
    head = head_of(common_dir, branch)
    if not head:
        raise TipMismatch('there is no branch %s' % branch)
    if head != tip:
        raise TipMismatch('%s is at %s, not the tip given %s' % (branch, head, tip))
    why = self_review_size_why(entry, self_review_max_lines, common_dir, main, main_why)
    if why:
        raise SelfReview(why)
    stored = dict(entry, records=list(entry['records']), queued_utc=utc(now), state=QUEUED,
                  last_result=None)
    with _locked(common_dir):
        _load(path, branch)              # an unreadable entry, or another branch's, is not replaced
        return _write(path, stored), stored


def carry(common_dir, root, branch, base, now=None, checker=None):
    """Move `branch`'s queued verdict to the branch's head when the head carries it
    (docs/gate.md "Carrying a verdict forward"): `(path, stored entry)`, with tip the head
    and `carried` the facts kitlib.gate.carry.check gave for the commit first reviewed (the
    entry's carried.from, else its tip), the head and `base` (main as fetched, a full hash).
    `root`: a path in the repository, where git runs. None when nothing is queued for `branch`.

    CarryRefused, nothing written, when the entry is marked failed, is already at the head, or the
    head does not carry it; TipMismatch when there is no such branch; VerdictError when the entry
    or the branch moved while the check ran (outside the queue lock: git may be slow). `checker`:
    the check to run (default kitlib.gate.carry.check); `now`: a datetime for carried.utc."""
    path = entry_path(common_dir, branch)
    with _locked(common_dir):
        entry = _load(path, branch)
    if entry is None:
        return None
    if entry['state'] != QUEUED:
        raise CarryRefused('the verdict of %s is marked %s (%s): review and queue it again'
                           % (branch, entry['state'], entry['last_result'] or 'no reason given'))
    head = head_of(common_dir, branch)
    if not head:
        raise TipMismatch('there is no branch %s' % branch)
    if head == entry['tip']:
        raise CarryRefused('the verdict of %s is queued at its head %s already: nothing to carry'
                           % (branch, head[:10]))
    reviewed = entry[CARRIED]['from'] if CARRIED in entry else entry['tip']
    facts, why = (checker or carrymod.check)(root, reviewed, head, base)
    if facts is None:
        raise CarryRefused('%s at %s does not carry the verdict given at %s: %s'
                           % (branch, head[:10], reviewed[:10], why))
    stored = dict(entry, tip=head, carried=dict(facts, utc=utc(now)))
    why = check(stored)
    if not why and (stored[CARRIED]['from'] != reviewed or stored[CARRIED]['main'] != base):
        why = 'carried.from or carried.main is not the commit asked about'
    if why:
        raise CarryRefused('the carry check gave facts that are not a carried record: %s' % why)
    with _locked(common_dir):
        if _load(path, branch) != entry:
            raise VerdictError('the verdict of %s changed while its carry was checked: run carry '
                               'again' % branch)
        if head_of(common_dir, branch) != head:
            raise VerdictError('%s moved while its carry was checked: run carry again' % branch)
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


def expired(common_dir, entry, main):
    """Why `entry` waits to land no more, or None: `landed` when its tip is an ancestor of `main`
    (main as fetched, a commit), `superseded` when it is not and `refs/heads/<branch>` is gone.
    None, the entry kept, whenever that cannot be told: no `main`, or git gives no answer."""
    if not main:
        return None
    landed, _ = refs.is_ancestor(entry['tip'], main, common_dir)
    if landed:
        return 'landed: %s is in main %s' % (entry['tip'][:10], main[:10])
    if landed is None:
        return None
    head, _ = refs.ref_commit('refs/heads/%s' % entry['branch'], common_dir)
    if head == '':
        return 'superseded: there is no branch %s' % entry['branch']
    return None


def expire(common_dir, items, main):
    """Drop each of the entries `items` that expired(): `[(entry, why)]` for those dropped. Git runs
    outside the queue lock; each drop is drop(tip=the entry's tip), so an entry re-added at another
    tip meanwhile is kept, and one that cannot be dropped (VerdictError) is kept too."""
    gone = []
    for e in items:
        why = expired(common_dir, e, main)
        if not why:
            continue
        try:
            if drop(common_dir, e['branch'], tip=e['tip']):
                gone.append((e, why))
        except VerdictError:
            continue
    return gone
