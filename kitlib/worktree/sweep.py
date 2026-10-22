"""The nightly sweep: remove every registered worktree that has landed, has been idle
`idle_s` (24 h) and that nothing holds; a dry run unless `apply` (ported from the PM kit's
`worktree_remove.py --sweep-merged`; docs/worktree-remove.md). Returns Outcome(code, lines).

Run-wide, each failing closed (exit 1, nothing removed), in this order: the landing lock free
(`landing_hold`: a landing moves the base, so it is read before anything is fetched); `git fetch`
of the base; the worktree list; the machine lock readable (`machine_lock_entries`); the base and
its history. Under `apply` the landing lock is read again just before each removal; held then,
the sweep stops (exit 1), what it already removed standing.

Per worktree (`sweep_skip`, the first failing check named): the main checkout, anything outside
the worktrees dir, `exempt`, a detached HEAD, a missing directory and a `git worktree lock` are
never swept. Landed is `remove.merged_worktrees`'s rule: the tip in the base AND off its
first-parent chain, so a fresh worktree is never swept. Then: its branch checked out elsewhere; a
machine lock record naming it; a backlog record `state: pulled` with `pulled_by` its branch (an
open claim); last activity under `idle_s`; any uncommitted, untracked or ignored-not-build-output
file; then the holder probe; and with `apply`, last, the status read again. A swept worktree goes
through `remove.git_remove` (never retried, never forced) and `remove.delete_branch` (`-d`).

Stale marks (kitlib.worktree.marks): an UNLANDED worktree a coordinator marked stale is ABANDONED
instead, by `remove._abandon` (its archive tag first, then the tree, then the branch), after the
same checks but the landed and fresh ones; its open-claim check matches the branch without regard
to case. A mark is void (the tree is skipped as UNLANDED, the mark kept) when the tree is not the
one it names, its tip moved, or its last activity is newer than the mark. The marks are read once,
after the base: a marks file that cannot be read counts as a failure (exit 1) and no marked tree is
acted on, while the landed sweep goes on. Under `apply`, the abandon runs under the marks lock and
only while the mark is still the one read; it is held to the checked tip (`expect`), and the mark is
dropped once the abandon is done. Last, a mark whose branch is gone is dropped (`apply`), and a
mark whose branch has no worktree is named for `--unmark-stale`.
"""
import datetime
import json
import os
import subprocess
import sys
import time

from kitlib import kitroot
from kitlib.config import ConfigError, user
from kitlib.proc import spawn
from kitlib.records import landing

from . import holders, marks, remove
from .holders import canon, extended, plain, prune, WALK_MAX
from .remove import OK, REFUSED, failure, git

SWEEP_IDLE_S = 24 * 3600            # idle 24h+
FETCH_TIMEOUT_S = 300


def sweep_log_name(ctx):
    """`<repo name>-worktree-sweep.log` (`"repo".name`, else the main checkout's folder name)."""
    return '%s-worktree-sweep.log' % ctx.s['name']


def fetch_args(s):
    """`git fetch` of the base with an explicit refspec, so the tracking ref is updated whatever
    the remote's configured fetch refspec says; `+`: the base reads as it stands on the remote."""
    return ('fetch', '--no-tags', s['remote'],
            '+refs/heads/%s:%s' % (s['base'], remove.merge_base_ref(s)))


def fetch_base(ctx, timeout=FETCH_TIMEOUT_S):
    """Update the base from its remote. None when it worked, else why. Never prompts
    (GIT_TERMINAL_PROMPT=0, GCM_INTERACTIVE=never): a scheduled task has no terminal."""
    args = fetch_args(ctx.s)
    rc, _, err = git(ctx.main, *args, timeout=timeout,
                     env=dict(remove.git_env(), GIT_TERMINAL_PROMPT='0'))
    if rc != 0:
        return failure(args, rc, err)
    return None


def landing_hold(ctx):
    """None when the landing lock is free, else why the sweep defers: held, unreadable, or its
    place unknown. The lock is kitlib.records.landing's (`<common dir>/kit-landing.lock`),
    the one every landing takes."""
    try:
        lock = landing.LandingLock.for_config(ctx.main, ctx.cfg)
    except (ConfigError, landing.LockError) as e:
        return 'the landing lock cannot be placed (%s)' % e
    try:
        held = lock.read()
    except landing.LockError as e:
        return 'the landing lock %s is there, so counted as held (%s)' % (lock.path, e)
    if held is None:
        return None
    return 'the landing lock %s is held (%s)' % (lock.path, landing.describe(held))


def machine_lock_path(kit=None):
    """The kit's own lock tool, `<kit>/tools/machine_lock.py` (kitlib.kitroot.lock_tool; with no
    `kit`, the checkout this kitlib is in)."""
    return kitroot.lock_tool(kit)


def machine_lock_entries(run=spawn.run, kit=None):
    """`(entries, None)`: every record `machine_lock.py status --json` lists - its `lock.held`,
    `waiting` and `background` lists, or (the kit lock's snapshot) its `runs` and `queue.entries`;
    or `(None, why)` when it cannot be read: the sweep then defers (fail closed)."""
    path = machine_lock_path(kit)
    if not os.path.isfile(path):
        return None, 'the machine lock tool is not at %s' % path
    try:
        r = run([sys.executable, '-B', path, 'status', '--json'], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, 'machine_lock.py status --json did not run: %s' % e
    if r.returncode != 0:
        return None, 'machine_lock.py status --json exited %d: %s' % (
            r.returncode, (r.stderr or r.stdout or '').strip()[:300] or 'nothing printed')
    try:
        data = json.loads(r.stdout)
    except ValueError as e:
        return None, 'machine_lock.py status --json printed no JSON (%s)' % e
    return entries_of(data)


def entries_of(data):
    """The records of a `status --json` document, either shape; `(None, why)` for any other."""
    lock = data.get('lock') if isinstance(data, dict) else None
    if isinstance(lock, dict) and isinstance(lock.get('held'), list):
        entries = []
        for key in ('held', 'waiting', 'background'):
            value = lock.get(key, [])
            if not isinstance(value, list):
                return None, 'machine_lock.py status --json: `lock.%s` is not a list' % key
            entries += value
        return entries, None
    if isinstance(data, dict) and isinstance(data.get('runs'), list):
        queue = data.get('queue', {})
        waiting = queue.get('entries', []) if isinstance(queue, dict) else None
        if not isinstance(waiting, list):
            return None, 'machine_lock.py status --json: `queue.entries` is not a list'
        return list(data['runs']) + waiting, None
    return None, 'machine_lock.py status --json has neither a `lock.held` nor a `runs` list'


def machine_lock_holder(entries, path, branch):
    """The first machine lock record that names this worktree, as a printable line, or None. Names
    means: any string field contains, ignoring case, the worktree's folder name, its branch, or its
    path (either slash). Over-matching only skips more."""
    flat = os.path.normcase(os.path.normpath(path))
    needles = {os.path.basename(flat).lower(), branch.lower(), flat.lower(),
               flat.lower().replace('\\', '/')}
    needles.discard('')
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for value in entry.values():
            if not isinstance(value, str):
                continue
            text = value.lower()
            if any(n in text or n in text.replace('/', '\\') for n in needles):
                return '%s/%s run %s (%s)' % (entry.get('repo', '?'), entry.get('slug', '?'),
                                              entry.get('id', '?'), entry.get('note') or 'no note')
    return None


def last_activity(main, path, branch):
    """`((newest time, what), None)`, or `(None, why)` when it cannot be read in full: the mtime of
    everything in the tree; everything in the worktree's own git dir except `index`, `index.lock`
    and the dir's own mtime (a status rewrites the index: a read, not work); the branch's reflog;
    the branch tip's committer date. A tree that cannot be read in full is never called idle."""
    best = [0.0, None]

    def see(when, what):
        if when > best[0]:
            best[0], best[1] = when, what

    def walk(root, label, skip=()):
        errors = []
        top = extended(root)
        seen = 0
        for where, dirs, files in os.walk(top, onerror=errors.append):
            seen += len(dirs) + len(files)
            if seen > WALK_MAX:
                return '%s could not be read in full: more than %d entries' % (label, WALK_MAX)
            for name in dirs + files:
                if where == top and name in skip:
                    continue
                full = os.path.join(where, name)
                try:
                    see(os.lstat(full).st_mtime, plain(full))
                except OSError as e:
                    errors.append(e)
            prune(where, dirs)
        try:
            when = os.lstat(top).st_mtime
            if not skip:
                see(when, plain(top))
        except OSError as e:
            errors.append(e)
        if errors:
            return '%s could not be read in full (%d error(s), first: %s)' % (
                label, len(errors), errors[0])
        return None

    why = walk(path, 'the tree')
    if why:
        return None, why
    rc, gitdir, err = git(path, 'rev-parse', '--path-format=absolute', '--git-dir')
    if rc != 0 or not gitdir:
        return None, failure(('rev-parse', '--git-dir'), rc, err)
    why = walk(os.path.normpath(gitdir), 'its git dir', skip=('index', 'index.lock'))
    if why:
        return None, why
    rc, common, err = git(main, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if rc != 0 or not common:
        return None, failure(('rev-parse', '--git-common-dir'), rc, err)
    reflog = os.path.join(os.path.normpath(common), 'logs', 'refs', 'heads', *branch.split('/'))
    try:
        see(os.lstat(reflog).st_mtime, 'the reflog of %s' % branch)
    except FileNotFoundError:
        pass
    except OSError as e:
        return None, 'the reflog %s cannot be read (%s)' % (reflog, e)
    args = ('log', '-1', '--format=%ct', 'refs/heads/' + branch, '--')
    rc, stamp, err = git(main, *args)
    if rc != 0 or not stamp.isdigit():
        return None, failure(args, rc, err)
    see(float(stamp), 'the commit date of %s' % branch)
    return (best[0], best[1]), None


def sweep_skip(ctx, entry, home, base, chain, ancestors, entries, machine, now, idle_s, mark=None):
    """Why this worktree is NOT swept, or None when every check before the holder probe passed.
    Cheapest checks first; the first that fails is the reason printed. `mark` is the stale mark of
    its branch (kitlib.worktree.marks), or None: an UNLANDED tree whose mark holds passes, to be
    abandoned."""
    s, main = ctx.s, ctx.main
    path, branch, head = entry['path'], entry['branch'], entry['head']
    up = s['upstream']
    key = canon(path)
    if key == canon(main):
        return 'the main checkout: never touched'
    if not remove.is_workstream(ctx, path):
        return 'not under %s: only workstream worktrees are swept' % s['worktrees_dir']
    if remove.exemption(s, path):
        return 'exempt (%s)' % remove.exemption(s, path)
    if branch is None:
        return 'detached HEAD: no branch, never swept'
    if not os.path.isdir(path):
        return 'its directory is missing (an orphan: `--check` reports it)'
    if entry['locked'] is not None:
        return 'locked with `git worktree lock`%s' % (
            ': ' + entry['locked'] if entry['locked'] else '')
    if not head:
        return 'git reported no HEAD for it'
    stale = head not in ancestors and mark is not None
    if head not in ancestors:
        rc, count, _ = git(main, 'rev-list', '--count', '%s..%s' % (base, head))
        unlanded = 'UNLANDED: %s has %s commit(s) not in %s %s' % (
            branch, count if rc == 0 and count else 'an unknown number of', up, base[:10])
        if mark is None:
            return unlanded
        void = marks.void_reason(mark, path, head)
        if void:
            return '%s; its stale mark is void: %s' % (unlanded, void)
    elif head in chain:
        return ('no commit of its own has landed (fresh, or only synced with %s): a fresh'
                ' worktree is never swept' % s['base'])
    others = sorted(os.path.normpath(e['path']) for e in entries
                    if e['branch'] == branch and canon(e['path']) != key)
    if others:
        return 'branch %s is also checked out at %s' % (branch, ', '.join(others))
    holder = machine_lock_holder(machine, path, branch)
    if holder:
        return 'machine lock holder: %s' % holder
    ids, why = (marks.claims if stale else remove.pulled_by)(ctx, base, branch)
    if ids is None:
        return 'the open-claim check failed (%s)' % why
    if ids:
        return 'a backlog record is still pulled by %s (an open claim): %s' % (
            branch, ', '.join(ids))
    got, why = last_activity(main, path, branch)
    if got is None:
        return 'its last activity cannot be read (%s)' % why
    when, what = got
    if stale:
        void = marks.void_reason(mark, path, head, got)
        if void:
            return 'UNLANDED; its stale mark is void: %s' % void
    if now - when < idle_s:
        return 'active %.1f h ago (newest: %s), under the %d h idle bar' % (
            max(now - when, 0) / 3600.0, what, idle_s // 3600)
    return sweep_status(ctx, path)


def sweep_status(ctx, path):
    """Why the status of `path` stops the sweep, or None when it is clean. Read in `sweep_skip`
    and AGAIN just before `git worktree remove`."""
    work, disposable, ignored, why = remove.dirty(ctx, path)
    if work is None:
        return 'its status could not be read (%s)' % why
    # disposable_untracked counts here: the sweep deletes nothing but the tree git removes.
    lines = work + ['?? ' + rel for rel in disposable]
    if lines:
        return 'uncommitted or untracked files (%d): %s%s' % (
            len(lines), '; '.join(lines[:5]), ' ...' if len(lines) > 5 else '')
    if ignored:
        return remove.ignored_reason(ignored, path)
    return holders.link_reason(path)


def sweep(top, apply=False, now=None, machine=machine_lock_entries, idle_s=SWEEP_IDLE_S,
          log_dir=None, cwd=None):
    """`--sweep-merged [--apply]` on the repository `top` is in, which must be its main checkout.
    `now` (epoch seconds) and `machine` (a `machine_lock_entries`-like callable) are test seams.
    Under `apply` the lines and the exit status are appended to `<log_dir or shared temp>/
    <sweep_log_name>`."""
    ctx, refused = remove.open_ctx(top, cwd)
    if refused:
        return refused
    if canon(top) != canon(ctx.main):
        ctx.say('refused   the sweep runs on the main checkout only (%s), not %s; nothing was'
                ' removed' % (ctx.main, top))
        return ctx.outcome(REFUSED)
    status = _sweep(ctx, apply, time.time() if now is None else now, machine, idle_s)
    if apply:
        try:
            log = os.path.join(log_dir or user.shared_temp(), sweep_log_name(ctx))
            with open(log, 'a', encoding='utf-8') as fh:
                fh.write(''.join(line + '\n' for line in ctx.out) + 'exit      %d\n\n' % status)
        except (OSError, RuntimeError) as e:
            ctx.say('note      the sweep log could not be written: %s' % e)
    return ctx.outcome(status)


def _sweep(ctx, apply, now, machine, idle_s):
    s, main = ctx.s, ctx.main
    up = s['upstream']
    mode = 'apply' if apply else 'dry run (nothing is removed; --apply acts)'
    ctx.say('sweep     %s, %s UTC, idle bar %d h, repository %s' % (
        mode, datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M'),
        idle_s // 3600, main))
    why = landing_hold(ctx)
    if why:
        ctx.say('deferred  %s: a landing is in progress or its lock is unreadable; nothing was'
                ' removed' % why)
        return REFUSED
    why = fetch_base(ctx)
    if why:
        ctx.say('failed    the fetch of %s failed (%s); nothing was removed (fail closed)'
                % (up, why))
        return REFUSED
    try:
        listing = remove.worktree_listing(main)
    except remove.GitReadError as e:
        ctx.say('failed    %s; nothing was removed' % e)
        return REFUSED
    held, why = machine()
    if held is None:
        ctx.say('deferred  the machine lock cannot be read (%s); nothing was removed' % why)
        return REFUSED
    base, why = remove.base_commit(ctx)
    if base is None:
        ctx.say('failed    cannot read %s (%s); nothing was removed' % (up, why))
        return REFUSED
    got, why = remove.ancestry(ctx, base)
    if got is None:
        ctx.say('failed    git rev-list --parents %s: %s; nothing was removed' % (base, why))
        return REFUSED
    chain, ancestors = got
    ctx.say('base      %s %s (fetched)' % (up, base[:10]))
    home = canon(remove.home_of(ctx))
    entries = remove.worktree_entries(listing)
    counts = {'removed': 0, 'would remove': 0, 'skipped': 0, 'failed': 0, 'abandoned': 0,
              'would abandon': 0}
    deferred = False
    where, marked = read_marks(ctx)
    if marked is None:
        counts['failed'] += 1
    for entry in entries:
        path = os.path.normpath(entry['path'])
        name = '%s [%s]' % (path, entry['branch'] or 'detached')
        mark = marks.find(marked, entry['branch']) if entry['branch'] else None
        stale = mark is not None and entry['head'] not in ancestors
        reason = sweep_skip(ctx, entry, home, base, chain, ancestors, entries, held, now, idle_s,
                            mark)
        if reason:
            ctx.say('skipped   %s: %s' % (name, reason))
            counts['skipped'] += 1
            continue
        blocked = holders.blockers(path)
        if blocked:
            ctx.say('skipped   %s: held - %d path(s) cannot be deleted; git was NOT run'
                    % (name, len(blocked)))
            ctx.describe(path, blocked)
            counts['skipped'] += 1
            continue
        if not apply:
            if stale:
                ctx.say('would     abandon %s under %s%s (marked stale %s by %s: %s), then delete'
                        ' branch %s' % (path, remove.ARCHIVE, os.path.basename(path),
                                        marks.shown_at(mark), mark['by'], mark['why'],
                                        entry['branch']))
                counts['would abandon'] += 1
            else:
                ctx.say('would     remove %s, then git branch -d %s' % (path, entry['branch']))
                counts['would remove'] += 1
            continue
        why = landing_hold(ctx)
        if why:
            ctx.say('deferred  %s: a landing started during the sweep; %s and every worktree after'
                    ' it were not removed' % (why, name))
            deferred = True
            break
        changed = sweep_status(ctx, path)
        if changed:
            ctx.say('skipped   %s: changed since it was checked - %s; git was NOT run'
                    % (name, changed))
            counts['skipped'] += 1
            continue
        if stale:
            counts[abandon_marked(ctx, where, entry, mark, name)] += 1
            continue
        if not remove.git_remove(ctx, path, entry['path']):
            counts['failed'] += 1
            continue
        counts['removed'] += 1
        if remove.delete_branch(ctx, entry['branch'], False) != OK:
            counts['failed'] += 1
    if marked and not deferred:
        counts['failed'] += end_marks(ctx, where, marked, entries, apply)
    ctx.say('summary   %s%s' % (', '.join('%s %d' % kv for kv in counts.items()),
                                ', stopped by the landing lock' if deferred else ''))
    return REFUSED if counts['failed'] or deferred else OK


def read_marks(ctx):
    """`(marks file, marks)` for the sweep, or `(file or None, None)` when they cannot be read:
    said as a failure, and then no marked tree is acted on."""
    where = None
    try:
        where = marks.file_of(ctx)
        got, why = marks.load(where)
    except marks.MarksError as e:
        got, why = None, str(e)
    if got is None:
        ctx.say('failed    the stale marks cannot be read (%s): no marked worktree is abandoned;'
                ' the landed sweep goes on' % why)
    return where, got


def abandon_marked(ctx, where, entry, mark, name):
    """Abandon the marked tree of `entry` under the marks lock, only while its mark is still
    `mark`, held to the tip it was checked at; drop the mark once that is done. Returns the count
    it adds to: abandoned, skipped or failed."""
    branch = entry['branch']
    try:
        with marks.locked(where):
            now_marks, why = marks.load(where)
            if now_marks is None:
                ctx.say('failed    %s: the stale marks cannot be read again (%s); git was NOT run'
                        % (name, why))
                return 'failed'
            if marks.find(now_marks, branch) != mark:
                ctx.say('skipped   %s: its stale mark changed or was cleared since it was read;'
                        ' git was NOT run' % name)
                return 'skipped'
            status = remove._abandon(ctx, entry['path'], marks.tag_why(mark), False, None,
                                     expect=entry['head'])
            if status != OK:
                ctx.say('  mark     the stale mark of %s stays' % branch)
                return 'failed'
            marks.save(where, [m for m in now_marks if m['branch'] != branch])
    except marks.MarksError as e:
        ctx.say('failed    %s: %s' % (name, e))
        return 'failed'
    ctx.say('unmarked  %s: abandoned, so its stale mark is dropped' % branch)
    return 'abandoned'


def end_marks(ctx, where, marked, entries, apply):
    """The last pass over the marks read at the start: a mark whose branch is gone is dropped
    (`apply`; else `would drop`), and a mark whose branch has no worktree is named for
    `--unmark-stale`. Returns the failures to count."""
    on = {e['branch'] for e in entries if e['branch']}
    gone, failed = [], 0
    for mark in marked:
        branch = mark['branch']
        tip, why = remove.branch_tip(ctx.main, branch)
        if tip is None:
            ctx.say('failed    the stale mark of %s: its branch cannot be read (%s)' % (branch, why))
            failed += 1
        elif not tip:
            gone.append(mark)
        elif branch not in on:
            ctx.say('note      the stale mark of %s names no worktree (none is on the branch, and'
                    ' the sweep abandons only a tree): clear it with `%s %s --unmark-stale`'
                    % (branch, ctx.s['tool'], branch))
    if not gone:
        return failed
    if not apply:
        for mark in gone:
            ctx.say('would     drop the stale mark of %s: the branch is gone' % mark['branch'])
        return failed
    try:
        with marks.locked(where):
            now_marks, why = marks.load(where)
            if now_marks is None:
                ctx.say('failed    the stale marks cannot be read again (%s); no mark was dropped'
                        % why)
                return failed + 1
            drop = [m for m in gone if marks.find(now_marks, m['branch']) == m]
            if drop:
                names = {m['branch'] for m in drop}
                marks.save(where, [m for m in now_marks if m['branch'] not in names])
    except marks.MarksError as e:
        ctx.say('failed    the stale marks of gone branches were not dropped: %s' % e)
        return failed + 1
    for mark in drop:
        ctx.say('dropped   the stale mark of %s: the branch is gone' % mark['branch'])
    return failed
