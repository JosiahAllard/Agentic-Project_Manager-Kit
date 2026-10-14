"""git worktree add / list / remove / lock / unlock / prune (builtin/worktree.c), for the forms
the kit uses. The files a linked worktree is made of are the ones real git writes (gitdir,
commondir, HEAD, ORIG_HEAD, index; the worktree's .git file), so real git reads them back.
"""
import os
import shutil
import stat as _stat

from . import command
from . import index as _index
from . import plumbing as _pl
from . import refs as _refs
from . import revs as _revs
from . import util as _u
from . import wt as _wt
from . import worktrees as _wts
from . import wtcmds as _wc
from .errors import GitExit, Unsupported, die
from .repo import Repo, slash


@command('worktree')
def worktree(ctx, args):
    if not args:
        raise Unsupported('git worktree with no subcommand')
    sub, rest = args[0], args[1:]
    fn = {'add': _add, 'list': _list, 'remove': _remove, 'lock': _lock, 'unlock': _unlock,
          'prune': _prune}.get(sub)
    if fn is None:
        raise Unsupported('git worktree %s' % sub)
    return fn(ctx, rest)


# -- add ------------------------------------------------------------------------------------

_ADD = _u.Spec('worktree add', flags='q|quiet f|force detach no-checkout lock no-track track',
               values='b B reason')


def _dir_nonempty(p):
    try:
        return bool(os.listdir(p))
    except NotADirectoryError:
        return True
    except OSError:
        return False


def _add(ctx, args):
    opts, pos, dd = _u.parse(_ADD, args)
    pos = pos + (dd or [])
    repo = ctx.need_repo()
    quiet = opts.get('quiet')
    if opts.get('no-checkout') or opts.get('track'):
        raise Unsupported('worktree add %s' % ' '.join(args))
    # --force is ported for one case only: checking out a branch another worktree has
    force = opts.get('force')
    if opts.get('reason') and not opts.get('lock'):
        raise die('--reason requires --lock')
    if not pos or len(pos) > 2:
        raise Unsupported('worktree add %s' % ' '.join(args))
    arg = pos[0]
    if not os.path.isabs(arg):
        raise Unsupported('worktree add with a relative path (%s)' % arg)
    path = os.path.normpath(arg)
    newb = opts.get('b') or opts.get('B')
    if opts.get('b') and opts.get('B'):
        raise Unsupported('worktree add -b with -B')
    _wc.other_operation(repo)
    if os.path.lexists(path):
        if _dir_nonempty(path):
            raise die("'%s' already exists" % slash(arg))
    for w in _wts.linked_worktrees(repo):
        if os.path.normcase(os.path.normpath(w.path)) == os.path.normcase(path):
            raise Unsupported('worktree add over a registered but missing worktree')
    if force and opts.get('B'):
        raise Unsupported('worktree add -f -B')
    branch_ref = None            # the branch the new worktree checks out
    start = pos[1] if len(pos) > 1 else None
    detach = bool(opts.get('detach'))
    kind = None
    if newb:
        ref = 'refs/heads/' + newb
        if not _refs.check_refname(ref):
            raise Unsupported('worktree add -b with an invalid branch name (%s)' % newb)
        if _refs.exists(repo, ref):
            if opts.get('b'):
                raise Unsupported('worktree add -b with an existing branch')
            for w in _pl.worktrees_using(repo, ref):
                raise die("cannot force update the branch '%s' used by worktree at '%s'" % (newb, w.path))
            if not quiet:
                raise Unsupported('worktree add -B of an existing branch (not --quiet)')
        target = _commit(ctx, repo, start or 'HEAD')
        branch_ref, kind = ref, 'new'
    elif start is None and not detach:
        name = os.path.basename(path)
        ref = 'refs/heads/' + name
        if _refs.check_refname(ref) and _refs.exists(repo, ref):
            branch_ref, kind = ref, 'existing'
            target = _refs.resolve(repo, ref)[0]
            newb = None
        else:
            if not _refs.check_refname(ref):
                raise Unsupported('worktree add whose directory name is not a branch name (%s)' % name)
            target = _commit(ctx, repo, 'HEAD')
            newb, branch_ref, kind = name, ref, 'new'
    elif start is None:
        target = _commit(ctx, repo, 'HEAD')
        kind = 'detach'
    else:
        local = _wc.local_branch(repo, start)
        if local is not None and not detach:
            branch_ref, kind = local, 'existing'
            target = _refs.resolve(repo, local)[0]
        else:
            o = _pl.resolve(ctx, start)
            target = _revs.peel(repo, o, 'commit') if o else None
            if target is None:
                if detach:
                    raise die("invalid reference: %s" % start)
                remote = _wc.unique_remote(repo, start)
                if remote is None:
                    raise die("invalid reference: %s" % start)
                raise Unsupported('worktree add guessing a remote-tracking branch')
            kind = 'detach'
    if kind == 'existing' and not force:
        for w in _pl.worktrees_using(repo, branch_ref):
            raise die("'%s' is already used by worktree at '%s'" % (branch_ref[len('refs/heads/'):], w.path))
    if repo.worktree_config and os.path.exists(repo.worktree_config_path):
        # builtin/worktree.c copy_filtered_worktree_config: git copies this worktree's
        # config.worktree into the new one, less core.bare and core.worktree. Not modelled.
        raise Unsupported('worktree add copying config.worktree (extensions.worktreeConfig)')
    # the admin directory
    name = os.path.basename(path)
    if not name or any(c in name for c in ' ~^:?*[\\') or name.startswith('.') or name.endswith('.lock'):
        raise Unsupported('worktree add with a directory name git would sanitize (%s)' % name)
    base = os.path.join(repo.commondir, 'worktrees')
    os.makedirs(base, exist_ok=True)
    wid, n = name, 0
    while True:
        admin = os.path.join(base, wid)
        try:
            os.mkdir(admin)
            break
        except FileExistsError:
            n += 1
            wid = '%s%d' % (name, n)
    os.makedirs(path, exist_ok=True)
    real = slash(os.path.realpath(path))
    if opts.get('lock'):
        _write(os.path.join(admin, 'locked'), (opts.get('reason') or '') + ('\n' if opts.get('reason') else ''))
    _write(os.path.join(admin, 'gitdir'), real + '/.git\n')
    _write(os.path.join(admin, 'commondir'), '../..\n')
    _write(os.path.join(path, '.git'), 'gitdir: %s\n' % slash(admin))
    _write(os.path.join(admin, 'HEAD'), target + '\n')
    _write(os.path.join(admin, 'ORIG_HEAD'), target + '\n')     # left by git's reset --hard
    if newb:
        if kind == 'new' and not _refs.exists(repo, branch_ref):
            _refs.update(repo, branch_ref, target, old='')
            _pl.setup_tracking(ctx, newb, start or 'HEAD', no_track=opts.get('no-track'), quiet=True)
        else:
            _refs.update(repo, branch_ref, target)
    new = Repo(admin, path, repo.env, repo.cli_config, gitdir_from_env=True)
    if new.hook_exists('post-checkout'):
        raise Unsupported('worktree add with a post-checkout hook')
    if not quiet:
        if kind == 'new':
            ctx.warn("Preparing worktree (new branch '%s')\n" % newb)
        elif kind == 'existing':
            ctx.warn("Preparing worktree (checking out '%s')\n" % branch_ref[len('refs/heads/'):])
        else:
            ctx.warn('Preparing worktree (detached HEAD %s)\n' % _pl.abbrev(repo, target))
    ix = _index.Index(new)
    lock = _index.IndexLock(ix.path).acquire()
    try:
        tree = _wt.Tree(new, ix)
        _wt.twoway(tree, {}, _wc.commit_side(repo, target), reset=True)
        ix.write(lock)
    except BaseException:
        lock.rollback()
        raise
    if branch_ref is not None:
        _write(os.path.join(admin, 'HEAD'), 'ref: %s\n' % branch_ref)
    if not quiet:
        ctx.write('HEAD is now at %s\n' % _wc.describe(repo, target))
    return 0


def _commit(ctx, repo, spec):
    o = _pl.resolve(ctx, spec)
    c = _revs.peel(repo, o, 'commit') if o else None
    if c is None:
        if spec == 'HEAD':
            raise Unsupported('worktree add from an unborn HEAD')
        raise die('invalid reference: %s' % spec)
    return c


def _write(path, text):
    with open(path, 'wb') as f:
        f.write(text.encode('utf-8'))


# -- list -----------------------------------------------------------------------------------

_LIST = _u.Spec('worktree list', flags='porcelain z v|verbose')


def _list(ctx, args):
    opts, pos, dd = _u.parse(_LIST, args)
    if pos or dd:
        raise Unsupported('worktree list %s' % ' '.join(args))
    if opts.get('z') and not opts.get('porcelain'):
        raise die('the option \'-z\' requires \'--porcelain\'')
    repo = ctx.need_repo()
    wts = _wts.all_worktrees(repo)
    for w in wts:
        if w.bare:
            raise Unsupported('worktree list of a bare repository')
        if w.head_oid is None and w.head_ref is None:
            raise Unsupported('worktree list with a worktree whose HEAD cannot be read')
        if not opts.get('porcelain') and (w.prunable or w.head_oid is None):
            raise Unsupported('worktree list with a prunable or unborn worktree (not --porcelain)')
    if opts.get('porcelain'):
        z = opts.get('z')
        t = '\0' if z else '\n'
        out = []
        for w in wts:
            rec = 'worktree %s%s' % (w.path, t)
            # an unborn branch prints the null oid (worktree.c add_head_info)
            rec += 'HEAD %s%s' % (w.head_oid or '0' * repo.hexlen, t)
            rec += ('detached' if w.detached else 'branch %s' % w.head_ref) + t
            if w.locked is not None:
                reason = w.locked.rstrip('\n')
                if reason:
                    if not z:
                        q = _u.cquote(reason, True)
                        reason = q
                    rec += 'locked %s%s' % (reason, t)
                else:
                    rec += 'locked' + t
            if w.prunable:
                rec += 'prunable %s%s' % (w.prunable, t)
            out.append(rec + t)
        ctx.write(''.join(out))
        return 0
    if opts.get('verbose'):
        raise Unsupported('worktree list -v')
    width = max(len(w.path) for w in wts)
    abbrevs = [_pl.abbrev(repo, w.head_oid) for w in wts]
    alen = max(len(a) for a in abbrevs)
    out = []
    for w, a in zip(wts, abbrevs):
        line = '%-*s %-*s ' % (width, w.path, alen, a)
        line += '(detached HEAD)' if w.detached else '[%s]' % _pl.shorten_ref(repo, w.head_ref)
        if w.locked is not None:
            line += ' locked'
        out.append(line + '\n')
    ctx.write(''.join(out))
    return 0


# -- remove, lock, unlock, prune ------------------------------------------------------------


def _target(ctx, repo, arg):
    w = _wts.find(repo, arg, ctx.cwd)
    if w is None:
        raise die("'%s' is not a working tree" % arg)
    return w


def _remove(ctx, args):
    force = 0
    pos = []
    for a in args:
        if a in ('-f', '--force'):
            force += 1
        elif a.startswith('-'):
            raise Unsupported('worktree remove %s' % a)
        else:
            pos.append(a)
    if len(pos) != 1:
        raise Unsupported('worktree remove %s' % ' '.join(args))
    repo = ctx.need_repo()
    w = _target(ctx, repo, pos[0])
    if w.is_main:
        raise die("'%s' is a main working tree" % pos[0])
    if w.locked is not None and force < 2:
        reason = w.locked.rstrip('\n')
        if reason:
            raise GitExit(128, "fatal: cannot remove a locked working tree, lock reason: %s\n"
                               "use 'remove -f -f' to override or unlock first\n" % reason)
        raise GitExit(128, "fatal: cannot remove a locked working tree;\n"
                           "use 'remove -f -f' to override or unlock first\n")
    wpath = w.os_path()
    if not os.path.isdir(wpath):
        raise Unsupported('worktree remove of a missing directory')
    if not force:
        sub = _wts.repo_for(repo, w)
        if _dirty(sub):
            raise die("'%s' contains modified or untracked files, use --force to delete it" % pos[0])
    cur = os.path.normcase(os.path.realpath(ctx.cwd))
    if cur == os.path.normcase(os.path.realpath(wpath)) or cur.startswith(
            os.path.normcase(os.path.realpath(wpath)) + os.sep):
        raise Unsupported('worktree remove of the worktree the command runs in')
    _rmtree(wpath)
    _rmtree(w.gitdir)
    _prune_empty(repo)
    return 0


def _dirty(sub):
    ix = _index.Index(sub).load()
    _wc.no_ita(ix)
    tree = _wt.Tree(sub, ix)
    _b, head_oid = _wc.head(sub)
    staged, unstaged, unmerged = _wc.changes(sub, ix, tree, head_oid)
    if staged or unstaged or unmerged:
        return True
    u, _ig = _wt.Walk(sub, ix, untracked='normal').run()
    return bool(u)


def _onerror(func, path, _exc):
    os.chmod(path, _stat.S_IWRITE)
    func(path)


def _rmtree(p):
    if os.path.isdir(p):
        shutil.rmtree(p, onerror=_onerror)


def _prune_empty(repo):
    try:
        os.rmdir(os.path.join(repo.commondir, 'worktrees'))
    except OSError:
        pass


def _lock(ctx, args):
    reason = None
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--reason':
            reason = args[i + 1]
            i += 2
            continue
        if a.startswith('--reason='):
            reason = a[len('--reason='):]
        elif a.startswith('-'):
            raise Unsupported('worktree lock %s' % a)
        else:
            pos.append(a)
        i += 1
    if len(pos) != 1:
        raise Unsupported('worktree lock %s' % ' '.join(args))
    repo = ctx.need_repo()
    w = _target(ctx, repo, pos[0])
    if w.is_main:
        raise die('The main working tree cannot be locked or unlocked')
    if w.locked is not None:
        old = w.locked.rstrip('\n')
        if old:
            raise die("'%s' is already locked, reason: %s" % (pos[0], old))
        raise die("'%s' is already locked" % pos[0])
    _write(os.path.join(w.gitdir, 'locked'), reason + '\n' if reason else '')
    return 0


def _unlock(ctx, args):
    if len(args) != 1 or args[0].startswith('-'):
        raise Unsupported('worktree unlock %s' % ' '.join(args))
    repo = ctx.need_repo()
    w = _target(ctx, repo, args[0])
    if w.is_main:
        raise die('The main working tree cannot be locked or unlocked')
    if w.locked is None:
        raise die("'%s' is not locked" % args[0])
    os.remove(os.path.join(w.gitdir, 'locked'))
    return 0


def _prune(ctx, args):
    if args:
        raise Unsupported('worktree prune %s' % ' '.join(args))
    repo = ctx.need_repo()
    for w in _wts.linked_worktrees(repo):
        if w.prunable and w.locked is None:
            _rmtree(w.gitdir)
    _prune_empty(repo)
    return 0
