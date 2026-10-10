"""The caller's checkout (docs/gate.md, The caller's checkout).

A stub (kitlib.stubs) runs its kit tool in the stub's checkout when the caller's directory is
outside it, so `-C .` names the stub's checkout. For a branch gate that is a hazard: a shell in
one checkout that runs another checkout's stub gates that other checkout, not its own. When a stub
moves the caller, it passes `[stub checkout, caller directory]` on in the variable VAR.

take() removes VAR from the environment (no step the gate runs inherits it) and returns the
caller's directory when the variable was set for this process. refusal() is the rule: the gate
is refused when the caller's directory is in a checkout other than the one it gates. A caller in
no repository passes; a checkout git cannot place is refused (never a silent pass).
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
