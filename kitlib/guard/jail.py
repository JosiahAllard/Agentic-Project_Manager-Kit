"""The write jail's roots and verdicts (docs/guard.md "Write jail"). rules.write_jail and
rules.git_jail turn a verdict into a deny; this module only reads paths and the config.

An agent session writes only inside its roots:
  its checkout     the git top of the folder the session runs in (a linked worktree: that
                   worktree alone; a main checkout: the whole checkout). Outside git, the folder
                   it runs in, unless that is a drive root or the home folder. None when it is
                   on, over or inside either home machine config folder, or on or inside the
                   PM repository's (config_dirs, touches_config).
  its worktrees    every linked worktree of the same repository under its main checkout
                   (sibling), so a session in one worktree reaches the others as a session in the
                   main checkout does. The main checkout itself stays outside. Not a listed
                   Root: write_jail and git_jail ask sibling() about an OUTSIDE verdict only.
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
A root of any kind on, over or inside either home machine config folder or (Linux) the user's PM-repo
pointer file (user_pointer_file), or on or inside the PM repository's <pointer>/.pm-kit, is dropped unless it is read-only (roots, touches_config): a data
root, TMP or AGENT_TEMP at the home folder would otherwise open the machine config. A root that
only holds <pointer>/.pm-kit (the PM repository's own checkout) is kept; rules.machine_config
refuses the writes inside that folder. A root's mode holds wherever it nests: a target inside a read-only data root is denied though
it also lies in the checkout or temp, and one inside an add-only root may only be new.

The outer bound (rules.dev_drive_bound, rule dev-drive): a write the roots allow is still refused
when it is not on the Dev Drive, the machine config's dev_drive (dev_root, on_drive); unset, the
bound is off. While it denies, roots drops a data or mark root off the Dev Drive.

Paths compare after kitlib.guard.paths.norm(real=True): os.path.realpath (symlinks and junctions
resolved, `..` folded), then the case fold (os.path.normcase on Windows). On Linux whether a root
allows a target is read with the strict fold (a mount in doubt does not fold); whether a
read-only or add-only root holds it, and every other check here, with the default fold.
"""
import os

from kitlib.config import user as kuser
from kitlib.config.migration import OLD_DIR_NAME
from kitlib.git import repo
from kitlib.guard import paths
from kitlib.platform.linux import paths as linux_paths

OPEN, ADD_ONLY, READ_ONLY = 'open', 'add-only', 'read-only'
MODES = (ADD_ONLY, READ_ONLY, OPEN)
OUTSIDE = 'outside'

CHECKOUT, TEMP, DATA, MARK = 'your checkout', 'temp', 'data', 'mark'


def real(path, strict=False):
    """`path` as the filesystem reaches it, case folded: links and junctions resolved. `strict`:
    the fold for what the jail allows (paths.norm: on Linux, a mount in doubt does not fold)."""
    return paths.norm(path, real=True, strict=strict)


def allow_real(path):
    """real(path, strict=True) on Linux; None elsewhere, where the strict fold is real(path)'s own
    (the caller then uses the real() it already has)."""
    return real(path, strict=True) if paths.posix() else None


def inside(path, root):
    """True when the real()ed `path` is `root` or below it."""
    return path == root or path.startswith(root.rstrip('/' if paths.posix() else '\\/') + os.sep)


class Root(object):
    __slots__ = ('path', 'kind', 'mode', 'real', 'strict')

    def __init__(self, path, kind, mode=OPEN):
        self.path, self.kind, self.mode = path, kind, mode
        self.real = real(path)
        self.strict = allow_real(path) or self.real     # what it allows (real, strict)

    def label(self):
        if self.kind in (DATA, MARK):
            return '"%s" (%s, %s)' % (fwd(self.path), self.kind, self.mode)
        return '"%s" (%s)' % (fwd(self.path), self.kind)


def fwd(p):
    return p.replace('\\', '/')


def own_root(ctx, cwd, is_root):
    """The session's own write root: the git top of `cwd`, else `cwd` itself outside git when
    is_root(cwd) is false (a drive root or the home folder is never one). None when that root
    touches a machine config folder (touches_config): a session run there would otherwise write
    the machine config, its own guard settings and ledger_path among them."""
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


def sibling(ctx, target, cwd):
    """True when `target` lies in a linked worktree of the repository `cwd` belongs to, under that
    repository's main checkout: what a session in the main checkout already reaches through its own
    root, given to a session in a linked worktree too. Never the main checkout itself, the PM data
    worktree (Ctx.place answers it as MAIN; none at all while the guard cannot tell which it is,
    Ctx.pm_data_error), another repository's worktree, a target a link carries out of its
    worktree, or a worktree that touches a machine config folder (touches_config)."""
    if not target or not cwd or ctx.pm_data_error(target):
        return False
    here, there = ctx.place(cwd), ctx.place(target)
    if here.kind not in (repo.MAIN, repo.LINKED) or not here.common:
        return False
    strict = paths.posix()                      # what it allows: real, strict
    if there.kind != repo.LINKED or not there.top or not there.common or \
            real(there.common, strict) != real(here.common, strict):
        return False
    main = ctx.main_top(cwd)
    t, top = real(target, strict), real(there.top, strict)
    if not main or not inside(t, top) or not inside(top, real(main, strict)):
        return False
    return not touches_config(ctx, there.top)


def touches_config(ctx, path):
    """True when `path` is, holds or lies inside either home machine config folder or the user's
    PM-repo pointer file (user_pointer_file), or is or lies inside the PM repository's
    (pointer_config_dir). Holding the PM repository's folder does not count: that is the PM
    repository's own checkout, a root its sessions write in, and rules.machine_config refuses the
    writes inside the folder."""
    p = real(path)
    pointer = user_pointer_file()
    held = home_config_dirs(ctx) + ([pointer] if pointer else [])
    if any(inside(p, c) or inside(c, p) for c in (real(d) for d in held)):
        return True
    d = pointer_config_dir(ctx)
    return d is not None and inside(p, real(d))


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
    """Every machine config folder: the PM repository's (pointer_config_dir) when the pointer
    names one, then both under ctx.home (home_config_dirs)."""
    d = pointer_config_dir(ctx)
    return ([d] if d else []) + home_config_dirs(ctx)


def home_config_dirs(ctx):
    """Both machine config folders under ctx.home, the current (kitlib.config.user.DIR_NAME) and
    the retired one (kitlib.config.migration.OLD_DIR_NAME), whichever kitlib.config.user.locate
    reads: no root that may write (roots) may be on, hold or lie in either. A root on the
    new folder while only the old one has a config would let the session write the file locate
    then reads instead."""
    return [os.path.join(ctx.home, kuser.DIR_NAME), os.path.join(ctx.home, OLD_DIR_NAME)]


def user_pointer_file():
    """Linux: the user's PM-repo pointer file, <profile>/.config/pm-kit/pointer from the password
    database (kitlib.platform.linux.paths.pointer_files, the file kitlib.config.pointer reads when
    there is no machine one). The user can write it, so a session that could would point the kit
    at another PM repository and its machine config. None elsewhere (Windows keeps the pointer in
    HKLM, which only an administrator writes) and when the password database gives no profile
    folder (the kit then reads no user pointer file either). The machine file /etc/pm-kit/pointer
    is an administrator's to write, as HKLM is."""
    if not paths.posix():
        return None
    try:
        return linux_paths.pointer_files()[1]
    except OSError:
        return None


def pointer_config_dir(ctx):
    """The PM repository's machine config folder, <pointer>/.pm-kit (kitlib.config.user.DIR_NAME
    under the PM-repo pointer's value as stored, Ctx.pm_repo_pointer), or None when there is no
    pointer or its value is no absolute path (a relative one names no folder the guard can
    place; rules.machine_config still reads a .pm-kit folder at a checkout's top by name)."""
    pointer = ctx.pm_repo_pointer()
    if not isinstance(pointer, str) or not os.path.isabs(pointer):
        return None
    return os.path.join(pointer, kuser.DIR_NAME)


def mark_roots(ctx, mark):
    """[(path, mode)] from the machine config's guard_mark_roots for the session mark `mark`
    (case ignored; None: none). `~/` reads as ctx.home. roots drops one that touches a machine
    config folder (touches_config), unless it is read-only, so no session
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


def roots(ctx, cwd, is_root, mark=None, dev=None, dropped=None):
    """[Root] for a session running in `cwd`, its own checkout first; `mark` (a main session's
    launcher mark, None for a subagent) adds its guard_mark_roots. A root of any kind that touches
    a machine config folder (touches_config) is dropped unless it is read-only: a data root, TMP or AGENT_TEMP at the home folder or above would otherwise let the
    session write the machine config. A read-only root only denies, so it is kept. `dev`, the Dev
    Drive's root (dev_root) when the outer bound denies: a data or mark root off it that may write
    is dropped too, and appended to the list `dropped` when one is given."""
    out = []
    own = own_root(ctx, cwd, is_root)
    if own:
        out.append(Root(own, CHECKOUT))
    out.extend(Root(p, TEMP) for p in temp_roots(ctx))
    out.extend(Root(p, DATA, mode) for p, mode in data_roots(ctx, cwd))
    out.extend(Root(p, MARK, mode) for p, mode in mark_roots(ctx, mark))
    kept = []
    for r in out:
        if r.mode != READ_ONLY and touches_config(ctx, r.path):
            continue
        if dev and r.mode != READ_ONLY and r.kind in (DATA, MARK) and not on_drive(r.path, dev):
            if dropped is not None:
                dropped.append(r)
            continue
        kept.append(r)
    return kept


def dev_root(ctx):
    """The Dev Drive's root folder, the machine config's dev_drive (kitlib.config.user.dev_drive),
    or None: unset, or a config that does not read (ctx.user's tolerant read sets no key). None
    leaves the outer bound off."""
    try:
        user = ctx.user()
    except Exception:  # noqa: BLE001 - no config read: the bound stays off, as with no key
        return None
    return kuser.dev_drive(user) if isinstance(user, dict) else None


def on_drive(path, dev):
    """True when `path`, real()ed (links and junctions resolved), is `dev` or below it; read with
    the strict fold, as what the jail allows is (allow_real)."""
    t = allow_real(path) or real(path)
    d = allow_real(dev) or real(dev)
    return inside(t, d)


def judge(target, rs):
    """(verdict, root): (None, None) when `target` may be written; else OUTSIDE, READ_ONLY or
    ADD_ONLY (the target exists) with the root that decided it. A read-only or add-only root
    holds the target by the fold for what the guard refuses, an allowing one by the strict fold
    (Root.strict): on Linux a mount in doubt never widens what may be written."""
    t = real(target)
    holders = [r for r in rs if inside(t, r.real)]
    for r in holders:
        if r.mode == READ_ONLY:
            return READ_ONLY, r
    for r in holders:
        if r.mode == ADD_ONLY and os.path.lexists(target):
            return ADD_ONLY, r
    ts = allow_real(target) or t
    if any(inside(ts, r.strict) for r in rs):
        return None, None
    return OUTSIDE, None


def destination(rs, dev=None):
    """Where the alternative aims: the session's own checkout, else the temp folder; never one a
    read-only or add-only root holds (a role whose checkout is read-only is pointed at temp), nor,
    with `dev` (the Dev Drive's root), one off the Dev Drive."""
    for kind in (CHECKOUT, TEMP):
        for r in rs:
            if r.kind == kind and judge(r.path, rs)[0] is None and (not dev or on_drive(r.path, dev)):
                return r
    return None


def describe(rs):
    return ', '.join(r.label() for r in rs) or 'none'
