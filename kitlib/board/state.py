"""--write-state: the board's facts into ONE marked block of the coordinator's state file; the
judgement fields around it (landing queue, waiting on owner, next) are the coordinator's and are
never touched.

The file is `.kit.json` "board".state_file (relative to the main checkout), else
docs/ops/state.local.md, else ops/state.local.md when the main checkout has ops/ and no docs/ops/.
"""
import os

BEGIN, END = '<!-- board:begin -->', '<!-- board:end -->'
TEMPLATE = ('# Coordinator state (overwritten, <= 8 KB)\n\n%s\n%s\n\n## Landing queue\n\n'
            '## Waiting on owner\n\n## Next\n' % (BEGIN, END))


def path(main_checkout, rel):
    """Where --write-state writes, for the configured `rel` (None when unset)."""
    if rel:
        return os.path.join(main_checkout, rel.strip())
    docs_ops, ops = os.path.join(main_checkout, 'docs', 'ops'), os.path.join(main_checkout, 'ops')
    if not os.path.isdir(docs_ops) and os.path.isdir(ops):
        return os.path.join(ops, 'state.local.md')
    return os.path.join(docs_ops, 'state.local.md')


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
