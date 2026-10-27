"""merge, merge-tree and revert for the forms the kit uses (builtin/merge.c, merge-tree.c,
revert.c over merge-ort). The merge is path-level with git's three-way content merge for a file
both sides changed; every case ort would resolve with rename or directory-rename logic, and every
conflict other than a plain content conflict, is Unsupported. Messages are git 2.55's as probed.
"""
import os

from . import command
from . import diffs as _diffs
from . import history as _hist
from . import index as _index
from . import plumbing as _pl
from . import refs as _refs
from . import revs as _revs
from . import util as _u
from . import wt as _wt
from . import wtcmds as _wc
from . import xdiff as _xdiff
from .errors import GitExit, Unsupported, die

_FILE = 0o100000


# -- the three-way merge --------------------------------------------------------------------


class Merged:
    """A merge result: `side` {path: (mode, oid)} (conflicted files hold the marker blob),
    `conflicts` {path: (base, ours, theirs)} for the index stages, and `messages` [(path, text)]
    in the order ort prints them."""

    def __init__(self):
        self.side = {}
        self.conflicts = {}
        self.messages = []


def _is_file(entry):
    return entry is not None and (entry[0] & 0o170000) == _FILE


def _guard_attributes(repo, sides):
    if os.path.exists(repo.git_path('info/attributes')):
        raise Unsupported('a content merge with info/attributes')
    for s in sides:
        for p in s:
            if p == '.gitattributes' or p.endswith('/.gitattributes'):
                raise Unsupported('a content merge with a .gitattributes file')


def _dirs(side):
    out = set()
    for p in side:
        parts = p.split('/')
        for i in range(1, len(parts)):
            out.add('/'.join(parts[:i]))
    return out


def merge_sides(repo, base, ours, theirs, label1, label2):
    """Merge flat sides {path: (mode int, oid)}. Raises Unsupported for what ort would treat
    with rename logic or as a non-content conflict."""
    cfg = repo.config
    for key in ('merge.conflictstyle', 'merge.renormalize', 'merge.verbosity', 'merge.renames',
                'diff.renames'):
        if cfg.get(key) is not None:
            raise Unsupported('merge with %s set' % key)
    _no_directory_renames(base, ours, theirs)
    _no_double_renames(base, ours, theirs)
    res = Merged()
    content = []
    for p in sorted(set(base) | set(ours) | set(theirs), key=_diffs.pkey):
        b, o, t = base.get(p), ours.get(p), theirs.get(p)
        if o == t:
            if o is not None:
                res.side[p] = o
            continue
        if o == b:
            if t is not None:
                res.side[p] = t
            continue
        if t == b:
            if o is not None:
                res.side[p] = o
            continue
        # both sides changed the path, differently
        if b is None:
            # add/add: ort merges the two files over an empty base (index stages 2 and 3 only)
            if not (_is_file(o) and _is_file(t)) or o[0] != t[0]:
                raise Unsupported('an add/add conflict of a symlink or with two modes (%s)' % p)
            content.append((p, o[0], None, o, t))
            continue
        if o is None or t is None:
            raise Unsupported('a modify/delete conflict (%s)' % p)
        if not (_is_file(b) and _is_file(o) and _is_file(t)):
            raise Unsupported('a merge of a symlink or a type change (%s)' % p)
        if o[0] == b[0]:
            mode = t[0]
        elif t[0] == b[0] or t[0] == o[0]:
            mode = o[0]
        else:
            raise Unsupported('a mode conflict (%s)' % p)
        if o[1] == t[1]:
            res.side[p] = (mode, o[1])
            continue
        if o[1] == b[1]:
            res.side[p] = (mode, t[1])
            continue
        if t[1] == b[1]:
            res.side[p] = (mode, o[1])
            continue
        content.append((p, mode, b, o, t))
    if content:
        _guard_attributes(repo, (base, ours, theirs))
    for p, mode, b, o, t in content:
        data = [repo.objects.read(x[1])[1] if x is not None else b'' for x in (b, o, t)]
        if any(b'\0' in d[:8000] for d in data):
            raise Unsupported('a content merge of a binary file (%s)' % p)
        merged, conflicts = _xdiff.merge3(data[0], data[1], data[2], label1, label2)
        oid = repo.objects.write('blob', merged)
        res.side[p] = (mode, oid)
        res.messages.append((p, 'Auto-merging %s\n' % p))
        if conflicts:
            res.conflicts[p] = (b, o, t)
            res.messages.append((p, 'CONFLICT (%s): Merge conflict in %s\n'
                                 % ('content' if b is not None else 'add/add', p)))
        elif b is None:
            raise Unsupported('an add/add that merges cleanly (%s)' % p)
    clash = _dirs(res.side) & set(res.side)
    if clash:
        raise Unsupported('a directory/file conflict (%s)' % sorted(clash)[0])
    return res


def _no_directory_renames(base, ours, theirs):
    """ort moves a file one side adds into a directory the other side renamed away; a path-level
    merge cannot, so a side adding into a directory the other side emptied is Unsupported."""
    base_dirs = _dirs(base)
    for side, other in ((ours, theirs), (theirs, ours)):
        gone = base_dirs - _dirs(side)
        if not gone:
            continue
        for p in other:
            if p in base:
                continue
            parts = p.split('/')
            for i in range(1, len(parts)):
                if '/'.join(parts[:i]) in gone:
                    raise Unsupported('an add into a directory the other side removed (%s)' % p)


def _no_double_renames(base, ours, theirs):
    """Both sides deleting a path while both add others may be a rename/rename ort reports as a
    conflict; Unsupported."""
    both_gone = [p for p in base if p not in ours and p not in theirs]
    if not both_gone:
        return
    added_o = [p for p in ours if p not in base]
    added_t = [p for p in theirs if p not in base]
    if added_o and added_t and set(added_o) != set(added_t):
        raise Unsupported('both sides deleting %s while adding other paths' % both_gone[0])


def write_side(repo, side):
    """The tree oid for a flat side."""
    ix = _index.Index(repo)
    for p, (mode, oid) in side.items():
        ix.entries[(p, 0)] = _index.Entry(p, mode, oid)
    return _pl.write_tree_from_index(repo, ix)


# -- merge ----------------------------------------------------------------------------------

_MERGE = _u.Spec('merge', flags='q|quiet no-ff ff-only no-commit no-edit abort '
                                'allow-unrelated-histories', multi='m')

_UNMERGED = ('error: Merging is not possible because you have unmerged files.\n'
             'hint: Fix them up in the work tree, and then use \'git add/rm <file>\'\n'
             'hint: as appropriate to mark resolution and make a commit.\n'
             'fatal: Exiting because of an unresolved conflict.\n')
_DIVERGED = ("hint: Diverging branches can't be fast-forwarded, you need to either:\n"
             'hint:\nhint: \tgit merge --no-ff\nhint:\nhint: or:\nhint:\nhint: \tgit rebase\nhint:\n'
             'hint: Disable this message with "git config set advice.diverging false"\n'
             'fatal: Not possible to fast-forward, aborting.\n')
_FAILED = 'Merge with strategy ort failed.\n'


def _merge_rejects(rejects):
    parts = []
    for kind in ('overwrite', 'untracked'):
        paths = rejects.get(kind)
        if paths:
            listed = ''.join('\t%s\n' % p for p in sorted(set(paths), key=_diffs.pkey))
            parts.append('error: ' + _wt.CHECKOUT_MSGS[kind] % ('merge', listed, 'merge') + '\n')
    if rejects.get('removed'):
        raise Unsupported('a merge refused for removing untracked files')
    return GitExit(2, ''.join(parts) + 'Aborting\n' + _FAILED)


def _merge_name(repo, arg, oid, branch_ref):
    """fmt-merge-msg's line for one merged head: "Merge branch 'f'" and the like."""
    full, _ = _revs.dwim_ref(repo, arg) if arg not in ('HEAD', '@') else (None, None)
    if full is not None and full.startswith('refs/heads/'):
        what = "branch '%s'" % arg
    elif full is not None and full.startswith('refs/remotes/'):
        what = "remote-tracking branch '%s'" % arg
    elif full is None and arg == oid:
        what = "commit '%s'" % arg
    else:
        raise Unsupported('the default merge message for %s' % arg)
    if repo.config.get('merge.suppressdest') is not None or repo.config.get('merge.log') is not None:
        raise Unsupported('merge.suppressDest or merge.log')
    if branch_ref is None or not branch_ref.startswith('refs/heads/'):
        raise Unsupported('a default merge message on a detached HEAD')
    short = branch_ref[len('refs/heads/'):]
    msg = 'Merge %s' % what
    if short not in ('main', 'master'):
        msg += ' into %s' % short
    return msg + '\n'


def stat_lines(repo, ctx, old, new):
    """git's --stat --summary of side `old` to side `new` for merge's report (80 columns)."""
    ch = _diffs.compare(old, new)
    reader = _diffs.Reader(repo)
    ch = _diffs.detect_renames(ch, reader)
    if any(c.status == 'R' for c in ch):
        raise Unsupported('a merge diffstat with a rename')
    if not ch:
        return ''
    for key in ('diff.statgraphwidth', 'diff.statnamewidth'):
        if repo.config.get(key) is not None:
            raise Unsupported('merge diffstat with %s' % key)
    if ctx.env.get('COLUMNS') not in (None, '', '80'):
        raise Unsupported('merge diffstat with COLUMNS set')
    q = _u.quotepath(repo)
    rows = []
    for c in ch:
        name = _u.cquote(c.path, q)
        if name != c.path:
            raise Unsupported('a merge diffstat of a quoted path')
        a = reader.data(c.path, c.a_oid) if c.a_oid else b''
        b = reader.data(c.path, c.b_oid) if c.b_oid else b''
        if _diffs.is_binary(a) or _diffs.is_binary(b):
            raise Unsupported('a merge diffstat of a binary file')
        added, deleted = _xdiff.counts(_xdiff.split_lines(a), _xdiff.split_lines(b))
        if added + deleted == 0:
            raise Unsupported('a merge diffstat line with no line changes (%s)' % c.path)
        rows.append((name, added, deleted))
    name_w = max(len(r[0]) for r in rows)
    max_change = max(r[1] + r[2] for r in rows)
    num_w = len(str(max_change))
    if name_w + num_w + 6 + max_change > 80:
        raise Unsupported('a merge diffstat wider than 80 columns')
    out = ''
    for name, added, deleted in rows:
        out += ' %-*s | %*d %s%s\n' % (name_w, name, num_w, added + deleted, '+' * added, '-' * deleted)
    out += _diffs.shortstat(ch, reader)
    for c in ch:
        if c.status == 'A':
            out += ' create mode %06o %s\n' % (c.b_mode, c.path)
        elif c.status == 'D':
            out += ' delete mode %06o %s\n' % (c.a_mode, c.path)
        elif c.a_mode != c.b_mode:
            out += ' mode change %06o => %06o %s\n' % (c.a_mode, c.b_mode, c.path)
    return out


@command('merge')
def merge(ctx, args):
    opts, pos, dd = _u.parse(_MERGE, args)
    if dd is not None:
        raise Unsupported('merge with --')
    repo = ctx.need_worktree()
    quiet = opts.get('quiet')
    if opts.get('abort'):
        if pos or len(opts) > 1:
            raise Unsupported('merge --abort with other arguments')
        return _abort(ctx, repo)
    for hook in ('pre-merge-commit', 'prepare-commit-msg', 'commit-msg', 'post-merge'):
        if repo.hook_exists(hook):
            raise Unsupported('merge with a %s hook' % hook)
    # git's order: unmerged entries, then MERGE_HEAD, then other operations
    if _index.Index(repo).load().unmerged():
        raise GitExit(128, _UNMERGED)
    if _wc.in_merge(repo):
        raise GitExit(128, 'fatal: You have not concluded your merge (MERGE_HEAD exists).\n'
                           'Please, commit your changes before you merge.\n')
    _wc.other_operation(repo)
    if len(pos) != 1:
        raise Unsupported('merge of %d heads' % len(pos))
    ms = opts.get('m') or []
    if len(ms) > 1:
        raise Unsupported('merge with more than one -m')
    if opts.get('ff-only') and (opts.get('no-ff') or opts.get('no-commit')):
        raise Unsupported('merge --ff-only with --no-ff or --no-commit')
    for key in ('merge.ff', 'pull.ff', 'merge.stat', 'merge.autostash', 'merge.verifysignatures'):
        if repo.config.get(key) is not None:
            raise Unsupported('merge with %s set' % key)
    if repo.config.bool('commit.gpgsign', False):
        raise Unsupported('merge with commit.gpgsign true')
    if any(k.startswith('branch.') and k.endswith('.mergeoptions') for k, _v in repo.config.items()):
        raise Unsupported('merge with branch.<name>.mergeOptions')
    arg = pos[0]
    branch_ref, head_oid = _wc.head(repo)
    if head_oid is None:
        raise Unsupported('merge into an unborn branch')
    o = _pl.resolve(ctx, arg)
    theirs = _revs.peel(repo, o, 'commit') if o and repo.objects.has(o) else None
    if theirs is None:
        raise GitExit(1, 'merge: %s - not something we can merge\n' % arg)
    if theirs != o:
        raise Unsupported('merging a tag')
    bases = _revs.merge_bases(repo, head_oid, [theirs])
    # git writes ORIG_HEAD once the merge bases are known, before any outcome below (probed:
    # also on "Already up to date" and on every refusal that follows)
    _wc.set_orig_head(repo, head_oid)
    if not bases and not opts.get('allow-unrelated-histories'):
        raise die('refusing to merge unrelated histories')
    if bases == [theirs]:
        if not quiet:
            ctx.write('Already up to date.\n')
        return 0
    if bases == [head_oid] and not opts.get('no-ff'):
        return _fast_forward(ctx, repo, branch_ref, head_oid, theirs, quiet)
    if opts.get('ff-only'):
        raise GitExit(128, _DIVERGED)
    if not bases:
        base_side = {}
    elif len(bases) > 1:
        raise Unsupported('a merge with %d merge bases' % len(bases))
    else:
        base_side = _wc.commit_side(repo, bases[0])
    if ms:
        message = _u.cleanup_message(ms[0], 'whitespace')
    elif (opts.get('no-edit') or opts.get('no-commit')
          or ctx.env.get('GIT_MERGE_AUTOEDIT') in (None, 'no')):
        # git opens an editor only when stdin and stdout are terminals (or GIT_MERGE_AUTOEDIT=yes);
        # a caller of the fake always captures stdout
        message = _merge_name(repo, arg, theirs, branch_ref)
    else:
        raise Unsupported('merge with GIT_MERGE_AUTOEDIT=%s (an editor)'
                          % ctx.env.get('GIT_MERGE_AUTOEDIT'))
    ours_side = _wc.commit_side(repo, head_oid)
    theirs_side = _wc.commit_side(repo, theirs)
    with _wc.Locked(repo) as lk:
        ix = lk.ix
        _wc.no_ita(ix)
        res = merge_sides(repo, base_side, ours_side, theirs_side, 'HEAD', arg)
        idx = _diffs.index_side(ix)[0]
        staged = [p for p in sorted(set(ours_side) | set(idx), key=_diffs.pkey)
                  if ours_side.get(p) != idx.get(p)]
        tree = _wt.Tree(repo, ix)
        try:
            if staged:
                if len(staged) > 1:
                    raise Unsupported('a merge refused for several staged changes')
                raise GitExit(2, 'error: Your local changes to the following files would be '
                                 'overwritten by merge:\n  %s\n' % staged[0] + _FAILED)
            _wt.twoway(tree, ours_side, res.side, cmd='merge', action='merge', errfn=_merge_rejects)
        except GitExit:
            _restore_state(repo, ix, tree, ours_side, idx)
            raise
        for p, (b, o_, t) in res.conflicts.items():
            ix.remove(p)
            for stage, e in ((1, b), (2, o_), (3, t)):
                if e is not None:                   # an add/add has no base stage
                    ix.entries[(p, stage)] = _index.Entry(p, e[0], e[1], stage=stage)
        lk.write()
    result_tree = write_side(repo, res.side)
    for _p, text in res.messages:
        ctx.write(text)
    if res.conflicts or opts.get('no-commit'):
        _write_state(repo, theirs, message, res, opts, result_tree)
        if res.conflicts:
            ctx.write('Automatic merge failed; fix conflicts and then commit the result.\n')
            return 1
        ctx.warn('Automatic merge went well; stopped before committing as requested\n')
        return 0
    author = _u.ident(ctx, 'author')
    committer = _u.ident(ctx, 'committer')
    hdr = ['tree %s' % result_tree, 'parent %s' % head_oid, 'parent %s' % theirs,
           'author %s' % author, 'committer %s' % committer]
    body = ('\n'.join(hdr) + '\n\n').encode('utf-8') + message.encode('utf-8', 'surrogateescape')
    oid = repo.objects.write('commit', body)
    _refs.update(repo, 'HEAD', oid, old=head_oid, deref=True)
    _wc.clear_merge_state(repo)
    if not quiet:
        ctx.write("Merge made by the 'ort' strategy.\n")
        ctx.write(stat_lines(repo, ctx, ours_side, _wc.commit_side(repo, oid)))
    return 0


def _write_state(repo, theirs, message, res, opts, result_tree):
    if res.conflicts:
        message += '\n# Conflicts:\n' + ''.join('#\t%s\n' % p for p in sorted(res.conflicts, key=_diffs.pkey))
    _write_files(repo, (('MERGE_HEAD', theirs + '\n'), ('MERGE_MSG', message),
                        ('MERGE_MODE', 'no-ff' if opts.get('no-ff') else ''),
                        ('AUTO_MERGE', result_tree + '\n')))


def _restore_state(repo, ix, tree, head_side, idx_side):
    """The trace of a refused merge. git stashes local changes to tracked files first
    (`stash create`) and, on failure, resets to HEAD and re-applies that stash; the re-apply is
    an ort merge whose result (HEAD with the local changes) git records as AUTO_MERGE (probed).
    With no tracked change there is no stash and AUTO_MERGE is left alone."""
    side = dict(idx_side)
    racy = ix.mtime_ns
    for (p, s), e in ix.entries.items():
        if s:
            continue
        mod, _st = tree.modified(e, racy)
        if not mod:
            continue
        fresh = tree.entry_for(p, write=True)
        if fresh is None:
            side.pop(p, None)
        else:
            side[p] = (fresh.mode, fresh.oid)
    if side == head_side:
        return
    _write_files(repo, (('AUTO_MERGE', write_side(repo, side) + '\n'),))


def _write_files(repo, files):
    for name, text in files:
        with open(repo.git_path(name), 'wb') as f:
            f.write(text.encode('utf-8', 'surrogateescape'))


def _fast_forward(ctx, repo, branch_ref, head_oid, theirs, quiet):
    old = _wc.commit_side(repo, head_oid)
    new = _wc.commit_side(repo, theirs)

    def refuse(rejects):
        raise Unsupported('a fast-forward merge refused by local changes')
    with _wc.Locked(repo) as lk:
        _wc.no_ita(lk.ix)
        tree = _wt.Tree(repo, lk.ix)
        _wt.twoway(tree, old, new, cmd='merge', action='merge', errfn=refuse)
        lk.write()
    _refs.update(repo, 'HEAD', theirs, old=head_oid, deref=True)
    _wc.clear_merge_state(repo)
    if not quiet:
        ctx.write('Updating %s..%s\nFast-forward\n' % (_pl.abbrev(repo, head_oid), _pl.abbrev(repo, theirs)))
        ctx.write(stat_lines(repo, ctx, old, new))
    return 0


def _abort(ctx, repo):
    if not _wc.in_merge(repo):
        raise die('There is no merge to abort (MERGE_HEAD missing).')
    _wc.other_operation(repo)
    branch_ref, head_oid = _wc.head(repo)
    if head_oid is None:
        raise Unsupported('merge --abort on an unborn branch')
    # merge --abort is reset --merge: paths whose work-tree file matches the index go back to
    # HEAD; a file with changes git did not stage stops it (Unsupported here).
    hs = _wc.commit_side(repo, head_oid)
    with _wc.Locked(repo) as lk:
        ix = lk.ix
        tree = _wt.Tree(repo, ix)
        racy = ix.mtime_ns
        for e in list(ix.entries.values()):
            if e.stage:
                continue
            mod, _st = tree.modified(e, racy)
            if mod and hs.get(e.path) != (e.mode, e.oid):
                raise Unsupported('merge --abort with an unstaged change to a merged path (%s)' % e.path)
        idx_side = {}
        for (p, s), e in ix.entries.items():
            if s == 0:
                idx_side[p] = (e.mode, e.oid)
        # unmerged paths: their work-tree files are overwritten from HEAD (one unpack_trees in
        # git, its result index HEAD's: the checkout reads HEAD's .gitattributes)
        _wt.refuse_nested_attributes(p for p in ix.unmerged() if p in hs)
        tree.conv.checkout_from(hs.get('.gitattributes'))
        for p in ix.unmerged():
            ix.remove(p)
            if p in hs:
                e = tree.write_blob(p, hs[p][1], hs[p][0])
                ix.add(e)
            else:
                tree.remove_file(p)
        _wt.twoway(tree, idx_side, hs, reset=True)
        lk.write()
    _wc.clear_merge_state(repo)
    return 0


# -- merge-tree -----------------------------------------------------------------------------

_MERGE_TREE = _u.Spec('merge-tree', flags='write-tree name-only no-messages z allow-unrelated-histories')


@command('merge-tree')
def merge_tree(ctx, args):
    opts, pos, dd = _u.parse(_MERGE_TREE, args)
    if not opts.get('write-tree'):
        raise Unsupported('merge-tree without --write-tree')
    if not opts.get('name-only') or not opts.get('no-messages'):
        raise Unsupported('merge-tree without --name-only --no-messages')
    if dd is not None or len(pos) != 2:
        raise Unsupported('merge-tree with %d commits' % len(pos))
    repo = ctx.need_repo()
    oids = []
    for spec in pos:
        o = _pl.resolve(ctx, spec)
        c = _revs.peel(repo, o, 'commit') if o and repo.objects.has(o) else None
        if c is None:
            raise GitExit(1, 'merge-tree: %s - not something we can merge\n' % spec)
        oids.append(c)
    bases = _revs.merge_bases(repo, oids[0], [oids[1]])
    if not bases:
        if not opts.get('allow-unrelated-histories'):
            raise die('refusing to merge unrelated histories')
        base_side = {}
    elif len(bases) > 1:
        raise Unsupported('merge-tree with %d merge bases' % len(bases))
    else:
        base_side = _wc.commit_side(repo, bases[0])
    res = merge_sides(repo, base_side, _wc.commit_side(repo, oids[0]), _wc.commit_side(repo, oids[1]),
                      pos[0], pos[1])
    tree = write_side(repo, res.side)
    end = '\0' if opts.get('z') else '\n'
    out = tree + end
    for p in sorted(res.conflicts, key=_diffs.pkey):
        if not opts.get('z') and _u.cquote(p, _u.quotepath(repo)) != p:
            raise Unsupported('merge-tree listing a quoted path')
        out += p + end
    ctx.write(out)
    return 1 if res.conflicts else 0


# -- revert ---------------------------------------------------------------------------------

_REVERT = _u.Spec('revert', flags='no-edit')


@command('revert')
def revert(ctx, args):
    opts, pos, dd = _u.parse(_REVERT, args)
    if not opts.get('no-edit'):
        raise Unsupported('revert without --no-edit (an editor)')
    if dd is not None or len(pos) != 1:
        raise Unsupported('revert of %d commits' % len(pos))
    repo = ctx.need_worktree()
    for hook in ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit', 'post-rewrite'):
        if repo.hook_exists(hook):
            raise Unsupported('revert with a %s hook' % hook)
    for key in ('commit.cleanup', 'revert.reference', 'i18n.commitencoding'):
        if repo.config.get(key) is not None:
            raise Unsupported('revert with %s set' % key)
    if repo.config.bool('commit.gpgsign', False):
        raise Unsupported('revert with commit.gpgsign true')
    if _wc.in_merge(repo):
        raise Unsupported('revert during a merge')
    _wc.other_operation(repo)
    o = _pl.resolve(ctx, pos[0])
    oid = _revs.peel(repo, o, 'commit') if o else None
    if oid is None:
        raise Unsupported('revert of %s, which is not a commit' % pos[0])
    c = _revs.commit(repo, oid)
    if len(c.parents) > 1:
        raise GitExit(128, 'error: commit %s is a merge but no -m option was given.\n'
                           'fatal: revert failed\n' % oid)
    if not c.parents:
        raise Unsupported('revert of a root commit')
    branch_ref, head_oid = _wc.head(repo)
    if head_oid is None or branch_ref is None:
        raise Unsupported('revert on an unborn branch or a detached HEAD')
    subj = _hist.subject_body(_hist._message(c))[0]
    if subj.startswith('Revert "') or subj.startswith('Reapply "'):
        raise Unsupported('revert of a revert')
    message = 'Revert "%s"\n\nThis reverts commit %s.\n' % (subj, oid)
    hs = _wc.commit_side(repo, head_oid)
    res = merge_sides(repo, _wc.commit_side(repo, oid), hs, _wc.commit_side(repo, c.parents[0]),
                      'HEAD', 'parent of %s (%s)' % (_pl.abbrev(repo, oid), subj))
    if res.conflicts:
        raise Unsupported('a revert with conflicts')
    if res.messages:
        raise Unsupported('a revert needing a content merge')
    if res.side == hs:
        raise Unsupported('a revert with nothing to commit')
    author = _u.ident(ctx, 'author')
    committer = _u.ident(ctx, 'committer')
    if author.rsplit(' ', 2)[0] != committer.rsplit(' ', 2)[0]:
        raise Unsupported('revert summary with an author other than the committer')
    ch = _diffs.detect_renames(_diffs.compare(hs, res.side), _diffs.Reader(repo))
    if any(x.status == 'R' for x in ch):
        raise Unsupported('revert summary with a rename')
    with _wc.Locked(repo) as lk:
        ix = lk.ix
        _wc.no_ita(ix)
        if _diffs.index_side(ix)[0] != hs:
            raise Unsupported('revert with staged changes')
        tree = _wt.Tree(repo, ix)

        def refuse(rejects):
            raise Unsupported('a revert refused by local changes')
        _wt.twoway(tree, hs, res.side, errfn=refuse)
        lk.write()
    tree_oid = write_side(repo, res.side)
    # the sequencer's ort merge leaves AUTO_MERGE behind even on a clean revert (probed)
    _write_files(repo, (('AUTO_MERGE', tree_oid + '\n'),))
    hdr =['tree %s' % tree_oid, 'parent %s' % head_oid, 'author %s' % author, 'committer %s' % committer]
    body = ('\n'.join(hdr) + '\n\n').encode('utf-8') + message.encode('utf-8')
    new = repo.objects.write('commit', body)
    _refs.update(repo, 'HEAD', new, old=head_oid, deref=True)
    ts, tz = author.rsplit(' ', 2)[1:]
    ts = int(ts)
    short = branch_ref[len('refs/heads/'):] if branch_ref.startswith('refs/heads/') else branch_ref
    out = '[%s %s] %s\n Date: %s\n' % (short, _pl.abbrev(repo, new), 'Revert "%s"' % subj,
                                       _hist.date_default(ts, tz))
    out += _diffs.shortstat(ch, _diffs.Reader(repo))
    for x in ch:
        if x.status == 'A':
            out += ' create mode %06o %s\n' % (x.b_mode, x.path)
        elif x.status == 'D':
            out += ' delete mode %06o %s\n' % (x.a_mode, x.path)
    ctx.write(out)
    return 0
