"""The render step of the kit's pre-commit hook (kitlib.githooks.precommit; docs/content.md "Render
on commit"): a plain `git commit` in a kit checkout that stages a path under `content/`, or
`kit.json` or `hosts.json`, gets the rendered files made current and staged into the same commit,
as tools/commit.py does (kitlib.content.oncommit).

check(commit) -> (exit code, message or None). Nothing to do (no render source staged, or a work
tree without the kit's layout, which is looked at first, with no git read) is (0, None) before any
process starts. A render or a stage that fails refuses the commit with the command to run by
hand. The files are staged into the index git
gave the hook (GIT_INDEX_FILE, kept by kitlib.git.cmd), so they go into the commit being made.
"""
from kitlib.content import oncommit

PREFIX = 'pre-commit: '


def check(commit):
    """The render step: `commit` has `root` and `staged()`."""
    if not oncommit.is_kit(commit.root) or not oncommit.wants_render(commit.staged()):
        return 0, None
    rendered, why = oncommit.render(commit.root)
    if why is None:
        why = oncommit.stage(commit.root, rendered) if rendered else None
    if why is not None:
        return 1, ('%sa render source is staged, and the rendered files could not be made current: %s. '
                   'Render by hand, stage what it wrote, then commit again: %s; commit refused'
                   % (PREFIX, why, oncommit.command(commit.root)))
    if not rendered:
        return 0, None
    return 0, '%srendered and staged: %s' % (PREFIX, ', '.join(rendered))
