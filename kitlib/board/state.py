"""--write-state: the board's facts into ONE marked block of the coordinator's state file; the
judgement fields around it (landing queue, waiting on owner, next) are the coordinator's and are
never touched.

The file is in the repository's git common dir, <common>/kit/board-state.md: git never lists it, so
it cannot leave the main checkout dirty (a dirty main checkout refuses `gate.py land`). A
`.kit.json` "board".state_file (relative to the main checkout) is used only when it resolves
inside the git common dir (".git/..."); any other path is inside the work tree, so it is refused.
"""
import os

from kitlib.git import repo

BEGIN, END = '<!-- board:begin -->', '<!-- board:end -->'
TEMPLATE = ('# Coordinator state (overwritten, <= 8 KB)\n\n%s\n%s\n\n## Landing queue\n\n'
            '## Waiting on owner\n\n## Next\n' % (BEGIN, END))


def path(main_checkout, rel):
    """`(file --write-state writes, None)` for the configured `rel` (None when unset), or
    `(None, why)` when git cannot say where its common dir is or `rel` lies in the work tree."""
    common, why = repo.common_dir(main_checkout)
    if common is None:
        return None, 'cannot find the git dir: %s' % why
    if not rel:
        return os.path.join(common, 'kit', 'board-state.md'), None
    where = os.path.join(main_checkout, rel.strip())
    if not repo.inside(repo.real(where), common):
        return None, ('"board".state_file %s is inside the work tree, where it would block a '
                      'landing; use a path under the git dir (e.g. .git/kit/board-state.md) or '
                      'leave it unset' % rel.strip())
    return where, None


def fill(body, stamp, text):
    """`body` (None: a new file from TEMPLATE) with only the marked block replaced by `stamp` and
    `text`; a body without the markers gets the block on top."""
    body = TEMPLATE if body is None else body
    if BEGIN not in body or END not in body[body.index(BEGIN):]:
        body = '%s\n%s\n\n%s' % (BEGIN, END, body)
    s = body.index(BEGIN)
    z = body.index(END, s)
    return body[:s] + BEGIN + '\n' + stamp + '\n' + text + '\n' + body[z:]


def write(state, stamp, text):
    """Rewrite the block in the file `state` (created with its folders when missing); returns the
    bytes written."""
    os.makedirs(os.path.dirname(state), exist_ok=True)
    body = None
    if os.path.exists(state):
        with open(state, encoding='utf-8') as fh:
            body = fh.read()
    body = fill(body, stamp, text)
    with open(state, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(body)
    return len(body.encode('utf-8'))
