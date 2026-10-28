"""The caller's checkout (docs/gate.md, The caller's checkout).

In a repository that holds the kit itself, every checkout has a real tools/gate.py, so
`python <worktree>/tools/gate.py` run from the main checkout (no -C) gates the main checkout with
the worktree's code, and so does a relative -C such as `-C .`. own_copy_refusal() refuses that:
no -C or a relative one, and the gate.py's own checkout and the tree it would gate are two
checkouts of one repository. An absolute -C names the tree on purpose. log_dir_refusal() refuses
a relative --log-dir that would be made in a checkout other than the gated one (a stray file
there).
"""
import os

from kitlib import gate as G
from kitlib.git import repo


def own_copy_refusal(cwd, kit_root, rel=None, classify=repo.classify):
    """None when a gate.py in `kit_root` may gate the tree `cwd`; else a REFUSED Outcome. `cwd`
    is the directory it was run from (no -C), or where the relative -C `rel` resolved; an
    absolute -C names the tree on purpose and is never checked here. Refused when the two are
    different checkouts of one repository (same git common dir, different git dir): the kit's own
    repository, where the code under test and the tree gated would come from two checkouts. A kit
    in another repository, the same checkout, or a `cwd` in no repository passes.

    A gate.py whose checkout git cannot place (dubious ownership, a git timeout) is refused only
    when its path nests (one inside the other) with the gated checkout or with the folder that
    checkout's git common dir is in, so the same repository could be involved; otherwise it
    passes, as a consumer repository's gate did before this check.

    The refusal names the command for the tree the caller meant, that tree's own gate.py with an
    absolute -C: a relative -C names it (the resolved tree). With no -C it is ambiguous (the
    gate.py's path names one checkout, the directory another), so both commands are given, each
    labelled by intent (advice())."""
    mine = classify(cwd)
    if mine.kind not in (repo.MAIN, repo.LINKED):
        return None                 # the gate itself refuses a tree git cannot place
    kit = classify(kit_root)
    if kit.kind == repo.NONE:
        return None
    gate = os.path.join(kit_root, 'tools', 'gate.py')
    if kit.kind not in (repo.MAIN, repo.LINKED):
        # The gated checkout and the folder its git common dir is in (the main checkout's top).
        tops = (mine.top, os.path.dirname(mine.common))
        if not any(nested(t, kit_root) or nested(kit_root, t) for t in tops):
            return None
        return G.Outcome(G.REFUSED, [
            'REFUSED: cannot tell which checkout %s is in: %s; it nests with the gated tree %s or '
            'its main checkout, so they may be two checkouts of one repository; nothing ran. Name '
            'the tree with an absolute -C, using its own gate.py:' % (gate, kit.detail, mine.top)]
            + advice(mine.top, kit_root, rel))
    if kit.common != mine.common or kit.git_dir == mine.git_dir:
        return None
    named = ('given the relative -C %s (%s)' % (rel, cwd) if rel
             else 'run from %s' % cwd)
    return G.Outcome(G.REFUSED, [
        'REFUSED: %s is in the checkout %s, and it was %s, in the checkout %s of the same '
        'repository: with no -C or a relative one it would gate %s with the code of %s; nothing '
        'ran. Name the tree with an absolute -C, using its own gate.py:' % (
            gate, kit.top, named, mine.top, mine.top, kit.top)]
        + advice(mine.top, kit.top, rel))


RAN_FROM = 'if you meant the directory you ran from'
THIS_GATE = 'if you meant the checkout this gate.py is in'


def advice(mine, kit, rel):
    """The refusal's command lines. A relative -C names the tree meant (`mine`): its command
    alone. With no -C the tree meant is ambiguous: both, each labelled by intent."""
    if rel:
        return [command(mine)]
    return [command(mine, RAN_FROM), command(kit, THIS_GATE)]


def quote(path):
    """`path` as one shell word: in double quotes when it holds a space."""
    return '"%s"' % path if ' ' in path else path


def command(top, intent=None):
    """The line naming the one documented command that gates the checkout `top`, headed by
    `intent`, or by the tree it gates when none."""
    return '  %s: python %s -C %s' % (intent or 'to gate %s' % top,
                                      quote(os.path.join(top, 'tools', 'gate.py')), quote(top))


def nested(path, root):
    """True when `path` is `root` or below it, compared where both really are."""
    try:
        p, r = repo.real(path), repo.real(root)
    except (OSError, ValueError):
        return False
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


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
