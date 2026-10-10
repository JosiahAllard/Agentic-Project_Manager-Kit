"""The repository's worktrees (worktree.c get_worktrees) and the files a linked one is made of."""
import os

from . import refs as _refs
from .repo import Repo, read_gitfile, slash


class Worktree:
    """One worktree: `path` ('/' form, as git prints it), `gitdir` (os form), `head_ref` (the
    branch HEAD points at, or None), `head_oid` (None when unborn), `bare`, `detached`, `id`
    (None for the main one), `locked` (None, or the reason text, '' for no reason)."""

    def __init__(self, path, gitdir, wid=None, bare=False):
        self.path = path
        self.gitdir = gitdir
        self.id = wid
        self.bare = bare
        self.head_ref = None
        self.head_oid = None
        self.detached = False
        self.locked = None
        self.prunable = None

    @property
    def is_main(self):
        return self.id is None

    @property
    def path_out(self):
        return self.path

    def os_path(self):
        return os.path.normpath(self.path)


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return None


def _head(repo, wt):
    """Fill head_ref / head_oid / detached from the worktree's HEAD file."""
    text = _read(os.path.join(wt.gitdir, 'HEAD'))
    if text is None:
        return
    t = text.strip()
    if t.startswith('ref:'):
        wt.head_ref = t[4:].strip()
        oid, _ = _refs.resolve(repo, wt.head_ref)
        wt.head_oid = oid
    else:
        wt.detached = True
        wt.head_oid = t[:repo.hexlen]


def main_worktree(repo):
    common = repo.commondir
    bare = repo.config.bool('core.bare', False) or os.path.basename(common).lower() != '.git'
    if bare and os.path.basename(common).lower() != '.git':
        w = Worktree(slash(os.path.realpath(common)), common, None, bare=True)
    else:
        w = Worktree(slash(os.path.realpath(os.path.dirname(common))), common, None,
                     bare=repo.config.bool('core.bare', False))
    _head(repo, w)
    return w


def linked_worktrees(repo):
    base = os.path.join(repo.commondir, 'worktrees')
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for wid in names:
        gd = os.path.join(base, wid)
        if not os.path.isdir(gd):
            continue
        gitfile = _read(os.path.join(gd, 'gitdir'))
        if gitfile is None:
            path = slash(gd)
        else:
            p = gitfile.strip()
            if not os.path.isabs(p):
                p = os.path.normpath(os.path.join(gd, p))
            p = slash(p)
            path = p[:-len('/.git')] if p.endswith('/.git') else p
        w = Worktree(path, gd, wid)
        _head(repo, w)
        lk = _read(os.path.join(gd, 'locked'))
        w.locked = lk
        if lk is None:
            if gitfile is None:
                w.prunable = 'gitdir file does not exist'
            else:
                target = gitfile.strip()
                if not os.path.isabs(target):
                    target = os.path.join(gd, target)
                if not os.path.exists(target):
                    w.prunable = 'gitdir file points to non-existent location'
        out.append(w)
    return out


def all_worktrees(repo):
    return [main_worktree(repo)] + linked_worktrees(repo)


def repo_for(repo, wt):
    """A Repo for worktree `wt` of `repo` (same environment and -c config)."""
    if wt.is_main:
        if wt.bare:
            return Repo(repo.commondir, None, repo.env, repo.cli_config)
        return Repo(repo.commondir, os.path.normpath(wt.path), repo.env, repo.cli_config)
    return Repo(wt.gitdir, os.path.normpath(wt.path), repo.env, repo.cli_config, gitdir_from_env=True)


def find(repo, arg, cwd):
    """The worktree `arg` (a path, relative to cwd, or a unique id suffix) names, or None."""
    wts = all_worktrees(repo)
    full = os.path.normcase(os.path.realpath(os.path.join(cwd, arg)))
    for w in wts:
        if os.path.normcase(os.path.realpath(w.os_path())) == full:
            return w
    # find_worktree_by_suffix: the trailing path components
    suffix = slash(arg).rstrip('/')
    hits = [w for w in wts if not w.is_main and
            (w.path == suffix or w.path.endswith('/' + suffix))]
    if len(hits) == 1:
        return hits[0]
    return None


def current(repo):
    """The Worktree object for `repo` itself."""
    for w in all_worktrees(repo):
        if os.path.normcase(os.path.normpath(w.gitdir)) == os.path.normcase(os.path.normpath(repo.gitdir)):
            return w
    return None


__all__ = ['Worktree', 'all_worktrees', 'find', 'current', 'repo_for', 'read_gitfile']
