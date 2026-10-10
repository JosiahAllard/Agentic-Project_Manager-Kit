"""The write tools' main-checkout refusal (from the PM kit's checkout_guard.refusal).

A tool that writes a checkout (commit.py, sync_base.py) asks `main_refusal` first. It answers
None when the path is in a linked worktree or in no git checkout at all, else the whole message to
print before exiting 2: the main checkout is lander-only, and a path git cannot place is "could not
tell", never a silent pass. There is one kit copy of each tool, so the message names the
`-C <worktree>` form and the repository's linked worktrees, never a copy to run instead.
"""
import os

from . import repo


def main_refusal(path, what, tool, rule_pointer=None):
    """None if `what` may be written at `path`, else the refusal text.

    `what` says what would have been written ("a commit", "a merge"); `tool` is the command's
    name as the user types it (`commit.py`); `rule_pointer` (`.kit.json` "checkout_guard"
    .rule_pointer) is quoted as where the rule is written down."""
    place = repo.classify(path)
    if place.kind in (repo.LINKED, repo.NONE):
        return None
    where = os.path.abspath(path)
    if place.kind != repo.MAIN:
        return ('REFUSED: could not tell whether %s is in the main checkout, so %s was not '
                'written. git said: %s. Nothing was written.' % (where, what, place.detail))
    others = repo.linked_worktrees(place.top)
    listing = ('\n'.join('  %s' % w for w in others) if others
               else '  (none registered: make one with new_worktree.py <area>/<slug>)')
    return ('REFUSED: %s is in the MAIN checkout (%s), so %s was not written. The main checkout '
            'is lander-only%s.\n'
            'Name your worktree, from anywhere: %s -C <worktree> ...\n'
            'Linked worktrees of this repository:\n%s\n'
            'Nothing was written.' % (where, place.top, what,
                                      ' (`%s`)' % rule_pointer if rule_pointer else '', tool,
                                      listing))


# Scratch patch scripts (from the PM kit's worktree_paths.py, GUARD-WORKTREE-WRITES): an agent's
# script that opens a RELATIVE path writes wherever its cwd happens to be, the main checkout
# included. Through these, a relative path joins the worktree `root` the script names, never the
# cwd, and every path is judged by repo.check_write first. There is one kit copy, so the
# root is always the caller's argument, never where this module was loaded from:
#     sys.path.insert(0, r'<kit root>'); from kitlib.git import checkout
#     text = checkout.read_text('docs/x.md', root=r'<your worktree>')
#     checkout.write_text('docs/x.md', text.replace('old', 'new'), root=r'<your worktree>')

def read_text(p, root, encoding='utf-8'):
    """The text of `p` (judged as repo.check_write(p, root) does), newlines as they are on disk."""
    with open(repo.check_write(p, root), encoding=encoding, newline='') as fh:
        return fh.read()


def write_text(p, text, root, encoding='utf-8'):
    """Write `text` to `p` (judged as repo.check_write(p, root) does), creating its folder; the
    absolute path written. Text holding CRLF raises WriteRefused: the kit's repositories keep text
    LF. Nothing is written on a refusal."""
    full = repo.check_write(p, root)
    if '\r\n' in text:
        raise repo.WriteRefused('the text for %s contains CRLF; text files here are LF' % full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', encoding=encoding, newline='\n') as fh:
        fh.write(text)
    return full
