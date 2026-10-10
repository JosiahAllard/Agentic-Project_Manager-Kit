"""The caller's checkout (docs/gate.md, The caller's checkout).

A stub (kitlib.stubs) runs its kit tool in the stub's checkout when the caller's directory is
outside it, so `-C .` names the stub's checkout. For a branch gate that is a hazard: a shell in
one checkout that runs another checkout's stub gates that other checkout, not its own. When a stub
moves the caller, it passes `[stub checkout, caller directory]` on in the variable VAR.

take() removes VAR from the environment (no step the gate runs inherits it) and returns the
caller's directory when the variable was set for this process. refusal() is the rule: the gate
is refused when the caller's directory is in a checkout other than the one it gates. A caller in
no repository passes; a checkout git cannot place is refused (never a silent pass).

With no stub there is a second hazard, in a repository that holds the kit itself: every checkout
has a real tools/gate.py, so `python <worktree>/tools/gate.py` run from the main checkout (no -C)
gates the main checkout with the worktree's code, and so does a relative -C such as `-C .`.
own_copy_refusal() refuses that: no -C or a relative one, and the gate.py's own checkout and the
tree it would gate are two checkouts of one repository. An absolute -C names the tree on purpose.
log_dir_refusal() refuses a relative --log-dir that would be made in a checkout other than the
gated one (a stray file there).
"""
import json
import os

from kitlib import gate as G
from kitlib.git import repo
from kitlib.stubs import CALLER_VAR as VAR


def same_directory(a, b):
    """True when `a` and `b` are one directory, however spelt (junction, symlink, 8.3 short name).
    A spelling compare would drop this process's own value and skip the check."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):    # one is gone or unusable: compare where the paths really are
        try:
            return repo.real(a) == repo.real(b)
        except (OSError, ValueError):
            return False


def take(environ=None, cwd=None):
    """`(caller directory, None)`, `(None, None)` when no stub moved this process, or
    `(None, why)` when VAR is malformed. VAR is removed from `environ` (default os.environ)
    in every case. A value whose stub checkout is not `cwd` (default os.getcwd()) was set for
    another process further up, and is dropped."""
    env = os.environ if environ is None else environ
    raw = env.pop(VAR, None)
    if raw is None:
        return None, None
    try:
        pair = json.loads(raw)
    except ValueError:
        pair = None
    if not (isinstance(pair, list) and len(pair) == 2
            and all(isinstance(p, str) and p and '\0' not in p for p in pair)):
        return None, '%s is not [stub checkout, caller directory]: %r' % (VAR, raw[:200])
    stub_root, here = pair
    if not same_directory(stub_root, os.getcwd() if cwd is None else cwd):
        return None, None
    return here, None


def refusal(root, here, classify=repo.classify):
    """None when a caller in `here` may gate the checkout `root` is in, else a REFUSED Outcome."""
    mine = classify(here)
    if mine.kind == repo.NONE:
        return None
    if mine.kind not in (repo.MAIN, repo.LINKED):
        return G.Outcome(G.REFUSED, [
            'REFUSED: cannot tell which checkout the caller\'s directory %s is in: %s; nothing '
            'ran' % (here, mine.detail)])
    gated = classify(root)
    if gated.kind not in (repo.MAIN, repo.LINKED):
        return G.Outcome(G.REFUSED, [
            'REFUSED: cannot tell which checkout %s is in: %s; nothing ran' % (root, gated.detail)])
    if gated.git_dir == mine.git_dir:
        return None
    return G.Outcome(G.REFUSED, [
        'REFUSED: the caller is in the checkout %s, but this gate would test %s (the stub of '
        'another checkout); nothing ran. Run the gate from inside the checkout you mean, or '
        'name it with -C' % (mine.top, gated.top)])


def own_copy_refusal(cwd, kit_root, rel=None, classify=repo.classify):
    """None when a gate.py in `kit_root`, run with no stub, may gate the tree `cwd`; else a
    REFUSED Outcome. `cwd` is the directory it was run from (no -C), or where the relative -C
    `rel` resolved; an absolute -C names the tree on purpose and is never checked here. Refused
    when the two are different checkouts of one repository (same git common dir, different git
    dir): the kit's own repository, where the code under test and the tree gated would come from
    two checkouts. A kit in another repository (a repository's stubs run the kit checkout written
    into them), the same checkout, or a `cwd` in no repository passes; a gate.py whose checkout
    git cannot place is refused."""
    mine = classify(cwd)
    if mine.kind not in (repo.MAIN, repo.LINKED):
        return None                 # the gate itself refuses a tree git cannot place
    kit = classify(kit_root)
    if kit.kind == repo.NONE:
        return None
    gate = os.path.join(kit_root, 'tools', 'gate.py')
    if kit.kind not in (repo.MAIN, repo.LINKED):
        return G.Outcome(G.REFUSED, [
            'REFUSED: cannot tell which checkout %s is in: %s; nothing ran. Name the tree with an '
            'absolute -C' % (gate, kit.detail)])
    if kit.common != mine.common or kit.git_dir == mine.git_dir:
        return None
    named = ('given the relative -C %s (%s)' % (rel, cwd) if rel
             else 'run from %s' % cwd)
    return G.Outcome(G.REFUSED, [
        'REFUSED: %s is in the checkout %s, and it was %s, in the checkout %s of the same '
        'repository: with no -C or a relative one it would gate %s with the code of %s; nothing '
        'ran. Name the tree with an absolute -C: python %s -C %s' % (
            gate, kit.top, named, mine.top, mine.top, kit.top, gate, kit.top)])


def log_dir_refusal(log_dir, root, cwd, classify=repo.classify):
    """None when --log-dir `log_dir` may be used for a gate of the checkout `root` run from `cwd`;
    else a USAGE Outcome: a relative --log-dir resolves against `cwd`, so when `cwd` is in another
    checkout than `root` the gate's logs would be made in that checkout, a stray file there.
    An absolute one, none, or a `cwd` in no repository passes."""
    if not log_dir or os.path.isabs(log_dir):
        return None
    mine = classify(cwd)
    if mine.kind not in (repo.MAIN, repo.LINKED):
        return None
    gated = classify(root)
    if gated.kind in (repo.MAIN, repo.LINKED) and gated.git_dir == mine.git_dir:
        return None
    return G.Outcome(G.USAGE, [
        'USAGE: --log-dir %s is relative, so it would be made under %s (the checkout %s), not in '
        'the gated tree %s; give --log-dir as an absolute path' % (
            log_dir, cwd, mine.top, gated.top or root)])
