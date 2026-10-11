"""The backlog store at a landing (docs/backlog-store.md "Lifecycle changes", "Merge safety";
called by kitlib.gate.land). The configured store takes part when it is a `ref` or `branch`
store; a main store's records travel with the branch, so a landing leaves them alone. The PM
data branch's store (kitlib.pmdata: a folder store in the worktree that has the recorded data
branch checked out) takes part too: the PM's records of this repository live there.

  ancestry_refusal  before any merge: why the branch may not land when its history holds the
                    store's root commit (a store never merges into main), or the PM data
                    branch's recorded root (pmdata_refusal; docs/pm-data-branch.md), else None
  close_landed      after the push is verified: close every record the landed branch still holds
                    pulled, in the configured store (one commit each, "closes <ID>: landed <merge
                    sha>") and in the PM data store (the record file deleted in that worktree, not
                    committed); and every record a `Closes: <ID>[, <ID>]` trailer of the branch's
                    commits names (closes_trailers), while pulled, whoever pulled it. What cannot
                    be closed is a WARNING line naming the command, and the landing stands
  landable_commands the `backlog.py landable` command of each store that takes part

Neither creates a store or adds the store's fetch refspec when the remote has no store, and
ancestry_refusal moves no ref: it fetches the store's objects with an empty --refmap.
"""
import os

import kitlib.config as kconfig
from kitlib import pluginsource, pmdata
from kitlib.git import cmd, refs
from kitlib.git import repo as gitrepo

from . import plan as pmod
from . import record as rmod
from . import store as smod
from .errors import Refusal

SESSION = 'gate-land'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FETCH_TIMEOUT = 300
# What a printed write command names for -C: a backlog write refuses in the main checkout (the
# landers'), so the command never names the main checkout a landing runs in.
WORKTREE = '<a worktree of this repository>'


def _one_line(text):
    return ' '.join(str(text).split())


def backlog_py():
    """The kit's tools/backlog.py, in the live kit checkout: KIT_LIVE_ROOT when this code runs
    from a pinned export of pushed main (kitlib.pluginsource.live_root), which a later prune may
    remove, so a printed command never names it."""
    return os.path.join(pluginsource.live_root(KIT_ROOT), 'tools', 'backlog.py')


def backlog_command(root, *args):
    """The backlog.py command line that runs `args` against `root`."""
    return 'python "%s" -C "%s" %s' % (backlog_py(), root, ' '.join(args))


def close_command(root, *args):
    """The command line that runs the backlog write `args` for the repository whose main checkout
    is `root`: -C names a worktree placeholder (WORKTREE), never `root`. The repository's own
    tools/backlog.py (the kit's, or a stub) when it has one, else the kit's."""
    if os.path.isfile(os.path.join(root, 'tools', 'backlog.py')):
        tool = 'tools/backlog.py'
    else:
        tool = '"%s"' % backlog_py()
    return 'python %s -C "%s" %s' % (tool, WORKTREE, ' '.join(args))


def ref_store(root, cfg, fetch=False):
    """The configured store's backend when it is a `ref` or `branch` store, else None."""
    backend = smod.open_backend(root, cfg, fetch=fetch)
    return None if isinstance(backend, smod.MainBackend) else backend


def _has_commit(root, sha):
    return cmd.call(['cat-file', '-e', '%s^{commit}' % sha], root).rc == 0


def _local_tip(ref, root):
    """`(commit, None)` of the local `ref`, `('', None)` when it does not exist, or `(None, why)`
    when it cannot be read (a git ended from outside included: refs.ref_commit)."""
    return refs.ref_commit(ref, root)


def pmdata_refusal(root, tip):
    """None when the repository has no PM data record (kit-pm-data.json in its git common dir,
    kitlib.pmdata) or `tip`'s history does not hold the recorded root; else why `tip` may not
    land. A record that cannot be read, a root this clone lacks, or an ancestry git cannot tell
    refuses (fail closed)."""
    common, why = gitrepo.common_dir(root)
    if common is None:
        return 'cannot find the git directory to look for the PM data record: %s' % why
    try:
        record = pmdata.read(common)
    except pmdata.PmDataError as exc:
        return 'cannot read the PM data record to check the branch\'s history: %s' % exc
    if record is None:
        return None
    if not _has_commit(root, record.root):
        return ('the PM data root commit %s that %s names is not in this clone, so the branch\'s '
                'history cannot be checked against it' % (record.root[:10], pmdata.record_path(common)))
    held, why = refs.is_ancestor(record.root, tip, root)
    if held is None:
        return ('cannot tell whether the branch holds the PM data root commit %s: %s'
                % (record.root[:10], why))
    if held:
        return ('the branch\'s history holds the PM data root commit %s (branch %s, private remote '
                '%s): PM data never merges into main - rebuild the branch without the data '
                'branch\'s commits' % (record.root[:10], record.branch, record.remote))
    return None


def ancestry_refusal(root, cfg, tip, env=None):
    """None when `tip` may land; else why not: its history holds the PM data branch's recorded
    root (pmdata_refusal) or a root commit of the backlog store (read from the remote, and the
    local copy too), or either cannot be read to tell."""
    why = pmdata_refusal(root, tip)
    if why:
        return why
    try:
        backend = ref_store(root, cfg)
    except Refusal as exc:
        return 'cannot read the backlog store to check the branch\'s history: %s' % exc
    if backend is None:
        return None
    tips = []
    if backend.remote:
        remote_tip, why = refs.ls_remote(backend.remote, backend.remote_ref, root,
                                         timeout=FETCH_TIMEOUT)
        if remote_tip is None:
            return ('cannot read the backlog store %s on %s to check the branch\'s history: %s'
                    % (backend.remote_ref, backend.remote, why))
        if remote_tip and not _has_commit(root, remote_tip):
            # Objects only: the empty --refmap stops git updating a remote-tracking ref (or the
            # local store ref) through a configured refspec, and no refspec is added.
            cmd.call(['fetch', '--quiet', '--no-tags', '--no-write-fetch-head', '--refmap=',
                      backend.remote, backend.remote_ref], root, timeout=FETCH_TIMEOUT, env=env)
            if not _has_commit(root, remote_tip):
                return ('cannot fetch the backlog store %s from %s (%s) to check the branch\'s '
                        'history' % (backend.remote_ref, backend.remote, remote_tip[:10]))
        if remote_tip:
            tips.append(remote_tip)
    local, why = _local_tip(backend.local_ref, root)
    if local is None:
        return ("cannot read the local backlog store %s to check the branch's history: %s"
                % (backend.local_ref, why))
    if local and local not in tips:
        tips.append(local)
    if not tips:
        return None
    out, why = cmd.run_text(['rev-list', '--max-parents=0'] + tips, root)
    if out is None:
        return 'cannot list the backlog store\'s root commit: %s' % why
    for store_root in out.split():
        held, why = refs.is_ancestor(store_root, tip, root)
        if held is None:
            return ('cannot tell whether the branch holds the backlog store\'s root commit %s: %s'
                    % (store_root[:10], why))
        if held:
            return ('the branch\'s history holds the backlog store\'s root commit %s (%s): the '
                    'store has no history shared with main and never merges into it - rebuild '
                    'the branch without the store\'s commits'
                    % (store_root[:10], backend.describe()))
    return None


def closes_ids(values):
    """The record ids in `Closes:` trailer values: each value split on commas, the first word of
    each part, kept when it is a plain id (store.PLAIN_ID), so `A, B (both done)` is A and B and
    `#12` is nothing. Pure."""
    out = []
    for value in values:
        for part in value.split(','):
            words = part.split()
            if words and smod.PLAIN_ID.match(words[0]) and words[0] not in out:
                out.append(words[0])
    return out


def closes_trailers(root, tip, base):
    """`(ids, None)`: the ids the `Closes:` trailers name (git's trailer parser, so only the
    message's last paragraph counts; the key in any case) in the commits `tip` has and `base` does
    not, merges included; or `(None, why)`."""
    out, why = cmd.run_text(['log', '--format=%(trailers:key=Closes,valueonly)', tip, '--not',
                             base, '--'], root)
    if out is None:
        return None, why
    return closes_ids(out.splitlines()), None


def _close_plan(ctx, rid, branch, reason, named=False):
    """plan_close, run only while `rid` is still pulled by `branch` in the fresh snapshot; or, when
    a `Closes:` trailer of the branch names it (`named`), while it is still pulled by anyone."""
    def plan(snap):
        rec = snap.get(rid)
        if rec is None or rec.get('state') != 'pulled' or (
                not named and rec.get('pulled_by') != branch):
            return smod.Change({}, '', ['%s is no longer pulled%s; left as it is'
                                        % (rid, '' if named else ' by %s' % branch)])
        return pmod.plan_close(snap, ctx, rid, reason)
    return plan


def _store_unread(branch, what, how):
    return ('WARNING: %s landed, and the backlog store could not be read (%s), so no record it '
            'holds pulled was closed; see which with: %s' % (branch, _one_line(what), how))


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def pm_data_root(root):
    """`(path, None)` of the worktree of `root`'s repository that has the PM data branch checked
    out (the branch kit-pm-data.json records; kitlib.pmdata), `(None, None)` when the repository
    has no PM data record, or `(None, why)` when there is one and that worktree cannot be found."""
    common, why = gitrepo.common_dir(root)
    if common is None:
        return None, 'cannot find the git directory to look for the PM data record: %s' % why
    try:
        record = pmdata.read(common)
    except pmdata.PmDataError as exc:
        return None, 'cannot read the PM data record: %s' % exc
    if record is None:
        return None, None
    wts, why = gitrepo.worktrees(root)
    if wts is None:
        return None, 'cannot list the worktrees: %s' % why
    for wt in wts:
        if wt.branch == record.branch and not wt.bare:
            return wt.path, None
    return None, 'no worktree has the PM data branch %s checked out' % record.branch


def _pm_data_store(root, configured):
    """`(store root, Config, backend, None)` of the PM data branch's store, `(None,) * 4` when
    there is none or it is the configured store (`configured`: its describe(), or None), or
    `(None, None, None, why)` when it cannot be opened."""
    pm_root, why = pm_data_root(root)
    if pm_root is None:
        return None, None, None, why
    if _same_path(pm_root, root):
        return None, None, None, None
    try:
        pm_cfg = kconfig.load(pm_root)
        backend = smod.open_backend(pm_root, pm_cfg, fetch=True)
    except (kconfig.ConfigError, Refusal, OSError) as exc:
        return None, None, None, 'cannot open the backlog store in %s: %s' % (pm_root, exc)
    if configured is not None and backend.describe() == configured:
        return None, None, None, None
    return pm_root, pm_cfg, backend, None


def close_landed(root, cfg, branch, merge, session=SESSION, tip=None, base=None):
    """Close every record `branch`, which landed as `merge`, holds pulled: in the configured store
    when it is a `ref` or `branch` store, and in the PM data branch's store (_pm_data_store). With
    `tip` and `base` (the branch tip merged and the main it was merged onto), every record a
    `Closes:` trailer in `tip --not base` names (closes_trailers) is closed too while it is
    pulled, whoever pulled it: a pull made in the main checkout records the main branch, which no
    landing matches. The lines to print (`closed ...`, and a `WARNING: ...` naming the command for
    each record or store that could not be closed, or trailers that could not be read). [] when
    neither store takes part and no trailer names a record, or nothing is pulled."""
    lines, named = [], []
    if tip and base:
        named, why = closes_trailers(root, tip, base)
        if named is None:
            named = []
            lines.append('WARNING: %s landed, and the Closes trailers of its commits could not be '
                         'read (%s), so only the records it holds pulled are closed; see the '
                         'trailers with: git log --format=%%(trailers:key=Closes,valueonly) %s '
                         '--not %s' % (branch, _one_line(why), tip, base))
    backend = ref_store(root, cfg, fetch=True)
    if backend is not None:
        lines.extend(_close_in(backend, root, cfg, branch, merge, session,
                               lambda *args: close_command(root, *args), named))
    pm_root, pm_cfg, pm_backend, why = _pm_data_store(
        root, backend.describe() if backend is not None else None)
    if why:
        lines.append('WARNING: %s landed, and the PM data branch\'s backlog store was not read '
                     '(%s), so no record it holds pulled there was closed; see which from that '
                     'worktree with: %s' % (branch, _one_line(why),
                                            backlog_command('<the PM data worktree>', 'landable',
                                                            branch)))
    elif pm_backend is not None:
        lines.extend(_close_in(pm_backend, pm_root, pm_cfg, branch, merge, session,
                               lambda *args: backlog_command(pm_root, *args), named))
    return lines


def _close_in(backend, root, cfg, branch, merge, session, command, named=()):
    """close_landed for one store: `backend` of the repository or worktree `root` with `cfg`;
    `command(*args)` is the printed command line for a write the landing could not make. `named`:
    the ids the branch's `Closes:` trailers name; one this store does not hold is another store's
    or none, and says nothing; one it holds and not pulled is a WARNING naming the command."""
    reason = 'landed %s' % merge
    unread = backlog_command(root, 'landable', branch)
    if getattr(backend, 'remote', None):
        remote_tip, why = refs.ls_remote(backend.remote, backend.remote_ref, root,
                                         timeout=FETCH_TIMEOUT)
        if remote_tip is None:
            return [_store_unread(branch, '%s on %s: %s' % (backend.remote_ref, backend.remote,
                                                            why), unread)]
        if not remote_tip:
            return []                       # no store: create none, add no refspec
    section = cfg.section('backlog')
    store = smod.Store(backend, rmod.Rules(section['id_grammar'], section['phases']),
                       session=session)
    try:
        snap = store.load()
        smod.require_readable(snap)
    except Refusal as exc:
        return [_store_unread(branch, '%s: %s' % (exc.label, exc), unread)]
    held = set(r.stem for r in snap.readable               # the store's lookup key (Snapshot.by_id)
               if r.get('state') == 'pulled' and r.get('pulled_by') == branch)

    def close_args(rid):
        args = ['close', rid, '--reason', '"%s"' % reason]
        rec = snap.get(rid)
        if rec is not None and rmod.is_owner_held(rec):
            args += ['--ruling', 'R-nnnn']          # his ruling ends the hold (plan_close)
        return args

    lines = []
    by_trailer = {}                         # id -> its pulled_by, for a record held by another
    for rid in named:
        rec = snap.get(rid)
        if rec is None or rid in held:
            continue
        if rec.get('state') != 'pulled':
            lines.append('WARNING: %s landed, and a Closes trailer of its commits names %s, which '
                         'is %s, not pulled, so it was not closed; close it with: %s'
                         % (branch, rid, rec.get('state') or '?', command(*close_args(rid))))
            continue
        by_trailer[rid] = rec.get('pulled_by') or '?'
    ctx = pmod.Context(store, root, remote=cfg.remote, main_branch=cfg.main_branch, branch=branch)
    folder = isinstance(backend, smod.MainBackend)
    for rid in sorted(held | set(by_trailer)):
        how = (', which a Closes trailer of its commits names' if rid in by_trailer
               else ', which it holds pulled')
        try:
            change, tip = store.write(_close_plan(ctx, rid, branch, reason,
                                                  named=rid in by_trailer))
        except Exception as exc:            # one record's failure never stops the others
            what = ('%s: %s' % (exc.label, exc) if isinstance(exc, Refusal)
                    else '%s: %s' % (type(exc).__name__, exc))
            lines.append('WARNING: %s landed, and %s%s, was not closed (%s); close it with: %s'
                         % (branch, rid, how, _one_line(what), command(*close_args(rid))))
            continue
        if not change.writes:
            lines.extend(change.lines)
            continue
        whose = ('; named by a Closes trailer of %s; it was pulled by %s' % (branch, by_trailer[rid])
                 if rid in by_trailer else '')
        if folder:
            lines.append('closed %s in the backlog store %s: %s (the record file is deleted, '
                         'not committed)%s' % (rid, backend.describe(), reason, whose))
        else:
            lines.append('closed %s in the backlog store: %s (store at %s)%s'
                         % (rid, reason, (tip or '?')[:10], whose))
        lines.extend('  ' + line for line in change.lines if line != 'CLOSED %s' % rid)
    return lines


def landable_commands(root, cfg, branch):
    """The `backlog.py landable` command of each store a landing closes records in (close_landed):
    the configured store's when it is a `ref` or `branch` store, the PM data store's when there is
    one. A store that cannot be opened names its command anyway."""
    out = []
    try:
        configured = ref_store(root, cfg)
    except Exception:                       # noqa: BLE001 - cannot tell: name it anyway
        configured, out = None, [backlog_command(root, 'landable', branch)]
    if configured is not None:
        out.append(backlog_command(root, 'landable', branch))
    try:
        pm_root, _why = pm_data_root(root)
    except Exception:                       # noqa: BLE001
        pm_root = None
    if pm_root is not None and not _same_path(pm_root, root):
        out.append(backlog_command(pm_root, 'landable', branch))
    return out
