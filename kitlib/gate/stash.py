"""The stash guard (docs/gate.md "The stash guard"): no gate and no landing while the repository's
stash stack holds an entry.

A repository has one stash stack: the ref refs/stash and its log live in the git common dir, so
the main checkout and every linked worktree push to and pop from the same stack. A tree that was
stashed is not the tree its commit names, and an entry one checkout made is one command away from
being applied in another. So the branch gate (kitlib.gate.runner) and a landing (kitlib.gate.land)
ask here before they start and again when their steps have run.

The stack is read as the ref: kitlib.git.refs.ref_commit('refs/stash'). git deletes that ref when
the last entry is dropped, so "the ref exists" covers "the stack holds an entry"; it also refuses
a refs/stash set by hand that `git stash list` prints nothing for. Nothing here runs `git stash`,
and nothing here changes the stack: clearing it is for whoever made the entries.

No answer from git is a refusal, never an empty stack. An entry made and dropped again wholly
within a run leaves nothing to read here."""
from kitlib.git import refs

REF = 'refs/stash'
CANNOT_TELL = 'git cannot tell whether the stash stack is empty: %s'
NOT_EMPTY = 'the stash stack is not empty (%s is at %s): '
CLEAR = ('every checkout of this repository shares the one stack, so an entry is work set aside '
         'outside any commit; list it with `git stash list`, have whoever made each entry commit '
         'it on a branch or drop it, then run this again')
LATE = ('an entry was made while the gate ran, so the steps may have run on a tree that is not '
        'the commit\'s; list it with `git stash list`, clear it, then run this again')


def tip(root):
    """`(commit, None)`: the newest entry of the stash stack of the repository `root` is in;
    `('', None)`: the stack is empty; `(None, why)`: git cannot tell."""
    return refs.ref_commit(REF, root)


def _why(root, then):
    commit, why = tip(root)
    if commit is None:
        return CANNOT_TELL % (why or 'no answer')
    if commit:
        return NOT_EMPTY % (REF, commit[:10]) + then
    return None


def refusal(root):
    """None when the stash stack of the repository `root` is in is empty, else why a gate or a
    landing must not start: one line, for the caller to word as its refusal."""
    return _why(root, CLEAR)


def appeared(root):
    """None when the stack is empty after a run that started with it empty (refusal() was None),
    else why the run's result does not stand: an entry was made while it ran, or git cannot
    tell."""
    return _why(root, LATE)
