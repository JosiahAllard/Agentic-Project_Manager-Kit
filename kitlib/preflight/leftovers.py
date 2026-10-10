"""preflight's `leftovers` lines: registered worktrees whose own work has already landed in
`<remote>/<main_branch>`, shown to every session at its start so each is driven to an end (ported
from the PM kit's preflight `leftover_warn`; docs/preflight.md).

kitlib.worktree.remove's own check (`merged_worktrees`), imported, not shelled out to: a fresh
worktree with no commit of its own is not listed, the `"worktree_remove".exempt` folders are not,
and neither is the worktree this run stands in. The base is read as it stands, never fetched.
Nothing here raises: a failure is a `not checked` line naming it.
"""
import os

from kitlib.worktree import remove
from kitlib.worktree.holders import canon

LABEL = 'leftovers '
INDENT = ' ' * 12


def lines(top):
    """The lines for the checkout whose top level is `top`."""
    try:
        ctx, refused = remove.open_ctx(top, cwd=top)
        if refused:
            return [LABEL + 'WARN not checked: %s' % ' '.join(' '.join(refused.lines).split())]
        listing = remove.worktree_listing(ctx.main)
        own = [top] if canon(top) != canon(ctx.main) else []
        merged, why = remove.merged_worktrees(ctx, listing, skip=own)
    except Exception as e:               # noqa: BLE001 - a WARN line must never become the failure
        return [LABEL + 'WARN not checked: %s: %s' % (type(e).__name__, e)]
    s = ctx.s
    where = s['worktrees_dir'].replace('\\', '/')
    if merged is None:
        return [LABEL + 'WARN merged worktrees not checked: %s' % why]
    if not merged:
        return [LABEL + 'none under %s' % where]
    out = [LABEL + 'WARN %d under %s - drive each to an end:' % (len(merged), where)]
    for path, branch in merged:
        slug = os.path.basename(path)
        out.append(INDENT + "%s [%s]  landed (its own commits are in %s's %s): %s %s"
                   % (slug, branch or 'detached', s['remote'], s['base'], s['tool'], slug))
    return out
