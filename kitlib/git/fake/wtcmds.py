"""Work-tree commands: add, rm, mv, update-index, status (porcelain v1), ls-files, commit, reset,
checkout and switch, for the forms the kit uses (builtin/add.c, rm.c, mv.c, update-index.c,
commit.c, wt-status.c, ls-files.c, reset.c, checkout.c). Messages are git's; every case whose
output was not checked against real git is Unsupported rather than guessed.
"""
import os
import re
import stat as _stat

from . import command
from . import diffs as _diffs
from . import history as _hist
from . import index as _index
from . import plumbing as _pl
from . import refs as _refs
from . import revs as _revs
from . import util as _u
from . import wt as _wt
from . import worktrees as _wts
from .errors import GitExit, Unsupported, die

# -- shared ---------------------------------------------------------------------------------


class Locked:
    """The index under its lock. Leaving the block without write() rolls the lock back."""

    def __init__(self, repo, path=None):
        self.ix = _index.Index(repo, path)
        self.lock = _index.IndexLock(self.ix.path).acquire()
        self.done = False
        try:
            self.ix.load()
        except BaseException:
            self.lock.rollback()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self.done:
            self.lock.rollback()
        return False

    def write(self):
        self.done = True
        try:
            self.ix.write(self.lock)
        except BaseException:
            self.lock.rollback()
            raise


def head(repo):
    """(branch ref or None when detached, oid or None when unborn)."""
    raw = _refs.read_raw(repo, 'HEAD')
    if raw is None:
        raise Unsupported('a HEAD that is not a ref or an object name')
    if raw[0] == 'sym':
        oid, _ = _refs.resolve(repo, raw[1])
        return raw[1], oid
    return None, raw[1]


def commit_side(repo, oid):
    if oid is None:
        return {}
    c = _revs.commit(repo, oid)
    if c is None:
        raise die('bad object %s' % oid)
    return _diffs.tree_side(repo, c.tree)


def describe(repo, oid):
    """'abc1234 subject', as 'HEAD is now at' prints a commit."""
    c = _revs.commit(repo, oid)
    return '%s %s' % (_pl.abbrev(repo, oid), _hist.subject_body(_hist._message(c))[0])


def _ref_commits(repo):
    """The commits every ref under refs/ peels to (other objects left out)."""
    out = set()
    for _name, oid in _refs.list_refs(repo):
        c = _revs.peel(repo, oid, 'commit') if repo.objects.has(oid) else None
        if c is not None:
            out.add(c)
    return out


def pathspec(ctx, specs):
    """A Pathspec from command-line paths; absolute paths inside the work tree are allowed."""
    prefix = ctx.prefix or ''
    ps = _u.Pathspec(prefix, [])
    for s in specs:
        if os.path.isabs(s) or re.match(r'^[A-Za-z]:[\\/]', s):
            full = _u.to_top(ctx, s)
            ps.items.append((full, bool(re.search(r'[*?\[]', full)), s))
        else:
            ps.items.extend(_u.Pathspec(prefix, [s]).items)
    ps.empty = not ps.items
    return ps


def no_ita(ix):
    for e in ix.entries.values():
        if e.intent_to_add:
            raise Unsupported('an intent-to-add index entry (%s)' % e.path)


def changes(repo, ix, tree, head_oid, filt=None, renames=False):
    """(staged changes, unstaged changes, unmerged paths) as status computes them."""
    no_ita(ix)
    hs = commit_side(repo, head_oid)
    side, unmerged = _diffs.index_side(ix)
    um = set(unmerged)

    def keep(p):
        return p not in um and (filt is None or filt(p))
    staged = _diffs.compare(hs, side, keep)
    if renames:
        staged = _diffs.detect_renames(staged, _diffs.Reader(repo))
    ws = _diffs.worktree_side(tree, ix)
    unstaged = _diffs.compare(side, ws, keep)
    return staged, unstaged, [p for p in unmerged if filt is None or filt(p)]


_STATE_FILES = ('MERGE_HEAD', 'MERGE_MSG', 'MERGE_MODE', 'AUTO_MERGE', 'SQUASH_MSG', 'MERGE_RR')


def in_merge(repo):
    return os.path.exists(repo.git_path('MERGE_HEAD'))


def other_operation(repo):
    for n in ('CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG'):
        if os.path.exists(repo.git_path(n)):
            raise Unsupported('a %s in progress' % n)


def clear_merge_state(repo):
    for n in _STATE_FILES:
        try:
            os.remove(repo.git_path(n))
        except OSError:
            pass


def set_orig_head(repo, oid):
    if oid is not None:
        _refs.update(repo, 'ORIG_HEAD', oid, deref=False)


def refresh_if_able(ctx, repo):
    """status's opportunistic refresh: the index is rewritten only when its lock is free and
    optional locks are allowed. Returns the loaded Index."""
    if ctx.no_optional_locks or ctx.env.get('GIT_OPTIONAL_LOCKS', '1') == '0':
        return _index.Index(repo).load()
    try:
        lk = Locked(repo)
    except GitExit:
        return _index.Index(repo).load()
    with lk:
        tree = _wt.Tree(repo, lk.ix)
        if tree.refresh():
            lk.write()
    return lk.ix


# -- add ------------------------------------------------------------------------------------

_ADD = _u.Spec('add', flags='A|all f|force u|update')

_IGNORED_MSG = ('The following paths are ignored by one of your .gitignore files:\n%s'
                'hint: Use -f if you really want to add them.\n'
                'hint: Disable this message with "git config set advice.addIgnoredFile false"\n')


@command('add')
def add(ctx, args):
    opts, pos, dd = _u.parse(_ADD, args)
    items = pos + (dd or [])
    repo = ctx.need_worktree()
    update, force = opts.get('update'), opts.get('force')
    if not items and not opts.get('all') and not update:
        raise Unsupported('git add with nothing specified')
    ps = pathspec(ctx, items)
    seen = [False] * len(ps.items)

    def mark(path, isdir=False):
        for i in ps.matched_items(path, isdir):
            seen[i] = True

    with Locked(repo) as lk:
        ix = lk.ix
        no_ita(ix)
        tree = _wt.Tree(repo, ix)
        plan = []                                    # (path, 'add' | 'remove')
        for path in ix.paths():
            if not ps.match(path):
                continue
            mark(path)
            stages = ix.stages(path)
            e0 = stages.get(0)
            st = _wt.lstat(repo, path)
            if st is None or not _wt.is_reg(st):
                if st is not None and _stat.S_ISLNK(st.st_mode):
                    raise Unsupported('symbolic link in the work tree: %s' % path)
                plan.append((path, 'remove'))
                continue
            if e0 is None:
                plan.append((path, 'add'))
                continue
            mod, _st = tree.modified(e0, ix.mtime_ns)
            if mod:
                plan.append((path, 'add'))
        untracked, ignored = [], []
        if not update:
            limit = None if ps.empty else ps.leading_dirs()
            walk = _wt.Walk(repo, ix, untracked='all', ignored='traditional' if force else 'matching')
            u, ig = walk.run(limit)
            for p in u:
                if not ps.match(p.rstrip('/'), p.endswith('/')):
                    continue
                if p.endswith('/'):
                    raise Unsupported('adding a nested repository (%s)' % p)
                mark(p)
                untracked.append(p)
            for p in ig:
                bare = p.rstrip('/')
                if force:
                    if not ps.match(bare, p.endswith('/')):
                        continue
                    if p.endswith('/'):
                        raise Unsupported('adding an ignored nested repository (%s)' % p)
                    mark(bare)
                    untracked.append(p)
                    continue
                for full, glob, _o in ps.items:
                    if not glob and full.rstrip('/') == bare:
                        ignored.append(bare)
                        break
        for i, (full, glob, orig) in enumerate(ps.items):
            if seen[i] or full == '':
                continue
            if glob or not os.path.lexists(repo.wt_path(full.rstrip('/'))):
                raise die("pathspec '%s' did not match any files" % orig)
        changed = False
        for path, act in plan:
            if act == 'remove':
                ix.remove(path)
            else:
                e = tree.entry_for(path, write=True, warn=True)
                ix.add(e)
            changed = True
        for p in untracked:
            e = tree.entry_for(p, write=True, warn=True)
            if e is not None:
                ix.add(e)
                changed = True
        for w in tree.warnings:
            ctx.warn(w)
        if changed:
            lk.write()
    if ignored:
        ctx.warn(_IGNORED_MSG % ''.join('%s\n' % p for p in ignored))
        return 1
    return 0


# -- rm -------------------------------------------------------------------------------------

_RM = _u.Spec('rm', flags='q|quiet r f|force cached ignore-unmatch')


@command('rm')
def rm(ctx, args):
    opts, pos, dd = _u.parse(_RM, args)
    items = pos + (dd or [])
    repo = ctx.need_worktree()
    if not items:
        raise Unsupported('git rm with no paths')
    ps = pathspec(ctx, items)
    cached, force, quiet = opts.get('cached'), opts.get('force'), opts.get('quiet')
    with Locked(repo) as lk:
        ix = lk.ix
        no_ita(ix)
        seen = [0] * len(ps.items)                  # 0 none, 1 exact, 2 recursively
        matched = []
        for path in ix.paths():
            hit = ps.matched_items(path)
            if not hit:
                continue
            matched.append(path)
            for i in hit:
                full, glob, _o = ps.items[i]
                how = 2 if (not glob and path != full.rstrip('/')) else 1
                seen[i] = max(seen[i], how)
        any_seen = False
        for i, (full, glob, orig) in enumerate(ps.items):
            if not seen[i]:
                if not opts.get('ignore-unmatch'):
                    raise die("pathspec '%s' did not match any files" % orig)
            else:
                any_seen = True
            if not opts.get('r') and seen[i] == 2:
                raise die("not removing '%s' recursively without -r" % (orig or '.'))
        if not any_seen:
            return 0
        if not force:
            _rm_check(repo, ix, matched, cached)
        tree = _wt.Tree(repo, ix)
        for path in matched:
            if not quiet:
                ctx.write("rm '%s'\n" % path)
            ix.remove(path)
        if not cached:
            for path in matched:
                st = _wt.lstat(repo, path)
                if st is not None and _wt.is_reg(st):
                    tree.remove_file(path)
        lk.write()
    return 0


def _rm_check(repo, ix, paths, index_only):
    """builtin/rm.c check_local_mod: a path whose removal would lose content is Unsupported
    (git's refusal text is not reproduced)."""
    _b, head_oid = head(repo)
    hs = commit_side(repo, head_oid)
    tree = _wt.Tree(repo, ix)
    for path in paths:
        e = ix.get(path)
        if e is None:
            continue                                  # unmerged
        st = _wt.lstat(repo, path)
        if st is None or not _wt.is_reg(st):
            continue
        local = tree.modified(e, ix.mtime_ns)[0]
        h = hs.get(path)
        staged = h is None or h[0] != e.mode or h[1] != e.oid
        if (local and staged) or (not index_only and (local or staged)):
            raise Unsupported('git rm of %s, which has local or staged changes' % path)


# -- mv -------------------------------------------------------------------------------------


@command('mv')
def mv(ctx, args):
    if len(args) != 2 or any(a.startswith('-') for a in args):
        raise Unsupported('git mv %s' % ' '.join(args))
    repo = ctx.need_worktree()
    src = _u.to_top(ctx, args[0])
    dst = _u.to_top(ctx, args[1])
    if os.path.isdir(repo.wt_path(dst)) and dst:
        dst = dst + '/' + src.rsplit('/', 1)[-1]
    with Locked(repo) as lk:
        ix = lk.ix
        if ix.unmerged():
            raise Unsupported('git mv with unmerged entries')
        if ix.get(src) is not None:
            moves = [(src, dst)]
        else:
            pre = src + '/'
            moves = [(p, dst + p[len(src):]) for p in ix.paths() if p.startswith(pre)]
            if not moves:
                raise Unsupported('git mv of an untracked path (%s)' % src)
        if os.path.lexists(repo.wt_path(dst)) or ix.has_path(dst):
            raise Unsupported('git mv onto an existing path (%s)' % dst)
        parent = os.path.dirname(repo.wt_path(dst))
        if not os.path.isdir(parent):
            raise Unsupported('git mv into a missing directory (%s)' % dst)
        os.rename(repo.wt_path(src), repo.wt_path(dst))
        for old, new in moves:
            e = ix.get(old).copy()
            ix.remove(old)
            e.path = new
            ix.add(e)
        lk.write()
    return 0


# -- update-index ---------------------------------------------------------------------------


@command('update-index')
def update_index(ctx, args):
    quiet = refresh = info = False
    for a in args:
        if a in ('-q',):
            quiet = True
        elif a == '--refresh':
            refresh = True
        elif a == '--index-info':
            info = True
        else:
            raise Unsupported('update-index %s' % a)
    if refresh == info:
        raise Unsupported('update-index %s' % ' '.join(args))
    repo = ctx.need_repo()
    lock_err = None
    try:
        lk = Locked(repo)
    except GitExit as e:
        lk, lock_err = None, e
    ix = lk.ix if lk is not None else _index.Index(repo).load()
    try:
        has_errors = False
        if refresh:
            repo.need_worktree()
            tree = _wt.Tree(repo, ix)
            reports = []
            changed = tree.refresh(lambda p, why: reports.append((p, why)))
            if reports:
                if quiet and any(w == 'needs merge' for _p, w in reports):
                    raise Unsupported('update-index -q --refresh with unmerged entries')
                if not quiet:
                    for p, why in reports:
                        ctx.write('%s: %s\n' % (p, why))
                    has_errors = True
        else:
            changed = _index_info(repo, ix, ctx.stdin_bytes())
        if changed:
            if lk is None:
                if quiet:
                    raise GitExit(128, '')
                raise lock_err
            lk.write()
    finally:
        if lk is not None and not lk.done:
            lk.lock.rollback()
    return 1 if has_errors else 0


def _index_info(repo, ix, data):
    changed = False
    for raw in data.decode('utf-8', 'surrogateescape').split('\n'):
        if not raw:
            continue
        meta, tab, path = raw.partition('\t')
        if not tab or path.startswith('"'):
            raise Unsupported('update-index --index-info line %r' % raw)
        f = meta.split()
        stage = 0
        if len(f) == 2:
            mode, oid = f
        elif len(f) == 3 and len(f[1]) >= 40:
            mode, oid, stage = f[0], f[1], int(f[2])
        elif len(f) == 3:
            mode, _kind, oid = f
        else:
            raise Unsupported('update-index --index-info line %r' % raw)
        m = int(mode, 8)
        changed = True
        if m == 0:
            ix.remove(path)
            continue
        if m not in (_index.MODE_FILE, _index.MODE_EXEC):
            raise Unsupported('update-index --index-info mode %s' % mode)
        e = _index.Entry(path, m, oid, stage)
        if stage == 0:
            ix.add(e)
        else:
            ix.entries[(path, stage)] = e
    return changed


# -- status ---------------------------------------------------------------------------------

_STATUS = _u.Spec('status', flags='z b|branch no-renames renames', optional='porcelain u|untracked-files '
                  'ignored ignore-submodules')

_UNMERGED = {(1, 2, 3): 'UU', (2, 3): 'AA', (1, 2): 'UD', (1, 3): 'DU', (2,): 'AU', (3,): 'UA', (1,): 'DD'}


def _untracked_mode(repo, value):
    if value is None:
        value = repo.config.get('status.showuntrackedfiles')
        if value is None:
            return 'normal'
    if value is True:
        return 'all'
    v = value.strip().lower()
    if v in ('no', 'false', 'off', '0'):
        return 'no'
    if v in ('normal', 'true', 'on', '1', 'yes'):
        return 'normal'
    if v == 'all':
        return 'all'
    raise die('Invalid untracked files mode \'%s\'' % value)


def quote_sp(repo, path):
    """quote_path with QUOTE_PATH_QUOTE_SP: a path with a space is quoted too."""
    q = _u.cquote(path, _u.quotepath(repo))
    if not q.startswith('"') and ' ' in path:
        q = '"%s"' % q
    return q


def branch_header(repo, branch_ref, head_oid):
    if branch_ref is None:
        return '## HEAD (no branch)'
    short = branch_ref[len('refs/heads/'):] if branch_ref.startswith('refs/heads/') else branch_ref
    line = '## ' + ('No commits yet on ' if head_oid is None else '') + short
    cfg = repo.config
    remote = cfg.get('branch.%s.remote' % short)
    merge = cfg.get('branch.%s.merge' % short)
    if remote is None or merge is None:
        return line
    base = merge if remote == '.' else _revs.map_ref(repo, remote, merge)
    if base is None:
        return line
    line += '...' + _pl.shorten_ref(repo, base)
    up, _ = _refs.resolve(repo, base)
    if up is None:
        return line + ' [gone]'
    if head_oid is None:
        raise Unsupported('status --branch on an unborn branch with an upstream')
    ahead = len(_revs.walk(repo, [head_oid], [up]))
    behind = len(_revs.walk(repo, [up], [head_oid]))
    if not ahead and not behind:
        return line
    if not ahead:
        return line + ' [behind %d]' % behind
    if not behind:
        return line + ' [ahead %d]' % ahead
    return line + ' [ahead %d, behind %d]' % (ahead, behind)


@command('status')
def status(ctx, args):
    opts, pos, dd = _u.parse(_STATUS, args)
    items = pos + (dd or [])
    porcelain = opts.get('porcelain')
    z = opts.get('z')
    if porcelain not in (None, True, 'v1'):
        raise Unsupported('status --porcelain=%s' % porcelain)
    if porcelain is None and not z:
        raise Unsupported('status in the long format')
    repo = ctx.need_worktree()
    other_operation(repo)
    umode = _untracked_mode(repo, opts.get('untracked-files'))
    ign = opts.get('ignored')
    imode = None
    if ign is True or ign == 'traditional':
        imode = 'traditional'
    elif ign == 'matching':
        imode = 'matching'
    elif ign not in (None, 'no'):
        raise die('Invalid ignored mode \'%s\'' % ign)
    renames = opts.get('renames', True) and not opts.get('no-renames')
    if renames:
        v = repo.config.get('status.renames')
        if v is not None:
            from .config import parse_bool
            renames = parse_bool(v, 'status.renames') if v.strip().lower() not in ('copy', 'copies') else None
            if renames is None:
                raise Unsupported('status.renames=copies')
        else:
            renames = _diffs.renames_wanted(repo, {})
    ps = pathspec(ctx, items)
    ix = refresh_if_able(ctx, repo)
    tree = _wt.Tree(repo, ix)
    branch_ref, head_oid = head(repo)
    filt = None if ps.empty else ps.match
    staged, unstaged, unmerged = changes(repo, ix, tree, head_oid, filt, renames)
    term = '\0' if z else '\n'
    lines = []                                   # (sort key, text)
    xy = {}
    for c in staged:
        xy.setdefault(c.path, [' ', ' ', None])[0] = c.status
        if c.status == 'R':
            xy[c.path][2] = c.a_path
    for c in unstaged:
        xy.setdefault(c.path, [' ', ' ', None])[1] = c.status
    for p, (x, y, orig) in xy.items():
        if z:
            text = '%s%s %s\0' % (x, y, p) + ('%s\0' % orig if orig else '')
        else:
            shown = quote_sp(repo, p)
            if orig:
                shown = '%s -> %s' % (quote_sp(repo, orig), shown)
            text = '%s%s %s\n' % (x, y, shown)
        lines.append((_diffs.pkey(p), text))
    for p in unmerged:
        code = _UNMERGED[tuple(sorted(ix.stages(p)))]
        lines.append((_diffs.pkey(p), '%s %s%s' % (code, p if z else quote_sp(repo, p), term)))
    lines.sort(key=lambda t: t[0])
    out = []
    if opts.get('branch'):
        out.append(branch_header(repo, branch_ref, head_oid) + term)
    out.extend(t for _k, t in lines)
    if umode != 'no' or imode:
        walk = _wt.Walk(repo, ix, untracked=umode if umode != 'no' else 'normal', ignored=imode)
        u, ig = walk.run(None if ps.empty else ps.leading_dirs())
        if umode == 'no':
            u = []
        for tag, lst in (('??', u), ('!!', ig)):
            for p in sorted(lst, key=_diffs.pkey):
                bare = p.rstrip('/')
                if not ps.empty and not ps.match(bare, p.endswith('/')):
                    if p.endswith('/') and any(f.startswith(p) for f, _g, _o in ps.items):
                        raise Unsupported('status pathspec inside an untracked directory')
                    continue
                out.append('%s %s%s' % (tag, p if z else quote_sp(repo, p), term))
    ctx.write(''.join(out))
    return 0


# -- ls-files -------------------------------------------------------------------------------

_LSF = _u.Spec('ls-files', flags='c|cached o|others t z s|stage exclude-standard full-name')


@command('ls-files')
def ls_files(ctx, args):
    opts, pos, dd = _u.parse(_LSF, args)
    items = pos + (dd or [])
    repo = ctx.need_worktree()
    z, tag = opts.get('z'), opts.get('t')
    stage = opts.get('stage')
    others = opts.get('others')
    cached = opts.get('cached') or stage or not others
    if others and not opts.get('exclude-standard'):
        raise Unsupported('ls-files --others without --exclude-standard')
    prefix = '' if opts.get('full-name') else (ctx.prefix or '')
    limit = ctx.prefix or ''
    ps = pathspec(ctx, items)
    ix = _index.Index(repo).load()
    quote = _u.quotepath(repo)

    def show(path):
        if limit and not path.startswith(limit):
            return None
        if not ps.empty and not ps.match(path.rstrip('/'), path.endswith('/')):
            return None
        rel = _u.rel_to_prefix(path, prefix) if prefix else path
        return rel if z else _u.cquote(rel, quote)
    out = []
    term = '\0' if z else '\n'
    if others:
        walk = _wt.Walk(repo, ix, untracked='all', ignored=None)
        u, _ig = walk.run([limit.rstrip('/')] if limit else None)
        for p in sorted(u, key=_diffs.pkey):
            s = show(p)
            if s is not None:
                out.append(('? ' if tag else '') + s + term)
    if cached:
        for e in ix.sorted_entries():
            s = show(e.path)
            if s is None:
                continue
            if e.skip_worktree:
                raise Unsupported('ls-files with skip-worktree entries')
            t = ('M ' if e.stage else 'H ') if tag else ''
            if stage:
                out.append('%s%06o %s %d\t%s%s' % (t, e.mode, e.oid, e.stage, s, term))
            else:
                out.append(t + s + term)
    ctx.write(''.join(out))
    return 0


# -- commit ---------------------------------------------------------------------------------

_COMMIT = _u.Spec('commit', flags='q|quiet a|all allow-empty allow-empty-message no-edit amend '
                  'n|no-verify no-gpg-sign o|only', values='author cleanup', multi='m|message F|file')

_UNMERGED_MSG = ('error: Committing is not possible because you have unmerged files.\n'
                 'hint: Fix them up in the work tree, and then use \'git add/rm <file>\'\n'
                 'hint: as appropriate to mark resolution and make a commit.\n'
                 'fatal: Exiting because of an unresolved conflict.\n')


def _commit_message(ctx, repo, opts, amend_of):
    ms, fs = opts.get('m'), opts.get('F')
    if ms and fs:
        raise Unsupported('commit -m with -F')
    if ms or fs:
        return _pl.message_from(ctx, ms, fs)
    if opts.get('no-edit'):
        if in_merge(repo):
            with open(repo.git_path('MERGE_MSG'), 'rb') as f:
                return f.read().decode('utf-8', 'surrogateescape')
        if amend_of is not None:
            return _hist._message(amend_of)
        return ''
    raise Unsupported('commit without a message (an editor)')


def _cleanup_mode(repo, opts):
    mode = opts.get('cleanup') or repo.config.get('commit.cleanup')
    if mode in (None, 'default'):
        return 'whitespace'
    if mode in ('whitespace', 'verbatim', 'strip'):
        if mode == 'strip' and repo.config.get('core.commentchar'):
            raise Unsupported('core.commentChar')
        return mode
    raise Unsupported('commit cleanup mode %s' % mode)


@command('commit')
def commit(ctx, args):
    opts, pos, dd = _u.parse(_COMMIT, args)
    items = pos + (dd or [])
    repo = ctx.need_worktree()
    other_operation(repo)
    quiet = opts.get('quiet')
    amend = opts.get('amend')
    for hook in (('prepare-commit-msg', 'post-commit') +
                 (() if opts.get('no-verify') else ('pre-commit', 'commit-msg')) +
                 (('post-rewrite',) if amend else ())):
        if repo.hook_exists(hook):
            raise Unsupported('commit with a %s hook' % hook)
    enc = repo.config.get('i18n.commitencoding')
    if enc is not None and enc.strip().lower() not in ('utf-8', 'utf8'):
        raise Unsupported('i18n.commitEncoding')
    if opts.get('all') and items:
        raise Unsupported('commit -a with paths')
    branch_ref, head_oid = head(repo)
    merging = in_merge(repo)
    amend_of = None
    if amend:
        if head_oid is None:
            raise die('You have nothing to amend.')
        if merging:
            raise Unsupported('commit --amend during a merge')
        amend_of = _revs.commit(repo, head_oid)
        if amend_of.encoding:
            raise Unsupported('amending a commit with an encoding header')
    with Locked(repo) as lk:
        ix = lk.ix
        no_ita(ix)
        tree = _wt.Tree(repo, ix)
        if items:
            if merging:
                raise die('cannot do a partial commit during a merge.')
            tree_oid = _partial(ctx, repo, ix, tree, head_oid, items)
            if tree_oid is None:
                return 1
        else:
            if opts.get('all'):
                if ix.unmerged():
                    raise Unsupported('commit -a with unmerged entries')
                for e in list(ix.sorted_entries()):
                    mod, st = tree.modified(e, ix.mtime_ns)
                    if not mod:
                        continue
                    if st is None:
                        ix.remove(e.path)
                    else:
                        ix.add(tree.entry_for(e.path, write=True))
            tree.refresh()
            if ix.unmerged():
                raise GitExit(128, _UNMERGED_MSG)
            tree_oid = _pl.write_tree_from_index(repo, ix)
        if amend:
            parents = list(amend_of.parents)
        else:
            parents = [head_oid] if head_oid else []
            if merging:
                with open(repo.git_path('MERGE_HEAD'), 'rb') as f:
                    for line in f.read().decode('ascii').split():
                        if line not in parents:
                            parents.append(line)
        base = parents[0] if parents else None
        base_tree = _revs.commit(repo, base).tree if base else None
        if (tree_oid == (base_tree or _empty_tree(repo)) and not opts.get('allow-empty') and not merging
                and not (amend and len(parents) > 1)):
            if items or amend:
                raise Unsupported('commit with nothing to commit (partial or amend)')
            lk.write()
            return _nothing_to_commit(ctx, repo, ix, tree, branch_ref, head_oid)
        message = _commit_message(ctx, repo, opts, amend_of)
        message = _u.cleanup_message(message, _cleanup_mode(repo, opts))
        if not message and not opts.get('allow-empty-message'):
            lk.write()
            ctx.warn('Aborting commit due to empty commit message.\n')
            return 1
        lk.write()
    if repo.config.bool('commit.gpgsign', False) and opts.get('no-gpg-sign') is None:
        raise Unsupported('commit.gpgSign')
    if opts.get('author'):
        override = _u.parse_author_arg(ctx, opts['author'])
    else:
        override = None
    if amend_of is not None:
        name, email, ts, tz = _ident_parts(amend_of.author)
        if override is not None:
            name, email = override
        author = '%s <%s> %d %s' % (name, email, ts, tz)
    else:
        author = _u.ident(ctx, 'author', override)
    committer = _u.ident(ctx, 'committer')
    hdr = ['tree %s' % tree_oid] + ['parent %s' % p for p in parents]
    hdr += ['author %s' % author, 'committer %s' % committer]
    body = ('\n'.join(hdr) + '\n\n').encode('utf-8') + message.encode('utf-8', 'surrogateescape')
    oid = repo.objects.write('commit', body)
    _refs.update(repo, 'HEAD', oid, old=head_oid or '', deref=True)
    clear_merge_state(repo)
    try:
        with open(repo.git_path('COMMIT_EDITMSG'), 'wb') as f:
            f.write(message.encode('utf-8', 'surrogateescape'))
    except OSError:
        pass
    if not quiet:
        _commit_summary(ctx, repo, oid, branch_ref, parents, author, committer, message, amend)
    return 0


def _ident_parts(raw):
    from .objects import parse_ident
    return parse_ident(raw)


def _empty_tree(repo):
    from .objects import hash_object
    return hash_object(repo.algo, 'tree', b'')


def _partial(ctx, repo, ix, tree, head_oid, items):
    """commit <paths> (--only): the paths' work-tree state is committed on top of HEAD's tree
    and staged in the index too. None after reporting an unmatched pathspec (exit 1)."""
    ps = pathspec(ctx, items)
    hs = commit_side(repo, head_oid)
    cand = sorted(set(ix.paths()) | set(hs), key=_diffs.pkey)
    matched, seen = [], set()
    for p in cand:
        hit = ps.matched_items(p)
        if hit:
            matched.append(p)
            seen.update(hit)
    for i, (_f, _g, orig) in enumerate(ps.items):
        if i not in seen:
            ctx.warn("error: pathspec '%s' did not match any file(s) known to git\n" % orig)
            return None
    for p in matched:
        if ix.get(p) is None and ix.has_path(p):
            raise Unsupported('partial commit of an unmerged path (%s)' % p)
    base = _index.Index(repo)
    for p, (mode, oid) in hs.items():
        base.entries[(p, 0)] = _index.Entry(p, mode, oid)
    for p in matched:
        e = tree.entry_for(p, write=True)
        if e is None:
            ix.remove(p)
            base.remove(p)
        else:
            ix.add(e)
            base.add(e.copy())
    tree.refresh()
    return _pl.write_tree_from_index(repo, base)


def _nothing_to_commit(ctx, repo, ix, tree, branch_ref, head_oid):
    """wt-status's long output for the clean cases (anything else is Unsupported)."""
    if branch_ref is None or head_oid is None:
        raise Unsupported('commit with nothing to commit while detached or unborn')
    short = branch_ref[len('refs/heads/'):]
    if repo.config.get('branch.%s.merge' % short) is not None:
        raise Unsupported('commit with nothing to commit on a branch with an upstream')
    staged, unstaged, unmerged = changes(repo, ix, tree, head_oid)
    if staged or unstaged or unmerged:
        raise Unsupported('commit with nothing to commit but unstaged changes')
    umode = _untracked_mode(repo, None)
    u = []
    if umode != 'no':
        u, _ig = _wt.Walk(repo, ix, untracked=umode).run()
    if u and ctx.prefix:
        raise Unsupported('commit with nothing to commit from a subdirectory')
    out = 'On branch %s\n' % short
    if u:
        out += ('Untracked files:\n  (use "git add <file>..." to include in what will be committed)\n'
                + ''.join('\t%s\n' % _u.cquote(p, _u.quotepath(repo)) for p in sorted(u, key=_diffs.pkey))
                + '\nnothing added to commit but untracked files present (use "git add" to track)\n')
    elif umode == 'no':
        raise Unsupported('commit with nothing to commit and untracked files hidden')
    else:
        out += 'nothing to commit, working tree clean\n'
    ctx.write(out)
    return 1


def _commit_summary(ctx, repo, oid, branch_ref, parents, author, committer, message, amend):
    if amend or len(parents) > 1:
        raise Unsupported('commit summary for an amend or a merge (not --quiet)')
    if author.rsplit(' ', 2)[0] != committer.rsplit(' ', 2)[0]:
        raise Unsupported('commit summary with an author other than the committer (not --quiet)')
    if branch_ref is None:
        where = 'detached HEAD'
    else:
        where = branch_ref[len('refs/heads/'):] if branch_ref.startswith('refs/heads/') else branch_ref
    if not parents:
        where += ' (root-commit)'
    subj = _hist.subject_body(message)[0]
    out = '[%s %s] %s\n' % (where, _pl.abbrev(repo, oid), subj)
    old = commit_side(repo, parents[0]) if parents else {}
    new = commit_side(repo, oid)
    ch = _diffs.compare(old, new)
    if _diffs.renames_wanted(repo, {}):
        ch = _diffs.detect_renames(ch, _diffs.Reader(repo))
        if any(c.status == 'R' for c in ch):
            raise Unsupported('commit summary with a rename (not --quiet)')
    out += _diffs.shortstat(ch, _diffs.Reader(repo))
    q = _u.quotepath(repo)
    for c in ch:
        if c.status == 'A':
            out += ' create mode %06o %s\n' % (c.b_mode, _u.cquote(c.path, q))
        elif c.status == 'D':
            out += ' delete mode %06o %s\n' % (c.a_mode, _u.cquote(c.path, q))
        elif c.a_mode != c.b_mode:
            out += ' mode change %06o => %06o %s\n' % (c.a_mode, c.b_mode, _u.cquote(c.path, q))
    ctx.write(out)


# -- reset ----------------------------------------------------------------------------------

_RESET = _u.Spec('reset', flags='q|quiet hard soft mixed keep')


@command('reset')
def reset(ctx, args):
    opts, pos, dd = _u.parse(_RESET, args)
    repo = ctx.need_repo()
    quiet = opts.get('quiet')
    kinds = [k for k in ('hard', 'soft', 'mixed', 'keep') if opts.get(k)]
    if len(kinds) > 1:
        raise Unsupported('reset with %s' % ' '.join(kinds))
    kind = kinds[0] if kinds else 'mixed'
    paths = dd
    if paths is None and len(pos) > 1:
        raise Unsupported('reset %s (paths without --)' % ' '.join(pos))
    spec = pos[0] if pos else 'HEAD'
    branch_ref, head_oid = head(repo)
    if spec == 'HEAD' and head_oid is None:
        target = None
    else:
        o = _pl.resolve(ctx, spec)
        target = _revs.peel(repo, o, 'commit') if o else None
        if target is None:
            raise Unsupported('reset to %s, which is not a commit' % spec)
    if paths is not None:
        if kind != 'mixed' or kinds:
            raise Unsupported('reset --%s with paths' % kind)
        if not quiet:
            raise Unsupported('reset with paths (not --quiet)')
        return _reset_paths(ctx, repo, target, paths)
    if kind != 'hard' or not quiet:
        if kind == 'mixed' and not quiet:
            raise Unsupported('reset --mixed (not --quiet)')
    if kind in ('soft', 'keep') and (in_merge(repo) or _index.Index(repo).load().unmerged()):
        raise die('Cannot do a %s reset in the middle of a merge.' % kind)
    if target is None:
        raise Unsupported('reset --%s on an unborn branch' % kind)
    other_operation(repo)
    if kind != 'soft':
        repo.need_worktree() if kind != 'mixed' else None
        with Locked(repo) as lk:
            ix = lk.ix
            new = commit_side(repo, target)
            if kind == 'mixed':
                old_entries = dict(ix.entries)
                ix.clear()
                for p, (mode, oid) in new.items():
                    cur = old_entries.get((p, 0))
                    if cur is not None and cur.mode == mode and cur.oid == oid:
                        ix.entries[(p, 0)] = cur
                    else:
                        ix.entries[(p, 0)] = _index.Entry(p, mode, oid)
                if repo.worktree is not None:
                    _wt.Tree(repo, ix).refresh()
            else:
                tree = _wt.Tree(repo, ix)
                old = commit_side(repo, head_oid)
                if kind == 'hard':
                    _wt.twoway(tree, old, new, reset=True)
                else:
                    _wt.twoway(tree, old, new, errfn=lambda rj: _keep_error(tree, old, rj, spec))
            lk.write()
    set_orig_head(repo, head_oid)
    _refs.update(repo, 'HEAD', target, deref=True)
    clear_merge_state(repo)
    if kind == 'hard' and not quiet:
        ctx.write('HEAD is now at %s\n' % describe(repo, target))
    return 0


def _keep_error(tree, old, rejects, spec):
    """unpack-trees' plain (non-porcelain) message for the first refused path."""
    allp = []
    for kind, paths in rejects.items():
        allp.extend((p, kind) for p in paths)
    p, kind = sorted(allp, key=lambda t: _diffs.pkey(t[0]))[0]
    if kind == 'overwrite':
        e = tree.index.get(p)
        o = old.get(p)
        if e is not None and o is not None and e.mode == o[0] and e.oid == o[1]:
            msg = "error: Entry '%s' not uptodate. Cannot merge.\n" % p
        else:
            msg = "error: Entry '%s' would be overwritten by merge. Cannot merge.\n" % p
    elif kind == 'untracked':
        msg = "error: Untracked working tree file '%s' would be overwritten by merge.\n" % p
    else:
        raise Unsupported('reset --keep refusing a removal (%s)' % p)
    return GitExit(128, msg + "fatal: Could not reset index file to revision '%s'.\n" % spec)


def _reset_paths(ctx, repo, target, paths):
    ps = pathspec(ctx, paths)
    new = commit_side(repo, target)
    with Locked(repo) as lk:
        ix = lk.ix
        no_ita(ix)
        for p in sorted(set(ix.paths()) | set(new), key=_diffs.pkey):
            if not ps.match(p):
                continue
            n = new.get(p)
            stages = ix.stages(p)
            cur = stages.get(0)
            if n is None:
                if stages:
                    ix.remove(p)
                continue
            if cur is None or len(stages) > 1 or cur.mode != n[0] or cur.oid != n[1]:
                ix.add(_index.Entry(p, n[0], n[1]))
        if repo.worktree is not None:
            _wt.Tree(repo, ix).refresh()
        lk.write()
    return 0


# -- checkout and switch --------------------------------------------------------------------

_CHECKOUT = _u.Spec('checkout', flags='q|quiet f|force detach no-track', values='b B orphan',
                    optional='t|track')
_SWITCH = _u.Spec('switch', flags='q|quiet d|detach f|force|discard-changes no-track no-guess',
                  values='c|create C|force-create orphan', optional='t|track')


def local_branch(repo, name):
    ref = 'refs/heads/' + name
    if _refs.check_refname(ref) and _refs.exists(repo, ref):
        return ref
    return None


def unique_remote(repo, name):
    """refs/remotes/<r>/<name> when exactly one remote has it (checkout's DWIM)."""
    hits = [r for r in _pl._remote_names(repo)
            if _refs.check_refname('refs/remotes/%s/%s' % (r, name)) and
            _refs.exists(repo, 'refs/remotes/%s/%s' % (r, name))]
    if len(hits) > 1:
        raise Unsupported('%s matches more than one remote-tracking branch' % name)
    return 'refs/remotes/%s/%s' % (hits[0], name) if hits else None


def _commit_of(ctx, repo, spec):
    o = _pl.resolve(ctx, spec)
    return _revs.peel(repo, o, 'commit') if o else None


@command('checkout')
def checkout(ctx, args):
    opts, pos, dd = _u.parse(_CHECKOUT, args)
    repo = ctx.need_worktree()
    if opts.get('track'):
        raise Unsupported('checkout --track')
    quiet = opts.get('quiet')
    newb = opts.get('b') or opts.get('B')
    if dd is not None:
        if len(pos) > 1 or newb or opts.get('orphan') or opts.get('detach') or opts.get('force'):
            raise Unsupported('checkout %s' % ' '.join(args))
        return _checkout_paths(ctx, repo, pos[0] if pos else None, dd, quiet)
    if opts.get('orphan'):
        if pos:
            raise Unsupported('checkout --orphan with a start point')
        name = opts['orphan']
        if local_branch(repo, name):
            raise die("a branch named '%s' already exists" % name)
        _refs.write_symref(repo, 'HEAD', 'refs/heads/' + name)
        if not quiet:
            ctx.warn("Switched to a new branch '%s'\n" % name)
        return 0
    if len(pos) > 1:
        raise Unsupported('checkout %s (several arguments without --)' % ' '.join(pos))
    if newb:
        return switch_to(ctx, repo, pos[0] if pos else None, newb=newb, reset_branch=bool(opts.get('B')),
                         detach=False, force=opts.get('force'), quiet=quiet, no_track=opts.get('no-track'))
    if not pos:
        if opts.get('detach'):
            return switch_to(ctx, repo, 'HEAD', detach=True, force=opts.get('force'), quiet=quiet)
        raise Unsupported('checkout with no arguments')
    spec = pos[0]
    if spec == '-':
        raise Unsupported('checkout -')
    target = _commit_of(ctx, repo, spec)
    if target is not None:
        if os.path.lexists(os.path.join(ctx.cwd, spec)):
            raise Unsupported('checkout %s: both a revision and a file' % spec)
        if opts.get('detach') or local_branch(repo, spec) is None:
            # git prints its detached-HEAD advice only when leaving a branch for a commit
            if not opts.get('detach') and not quiet and _revs.current_branch(repo) is not None:
                raise Unsupported('checkout of a commit without --detach (detached HEAD advice)')
            return switch_to(ctx, repo, spec, detach=True, force=opts.get('force'), quiet=quiet)
        return switch_to(ctx, repo, spec, force=opts.get('force'), quiet=quiet)
    if opts.get('detach'):
        raise Unsupported('checkout --detach of an unknown revision')
    remote = unique_remote(repo, spec)
    if remote is not None:
        return switch_to(ctx, repo, remote, newb=spec, force=opts.get('force'), quiet=quiet)
    return _checkout_paths(ctx, repo, None, [spec], quiet)


@command('switch')
def switch(ctx, args):
    opts, pos, dd = _u.parse(_SWITCH, args)
    repo = ctx.need_worktree()
    if dd is not None or len(pos) > 1 or opts.get('orphan') or opts.get('track'):
        raise Unsupported('switch %s' % ' '.join(args))
    quiet, force = opts.get('quiet'), opts.get('force')
    newb = opts.get('create') or opts.get('force-create')
    if newb:
        return switch_to(ctx, repo, pos[0] if pos else None, newb=newb,
                         reset_branch=bool(opts.get('force-create')), force=force, quiet=quiet,
                         no_track=opts.get('no-track'))
    if not pos:
        if opts.get('detach'):
            return switch_to(ctx, repo, 'HEAD', detach=True, force=force, quiet=quiet)
        raise GitExit(128, 'fatal: missing branch or commit argument\n')
    spec = pos[0]
    if spec == '-':
        raise Unsupported('switch -')
    if opts.get('detach'):
        if _commit_of(ctx, repo, spec) is None:
            raise die('invalid reference: %s' % spec)
        return switch_to(ctx, repo, spec, detach=True, force=force, quiet=quiet)
    if local_branch(repo, spec):
        return switch_to(ctx, repo, spec, force=force, quiet=quiet)
    remote = None if opts.get('no-guess') else unique_remote(repo, spec)
    if remote is not None:
        return switch_to(ctx, repo, remote, newb=spec, force=force, quiet=quiet)
    target = _commit_of(ctx, repo, spec)
    if target is None:
        raise die('invalid reference: %s' % spec)
    full = _pl._full_name_quiet(repo, spec)
    if full is not None:
        raise Unsupported('switch to a non-branch ref (%s)' % full)
    raise GitExit(128, "fatal: a branch is expected, got commit '%s'\nhint: If you want to detach HEAD at "
                       "the commit, try again with the --detach option.\n" % spec)


def switch_to(ctx, repo, spec, newb=None, reset_branch=False, detach=False, force=False, quiet=False,
              no_track=False):
    """Move HEAD (and the index and work tree) to `spec`: an existing branch, a new branch `newb`
    started at `spec` (None: HEAD), or a detached commit."""
    other_operation(repo)
    branch_ref, head_oid = head(repo)
    start = spec if spec is not None else 'HEAD'
    if start == 'HEAD' and head_oid is None:
        if newb and not reset_branch and spec is None:
            if local_branch(repo, newb):
                raise die("a branch named '%s' already exists" % newb)
            _refs.write_symref(repo, 'HEAD', 'refs/heads/' + newb)
            if not quiet:
                ctx.warn("Switched to a new branch '%s'\n" % newb)
            return 0
        raise Unsupported('checkout from an unborn branch')
    target = _commit_of(ctx, repo, start)
    if target is None:
        raise Unsupported('checkout of %s, which is not a commit' % start)
    new_ref = None
    msg = None
    if newb:
        new_ref = 'refs/heads/' + newb
        if not _refs.check_refname(new_ref):
            raise Unsupported('checkout -b with an invalid branch name (%s)' % newb)
        exists = _refs.exists(repo, new_ref)
        if exists and not reset_branch:
            raise die("a branch named '%s' already exists" % newb)
        if exists:
            for w in _pl.worktrees_using(repo, new_ref):
                if os.path.normcase(os.path.normpath(w.gitdir)) != os.path.normcase(os.path.normpath(repo.gitdir)):
                    raise die("cannot force update the branch '%s' used by worktree at '%s'" % (newb, w.path))
            msg = "Reset branch '%s'\n" % newb
        else:
            msg = "Switched to a new branch '%s'\n" % newb
    elif not detach:
        new_ref = local_branch(repo, spec)
        for w in _pl.worktrees_using(repo, new_ref):
            if os.path.normcase(os.path.normpath(w.gitdir)) != os.path.normcase(os.path.normpath(repo.gitdir)):
                raise die("'%s' is already used by worktree at '%s'" % (spec, w.path))
        msg = ("Already on '%s'\n" if new_ref == branch_ref else "Switched to branch '%s'\n") % spec
    previous = None
    if not quiet and branch_ref is None and head_oid != target:
        # git's orphaned_commit_warning: the old detached commit, when a ref still reaches it
        if not any(_revs.is_ancestor(repo, head_oid, c) for c in _ref_commits(repo)):
            raise Unsupported('leaving a detached commit no ref reaches (not --quiet)')
        previous = 'Previous HEAD position was %s\n' % describe(repo, head_oid)
    with Locked(repo) as lk:
        ix = lk.ix
        no_ita(ix)
        if ix.unmerged() and not force:
            raise Unsupported('checkout with unmerged entries')
        tree = _wt.Tree(repo, ix)
        old = commit_side(repo, head_oid)
        new = commit_side(repo, target)
        _wt.twoway(tree, old, new, reset=bool(force))
        lk.write()
    if not quiet:
        st, us, _um = changes(repo, ix, _wt.Tree(repo, ix), target)
        if st or us:
            raise Unsupported('checkout carrying local changes (not --quiet)')
    if previous:
        ctx.warn(previous)
    if newb:
        _refs.update(repo, new_ref, target)
        if not exists or reset_branch:
            _pl.setup_tracking(ctx, newb, start, no_track=no_track, quiet=quiet)
    if detach:
        _refs.update(repo, 'HEAD', target, deref=False)
        if not quiet:
            ctx.warn('HEAD is now at %s\n' % describe(repo, target))
    else:
        if branch_ref != new_ref:
            _refs.write_symref(repo, 'HEAD', new_ref)
        if not quiet:
            short = new_ref[len('refs/heads/'):]
            if repo.config.get('branch.%s.merge' % short) is not None:
                raise Unsupported('checkout of a branch with an upstream (not --quiet)')
            ctx.warn(msg)
    return 0


def _checkout_paths(ctx, repo, rev, items, quiet):
    ps = pathspec(ctx, items)
    fresh = set()                    # paths whose index entry the source tree replaced
    with Locked(repo) as lk:
        ix = lk.ix
        no_ita(ix)
        tree = _wt.Tree(repo, ix)
        seen = set()
        src = None
        if rev is not None:
            target = _pl.resolve(ctx, rev)
            t = _revs.peel(repo, target, 'tree') if target else None
            if t is None:
                raise Unsupported('checkout of paths from %s' % rev)
            src = _diffs.tree_side(repo, t)
            for p in src:
                hit = ps.matched_items(p)
                if hit:
                    seen.update(hit)
                    was = ix.get(p)
                    if was is None or (was.mode, was.oid) != (src[p][0], src[p][1]):
                        ix.add(_index.Entry(p, src[p][0], src[p][1]))
                        fresh.add(p)
        matched = []
        for p in ix.paths():
            hit = ps.matched_items(p)
            if hit:
                seen.update(hit)
                matched.append(p)
        bad = False
        for i, (_f, _g, orig) in enumerate(ps.items):
            if i not in seen:
                ctx.warn("error: pathspec '%s' did not match any file(s) known to git\n" % orig)
                bad = True
        if bad:
            return 1
        for p in matched:
            if ix.get(p) is None:
                ctx.warn("error: path '%s' is unmerged\n" % p)
                bad = True
        if bad:
            return 1
        for p in matched:
            e = ix.get(p)
            if p in fresh or tree.modified(e, ix.mtime_ns)[0]:
                ix.add(tree.write_blob(p, e.oid, e.mode))
        lk.write()
    # not --quiet prints nothing more: git's "Updated N paths" count goes to a terminal only, and
    # the fake's output is always captured (checked against real git, 2026-10-10)
    return 0
