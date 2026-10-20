"""`worktree_remove.py --classify [--json]`: sort every registered worktree but the main checkout
into one class, read-only (docs/worktree-remove.md "Classify"). Nothing here deletes, moves,
fetches or writes: every git call is a read (the status with `--no-optional-locks`), the base is
read as it stands, never fetched, and the backlog store is opened with `fetch=False`.

The classes, decided first-match in this order:

- L live or kept (reported only): the main checkout's own worktree list says it is locked, it is
  exempt, it is the landing tree, its folder is missing, or a liveness probe holds it: the landing
  lock names its branch, a machine lock record names it, a process has its current directory in
  it, or its last activity is newer than the dead bar (DEAD_AFTER_S, 24 h). A probe that cannot be
  read in full makes the tree L too (`unreadable: ...`), as does a status that cannot be read.
  When the scan could not read some process, a tree is L unless every folder in it opens for
  DELETE (holders.held_folder): a process we could not read may hold it.
- A landed: the tip is in the base AND off its first-parent chain (remove.merged_worktrees's
  rule), the tree is clean, and a branch's reflog shows a commit made on it (own_commit: a
  branch made at a landed commit and never worked on is C; a reflog that cannot be read, C).
- E evidence: the folder name matches EVIDENCE_GLOBS, or a detached tree outside every worktree
  folder.
- B finished: clean, ahead of the base (tip not in it), and the tip has a gate-OK record from a
  --full gate that git still agrees with (kitlib.records.gate_ok.verify_full).
- C half-done: everything else (dirty, no gate-OK record, a record that cannot be read, or no
  commit of its own).

Who the tree belongs to (a backlog record pulled by its branch) is shown, never used to classify.
`classify` returns Result(code, lines, data): 0 when no tree is A, B, C or E; 1 when one is, or a
run-wide read failed; 2 for a refused kit configuration.
"""
import collections
import datetime
import fnmatch
import os
import time

from kitlib.records import gate_ok

from . import holders, remove, sweep
from .holders import WALK_MAX, canon, extended, plain, prune
from .remove import OK, REFUSED, failure, git

Result = collections.namedtuple('Result', 'code lines data')

DEAD_AFTER_S = 24 * 3600                       # owner answer Q3: 24 h, the sweep's idle bar
EVIDENCE_GLOBS = ('agent-*', '*-red', 'redcheck', 'mutant*')
LANDING_TREE = '_landing'                      # kitlib/gate/land.py TREE_NAME
CLASSES = ('A', 'B', 'C', 'E', 'L')
HEADINGS = {
    'A': ('landed', 'tip in the base, clean', 'removable: worktree_remove.py <slug>'),
    'B': ('finished', 'clean, ahead of the base, gate-OK record, dead',
          'take up: land it or record why not'),
    'C': ('half-done', 'dirty, no gate-OK record, or nothing of its own; dead',
          'take up, or block/release its record; then --abandon'),
    'E': ('evidence', 'agent-*/red/mutant name, or detached outside the worktree folder; dead',
          'removable: --scratch or --abandon "evidence"'),
    'L': ('live/kept', 'locked, exempt, held, active or unreadable', 'reported only'),
}


def process_cwds():
    """`((holders, unreadable, total), None)`: every process's (pid, image, current directory) this
    scan could read, how many it could not open and how many it saw; or `(None, why)` when there is
    no process list to read (the scan is Windows only; an empty list is no answer)."""
    if not holders.WINDOWS:
        return None, 'the process scan is Windows only'
    seen = holders.processes()
    if not seen:
        return None, 'the process list came back empty'
    found, unreadable = [], 0
    for pid, name in seen:
        try:
            found.append((pid, name, holders.current_directory(pid)))
        except OSError:
            unreadable += 1
    return (found, unreadable, len(seen)), None


def cwd_forms(cwds):
    """Each scanned process as (pid, image, its cwd's comparable forms): normcase, and canon for a
    cwd taken through a junction (holders.cwd_holders matches both). Computed once per run, never
    once per tree."""
    out = []
    for pid, name, where in cwds:
        forms = {os.path.normcase(where).rstrip('\\/')}
        try:
            forms.add(canon(where).rstrip('\\/'))
        except (OSError, ValueError):
            pass
        out.append((pid, name, forms))
    return out


def _holds(forms, target):
    """True when one of a cwd's `forms` is `target` or under it."""
    return any(flat == target or flat.startswith(target + os.sep) for flat in forms)


def _newest(root, label, skip=()):
    """`((mtime, path), None)` of the newest entry under `root` (links not followed; names in
    `skip` at the top left out, and then the top's own mtime too), or `(None, why)`."""
    best, errors, seen = [0.0, None], [], 0
    top = extended(root)
    for where, dirs, files in os.walk(top, onerror=errors.append):
        seen += len(dirs) + len(files)
        if seen > WALK_MAX:
            return None, '%s could not be read in full: more than %d entries' % (label, WALK_MAX)
        for name in dirs + files:
            if where == top and name in skip:
                continue
            full = os.path.join(where, name)
            try:
                when = os.lstat(full).st_mtime
            except OSError as e:
                errors.append(e)
                continue
            if when > best[0]:
                best[:] = [when, plain(full)]
        prune(where, dirs)
    if not skip:
        try:
            when = os.lstat(top).st_mtime
            if when > best[0]:
                best[:] = [when, plain(top)]
        except OSError as e:
            errors.append(e)
    if errors:
        return None, '%s could not be read in full (%d error(s), first: %s)' % (
            label, len(errors), errors[0])
    return (best[0], best[1]), None


def activity(main, path, branch, head):
    """`((newest time, what), None)` or `(None, why)`. A tree on a branch: sweep.last_activity. A
    detached tree: the same reads without a branch (the tree, its git dir but the index, and the
    HEAD commit's committer date)."""
    if branch:
        return sweep.last_activity(main, path, branch)
    got, why = _newest(path, 'the tree')
    if got is None:
        return None, why
    rc, gitdir, err = git(path, 'rev-parse', '--path-format=absolute', '--git-dir')
    if rc != 0 or not gitdir:
        return None, failure(('rev-parse', '--git-dir'), rc, err)
    inner, why = _newest(os.path.normpath(gitdir), 'its git dir', skip=('index', 'index.lock'))
    if inner is None:
        return None, why
    args = ('log', '-1', '--format=%ct', head, '--')
    rc, stamp, err = git(main, *args)
    if rc != 0 or not stamp.isdigit():
        return None, failure(args, rc, err)
    return max([got, inner, (float(stamp), 'the commit date of %s' % head[:10])],
               key=lambda pair: pair[0]), None


def landing_branches(ctx):
    """`(branches the held landing lock names, None)`, an empty set when it is free; or
    `(None, why)` when it cannot be placed or read (a lock that cannot be read holds every tree)."""
    from kitlib.config import ConfigError
    from kitlib.records import landing
    try:
        lock = landing.LandingLock.for_config(
            ctx.main, ctx.cfg, lanes=tuple(ctx.cfg.section('landing').get('lanes') or ()))
        held = lock.read()
    except (ConfigError, landing.LockError) as e:
        return None, 'the landing lock cannot be read (%s)' % e
    if held is None:
        return set(), None
    return set(held.get('branches') or ()), None


def evidence_name(path):
    name = os.path.basename(os.path.normpath(path)).lower()
    return any(fnmatch.fnmatchcase(name, glob) for glob in EVIDENCE_GLOBS)


def gate_state(main, head):
    """'green' (a gate-OK record from a --full gate that git agrees with: gate_ok.verify_full),
    'none', 'not full: <why>' (a record git agrees with, from a gate run without --full), or
    'unknown: <why>'."""
    rec, why = gate_ok.verify_full(main, head)
    if rec is not None:
        return 'green'
    if why == 'no gate-OK record for %s' % head:
        return 'none'
    if gate_ok.verify(main, head)[0] is not None:
        return 'not full: %s' % why
    return 'unknown: %s' % why


def own_commit(main, branch):
    """`(True, None)` when the branch's reflog shows a commit made on it after the entry that
    created it, `(False, None)` when it shows none; `(None, why)` when the reflog is missing,
    expired or cannot be read. A branch made at a landed commit and never committed on has its tip
    in the base off the first-parent chain, as a landed one does: this tells the two apart."""
    args = ('reflog', 'show', '--format=%gs', 'refs/heads/%s' % branch, '--')
    rc, out, err = git(main, *args)
    if rc != 0:
        return None, 'its reflog cannot be read (%s)' % failure(args, rc, err)
    subjects = [line for line in out.splitlines() if line.strip()]
    if not subjects:
        return None, 'its reflog is missing or expired'
    return any(s.startswith(('commit', 'cherry-pick')) for s in subjects[:-1]), None


def live_reason(ctx, entry, run_wide, landing, machine, cwds, now, dead_after_s):
    """Why this tree is L (kept, held or unreadable), or None. Also `(when, what)` of its last
    activity when it was read. `cwds` is (cwd_forms, unreadable, total): when any process could
    not be read, a tree no other probe holds is L unless every folder in it opens for DELETE
    (holders.held_folder: a process's current directory never does)."""
    path, branch = entry['path'], entry['branch']
    if run_wide:
        return 'unreadable: %s' % run_wide, None
    ruling = remove.exemption(ctx.s, path)
    if ruling:
        return 'exempt (%s)' % ruling, None
    if remove.is_workstream(ctx, path) and \
            os.path.normcase(os.path.basename(os.path.normpath(path))) == LANDING_TREE:
        return 'the landing tree (kitlib/gate/land.py)', None
    if entry['locked'] is not None:
        return 'locked with `git worktree lock`%s' % (
            ': ' + entry['locked'] if entry['locked'] else ''), None
    if not os.path.isdir(path):
        return 'its folder is missing (an orphan: `--check` reports it)', None
    if not entry['head'] or not entry['head'].strip('0'):
        # git lists an unborn branch's HEAD as forty zeros.
        return 'unreadable: git reported no HEAD for it', None
    if branch and branch in landing:
        return 'the landing lock names %s' % branch, None
    holder = sweep.machine_lock_holder(machine, path, branch or '')
    if holder:
        return 'machine lock: %s' % holder, None
    target = entry.get('canon') or canon(path)
    target = target.rstrip('\\/')
    for pid, name, forms in cwds[0]:
        if _holds(forms, target):
            return 'process pid %d %s has its current directory in it' % (pid, name), None
    got, why = activity(ctx.main, path, branch, entry['head'])
    if got is None:
        return 'unreadable: its last activity (%s)' % why, None
    if now - got[0] < dead_after_s:
        return 'active %.1f h ago (newest: %s), under the %d h dead bar' % (
            max(now - got[0], 0) / 3600.0, got[1], dead_after_s // 3600), got
    if cwds[1]:
        held = holders.held_folder(path)
        if held is not None:
            where, err = held
            what = ('was not probed: the open for delete is made on Windows only'
                    if err == holders.NOT_SEARCHED and not holders.WINDOWS else
                    'has more than %d entries, so not every folder in it was opened' % WALK_MAX
                    if err == holders.NOT_SEARCHED else
                    'cannot be opened for delete (Windows error %d)' % err if err else
                    'cannot be read in full')
            return ('unreadable: %d process(es) could not be read and %s %s: a process we could '
                    'not read may hold it' % (cwds[1], where, what)), got
    return None, got


def judge(ctx, entry, base, chain, ancestors):
    """`(class, fields)` for a dead tree: A, E, B or C, first match."""
    path, branch, head = entry['path'], entry['branch'], entry['head']
    work, disposable, ignored, why = remove.dirty(ctx, path)
    if work is None:
        return 'L', {'reason': 'unreadable: its status (%s)' % why}
    dirt = len(work) + len(disposable) + len(ignored)
    fields = {'dirty': dirt}
    landed = head in ancestors and head not in chain
    if landed and not dirt:
        own, why = (True, None) if not branch else own_commit(ctx.main, branch)
        if own is None:
            # Whether it was ever worked on cannot be told: C, the class that never leads to
            # removal.
            fields['note'] = 'nothing of its own shown: %s' % why
            return 'C', fields
        if own:
            return 'A', fields
    outside = not remove.is_workstream(ctx, path)
    if evidence_name(path) or (branch is None and outside):
        return 'E', fields
    if head in ancestors:
        fields['note'] = 'landed, not clean' if landed and dirt else 'nothing of its own'
        return 'C', fields
    rc, count, err = git(ctx.main, 'rev-list', '--count', '%s..%s' % (base, head))
    fields['ahead'] = int(count) if rc == 0 and count.isdigit() else None
    fields['gate'] = gate_state(ctx.main, head)
    if not dirt and fields['gate'] == 'green':
        return 'B', fields
    return 'C', fields


def classify(top, now=None, machine=sweep.machine_lock_entries, procs=process_cwds,
             dead_after_s=DEAD_AFTER_S, cwd=None):
    """`--classify` on the repository `top` is in. `now` (epoch seconds), `machine` (a
    sweep.machine_lock_entries-like callable) and `procs` (a process_cwds-like callable) are test
    seams. Read-only."""
    ctx, refused = remove.open_ctx(top, cwd)
    if refused:
        return Result(refused.code, refused.lines, {'error': ' '.join(refused.lines)})
    now = time.time() if now is None else now
    return _classify(ctx, now, machine, procs, dead_after_s)


def trees_besides_main(main, listing):
    """worktree_entries but the main checkout, each with its canon path (one canon per tree)."""
    home = canon(main)
    out = []
    for entry in remove.worktree_entries(listing):
        entry['canon'] = canon(entry['path'])
        if entry['canon'] != home:
            out.append(entry)
    return out


def run_probes(ctx, machine, procs):
    """The run-wide reads every tree's live_reason takes: `((run_wide, landing, machine entries,
    (cwd_forms, unreadable, total)), raw process scan or None)`. run_wide names every read that
    failed ('' when none did), and then every tree is L."""
    unread = []
    landing, why = landing_branches(ctx)
    if landing is None:
        unread.append(why)
    held, why = machine()
    if held is None:
        unread.append('the machine lock cannot be read (%s)' % why)
    cwds, why = procs()
    if cwds is None:
        unread.append('the process scan failed (%s)' % why)
    forms = (cwd_forms(cwds[0]), cwds[1], cwds[2]) if cwds is not None else ([], 0, 0)
    return ('; '.join(unread), landing or set(), held or [], forms), cwds


def branch_liveness(top, branch, now=None, machine=sweep.machine_lock_entries,
                    procs=process_cwds, dead_after_s=DEAD_AFTER_S, cwd=None):
    """`(why it is live, None)` or `(None, tip)`: whether the worktree on `branch` is dead by
    `--classify`'s L rule (live_reason), for the backlog's block and release of a stopped pull
    (kitlib/backlog/opener.py hands it to plan.Context as `liveness`). Anything that cannot be
    read, and a branch no worktree here is on, is live: only a dead tree's pull may be stopped by
    anyone but its holder. Read-only."""
    ctx, refused = remove.open_ctx(top, cwd)
    if refused:
        return 'the worktrees cannot be read (%s)' % ' '.join(refused.lines), None
    try:
        listing = remove.worktree_listing(ctx.main)
    except remove.GitReadError as e:
        return 'the worktrees cannot be read (%s)' % e, None
    on = [e for e in trees_besides_main(ctx.main, listing) if e['branch'] == branch]
    if not on:
        return ('no worktree here is on %s, so whether its work goes on cannot be judged'
                % branch), None
    probes, _ = run_probes(ctx, machine, procs)
    now = time.time() if now is None else now
    reason, _ = live_reason(ctx, on[0], *probes, now=now, dead_after_s=dead_after_s)
    if reason:
        return '%s (%s)' % (reason, os.path.normpath(on[0]['path'])), None
    return None, on[0]['head']


def _fail(ctx, why):
    ctx.say('failed    %s; nothing was classified' % why)
    return Result(REFUSED, list(ctx.out), {'error': why})


def _classify(ctx, now, machine, procs, dead_after_s):
    s, main = ctx.s, ctx.main
    try:
        listing = remove.worktree_listing(main)
    except remove.GitReadError as e:
        return _fail(ctx, str(e))
    base, why = remove.base_commit(ctx)
    if base is None:
        return _fail(ctx, 'cannot read %s (%s)' % (s['upstream'], why))
    got, why = remove.ancestry(ctx, base)
    if got is None:
        return _fail(ctx, 'git rev-list --parents %s: %s' % (base, why))
    chain, ancestors = got
    entries = trees_besides_main(main, listing)
    ctx.say('classify  %d worktree(s) besides the main checkout, base %s %s (as it stands, not'
            ' fetched), dead bar %d h, %s UTC' % (
                len(entries), s['upstream'], base[:10], dead_after_s // 3600,
                datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime(
                    '%Y-%m-%d %H:%M')))
    probes, cwds = run_probes(ctx, machine, procs)
    if cwds is not None:
        ctx.say('processes %d scanned, %d unreadable (not this user, or protected: not searched;'
                ' a tree is then L unless every folder in it opens for delete)'
                % (cwds[2], cwds[1]))
    run_wide = probes[0]
    if run_wide:
        ctx.say('note      %s: every tree is L' % run_wide)
    claims, claims_why = (remove.pulled_index(ctx, base) if any(e['branch'] for e in entries)
                          else ({}, None))
    trees = []
    for entry in entries:
        path = os.path.normpath(entry['path'])
        row = {'path': path, 'branch': entry['branch'], 'head': entry['head']}
        reason, seen = live_reason(ctx, entry, *probes, now=now, dead_after_s=dead_after_s)
        if seen is not None:
            row['idle_s'] = round(max(now - seen[0], 0))
        if reason:
            row.update({'class': 'L', 'reason': reason})
        else:
            klass, fields = judge(ctx, entry, base, chain, ancestors)
            row['class'] = klass
            row.update(fields)
        if entry['branch']:
            row['claims'] = (claims.get(entry['branch'], []) if claims is not None
                             else 'unknown: %s' % claims_why)
        trees.append(row)
    counts = {k: sum(1 for t in trees if t['class'] == k) for k in CLASSES}
    for klass in CLASSES:
        rows = [t for t in trees if t['class'] == klass]
        name, rule, action = HEADINGS[klass]
        ctx.say('%s %-10s %3d  %s -> %s' % (klass, name, len(rows), rule, action))
        for t in rows:
            ctx.say('  %s  %s' % (klass, describe(t, base)))
    for t in trees:
        if t['class'] in ('B', 'C'):
            ctx.say('next      %s %s --abandon "<why>"   (%s, after it is taken up or'
                    ' recorded)' % (s['tool'], remove.handle(ctx, t['path']), t['class']))
    ctx.say('summary   %s' % ', '.join('%s %d' % (k, counts[k]) for k in CLASSES))
    code = REFUSED if any(counts[k] for k in ('A', 'B', 'C', 'E')) else OK
    data = {'base': base, 'upstream': s['upstream'], 'dead_after_h': dead_after_s // 3600,
            'now_utc': gate_ok.utc(datetime.datetime.fromtimestamp(now, datetime.timezone.utc)),
            'unreadable': run_wide or None, 'counts': counts, 'trees': trees}
    if cwds is not None:
        data['processes'] = {'scanned': cwds[2], 'unreadable': cwds[1]}
    return Result(code, list(ctx.out), data)


def describe(t, base):
    """One tree's line after its class letter."""
    bits = [t['path'], t['branch'] or 'detached', 'tip %s' % (t['head'] or '?')[:10]]
    if t['class'] == 'L':
        bits.append(t['reason'])
    else:
        if t['class'] == 'A':
            bits.append('in %s' % base[:10])
        if t.get('ahead') is not None:
            bits.append('+%d commit(s)' % t['ahead'])
        if t.get('dirty'):
            bits.append('%d dirty file(s)' % t['dirty'])
        if t.get('note'):
            bits.append(t['note'])
        if 'gate' in t:
            bits.append('gate: %s' % t['gate'])
        if 'idle_s' in t:
            bits.append('dead %.1f d' % (t['idle_s'] / 86400.0))
    if 'claims' in t:
        claims = t['claims']
        bits.append('claim: %s' % (', '.join(claims) or 'none' if isinstance(claims, list)
                                   else claims))
    return '  '.join(bits)
