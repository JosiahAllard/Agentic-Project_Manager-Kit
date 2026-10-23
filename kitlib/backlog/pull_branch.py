"""`pull` never records the main branch as `pulled_by` (docs/backlog-store.md
"Lifecycle changes").

A pull records the branch it was made from (plan.Context.current_branch: `--as`, else the branch
checked out in -C). Made in the main checkout, that is the main branch, which never lands, so the
landing's close (kitlib.backlog.at_landing, which closes what the landed branch holds pulled)
would never match the record and it would stay pulled. plan_pull refuses that pull and says how to
make it: with `--as <branch>`, or from the branch's worktree.

In the PM data worktree (kitlib.pmdata) the configured main branch is the data branch, so the code
repository's main (main_branches: the main checkout's) is refused there too.

The rule lives here, wrapping plan.plan_pull, so tools/backlog.py's `pull` goes through it (by
opener.planner); it may
move into plan.plan_pull itself later.
"""
import os

import kitlib.config as kconfig
from kitlib import pmdata
from kitlib.git import repo as gitrepo

from . import plan as pmod
from .errors import Refusal

# The code repository's main when its main checkout's configuration cannot be read: refuse both
# common names rather than let a pull record the main branch (fail closed).
FALLBACK_MAINS = ('main', 'master')


def refusal(rid, branch, main_branch, session=None):
    """Why `rid` may not be pulled as `branch`, or None. `main_branch`: one name, or the names
    (main_branches) a pull may not record. `session`: the record's own `session:` field, if it has
    one: it is named in the `--as` form as `--session <s>`, and left out otherwise, because
    `--session` narrows `next` to session-owned records and a record with none would then be
    refused as not in next's offer. The `--as` form comes first: a coordinator pulling at dispatch,
    from this checkout, can run it as it stands. Pure."""
    mains = (main_branch,) if isinstance(main_branch, str) else tuple(main_branch)
    if branch not in mains:
        return None
    flag = '--session %s ' % session if session else ''
    return ('a pull of %s here would record pulled_by: %s, and the main branch never lands, so '
            'no landing would close %s: name the branch with --as <branch> (backlog.py -C <store> '
            '%s--as <branch> pull %s), or pull it from the branch\'s worktree '
            '(backlog.py -C <worktree> pull %s)'
            % (rid, branch, rid, flag, rid, rid))


def _main_checkout(root):
    """The main checkout's path when `root` is the code repository's PM data worktree (the
    repository has a PM data record and `root` has the recorded data branch checked out); '' for
    any other checkout, or when the PM data record cannot be read (no data branch is known); None
    when it is a PM data repository whose worktrees git cannot list."""
    common, _why = gitrepo.common_dir(root)
    if common is None:
        return ''
    try:
        record = pmdata.read(common)
    except pmdata.PmDataError:
        return ''
    if record is None:
        return ''
    wts, _why = gitrepo.worktrees(root)
    if not wts:
        return None
    here = [w for w in wts if not w.bare and _same_path(w.path, root)]
    if not here or here[0].branch != record.branch:
        return ''
    return wts[0].path


def code_mains(root):
    """The code repository's main branches when `root` is its PM data worktree (_main_checkout):
    the main checkout's configured mains (Config.guarded_mains), or FALLBACK_MAINS when that
    cannot be read. () for any other checkout, or when the PM data record cannot be read (no data
    branch is known). Failing open there is deliberate: a landing warns that it could not read
    that store."""
    path = _main_checkout(root)
    if path == '':
        return ()
    if path is None:
        return FALLBACK_MAINS
    try:
        return tuple(kconfig.load(path).guarded_mains())
    except (kconfig.ConfigError, OSError):
        return FALLBACK_MAINS


def code_config(root):
    """The main checkout's Config when `root` is the PM data worktree (_main_checkout), else None;
    None too when it cannot be read. kitlib.backlog.opener.open_store measures the code main on
    its `remote` (docs/backlog.md "The fold-in guard"); with None the store's own configuration is
    used, as for any other checkout."""
    path = _main_checkout(root)
    if not path:
        return None
    try:
        return kconfig.load(path)
    except (kconfig.ConfigError, OSError):
        return None


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def main_branches(ctx):
    """The branch names a pull through `ctx` may not record: its main branch, and the code
    repository's mains when it is the PM data worktree (code_mains)."""
    out = [ctx.main_branch]
    out.extend(m for m in code_mains(ctx.root) if m not in out)
    return out


def change_session(snap, rid):
    """The `session:` field of record `rid`, or None."""
    rec = snap.get(rid)
    return rec.get('session') if rec else None


def plan_pull(snap, ctx, rid, reason=None, may_collide=False, blocker=None):
    """plan.plan_pull, refused when the branch it records is a main branch (main_branches). The
    record's own refusals come first: an unpullable record says why before where it is pulled
    from."""
    change = pmod.plan_pull(snap, ctx, rid, reason, may_collide, blocker)
    why = refusal(rid, ctx.current_branch(), main_branches(ctx), change_session(snap, rid))
    if why:
        raise Refusal(why)
    return change
