"""`worktree_remove.py --classify [--json] [--apply]`: sort every registered worktree but the main
checkout into one class (docs/worktree-remove.md "Classify"). Without `apply` nothing here
deletes, moves, fetches or writes: every git call is a read (the status with
`--no-optional-locks`), the base is read as it stands, never fetched, and the backlog store is
opened with `fetch=False`. With `apply` (`apply_tree`), A and E trees then go through the existing
remove paths, which make their own checks again, and an O tree's stale registration is dropped
(remove.prune_orphan); B, C and L are never touched.

The classes, decided first-match in this order:

- L live or kept (reported only): the main checkout's own worktree list says it is locked, it is
  exempt, it is the landing tree, or a liveness probe holds it: the landing
  lock names its branch, a machine lock record names it, a process has its current directory in
  it, or its last activity is newer than the dead bar (DEAD_AFTER_S, 24 h). A probe that cannot be
  read in full makes the tree L too (`unreadable: ...`), as does a status that cannot be read.
  When the scan could not read some process, a tree is L unless every folder in it opens for
  DELETE (holders.held_folder): a process we could not read may hold it.
- O orphan: registered, not locked, exempt or the landing tree, and its folder is missing (with a
  run-wide read failed it is L, as every tree is). Its note says whether its admin folder keeps a
  commit no ref reaches (remove.orphan_unsaved: its HEAD reflog, per-worktree refs and
  pseudo-refs); one that does, or cannot be read, is left for the owner. So is one outside every
  worktree folder and scratch root (remove.orphan_outside: an unmounted or remapped drive looks
  the same): reported only.
- A landed: the tip is in the base AND off its first-parent chain (remove.merged_worktrees's
  rule), the tree is clean, and a branch's reflog shows a commit made on it (own_commit; a
  reflog that cannot be read, C). Also A, empty: clean, tip in the base, nothing of its own, and
  no commit to lose (unsaved_reflog), when the live backlog stores can be read and none claims
  it (live_claims; empty_keep says why one stays C). The PM data worktree is never empty-A.
- E evidence: a detached tree outside every worktree folder; or the folder name matches
  EVIDENCE_GLOBS and the tree is clean and, on a branch, the live backlog stores can be read and
  none claims it (evidence_keep). An evidence name that fails that goes on to B or C.
- B finished: clean, ahead of the base (tip not in it), and the tip has a gate-OK record from a
  --full gate that git still agrees with (kitlib.records.gate_ok.verify_full).
- C half-done: everything else (dirty, no gate-OK record, a record that cannot be read, or an
  empty tree a record claims, whose claims cannot be read, or whose reflogs hold a commit).

Who the tree belongs to (a backlog record pulled by its branch) is shown; it decides only whether
an empty tree is A or C. Each claim on a B or C tree gets a `next` release line. A stale mark
(kitlib.worktree.marks) on a tree's branch is shown, void or not (`mark`); a B or C tree in a
worktree folder gets a `next --unmark-stale` line when its branch has a mark that holds; else a
`next --mark-stale` line, or, when `--mark-stale` would refuse it (dirty, its tip in the base, a
claim shown: `mark_bar`), a note saying why. A marks file that cannot be read is a note, and no
mark is shown.
`classify` returns Result(code, lines, data): 0 when every tree is L; 1 when one is A, B, C, E or
O, or a run-wide read failed; 2 for a refused kit configuration. With `apply`: 0 when every A, E
and O tree was removed and no B or C tree is left; 1 when the run was refused (not the main
checkout, the landing lock held), a tree failed or was skipped, or a B or C tree is left.
"""
import collections
import copy
import datetime
import fnmatch
import os
import time

import kitlib.platform
from kitlib import kitroot
from kitlib.records import gate_ok

from kitlib.git import refs
from kitlib.platform.linux import paths as linux_paths

from . import holders, marks, remove, scratch, sweep
from .holders import WALK_MAX, canon, extended, plain, prune
from .remove import OK, REFUSED, failure, git

Result = collections.namedtuple('Result', 'code lines data')
# live_claims: {lower-cased branch: sorted ids pulled by it}, {id: the checkout whose backlog.py
# writes it, or None for a main or folder store named in .kit.json (it writes only from a linked
# worktree whose branch holds the record, never the main checkout)}, and the PM data worktree's
# canon path (None when there is none).
Claims = collections.namedtuple('Claims', 'index stores pm_root')


def claimed(got, branch):
    """The ids `got` (Claims) holds pulled by `branch`, matched without regard to case as
    backlog.py matches `pulled_by` (plan.py)."""
    return got.index.get(branch.lower(), []) if branch else []

DEAD_AFTER_S = 24 * 3600                       # owner answer Q3: 24 h, the sweep's idle bar
EVIDENCE_GLOBS = ('agent-*', '*-red', 'redcheck', 'mutant*')
LANDING_TREE = '_landing'                      # kitlib/gate/land.py TREE_NAME
CLASSES = ('A', 'B', 'C', 'E', 'L', 'O')
ACTED = ('A', 'E', 'O')                        # the classes --apply acts on
ORPHAN = 'its folder is missing (an orphan: `--check` reports it)'
HEADINGS = {
    'A': ('landed', 'tip in the base, clean, a commit of its own or empty and unclaimed',
          'removable: worktree_remove.py <slug>'),
    'B': ('finished', 'clean, ahead of the base, gate-OK record, dead',
          'take up: land it or record why not'),
    'C': ('half-done', 'dirty, no gate-OK record, or empty and claimed or unknown; dead',
          'take up, or block/release its record; then --abandon'),
    'E': ('evidence', 'agent-*/red/mutant name, clean and unclaimed, or detached outside the'
          ' worktree folder; dead',
          'removable: --scratch or --abandon "evidence"'),
    'L': ('live/kept', 'locked, exempt, held, active or unreadable', 'reported only'),
    'O': ('orphan', 'registered, its folder gone, not locked or exempt',
          '--apply drops the registration (the branch stays) unless it keeps an unsaved commit'
          ' or is outside the worktree folders and scratch roots'),
}


def process_cwds():
    """`((holders, unreadable, total), None)`: every process's (pid, image, current directory) this
    scan could read, how many it could not open and how many it saw; or `(None, why)` when there is
    no process list to read (the scan is Windows's and Linux's; an empty list is no answer). On
    Linux it reads /proc (kitlib.platform.linux.paths.process_paths): this user's processes, and
    `unreadable` counts those of this user it could not read."""
    if kitlib.platform.NAME == 'linux':
        try:
            scan = linux_paths.process_paths(('cwd',))
        except OSError as e:
            return None, 'the process list cannot be read: %s' % e
        if not scan.total:
            return None, 'the process list came back empty'
        return ([(pid, name, where) for pid, name, _kind, where in scan.entries],
                scan.unreadable, scan.total), None
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
        lock = landing.LandingLock.for_config(ctx.main, ctx.cfg)
        held = lock.read()
    except (ConfigError, landing.LockError) as e:
        return None, 'the landing lock cannot be read (%s)' % e
    if held is None:
        return set(), None
    return set(held.get('branches') or ()), None


def evidence_name(path):
    name = os.path.basename(os.path.normpath(path)).lower()
    return any(fnmatch.fnmatchcase(name, glob) for glob in EVIDENCE_GLOBS)


def gate_state(main, head, cfg=None):
    """'green' (a gate-OK record from a --full gate that git agrees with: gate_ok.verify_full;
    or a default-mode one when `cfg`, the main checkout's Config, sets "landing".gate_once:
    docs/gate.md "Gate once at landing"), 'none', 'not full: <why>' (a record git agrees with,
    from a gate run without --full), or 'unknown: <why>'."""
    rec, why = gate_ok.verify_full(main, head)
    if rec is not None:
        return 'green'
    if why == 'no gate-OK record for %s' % head:
        return 'none'
    scoped = gate_ok.verify(main, head)[0]
    if scoped is not None:
        if (cfg is not None and cfg.section('landing').get('gate_once') is True
                and gate_ok.mode_of(scoped) == 'select'):
            return 'green'
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
        return ORPHAN, None
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
            what = ('was not probed: Linux has no open that shows which folders a process holds'
                    if err == holders.NOT_SEARCHED and kitlib.platform.NAME == 'linux' else
                    'was not probed: the open for delete is made on Windows only'
                    if err == holders.NOT_SEARCHED and not holders.WINDOWS else
                    'has more than %d entries, so not every folder in it was opened' % WALK_MAX
                    if err == holders.NOT_SEARCHED else
                    'cannot be opened for delete (Windows error %d)' % err if err else
                    'cannot be read in full')
            return ('unreadable: %d process(es) could not be read and %s %s: a process we could '
                    'not read may hold it' % (cwds[1], where, what)), got
    return None, got


def live_claims(ctx, base):
    """`(Claims, None)`, or `(None, why)` when "no claim" would prove nothing: the pulls of every
    live backlog store of this repository, the store its `.kit.json` names (read as
    remove.pulled_index reads it) and the PM data worktree's (store.pm_data_live_store) when that
    names one, its records read as backlog.py there reads them. `why` when neither names a store
    (the implicit default ref is read-only: no pull is written there) or a named one cannot be
    read."""
    from kitlib.backlog import opener
    from kitlib.backlog import store as smod
    from kitlib.backlog.errors import Refusal
    from kitlib.config import ConfigError
    index, stores, read = {}, {}, 0
    if ctx.cfg.written('backlog', 'store') is not None:
        got, why = remove.pulled_index(ctx, base)
        if got is None:
            return None, why
        # A main or folder store (open_backend: neither `ref` nor `branch`) writes only from a
        # linked worktree (MainBackend.publish): no -C line can name the main checkout.
        main_kind = ctx.cfg.written('backlog', 'store') not in ('ref', 'branch')
        for branch, ids in got.items():
            index.setdefault(branch.lower(), set()).update(ids)
            stores.update((i, None if main_kind else ctx.main) for i in ids)
        read += 1
    pm_root, named, why = smod.pm_data_live_store(ctx.main)
    if why:
        return None, ('the PM data worktree\'s backlog store cannot be read (%s)'
                      % (smod.unreadable_kit_json(pm_root, why) if pm_root else why))
    if pm_root and named is not None:
        try:
            top, cfg, rules = opener.open_repo(pm_root)
            snap = smod.Store(smod.open_backend(top, cfg, fetch=False), rules).load()
        except (Refusal, ConfigError, OSError, KeyError) as e:
            return None, 'the backlog store in %s cannot be read (%s)' % (pm_root, e)
        if snap.unreadable:
            return None, 'unreadable backlog record(s) in %s: %s' % (
                snap.where, ', '.join(r.stem for r in snap.unreadable))
        for r in snap.records:
            if r.get('state') == 'pulled' and r.get('pulled_by'):
                index.setdefault(r.get('pulled_by').lower(), set()).add(r.id)
                stores[r.id] = pm_root
        read += 1
    if not read:
        return None, ('no backlog store is named: this repository\'s .kit.json names no'
                      ' "backlog".store (the implicit default is read-only, so no pull is'
                      ' written there) and no PM data worktree names one')
    return Claims({b: sorted(ids) for b, ids in index.items()}, stores,
                  canon(pm_root) if pm_root else None), None


def unsaved_reflog(ctx, entry):
    """Why an empty tree must stay C (judge): a commit its HEAD reflog or its branch's reflog holds
    that no ref and not its tip reaches, `git worktree remove` and `git branch -d` deleting both
    reflogs; or a reflog that cannot be read. None when they hold none."""
    path, branch, head = entry['path'], entry['branch'], entry['head']
    logged, why = scratch.reflog(path)
    if logged is None:
        return 'its HEAD reflog cannot be read (%s)' % why
    if branch:
        args = ('reflog', 'show', '--format=%H', 'refs/heads/%s' % branch, '--')
        rc, out, err = git(ctx.main, *args)
        if rc != 0:
            return 'its branch reflog cannot be read (%s)' % failure(args, rc, err)
        logged += [oid for oid in out.split() if oid not in logged]
    if not logged:
        return None
    lost, why = unsaved_count(ctx, logged, (head,))
    if lost is None:
        return 'its reflogs cannot be checked (%s)' % why
    if lost:
        return 'its reflogs hold %d commit(s) no ref and not its tip reaches' % lost
    return None


def empty_keep(ctx, entry, own, claims):
    """Why a clean tree whose tip is in the base and that has nothing of its own stays C; None
    when it is A (no commit can be lost and no backlog record claims it). `own`: own_commit's
    answer when judge read it, else None (read here). `claims`: `(Claims, why)` (live_claims)."""
    got, why = claims
    if got is None:
        return 'whether a backlog record claims it is unknown: %s' % why
    if got.pm_root and (entry.get('canon') or canon(entry['path'])) == got.pm_root:
        return 'the PM data worktree'
    branch = entry['branch']
    if claimed(got, branch):
        return 'claimed by %s' % ', '.join(claimed(got, branch))
    if branch and own is None:
        own, why = own_commit(ctx.main, branch)
        if own is None:
            return why
    if own:
        return 'its branch reflog shows a commit of its own'
    return unsaved_reflog(ctx, entry)


def judge(ctx, entry, base, chain, ancestors, claims=(None, 'not read')):
    """`(class, fields)` for a dead tree: A, E, B or C, first match. `claims`: `(Claims, why)`
    (live_claims): an empty tree is A, and an evidence-named one on a branch E, only when it is
    readable and names no claim (evidence_keep)."""
    path, branch, head = entry['path'], entry['branch'], entry['head']
    work, disposable, ignored, why = remove.dirty(ctx, path)
    if work is None:
        return 'L', {'reason': 'unreadable: its status (%s)' % why}
    dirt = len(work) + len(disposable) + len(ignored)
    fields = {'dirty': dirt}
    landed = head in ancestors and head not in chain
    own = None
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
    if branch is None and outside:
        return 'E', fields
    kept = None
    if evidence_name(path):
        kept = evidence_keep(ctx, entry, dirt, claims)
        if kept is None:
            return 'E', fields
    klass, fields = work_class(ctx, entry, base, ancestors, own, claims, dirt, landed, fields)
    if kept and kept not in fields.get('note', ''):
        fields['note'] = '; '.join(n for n in (fields.get('note'),
                                               'an evidence name, kept from E: %s' % kept) if n)
    return klass, fields


def evidence_keep(ctx, entry, dirt, claims):
    """Why a dead tree with an evidence name is not E (judge), or None when it is: the name alone
    proves nothing about the work in it, so it is E only when it is clean and, on a branch, every
    live backlog store can be read and none claims the branch (`claims`: live_claims)."""
    if dirt:
        return '%d dirty file(s)' % dirt
    branch = entry['branch']
    if branch is None:
        return None
    got, why = claims
    if got is None:
        return 'whether a backlog record claims it is unknown: %s' % why
    if got.pm_root and (entry.get('canon') or canon(entry['path'])) == got.pm_root:
        return 'the PM data worktree'
    ids = claimed(got, branch)
    return 'claimed by %s' % ', '.join(ids) if ids else None


def work_class(ctx, entry, base, ancestors, own, claims, dirt, landed, fields):
    """judge's B or C (or an empty tree's A) for a tree that is neither landed-A nor E."""
    head = entry['head']
    if head in ancestors:
        if dirt:
            fields['note'] = 'landed, not clean' if landed else 'nothing of its own'
            return 'C', fields
        keep = empty_keep(ctx, entry, own, claims)
        fields['note'] = 'nothing of its own' + ('; kept: %s' % keep if keep else '')
        return ('C' if keep else 'A'), fields
    rc, count, err = git(ctx.main, 'rev-list', '--count', '%s..%s' % (base, head))
    fields['ahead'] = int(count) if rc == 0 and count.isdigit() else None
    fields['gate'] = gate_state(ctx.main, head, ctx.cfg)
    if not dirt and fields['gate'] == 'green':
        return 'B', fields
    return 'C', fields


def classify(top, now=None, machine=sweep.machine_lock_entries, procs=process_cwds,
             dead_after_s=DEAD_AFTER_S, cwd=None, apply=False, environ=None, today=None):
    """`--classify` on the repository `top` is in. `now` (epoch seconds), `machine` (a
    sweep.machine_lock_entries-like callable) and `procs` (a process_cwds-like callable) are test
    seams. Read-only unless `apply`: then `top` must be the main checkout, a held landing lock
    refuses the run before anything is read, and each A, E and O tree goes through `apply_tree`
    (`environ`: the scratch roots' environment, scratch.roots; `today`: the abandon tag's date)."""
    ctx, refused = remove.open_ctx(top, cwd)
    if refused:
        return Result(refused.code, refused.lines, {'error': ' '.join(refused.lines)})
    now = time.time() if now is None else now
    if not apply:
        return _classify(ctx, now, machine, procs, dead_after_s, environ=environ)
    if canon(top) != canon(ctx.main):
        why = '--classify --apply runs on the main checkout only (%s), not %s' % (ctx.main, top)
    else:
        why = sweep.landing_hold(ctx)
    if why:
        ctx.say('refused   %s; nothing was classified or removed' % why)
        return Result(REFUSED, list(ctx.out), {'error': why})
    return _classify(ctx, now, machine, procs, dead_after_s,
                     act=lambda entry, row, base: apply_tree(ctx, entry, row, base, environ,
                                                             today), environ=environ)


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


def _classify(ctx, now, machine, procs, dead_after_s, act=None, environ=None):
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
    claims = live_claims(ctx, base) if entries else (None, 'no tree to claim')
    marked, marks_error = read_marks(ctx)
    if marks_error:
        ctx.say('note      the stale marks cannot be read (%s): no mark is shown' % marks_error)
    trees, acted = [], []
    for entry in entries:
        path = os.path.normpath(entry['path'])
        row = {'path': path, 'branch': entry['branch'], 'head': entry['head']}
        reason, seen = live_reason(ctx, entry, *probes, now=now, dead_after_s=dead_after_s)
        if seen is not None:
            row['idle_s'] = round(max(now - seen[0], 0))
        if reason == ORPHAN:
            row.update({'class': 'O', 'reason': reason})
            lost, why = remove.orphan_unsaved(ctx, path)
            row['unsaved'] = lost
            row['note'] = ('nothing only its registration keeps' if lost == 0 else
                           '%d commit(s) only its registration keeps, which no ref reaches:'
                           ' left for the owner' % lost if lost else
                           'whether its registration keeps an unsaved commit is unknown (%s):'
                           ' left for the owner' % why)
            row['outside'] = remove.orphan_outside(ctx, path, environ)
            if row['outside']:
                row['note'] = '%s; %s' % (row['outside'], row['note'])
        elif reason:
            row.update({'class': 'L', 'reason': reason})
        else:
            klass, fields = judge(ctx, entry, base, chain, ancestors, claims)
            row['class'] = klass
            row.update(fields)
        if entry['branch']:
            row['claims'] = (claimed(claims[0], entry['branch']) if claims[0] is not None
                             else 'unknown: %s' % claims[1])
        mark = marks.find(marked, entry['branch']) if entry['branch'] else None
        if mark is not None:
            row['mark'] = {'at': mark['at'], 'why': mark['why'], 'by': mark['by'],
                           'tip': mark['tip'],
                           'void': marks.void_reason(mark, path, entry['head'], seen)}
        trees.append(row)
        if row['class'] in ACTED:
            acted.append((entry, row))
    counts = {k: sum(1 for t in trees if t['class'] == k) for k in CLASSES}
    applied = None
    if act is not None:
        # The landing lock is read again before each tree: taken mid-run, the rest are skipped.
        # The machine lock is read again too: a tree a record now names is skipped.
        applied = {'removed': 0, 'failed': 0, 'skipped': 0}
        stop = None
        for entry, row in acted:
            if stop is None:
                stop = sweep.landing_hold(ctx)
            held = stop or machine_hold(machine, entry)
            if held:
                row['apply'] = {'result': 'skipped', 'why': held, 'lines': []}
            else:
                row['apply'] = act(entry, row, base)
            applied[row['apply']['result']] += 1
            ctx.say('%-9s %s: %s' % (row['apply']['result'], row['path'], row['apply']['why']))
    for klass in CLASSES:
        if act is not None and klass in ACTED:
            continue
        rows = [t for t in trees if t['class'] == klass]
        name, rule, action = HEADINGS[klass]
        ctx.say('%s %-10s %3d  %s -> %s' % (klass, name, len(rows), rule, action))
        for t in rows:
            ctx.say('  %s  %s' % (klass, describe(t, base)))
    for t in trees:
        if t['class'] in ('B', 'C'):
            for rid in t['claims'] if isinstance(t.get('claims'), list) else ():
                writer = claims[0].stores[rid]
                if writer is None:
                    ctx.say('next      release %s or block it (%s): its store is in the main'
                            ' checkout, which takes no backlog write; run `backlog.py release %s'
                            ' --reason "<why>"` (or `block %s --on <M> --reason "<why>"` to keep'
                            ' the branch) from a worktree whose branch holds the record'
                            % (rid, t['class'], rid, rid))
                    continue
                ctx.say('next      %s release %s --reason "<why>"   (%s, or `block %s --on <M>'
                        ' --reason "<why>"` to keep the branch)' % (
                            kitroot.command('backlog', writer), rid, t['class'], rid))
            ctx.say('next      %s %s --abandon "<why>"   (%s, after it is taken up or'
                    ' recorded)' % (s['tool'], remove.handle(ctx, t['path']), t['class']))
            if not t['branch'] or marks_error or not remove.is_workstream(ctx, t['path']):
                continue
            if t.get('mark') and not t['mark']['void']:
                ctx.say('next      %s %s --unmark-stale   (%s, marked stale: the nightly sweep'
                        ' abandons it once idle; this clears the mark)' % (
                            s['tool'], t['branch'], t['class']))
            elif mark_bar(t):
                ctx.say('note      %s takes no --mark-stale (%s): %s' % (
                    t['branch'], t['class'], mark_bar(t)))
            else:
                ctx.say('next      %s %s --mark-stale "<why>"   (%s, so the nightly sweep abandons'
                        ' it once idle, if it is stale)' % (s['tool'], t['branch'], t['class']))
    ctx.say('summary   %s' % ', '.join('%s %d' % (k, counts[k]) for k in CLASSES))
    code = REFUSED if any(counts[k] for k in CLASSES if k != 'L') else OK
    if applied is not None:
        ctx.say('applied   %s' % ', '.join('%s %d' % (k, applied[k])
                                           for k in ('removed', 'failed', 'skipped')))
        code = REFUSED if (counts['B'] or counts['C'] or applied['failed']
                           or applied['skipped']) else OK
    data = {'base': base, 'upstream': s['upstream'], 'dead_after_h': dead_after_s // 3600,
            'now_utc': gate_ok.utc(datetime.datetime.fromtimestamp(now, datetime.timezone.utc)),
            'unreadable': run_wide or None, 'counts': counts, 'trees': trees}
    if marks_error:
        data['marks_error'] = marks_error
    if cwds is not None:
        data['processes'] = {'scanned': cwds[2], 'unreadable': cwds[1]}
    if applied is not None:
        data['applied'] = applied
    return Result(code, list(ctx.out), data)


def mark_bar(t):
    """Why `--mark-stale` (marks.mark) would refuse the B or C row `t`, or None: a dirty tree, a
    tip in the base (no `ahead`: landed, or nothing of its own) or a claim it shows."""
    if t.get('dirty'):
        return '%d dirty file(s): it marks a clean tree only' % t['dirty']
    if 'ahead' not in t:
        return 'its tip is in the base: landed work needs no mark'
    if isinstance(t.get('claims'), list) and t['claims']:
        return 'a backlog record still pulls it (%s): release it first' % ', '.join(t['claims'])
    return None


def read_marks(ctx):
    """`(marks, None)` or `([], why)` when the stale marks cannot be read (read-only)."""
    try:
        got, why = marks.load(marks.file_of(ctx))
    except marks.MarksError as e:
        got, why = None, str(e)
    return (got, None) if got is not None else ([], why)


def machine_hold(machine, entry):
    """Why the machine lock keeps `entry` now (read afresh, as sweep_skip does), or None."""
    held, why = machine()
    if held is None:
        return 'the machine lock cannot be read (%s)' % why
    holder = sweep.machine_lock_holder(held, entry['path'], entry['branch'] or '')
    return 'machine lock: %s' % holder if holder else None


def unsaved_count(ctx, commits, also_saved=()):
    """remove.unsaved_count, called through this module so a test can replace it for classify
    alone (tests/worktree/test_classify_apply.py, class Unsaved, the failed-count case);
    remove.prune_orphan and remove.orphan_unsaved call remove's own."""
    return remove.unsaved_count(ctx, commits, also_saved)


def scratch_route(ctx, entry, environ):
    """`(True, None)` when a detached tree is for `--scratch`: under a scratch root (scratch.roots)
    and every commit its HEAD and HEAD reflog hold is reached by a branch, remote-tracking branch
    or tag; `(False, None)` when it is not; `(None, why)` when that cannot be read. The scratch
    path checks it all again."""
    path = entry['path']
    if entry['branch']:
        return False, None
    rts, _ = scratch.roots(environ, ctx.main)
    if not any(scratch._strictly_under(path, r) for r in rts):
        return False, None
    logged, why = scratch.reflog(path)
    if logged is None:
        return None, why
    count, why = unsaved_count(ctx, (entry['head'], *logged))
    if count is None:
        return None, why
    return count == 0, None


def reflog_skip(ctx, path, head):
    """The skipped result for a tree whose HEAD reflog holds a commit no ref and not its tip
    `head` reaches, or cannot be read; None when it holds none. `git worktree remove` deletes that
    reflog, and the remove path checks it (kept_reflog) only for a tree outside the worktree
    folders or a detached one."""
    logged, why = scratch.reflog(path)
    lost, why = (None, why) if logged is None else \
        unsaved_count(ctx, logged, (head,)) if logged else (0, None)
    if lost is None:
        return {'result': 'skipped', 'why': 'its HEAD reflog cannot be read (%s)' % why,
                'lines': []}
    if lost:
        return {'result': 'skipped', 'why': 'its HEAD reflog holds %d commit(s) no ref and not'
                ' its tip reaches (reflog holds unsaved commits); left for the owner' % lost,
                'lines': []}
    return None


def apply_tree(ctx, entry, row, base, environ=None, today=None):
    """Send one A, E or O tree through an existing remove path, each making its own checks again
    (a tree held or changed since it was classified is refused there): A plain; E `--scratch` when
    scratch_route says so, plain when its tip is in the base, else `--abandon "evidence"` (tag
    archive/<slug>); O remove.prune_orphan, unless it is outside the worktree folders and scratch
    roots (remove.orphan_outside) or classify found its registration keeps a commit no ref
    reaches or could not tell (skipped: left for the owner). An A tree, or an E tree not
    for --scratch, whose HEAD reflog holds a commit no ref and not its tip reaches is skipped
    instead (reflog_skip): the remove deletes that reflog, and the tag holds only the tip. Returns
    {'result': removed|failed|skipped, 'why', 'lines': the path's own}. A tree removed whose
    branch then stayed (or any other later refusal) is failed."""
    path, head = row['path'], entry['head']
    sub = copy.copy(ctx)
    sub.out = []
    if row['class'] == 'O':
        if row.get('outside'):
            return {'result': 'skipped', 'why': 'O: %s' % row['outside'], 'lines': []}
        if row.get('unsaved') != 0:
            return {'result': 'skipped', 'why': 'O: %s' % row['note'], 'lines': []}
        how, verb = 'O, its stale registration dropped', 'orphan'
    elif row['class'] == 'A':
        how, verb = 'A, plain remove', 'plain'
        held = reflog_skip(ctx, path, head)
        if held:
            return held
    else:
        route, why = scratch_route(ctx, entry, environ)
        if route is None:
            return {'result': 'skipped', 'why': 'whether it is for --scratch cannot be read (%s)'
                    % why, 'lines': []}
        if route:
            how, verb = 'E, --scratch', 'scratch'
        else:
            landed, why = refs.is_ancestor(head, base, ctx.main)
            if landed is None:
                return {'result': 'skipped', 'why': 'whether %s is in the base cannot be read (%s)'
                        % (head[:10], why), 'lines': []}
            how, verb = ('E, plain remove', 'plain') if landed else \
                ('E, --abandon "evidence"', 'abandon')
            held = reflog_skip(ctx, path, head)
            if held:
                return held
    if verb == 'orphan':
        code = remove.prune_orphan(sub, path, environ)
    elif verb == 'plain':
        code = remove._remove(sub, path, False)
    elif verb == 'scratch':
        code = scratch._remove_scratch(sub, path, environ)
    else:
        code = remove._abandon(sub, path, 'evidence', False, today)
    lines = list(sub.out)
    tags = [line.split()[1] for line in lines if line.startswith('tagged    ')] + \
        [line.split()[2] for line in lines if line.startswith('reused    tag ')]
    if tags:
        how += ', tag %s' % tags[0]
    problems = [line.split(None, 1)[1] for line in lines
                if line.startswith(('refused ', 'failed ')) and len(line.split(None, 1)) > 1]
    problem = problems[0] if problems else 'it exited %s' % code
    removed = any(line.startswith('removed   ') for line in lines)
    if removed and code == OK:
        return {'result': 'removed', 'why': how, 'lines': lines}
    if removed:
        return {'result': 'failed', 'why': '%s: the worktree was removed, but %s' % (how, problem),
                'lines': lines}
    if code == OK:
        return {'result': 'skipped', 'why': '%s: %s' % (how, ' '.join(lines) or 'nothing done'),
                'lines': lines}
    return {'result': 'failed', 'why': '%s: %s' % (how, problem), 'lines': lines}


def describe(t, base):
    """One tree's line after its class letter."""
    bits = [t['path'], t['branch'] or 'detached', 'tip %s' % (t['head'] or '?')[:10]]
    if t['class'] == 'L':
        bits.append(t['reason'])
    else:
        if t['class'] == 'O':
            bits.append(t['reason'])
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
    mark = t.get('mark')
    if mark:
        bits.append('mark: void (%s)' % mark['void'] if mark['void'] else
                    'mark: stale since %s (%s)' % (marks.shown_at(mark), mark['why']))
    return '  '.join(bits)
