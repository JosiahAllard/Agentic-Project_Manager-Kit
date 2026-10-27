"""The guard on Linux, for tests that run on any platform (docs/linux-support-plan.md section 3,
slice 8): on_linux() holds kitlib.platform.NAME at 'linux' and gives each guard module an `os`
whose path is posixpath, so on Windows the guard reads paths as it does on Linux (where os.path
is posixpath already and the stand-in changes nothing). The mount table is a stand-in too
(paths._mounts), and PosixWorld is a fake machine spelt in POSIX paths: a fake git answers
classify from its table, and no folder of it exists. Nothing here touches the real filesystem.

  on_linux(mounts=...)   context manager; `mounts` is [(mount point, fstype)] or None (no table)
  PosixWorld()           /work/repo (main), /work/repo/.claude/worktrees/wt (linked), /home/u,
                         /scratch/tmp (Ctx.tempdir); ctx(**kw) a Ctx on it
"""
import contextlib
import os
import posixpath
from unittest import mock

import kitlib.platform
from kitlib.git import repo
from kitlib.guard import (backlogrole, context, delegfile, fallback, generated, hub, jail, paths,
                          place, prepush, refusal_log, rolepolicy, rules, shell)

from .world import FakeGit, SHA_GATED

GUARD_MODULES = (backlogrole, context, delegfile, fallback, generated, hub, jail, paths, place,
                 prepush, refusal_log, rolepolicy, rules, shell)

EXT4 = [('/', 'ext4')]


class PosixOs(object):
    """The os module with posixpath as its path and '/' as its separator."""
    path = posixpath
    sep = '/'
    altsep = None

    def __getattr__(self, name):
        return getattr(os, name)


@contextlib.contextmanager
def on_linux(mounts=EXT4):
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(kitlib.platform, 'NAME', 'linux'))
        if os.name != 'posix':
            proxy = PosixOs()
            for m in GUARD_MODULES:
                stack.enter_context(mock.patch.object(m, 'os', proxy))
        stack.enter_context(mock.patch.object(paths, '_mounts', lambda: mounts, create=True))
        yield


class PosixGit(FakeGit):
    def where(self, cwd):
        return 'wt' if cwd and posixpath.normpath(cwd).startswith(self.w.wt) else 'main'

    def is_clean(self, cwd):
        return False, None


class PosixWorld(object):
    def __init__(self, base='/work'):
        self.base = base
        self.main = base + '/repo'
        self.wt = self.main + '/.claude/worktrees/wt'
        self.home = '/home/u'
        self.temp = '/scratch/tmp'
        self.kit_dir = base + '/kit'
        self.git = PosixGit(self)
        self.environ = {}
        self.held = False

    def classify(self, path):
        p = posixpath.normpath(path)
        git = self.main + '/.git'
        if p == self.wt or p.startswith(self.wt + '/'):
            return repo.Place(repo.LINKED, self.wt, '', git + '/worktrees/wt', git)
        if p == self.main or p.startswith(self.main + '/'):
            return repo.Place(repo.MAIN, self.main, '', git, git)
        return repo.Place(repo.NONE, None, 'not a git repository', None, None)

    def gate_ok(self, root, sha):
        return ({'commit': sha}, None) if sha == SHA_GATED else (None, 'no gate-OK record for %s' % sha)

    def ctx(self, **kw):
        args = dict(environ=self.environ, home=self.home, classify=self.classify, gate_ok=self.gate_ok,
                    git=self.git, lock_held=lambda: (self.held, None), tempdir=self.temp, kit=self.kit_dir)
        args.update(kw)
        return context.Ctx(**args)
