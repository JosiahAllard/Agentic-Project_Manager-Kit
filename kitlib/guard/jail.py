"""The write jail's roots and verdicts (docs/guard.md "Write jail"). rules.write_jail and
rules.git_jail turn a verdict into a deny; this module only reads paths and the config.

An agent session writes only inside its roots:
  its checkout     the git top of the folder the session runs in (a linked worktree: that
                   worktree alone; a main checkout: the whole checkout). Outside git, the folder
                   it runs in, unless that is a drive root or the home folder. None when it is
                   on, over or inside either machine config folder (config_dirs).
  temp             the system temp folder (Ctx.tempdir, and TMP when it differs), and the agents'
                   temp folder AGENT_TEMP names when it is set. The session scratch folder
                   Claude Code makes lives under the system one.
  data roots       .kit.json "guard".data_roots of the session's repository, read from the main
                   checkout only (Ctx.config): {path, mode}, mode one of
                     open       write anything
                     add-only   create new files; never overwrite, move or delete one
                     read-only  no write
  mark roots       the machine config's guard_mark_roots for a main session's launcher mark
                   ({mark: [{path, mode, why}]}, `~/` read as the home folder); a subagent gets
                   none
A root of any kind on, over or inside either machine config folder is dropped unless it is
read-only (roots, touches_config): a data root, TMP or AGENT_TEMP at the home folder would
otherwise open the machine config. A root's mode holds wherever it nests: a target inside a read-only data root is denied though
it also lies in the checkout or temp, and one inside an add-only root may only be new.

Paths compare after kitlib.guard.paths.norm(real=True): os.path.realpath (symlinks and junctions
resolved, `..` folded), then os.path.normcase.
"""
import os

from kitlib.config import user as kuser
from kitlib.config.migration import OLD_DIR_NAME
from kitlib.git import repo
from kitlib.guard import paths

OPEN, ADD_ONLY, READ_ONLY = 'open', 'add-only', 'read-only'
MODES = (ADD_ONLY, READ_ONLY, OPEN)
OUTSIDE = 'outside'

CHECKOUT, TEMP, DATA, MARK = 'your checkout', 'temp', 'data', 'mark'


def real(path):
    """`path` as the filesystem reaches it, case folded: links and junctions resolved."""
    return paths.norm(path, real=True)


def inside(path, root):
    """True when the real()ed `path` is `root` or below it."""
    return path == root or path.startswith(root.rstrip('\\/') + os.sep)


class Root(object):
    __slots__ = ('path', 'kind', 'mode', 'real')

    def __init__(self, path, kind, mode=OPEN):
        self.path, self.kind, self.mode = path, kind, mode
        self.real = real(path)

    def label(self):
        if self.kind in (DATA, MARK):
            return '"%s" (%s, %s)' % (fwd(self.path), self.kind, self.mode)
        return '"%s" (%s)' % (fwd(self.path), self.kind)


def fwd(p):
    return p.replace('\\', '/')


def own_root(ctx, cwd, is_root):
    """The session's own write root: the git top of `cwd`, else `cwd` itself outside git when
    is_root(cwd) is false (a drive root or the home folder is never one). None when that root is
    on, over or inside either machine config folder (config_dirs): a session run there would
    otherwise write the machine config, its own guard settings and ledger_path among them."""
    if not cwd:
        return None
    place = ctx.place(cwd)
    if place.kind in (repo.MAIN, repo.LINKED) and place.top:
        own = place.top
    elif is_root(cwd):
        return None
    else:
        own = paths.norm(cwd, fold=False)
    return None if touches_config(ctx, own) else own


def touches_config(ctx, path):
    """True when `path` is, holds or lies inside either machine config folder (config_dirs)."""
    p = real(path)
    return any(inside(p, c) or inside(c, p) for c in (real(d) for d in config_dirs(ctx)))


AGENT_TEMP = 'AGENT_TEMP'     # the agents' temp folder (optional/comfy and the temp sweep use it too)


def temp_roots(ctx):
    out, seen = [], set()
    for p in (ctx.tempdir, ctx.environ.get('TMP'), ctx.environ.get(AGENT_TEMP)):
        if p and os.path.isabs(p) and real(p) not in seen:
            seen.add(real(p))
            out.append(p)
    return out


def bash_tmp(ctx):
    """The folder Git Bash's /tmp stands for: AGENT_TEMP when it is set (an install that names an
    agents' temp folder mounts /tmp there), else the system temp folder (Git Bash's default
    mount). The mount table is not read."""
    p = ctx.environ.get(AGENT_TEMP)
    return p if p and os.path.isabs(p) else ctx.tempdir


def data_roots(ctx, cwd):
    """[(path, mode)] from the .kit.json of the main checkout `cwd` belongs to; [] when there is
    none or it does not load."""
    cfg = ctx.config(cwd) if cwd else None
    if cfg is None:
        return []
    return [(e['path'], e['mode']) for e in cfg.section('guard').get('data_roots') or []]


def config_dirs(ctx):
    """Both machine config folders under ctx.home, the current (kitlib.config.user.DIR_NAME) and
    the retired one (kitlib.config.migration.OLD_DIR_NAME), whichever kitlib.config.user.locate
    reads: no root that may write (roots) may be on, hold or lie in either. A root on the
    new folder while only the old one has a config would let the session write the file locate
    then reads instead."""
    return [os.path.join(ctx.home, kuser.DIR_NAME), os.path.join(ctx.home, OLD_DIR_NAME)]


def mark_roots(ctx, mark):
    """[(path, mode)] from the machine config's guard_mark_roots for the session mark `mark`
    (case ignored; None: none). `~/` reads as ctx.home. roots drops one that is either machine
    config folder (config_dirs), holds it or lies inside it, unless it is read-only, so no session
    can be given its own guard settings. A config the strict reader refuses has no mark roots
    (Ctx.user reads it tolerantly, and a map is not kept)."""
    table = ctx.user().get('guard_mark_roots') if mark else None
    if not isinstance(table, dict):
        return []
    want = str(mark).strip().lower()
    out = []
    for name, entries in table.items():
        if not isinstance(name, str) or name.strip().lower() != want or not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            path = kuser.expand_home(e.get('path'), ctx.home)
            if not isinstance(path, str) or not os.path.isabs(path) or e.get('mode') not in MODES:
                continue
            out.append((path, e['mode']))
    return out


def roots(ctx, cwd, is_root, mark=None):
    """[Root] for a session running in `cwd`, its own checkout first; `mark` (a main session's
    launcher mark, None for a subagent) adds its guard_mark_roots. A root of any kind that is on,
    holds or lies inside either machine config folder (touches_config) is dropped unless it is
    read-only: a data root, TMP or AGENT_TEMP at the home folder or above would otherwise let the
    session write the machine config. A read-only root only denies, so it is kept."""
    out = []
    own = own_root(ctx, cwd, is_root)
    if own:
        out.append(Root(own, CHECKOUT))
    out.extend(Root(p, TEMP) for p in temp_roots(ctx))
    out.extend(Root(p, DATA, mode) for p, mode in data_roots(ctx, cwd))
    out.extend(Root(p, MARK, mode) for p, mode in mark_roots(ctx, mark))
    return [r for r in out if r.mode == READ_ONLY or not touches_config(ctx, r.path)]


def judge(target, rs):
    """(verdict, root): (None, None) when `target` may be written; else OUTSIDE, READ_ONLY or
    ADD_ONLY (the target exists) with the root that decided it."""
    t = real(target)
    holders = [r for r in rs if inside(t, r.real)]
    for r in holders:
        if r.mode == READ_ONLY:
            return READ_ONLY, r
    for r in holders:
        if r.mode == ADD_ONLY and os.path.lexists(target):
            return ADD_ONLY, r
    if holders:
        return None, None
    return OUTSIDE, None


def destination(rs):
    """Where the alternative aims: the session's own checkout, else the temp folder."""
    for kind in (CHECKOUT, TEMP):
        for r in rs:
            if r.kind == kind:
                return r
    return None


def describe(rs):
    return ', '.join(r.label() for r in rs) or 'none'
