"""The render step of the kit's pre-commit hook (kitlib.githooks.precommit; docs/content.md "Render
on commit"): a plain `git commit` in a kit checkout that stages a path under `content/`, or
`kit.json` or `hosts.json`, gets the rendered files made current and staged into the same commit,
as tools/commit.py does (kitlib.content.oncommit).

check(commit) -> (exit code, message or None). Nothing to do (no render source staged, or a work
tree without the kit's layout, which is looked at first, with no git read) is (0, None) before any
process starts. A render or a stage that fails refuses the commit with the command to run by
hand. The files are staged into the index git gave the hook (GIT_INDEX_FILE, kept by
kitlib.git.cmd), so they go into the commit being made.

A partial commit (`git commit <paths>`, `--only`) is built from a temporary index that git names
`next-index-<pid>.lock` (PARTIAL_INDEX); git's own index then gets only the named paths. A
rendered file staged into the temporary index would be in the commit but stay old in the index,
and the next commit would undo it. So when the render changed a file there, the commit is refused
with what to stage (the render has already written the files).
"""
import os

from kitlib.content import oncommit

PREFIX = 'pre-commit: '
PARTIAL_INDEX = 'next-index-'      # git's temporary index for a partial commit (builtin/commit.c)


def partial(environ=None):
    """True when the hook runs for a partial commit (module docstring): GIT_INDEX_FILE in
    `environ` (default os.environ) is git's temporary index for one."""
    name = (os.environ if environ is None else environ).get('GIT_INDEX_FILE') or ''
    return os.path.basename(name).startswith(PARTIAL_INDEX)


def check(commit, environ=None):
    """The render step: `commit` has `root` and `staged()`; `environ` (default os.environ) holds
    the GIT_INDEX_FILE git gave the hook."""
    if not oncommit.is_kit(commit.root) or not oncommit.wants_render(commit.staged()):
        return 0, None
    rendered, why = oncommit.render(commit.root)
    if why is None and rendered and partial(environ):
        return 1, ('%sa render source is in a commit of named paths (git commit <paths>), and the render '
                   'changed %s: git would commit them but leave them old in its index, so the next commit '
                   'would undo them. Stage them and the source, then commit without paths: git add -- %s, '
                   'then git commit; commit refused' % (PREFIX, ', '.join(rendered), ' '.join(rendered)))
    if why is None:
        why = oncommit.stage(commit.root, rendered) if rendered else None
    if why is not None:
        return 1, ('%sa render source is staged, and the rendered files could not be made current: %s. '
                   'Render by hand, stage what it wrote, then commit again: %s; commit refused'
                   % (PREFIX, why, oncommit.command(commit.root)))
    if not rendered:
        return 0, None
    return 0, '%srendered and staged: %s' % (PREFIX, ', '.join(rendered))
