"""Stale marks: a coordinator's word that an UNLANDED worktree is stale, so the nightly sweep
abandons it under its archive tag (docs/worktree-remove.md "Stale marks").
tools/worktree_remove.py is the entry point (`<branch> --mark-stale "<why>"`, `<branch>
--unmark-stale`); kitlib.worktree.sweep reads the marks and acts on them; kitlib.worktree.classify
shows them.

The marks file is `<git common dir>/kit-stale-marks.json` (FILE_NAME): one per repository, the same
from every work tree and every session, never in a work tree (so it leaves no untracked file) and
never pushed. Its shape:

  {"version": 1, "marks": [{"branch", "tip", "path", "why", "at", "by"}]}

`tip` is the branch's 40-hex tip when it was marked, `path` the worktree git registered for it,
`at` the UTC time of the mark (`YYYY-MM-DDTHH:MM:SS.ffffffZ`), `by` who ran it (the session name
and role the launcher set: for the record only, an agent sets its own environment). A file that
does not parse, or holds a mark of any other shape, is unreadable as a whole: the sweep then acts
on no mark, and marking and clearing refuse. Every write is read-modify-write under the OS lock
`<git common dir>/kit-stale-marks.lock` (kitlib.fs.treelock), written to a temp file and moved over
the old one (kitlib.fs.replace).

`mark` refuses, writing nothing, unless the name is exactly a local branch other than the main
branch, checked out in exactly one registered worktree under a worktree folder (any host's:
remove.is_workstream), present, not exempt, not locked, on its branch template if one is set, with
work NOT in the base, clean (what `--abandon` would refuse), with no link inside, with no backlog
record still pulled by it (an open claim: release or block it first) and not live by
`--classify`'s L rule (classify.branch_liveness). A branch with no worktree is refused and pointed
to `--branch --abandon`.

A mark ends when the sweep abandons its tree, when the sweep finds its branch gone (under
`--apply`), or with `unmark`. It never expires by age. A mark is VOID, kept but not acted on, when
the worktree on its branch is not the one it names, when the tip moved, or when anything in the
tree, its git dir or the branch's reflog is newer than the mark (`void_reason`): mark it again, or
clear it.
"""
import contextlib
import datetime
import json
import os
import re
import tempfile
import time

from kitlib import sessionmark
from kitlib.fs import replace as fsreplace
from kitlib.fs import treelock
from kitlib.git import refs

from . import holders, remove
from .holders import canon
from .remove import OK, REFUSED

FILE_NAME = 'kit-stale-marks.json'
LOCK_NAME = 'kit-stale-marks.lock'
VERSION = 1
FIELDS = ('branch', 'tip', 'path', 'why', 'at', 'by')
AT_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'
LOCK_WAIT_S = 120           # a sweep holds the lock for one abandon at a time
LOCK_PAUSE_S = 0.05
TIP = re.compile(r'^[0-9a-f]{40}$')
SWEEP_IDLE_H = 24           # sweep.SWEEP_IDLE_S in hours, for the line `mark` prints


class MarksError(Exception):
    """The marks file cannot be placed, read, locked or written."""


def stamp(when):
    """`when` (epoch seconds) as a mark's `at`."""
    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).strftime(AT_FORMAT)


def at_epoch(mark):
    """A mark's `at` as epoch seconds; ValueError when it is not AT_FORMAT."""
    when = datetime.datetime.strptime(mark['at'], AT_FORMAT)
    return when.replace(tzinfo=datetime.timezone.utc).timestamp()


def shown_at(mark):
    """A mark's `at` for a line: to the minute."""
    return mark['at'][:16].replace('T', ' ') + ' UTC'


def by(environ=None):
    """Who is marking, for the record: the session name and role the launcher set, or
    `unmarked session`."""
    env = os.environ if environ is None else environ
    name = (env.get(sessionmark.SESSION_NAME_ENV) or '').strip()
    role = sessionmark.mark(env)
    if name and role:
        return '%s (%s)' % (name, role)
    return name or role or 'unmarked session'


def common_dir(ctx):
    """The repository's git common dir; MarksError when git cannot say."""
    try:
        out = remove.read(ctx.main, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    except remove.GitReadError as e:
        raise MarksError('the git common dir cannot be read (%s)' % e)
    return os.path.normpath(out)


def file_of(ctx):
    """`<git common dir>/kit-stale-marks.json`; MarksError when git cannot place it."""
    return os.path.join(common_dir(ctx), FILE_NAME)


def _shape(data, path):
    """None when `data` is a marks document, else why not."""
    if not isinstance(data, dict) or set(data) != {'version', 'marks'}:
        return '%s must be an object holding exactly "version" and "marks"' % path
    if data['version'] != VERSION:
        return '%s has version %r; this kit reads version %d' % (path, data['version'], VERSION)
    if not isinstance(data['marks'], list):
        return '%s: "marks" must be a list' % path
    seen = set()
    for n, mark in enumerate(data['marks']):
        where = '%s mark %d' % (path, n + 1)
        if not isinstance(mark, dict) or set(mark) != set(FIELDS):
            return '%s must hold exactly %s' % (where, ', '.join(FIELDS))
        bad = [k for k in FIELDS if not isinstance(mark[k], str) or not mark[k].strip()]
        if bad:
            return '%s: %s must be a non-empty string' % (where, ', '.join(bad))
        if not TIP.match(mark['tip']):
            return '%s: tip %r is not a full lower-case commit hash' % (where, mark['tip'])
        try:
            at_epoch(mark)
        except ValueError:
            return '%s: at %r is not %s' % (where, mark['at'], AT_FORMAT)
        if mark['branch'] in seen:
            return '%s: branch %s is marked twice' % (where, mark['branch'])
        seen.add(mark['branch'])
    return None


def load(path):
    """`(marks, None)`: the marks in the file at `path`, [] when there is no file; or `(None,
    why)` when it cannot be read or is not a marks document (then no mark is trusted)."""
    try:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
    except FileNotFoundError:
        return [], None
    except (OSError, UnicodeDecodeError) as e:
        return None, 'cannot read %s: %s' % (path, e)
    try:
        data = json.loads(text)
    except RecursionError:
        return None, '%s is nested too deeply to read' % path
    except ValueError as e:
        return None, '%s is not valid JSON: %s' % (path, e)
    why = _shape(data, path)
    if why:
        return None, why
    return [dict(m) for m in data['marks']], None


def save(path, marks):
    """Write `marks` to the file at `path` whole (a temp file moved over it); MarksError."""
    data = {'version': VERSION, 'marks': sorted(marks, key=lambda m: m['branch'])}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=FILE_NAME + '.', suffix='.tmp',
                                   dir=os.path.dirname(path))
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        fsreplace.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise MarksError('cannot write %s: %s' % (path, e))
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


@contextlib.contextmanager
def locked(path, wait=LOCK_WAIT_S):
    """Hold the marks file's write lock (`kit-stale-marks.lock` beside it) for the block. Busy
    for `wait` seconds, or the lock cannot be taken: MarksError. Never nests: a second take in
    one process waits like any other."""
    lock = treelock.TreeLock(os.path.join(os.path.dirname(path), LOCK_NAME),
                             what='stale marks write')
    deadline = time.monotonic() + wait
    while True:
        try:
            lock.acquire()
            break
        except treelock.TreeBusy as e:
            if time.monotonic() >= deadline:
                raise MarksError('%s is %s; waited %s s' % (lock.path, e, wait))
        except treelock.TreeLockError as e:
            raise MarksError(str(e))
        time.sleep(LOCK_PAUSE_S)
    try:
        yield lock.path
    finally:
        lock.release()


def find(marks, branch):
    """The mark of exactly `branch` in `marks`, or None."""
    for mark in marks or ():
        if mark['branch'] == branch:
            return mark
    return None


def claims(ctx, base, branch):
    """`(ids, None)`: the backlog records `state: pulled` whose `pulled_by` is `branch`, matched
    without regard to case as backlog.py matches it; or `(None, why)` (remove.pulled_index)."""
    index, why = remove.pulled_index(ctx, base)
    if index is None:
        return None, why
    want = branch.casefold()
    return sorted(i for b, ids in index.items() if b.casefold() == want for i in ids), None


def void_reason(mark, path, head, newest=None):
    """Why `mark` does not hold for the worktree at `path` whose HEAD is `head`, or None. `newest`,
    when given, is `(epoch seconds, what)` of its last activity (sweep.last_activity): anything
    after the mark voids it."""
    if canon(mark['path']) != canon(path):
        return 'it names the worktree %s, not this one' % os.path.normpath(mark['path'])
    if mark['tip'] != head:
        return 'the tip moved from %s to %s since the mark' % (mark['tip'][:10], (head or '?')[:10])
    if newest is not None and newest[0] > at_epoch(mark):
        return '%s changed at %s, after the mark of %s' % (
            newest[1], stamp(newest[0])[:19].replace('T', ' ') + ' UTC', shown_at(mark))
    return None


def tag_why(mark):
    """The abandon reason the sweep gives a marked tree (its archive tag's message)."""
    return 'stale (marked %s by %s): %s' % (shown_at(mark), mark['by'], mark['why'])


def _liveness(top, branch):
    from . import classify          # classify imports sweep, which imports this module
    return classify.branch_liveness(top, branch)


def _refuse(ctx, line):
    ctx.say('refused   %s; nothing was marked' % line)
    return ctx.outcome(REFUSED)


def mark(top, branch, why, cwd=None, liveness=None, environ=None, now=None):
    """`<branch> --mark-stale "<why>"`: record that the worktree on `branch` is stale, for the
    nightly sweep (the module docstring has every check). `liveness(main, branch)` is
    classify.branch_liveness's seam; `now` (epoch seconds) the mark's time. Outcome(code, lines);
    a mark already there is replaced."""
    ctx, refused = remove.open_ctx(top, cwd)
    if refused:
        return refused
    s, main = ctx.s, ctx.main
    why = (why or '').strip()
    if not why:
        return _refuse(ctx, '--mark-stale needs a reason: say why this work is stale (it becomes'
                            ' the archive tag\'s message)')
    tip, err = remove.local_branch(main, branch)
    if err:
        return _refuse(ctx, err)
    if tip is None:
        return _refuse(ctx, 'there is no local branch %s' % branch)
    if remove.same_name(branch, s['base']):
        return _refuse(ctx, '%s is the main branch' % branch)
    try:
        listing = remove.worktree_listing(main)
    except remove.GitReadError as e:
        return _refuse(ctx, 'the worktree list could not be read (%s)' % e)
    on = [e for e in remove.worktree_entries(listing) if e['branch'] == branch]
    if not on:
        return _refuse(ctx, 'no worktree is on %s: a stale mark is for a worktree the nightly sweep'
                            ' abandons. A branch with no worktree is abandoned by hand, on the'
                            ' PM\'s word: %s %s --branch --abandon "<why>"'
                       % (branch, s['tool'], branch))
    if len(on) > 1:
        return _refuse(ctx, 'branch %s is checked out at %s' % (
            branch, ', '.join(sorted(os.path.normpath(e['path']) for e in on))))
    entry = on[0]
    path = os.path.normpath(entry['path'])
    if canon(path) == canon(main):
        return _refuse(ctx, '%s is checked out in the main checkout' % branch)
    if not remove.is_workstream(ctx, path):
        return _refuse(ctx, '%s is not under a worktree folder (%s): the sweep takes only'
                            ' workstream worktrees' % (path, remove.home_of(ctx)))
    ruling = remove.exemption(s, path)
    if ruling:
        return _refuse(ctx, '%s is exempt (%s)' % (path, ruling))
    if not os.path.isdir(path):
        return _refuse(ctx, '%s is missing (an orphan: `--check` reports it)' % path)
    if entry['locked'] is not None:
        return _refuse(ctx, '%s is locked with `git worktree lock`%s' % (
            path, ': ' + entry['locked'] if entry['locked'] else ''))
    if entry['head'] != tip:
        return _refuse(ctx, 'git lists %s at %s but its tree at %s' % (
            branch, tip[:10], (entry['head'] or '?')[:10]))
    if s['branch_template']:
        wants = [t.replace('{slug}', os.path.basename(path)) for t in s['branch_template']]
        if branch not in wants:
            return _refuse(ctx, '%s is on %s, not %s (.kit.json worktree_remove.branch_template):'
                                ' --abandon would refuse it' % (path, branch, ' or '.join(wants)))
    base, err = remove.base_commit(ctx)
    if base is None:
        return _refuse(ctx, 'cannot read %s (%s)' % (s['upstream'], err))
    merged, err = refs.is_ancestor(tip, base, main)
    if merged is None:
        return _refuse(ctx, 'the merge check failed (%s)' % err)
    if merged:
        return _refuse(ctx, '%s is already in %s %s: landed work needs no mark (the plain verb or'
                            ' the nightly sweep removes it)' % (branch, s['upstream'], base[:10]))
    work, disposable, ignored, err = remove.dirty(ctx, path)
    if work is None:
        return _refuse(ctx, '%s: its status could not be read (%s)' % (path, err))
    if work:
        return _refuse(ctx, '%s has uncommitted or untracked files (%d): %s%s - `--abandon` keeps'
                            ' commits only and refuses it' % (
                                path, len(work), '; '.join(work[:5]), ' ...' if len(work) > 5 else ''))
    if disposable:
        # The sweep counts these as dirt (sweep.sweep_status), so a mark would never be acted on.
        lines = ['?? ' + rel for rel in disposable]
        return _refuse(ctx, '%s has untracked files (%d): %s%s - they match'
                            ' worktree_remove.disposable_untracked, but the nightly sweep counts'
                            ' them as dirt and never abandons the tree; delete them first' % (
                                path, len(lines), '; '.join(lines[:5]),
                                ' ...' if len(lines) > 5 else ''))
    if ignored:
        return _refuse(ctx, '%s has %s' % (path, remove.ignored_reason(ignored, path)))
    link = holders.link_reason(path)
    if link:
        return _refuse(ctx, '%s: %s' % (path, link))
    ids, err = claims(ctx, base, branch)
    if ids is None:
        return _refuse(ctx, 'the open-claim check failed (%s)' % err)
    if ids:
        return _refuse(ctx, 'a backlog record is still pulled by %s (an open claim): %s. Release'
                            ' it (`backlog.py release <ID> --reason "<why>"`) or block it first'
                       % (branch, ', '.join(ids)))
    live, _ = (liveness or _liveness)(main, branch)
    if live:
        return _refuse(ctx, '%s is live by --classify\'s L rule: %s' % (branch, live))
    record = {'branch': branch, 'tip': tip, 'path': path, 'why': why,
              'at': stamp(time.time() if now is None else now), 'by': by(environ)}
    try:
        where = file_of(ctx)
        with locked(where):
            marks, err = load(where)
            if marks is None:
                return _refuse(ctx, 'the stale marks cannot be read (%s): fix or delete the file'
                               % err)
            old = find(marks, branch)
            save(where, [m for m in marks if m['branch'] != branch] + [record])
    except MarksError as e:
        return _refuse(ctx, str(e))
    ctx.say('%s %s at %s (%s): stale: %s' % (
                'remarked ' if old else 'marked   ', branch, tip[:10], path, why),
            '  sweep    the nightly sweep abandons it under %s%s (the tag keeps the commits) once'
            ' it is idle %d h, if nothing in it changes first' % (
                remove.ARCHIVE, os.path.basename(path), SWEEP_IDLE_H),
            '  clear    %s %s --unmark-stale' % (s['tool'], branch))
    return ctx.outcome(OK)


def unmark(top, branch, cwd=None):
    """`<branch> --unmark-stale`: drop the stale mark of exactly `branch`. No mark: `nothing`,
    exit 0. Outcome(code, lines)."""
    ctx, refused = remove.open_ctx(top, cwd)
    if refused:
        return refused
    try:
        where = file_of(ctx)
        with locked(where):
            marks, err = load(where)
            if marks is None:
                ctx.say('refused   the stale marks cannot be read (%s): fix or delete the file;'
                        ' nothing was cleared' % err)
                return ctx.outcome(REFUSED)
            old = find(marks, branch)
            if old is None:
                ctx.say('nothing   %s has no stale mark' % branch)
                return ctx.outcome(OK)
            save(where, [m for m in marks if m['branch'] != branch])
    except MarksError as e:
        ctx.say('refused   %s; nothing was cleared' % e)
        return ctx.outcome(REFUSED)
    ctx.say('cleared   the stale mark of %s (marked %s by %s: %s)' % (
        branch, shown_at(old), old['by'], old['why']))
    return ctx.outcome(OK)
