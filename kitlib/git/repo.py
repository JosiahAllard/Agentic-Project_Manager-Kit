"""Which repository, work tree and checkout a path is in (from the PM kit's checkout_guard.py,
_repo_guard.py and worktree_paths.py).

classify(path) answers one `git rev-parse --path-format=absolute --git-dir --git-common-dir
--show-toplevel` (`--path-format=absolute` is load-bearing: without it git answers relative paths
whose comparison means nothing):

  MAIN     git dir == common dir: the repository's main checkout
  LINKED   a linked worktree (`git worktree add`)
  NONE     git says "not a git repository": no checkout at all
  UNKNOWN  anything else, a timeout included: "could not tell", never a silent pass

A path that does not exist yet is asked about through its nearest existing directory. Every git
child here runs through cmd.call: inherited GIT_DIR and friends are dropped, and a hung git
times out.
"""
import collections
import os
import re

from . import cmd

MAIN, LINKED, NONE, UNKNOWN = 'MAIN', 'LINKED', 'NONE', 'UNKNOWN'

# `top` is the work tree's root for MAIN and LINKED, else None; `detail` is git's own words when
# it failed; `git_dir` and `common` are normalised absolute paths for MAIN and LINKED.
Place = collections.namedtuple('Place', 'kind top detail git_dir common')

Worktree = collections.namedtuple('Worktree', 'path head branch detached bare locked prunable')


def norm(path):
    """`path` absolute, normalised and case-folded: the form two paths are compared in."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def real(path):
    """norm() after resolving junctions and symlinks: where a path really is."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def same_path(a, b):
    return norm(a) == norm(b)


def inside(path, root):
    """True when `path` is `root` or below it (both compared as norm() forms)."""
    p, r = norm(path), norm(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def existing_dir(path):
    """The nearest existing directory at or above `path` (a file answers its own directory)."""
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return p
        p = parent
    return p


def classify(path):
    """Place(kind, top, detail, git_dir, common) for `path`; see the module docstring."""
    d = existing_dir(path)
    r = cmd.call(['rev-parse', '--path-format=absolute', '--git-dir', '--git-common-dir',
                  '--show-toplevel'], d)
    err = r.err
    if r.rc is None:
        return Place(UNKNOWN, None, 'git did not answer: %s' % err, None, None)
    if r.rc != 0:
        if 'not a git repository' in err.lower():
            return Place(NONE, None, err, None, None)
        return Place(UNKNOWN, None, err or 'git rev-parse exited %d with no message' % r.rc, None, None)
    lines = cmd.text(r.out).strip().splitlines()
    if len(lines) != 3:
        return Place(UNKNOWN, None, 'git rev-parse printed %d line(s), expected 3: %r'
                     % (len(lines), cmd.text(r.out)), None, None)
    git_dir, common, top = (os.path.normpath(x) for x in lines)
    kind = MAIN if same_path(git_dir, common) else LINKED
    return Place(kind, top, '', real(git_dir), real(common))


def common_dir(path):
    """`(git common dir, None)` of the repository `path` is in, or `(None, why)`; real() form.
    Two work trees of one repository share it; nothing else does."""
    out, why = cmd.run_text(['rev-parse', '--path-format=absolute', '--git-common-dir'],
                            existing_dir(path))
    if not out:
        return None, why or 'git rev-parse --git-common-dir printed nothing'
    return real(out), None


def git_path(path, name):
    """`(absolute path of git's own file `name` for the work tree at `path`, None)` (git rev-parse
    --git-path), or `(None, why)`. git decides per name: MERGE_HEAD, HEAD and any name git does not
    know are per work tree; refs/, config and the others git lists as shared are in the common dir.
    A kit file every work tree must share goes under common_dir(), never here."""
    out, why = cmd.run_text(['rev-parse', '--path-format=absolute', '--git-path', name],
                            existing_dir(path))
    if not out:
        return None, why or 'git rev-parse --git-path printed nothing'
    return os.path.normpath(out), None


def parse_worktrees(data):
    """[Worktree] from `git worktree list --porcelain -z` bytes, in git's order (main first)."""
    out, cur = [], None
    for field in cmd.text(data).split('\0'):
        if not field:
            if cur:
                out.append(Worktree(**cur))
            cur = None
            continue
        key, _, value = field.partition(' ')
        if key == 'worktree':
            if cur:
                out.append(Worktree(**cur))
            cur = dict(path=os.path.normpath(value), head=None, branch=None, detached=False,
                       bare=False, locked=None, prunable=None)
        elif cur is None:
            continue
        elif key == 'HEAD':
            cur['head'] = value
        elif key == 'branch':
            cur['branch'] = value[len('refs/heads/'):] if value.startswith('refs/heads/') else value
        elif key in ('detached', 'bare'):
            cur[key] = True
        elif key in ('locked', 'prunable'):
            cur[key] = value
    if cur:
        out.append(Worktree(**cur))
    return out


def worktrees(path):
    """`([Worktree], None)` for the repository `path` is in, main work tree first, or `(None, why)`."""
    data, why = cmd.run(['worktree', 'list', '--porcelain', '-z'], existing_dir(path))
    if data is None:
        return None, why
    return parse_worktrees(data), None


def linked_worktrees(path):
    """Paths of every linked worktree of the repository at `path` ([] when git fails)."""
    wts, _why = worktrees(path)
    return [w.path for w in (wts or [])[1:]]


def main_checkout_toplevel(path, place=None):
    """The MAIN checkout's toplevel of the repository `path` is in (path may be the main checkout
    or any linked worktree), or None when git cannot place it. `place` is classify(path) when the
    caller already has it."""
    place = place or classify(path)
    if place.kind == MAIN:
        return place.top
    if place.kind == LINKED:
        wts, _why = worktrees(place.top)
        return wts[0].path if wts else None
    return None


def is_main_checkout(path):
    """True only when git places `path` in a main checkout (UNKNOWN is not True: callers that must
    fail closed test `classify(path).kind != LINKED` instead)."""
    return classify(path).kind == MAIN


def same_repository(a, b):
    """`(True/False, None)` when both paths are in git repositories, else `(None, why)`. Two work
    trees of one repository share the common dir."""
    ca, why_a = common_dir(a)
    cb, why_b = common_dir(b)
    if ca is None or cb is None:
        return None, why_a or why_b
    return ca == cb, None


def not_a_directory(path):
    """`'<path> is not a directory'`, with a hint added when `path` looks like a Windows path
    whose separators were stripped (a drive letter, then text with no `\\` or `/`): Git Bash
    drops the backslashes of an unquoted `-C Z:\\GitHub\\x`."""
    msg = '%s is not a directory' % path
    if re.match(r'^[A-Za-z]:[^\\/]+$', path):
        msg += ('; an unquoted backslash path in Git Bash loses its backslashes; quote the path'
                ' or use forward slashes')
    return msg


def toplevel(path):
    """`(top level, None)` of the work tree `path` is in, any repository, or `(None, why)`.

    A tool's -C is a plain path option: the checkout to act on is the one -C names (or the
    current directory), and no tool hands off to a copy of itself inside that checkout (there
    is one kit copy)."""
    if not os.path.isdir(path):
        return None, not_a_directory(path)
    place = classify(path)
    if place.kind not in (MAIN, LINKED) or not place.top:
        return None, '%s is in no git work tree git could name (%s%s)' % (
            path, place.kind, ': %s' % place.detail if place.detail else '')
    return place.top, None


class WriteRefused(Exception):
    """The path is not one this worktree may write. The message says why."""


def check_write(p, root):
    """The absolute, resolved path for `p` if the linked worktree `root` may write it, else raise
    WriteRefused (from worktree_paths.check). A relative `p` is joined to `root`, never to the
    current directory. Allowed: inside `root`, or outside every git work tree. Refused: `root`
    itself not a linked worktree, the main checkout, any other worktree, a relative path whose
    `..` climbs out of root, and a path git cannot place. Junctions and symlinks are resolved
    first, so one pointing into the main checkout is judged as the main checkout."""
    own = classify(root)
    if own.kind != LINKED or not own.top or not same_path(own.top, root):
        if own.kind == MAIN:
            raise WriteRefused('%s is the MAIN checkout, not a linked worktree' % root)
        raise WriteRefused('%s is not a linked worktree root (git: %s, %s)'
                           % (root, own.kind, own.detail or own.top))
    relative = not os.path.isabs(p)
    spelled = os.path.normpath(os.path.join(root, p) if relative else os.path.abspath(p))
    if relative and not inside(spelled, root):
        raise WriteRefused('%s climbs out of this worktree (%s) to %s' % (p, root, spelled))
    full = os.path.realpath(spelled)
    if inside(full, os.path.realpath(root)):
        return full
    place = classify(full)
    if place.kind == NONE:
        return full
    if place.kind == MAIN:
        raise WriteRefused('%s is in the MAIN checkout (%s), which is lander-only' % (full, place.top))
    if place.kind == LINKED:
        raise WriteRefused('%s is in another worktree (%s), not this one (%s)' % (full, place.top, root))
    raise WriteRefused('could not tell which checkout %s is in (git: %s)' % (full, place.detail))
