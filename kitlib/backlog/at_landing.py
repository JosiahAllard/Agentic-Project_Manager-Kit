"""The backlog store at a landing (docs/backlog-store.md "Lifecycle changes", "Merge safety";
called by kitlib.gate.land). Only a `ref` or `branch` store takes part; a main store's records
travel with the branch, so a landing leaves them alone.

  ancestry_refusal  before any merge: why the branch may not land when its history holds the
                    store's root commit (a store never merges into main), or the PM data
                    branch's recorded root (pmdata_refusal; docs/pm-data-branch.md), else None
  close_landed      after the push is verified: close every record the landed branch still holds
                    pulled, one commit each ("closes <ID>: landed <merge sha>"); what cannot be
                    closed is a WARNING line naming the command, and the landing stands

Neither creates a store or adds the store's fetch refspec when the remote has no store, and
ancestry_refusal moves no ref: it fetches the store's objects with an empty --refmap.
"""
import os

from kitlib import pmdata
from kitlib.git import cmd, refs
from kitlib.git import repo as gitrepo

from . import plan as pmod
from . import record as rmod
from . import store as smod
from .errors import Refusal

SESSION = 'gate-land'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKLOG_PY = os.path.join(KIT_ROOT, 'tools', 'backlog.py')
FETCH_TIMEOUT = 300


def _one_line(text):
    return ' '.join(str(text).split())


def backlog_command(root, *args):
    """The backlog.py command line that runs `args` against `root`."""
    return 'python "%s" -C "%s" %s' % (BACKLOG_PY, root, ' '.join(args))


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


def _close_plan(ctx, rid, branch, reason):
    """plan_close, run only while `rid` is still pulled by `branch` in the fresh snapshot."""
    def plan(snap):
        rec = snap.get(rid)
        if rec is None or rec.get('state') != 'pulled' or rec.get('pulled_by') != branch:
            return smod.Change({}, '', ['%s is no longer pulled by %s; left as it is'
                                        % (rid, branch)])
        return pmod.plan_close(snap, ctx, rid, reason)
    return plan


def _store_unread(branch, what, root):
    return ('WARNING: %s landed, and the backlog store could not be read (%s), so no record it '
            'holds pulled was closed; see which with: %s'
            % (branch, _one_line(what), backlog_command(root, 'landable', branch)))


def close_landed(root, cfg, branch, merge, session=SESSION):
    """Close every record the store holds pulled by `branch`, which landed as `merge`: the lines
    to print (`closed ...`, and a `WARNING: ...` naming the command for each record that could
    not be closed). [] for a main store, a remote with no store, or nothing pulled."""
    reason = 'landed %s' % merge
    backend = ref_store(root, cfg, fetch=True)
    if backend is None:
        return []
    if backend.remote:
        remote_tip, why = refs.ls_remote(backend.remote, backend.remote_ref, root,
                                         timeout=FETCH_TIMEOUT)
        if remote_tip is None:
            return [_store_unread(branch, '%s on %s: %s' % (backend.remote_ref, backend.remote,
                                                            why), root)]
        if not remote_tip:
            return []                       # no store: create none, add no refspec
    section = cfg.section('backlog')
    store = smod.Store(backend, rmod.Rules(section['id_grammar'], section['phases']),
                       session=session)
    try:
        snap = store.load()
        smod.require_readable(snap)
    except Refusal as exc:
        return [_store_unread(branch, '%s: %s' % (exc.label, exc), root)]
    held = sorted(r.stem for r in snap.readable           # the store's lookup key (Snapshot.by_id)
                  if r.get('state') == 'pulled' and r.get('pulled_by') == branch)
    ctx = pmod.Context(store, root, remote=cfg.remote, main_branch=cfg.main_branch, branch=branch)
    lines = []
    for rid in held:
        try:
            change, tip = store.write(_close_plan(ctx, rid, branch, reason))
        except Exception as exc:            # one record's failure never stops the others
            what = ('%s: %s' % (exc.label, exc) if isinstance(exc, Refusal)
                    else '%s: %s' % (type(exc).__name__, exc))
            args = ['close', rid, '--reason', '"%s"' % reason]
            rec = snap.get(rid)
            if rec is not None and rmod.is_owner_held(rec):
                args += ['--ruling', 'R-nnnn']      # his ruling ends the hold (plan_close)
            command = backlog_command(root, *args)
            lines.append('WARNING: %s landed, and %s, which it holds pulled, was not closed (%s); '
                         'close it with: %s' % (branch, rid, _one_line(what), command))
            continue
        if not change.writes:
            lines.extend(change.lines)
            continue
        lines.append('closed %s in the backlog store: %s (store at %s)'
                     % (rid, reason, (tip or '?')[:10]))
        lines.extend('  ' + line for line in change.lines if line != 'CLOSED %s' % rid)
    return lines
