"""`pull` never records the main branch as `pulled_by` (docs/backlog-store.md
"Lifecycle changes").

A pull records the branch it was made from (plan.Context.current_branch: `--as`, else the branch
checked out in -C). Made in the main checkout, that is the main branch, which never lands, so the
landing's close (kitlib.backlog.at_landing, which closes what the landed branch holds pulled)
would never match the record and it would stay pulled. plan_pull refuses that pull and says how to
make it: from the branch's worktree, or with `--as <branch>`.

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


def refusal(rid, branch, main_branch):
    """Why `rid` may not be pulled as `branch`, or None. `main_branch`: one name, or the names
    (main_branches) a pull may not record. Pure."""
    mains = (main_branch,) if isinstance(main_branch, str) else tuple(main_branch)
    if branch not in mains:
        return None
    return ('a pull of %s here would record pulled_by: %s, and the main branch never lands, so '
            'no landing would close %s: pull it from the branch\'s worktree (backlog.py -C '
            '<worktree> pull %s), or name the branch with --as <branch>'
            % (rid, branch, rid, rid))


def code_mains(root):
    """The code repository's main branches when `root` is its PM data worktree (the repository has
    a PM data record and `root` has the recorded data branch checked out): the main checkout's
    configured mains (Config.guarded_mains), or FALLBACK_MAINS when that cannot be read. () for
    any other checkout, or when the PM data record cannot be read (no data branch is known).
    Failing open there is deliberate: a landing warns that it could not read that store."""
    common, _why = gitrepo.common_dir(root)
    if common is None:
        return ()
    try:
        record = pmdata.read(common)
    except pmdata.PmDataError:
        return ()
    if record is None:
        return ()
    wts, _why = gitrepo.worktrees(root)
    if not wts:
        return FALLBACK_MAINS
    here = [w for w in wts if not w.bare and _same_path(w.path, root)]
    if not here or here[0].branch != record.branch:
        return ()
    try:
        return tuple(kconfig.load(wts[0].path).guarded_mains())
    except (kconfig.ConfigError, OSError):
        return FALLBACK_MAINS


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def main_branches(ctx):
    """The branch names a pull through `ctx` may not record: its main branch, and the code
    repository's mains when it is the PM data worktree (code_mains)."""
    out = [ctx.main_branch]
    out.extend(m for m in code_mains(ctx.root) if m not in out)
    return out


def plan_pull(snap, ctx, rid, reason=None, ruling=None, blocker=None):
    """plan.plan_pull, refused when the branch it records is a main branch (main_branches). The
    record's own refusals come first: an unpullable record says why before where it is pulled
    from."""
    change = pmod.plan_pull(snap, ctx, rid, reason, ruling, blocker)
    why = refusal(rid, ctx.current_branch(), main_branches(ctx))
    if why:
        raise Refusal(why)
    return change
