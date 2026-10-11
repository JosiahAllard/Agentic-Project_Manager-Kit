"""`pull` never records the main branch as `pulled_by` (KIT-PULL-MAIN-CHECKOUT-BRANCH;
docs/backlog-store.md "Lifecycle changes").

A pull records the branch it was made from (plan.Context.current_branch: `--as`, else the branch
checked out in -C). Made in the main checkout, that is the main branch, which never lands, so the
landing's close (kitlib.backlog.at_landing, which closes what the landed branch holds pulled)
would never match the record and it would stay pulled. plan_pull refuses that pull and says how to
make it: from the branch's worktree, or with `--as <branch>`.

The rule lives here, wrapping plan.plan_pull, so tools/backlog.py's `pull` goes through it; it may
move into plan.plan_pull itself later.
"""
from . import plan as pmod
from .errors import Refusal


def refusal(rid, branch, main_branch):
    """Why `rid` may not be pulled as `branch`, or None. Pure."""
    if branch != main_branch:
        return None
    return ('a pull of %s here would record pulled_by: %s, and the main branch never lands, so '
            'no landing would close %s: pull it from the branch\'s worktree (backlog.py -C '
            '<worktree> pull %s), or name the branch with --as <branch>'
            % (rid, branch, rid, rid))


def plan_pull(snap, ctx, rid, reason=None, ruling=None, blocker=None):
    """plan.plan_pull, refused when the branch it records is `ctx.main_branch`. The record's own
    refusals come first: an unpullable record says why before where it is pulled from."""
    change = pmod.plan_pull(snap, ctx, rid, reason, ruling, blocker)
    why = refusal(rid, ctx.current_branch(), ctx.main_branch)
    if why:
        raise Refusal(why)
    return change
