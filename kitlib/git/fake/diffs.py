"""Comparing trees, the index and the work tree (diff-lib.c, tree-diff.c), rename detection
(diffcore-rename.c: exact, same-basename and similarity matches, diffcore-delta.c's span
hashes), the output formats the kit reads (--name-only, --name-status, --numstat, --shortstat,
patches), and the diff and diff-tree commands.

A side is a flat map {path: (mode int, oid)}; the work tree's side holds None for an oid git
would have to hash (its patches and counts read the file instead).
"""
import os

from . import command
from . import index as _index
from . import revs as _revs
from . import util as _u
from . import wt as _wt
from . import xdiff as _xdiff
from .errors import GitExit, Unsupported, die

MAX_SCORE = 60000
MIN_SCORE = 30000
S_IFGITLINK = 0o160000


class Change:
    """One file pair: status 'A', 'D', 'M', 'T', 'U' or 'R'; a_* is the old side, b_* the new."""
    __slots__ = ('status', 'a_path', 'b_path', 'a_mode', 'b_mode', 'a_oid', 'b_oid', 'score')

    def __init__(self, status, a_path, b_path, a, b, score=0):
        self.status = status
        self.a_path = a_path
        self.b_path = b_path
        self.a_mode, self.a_oid = a if a else (0, None)
        self.b_mode, self.b_oid = b if b else (0, None)
        self.score = score

    @property
    def path(self):
        return self.b_path if self.status != 'D' else self.a_path

    def __repr__(self):
        return 'Change(%s %s %s)' % (self.status, self.a_path, self.b_path)


def pkey(path):
    return path.encode('utf-8', 'surrogateescape')


# -- sides ----------------------------------------------------------------------------------


def tree_side(repo, tree_oid):
    """{path: (mode int, oid)} for a tree (None: the empty side)."""
    if tree_oid is None:
        return {}
    out = {}
    for path, (mode, oid) in _revs.flatten_tree(repo, tree_oid).items():
        m = int(mode, 8)
        if m == S_IFGITLINK:
            raise Unsupported('a gitlink (submodule) in a tree')
        out[path] = (m, oid)
    return out


def index_side(ix):
    """({path: (mode, oid)} of stage-0 entries, sorted list of unmerged paths)."""
    side = {}
    for (p, s), e in ix.entries.items():
        if s == 0 and not e.intent_to_add:
            side[p] = (e.mode, e.oid)
    return side, ix.unmerged()


def worktree_side(tree, ix):
    """The work tree as a side over the index's stage-0 entries: a clean file keeps the index's
    (mode, oid), a changed one (mode, None), a missing one is absent. Unmerged paths apart."""
    side = {}
    racy = ix.mtime_ns
    for (p, s), e in ix.entries.items():
        if s != 0:
            continue
        changed, st = tree.modified(e, racy)
        if st is None:
            continue
        if not changed:
            if e.intent_to_add:
                side[p] = (e.mode, None)
            else:
                side[p] = (e.mode, e.oid)
            continue
        side[p] = (_wt.wt_mode(tree.repo, st, e.mode), None)
    return side


def compare(old, new, filter_paths=None):
    """Changes from side `old` to side `new`, in path order."""
    out = []
    for p in sorted(set(old) | set(new), key=pkey):
        if filter_paths is not None and not filter_paths(p):
            continue
        a, b = old.get(p), new.get(p)
        if a is None:
            out.append(Change('A', p, p, None, b))
        elif b is None:
            out.append(Change('D', p, p, a, None))
        elif a[0] != b[0] or a[1] != b[1] or b[1] is None:
            if (a[0] & 0o170000) != (b[0] & 0o170000):
                out.append(Change('T', p, p, a, b))
            else:
                out.append(Change('M', p, p, a, b))
    return out


# -- contents -------------------------------------------------------------------------------


class Reader:
    """Blob contents for one comparison; a None oid reads the work-tree file as git would store
    it (core.autocrlf conversion through wt.Tree)."""

    def __init__(self, repo, tree=None):
        self.repo = repo
        self.tree = tree
        self.cache = {}

    def data(self, path, oid):
        if oid is None:
            if self.tree is None:
                raise Unsupported('work-tree content without a work tree')
            key = ('wt', path)
            got = self.cache.get(key)
            if got is None:
                got = self.cache[key] = self.tree.blob_data(path)
            return got
        got = self.cache.get(oid)
        if got is None:
            obj = self.repo.objects.read(oid)
            if obj is None:
                raise GitExit(128, 'fatal: unable to read %s\n' % oid)
            got = self.cache[oid] = obj[1]
        return got

    def oid(self, path, oid):
        """The hash of a side's file (hashing a work-tree file when needed)."""
        if oid is not None:
            return oid
        from .objects import hash_object
        return hash_object(self.repo.algo, 'blob', self.data(path, None))


def is_binary(data):
    return b'\0' in data[:8000]


# -- renames --------------------------------------------------------------------------------


def _hash_chars(data):
    """diffcore-delta.c hash_chars: {span hash: bytes}."""
    is_text = not is_binary(data)
    out = {}
    accum1 = accum2 = 0
    n = 0
    sz = len(data)
    i = 0
    while i < sz:
        c = data[i]
        i += 1
        old_1 = accum1
        if is_text and c == 13 and i < sz and data[i] == 10:
            continue
        accum1 = ((accum1 << 7) ^ (accum2 >> 25)) & 0xffffffff
        accum2 = ((accum2 << 7) ^ (old_1 >> 25)) & 0xffffffff
        accum1 = (accum1 + c) & 0xffffffff
        n += 1
        if n < 64 and c != 10:
            continue
        h = ((accum1 + accum2 * 0x61) & 0xffffffff) % 107927
        out[h] = out.get(h, 0) + n
        n = 0
        accum1 = accum2 = 0
    if n > 0:
        h = ((accum1 + accum2 * 0x61) & 0xffffffff) % 107927
        out[h] = out.get(h, 0) + n
    return out


def _similarity(src, dst, min_score=MIN_SCORE, cache=None):
    """diffcore-rename.c estimate_similarity for two blobs' bytes."""
    max_size = max(len(src), len(dst))
    base_size = min(len(src), len(dst))
    delta = max_size - base_size
    if max_size * (MAX_SCORE - min_score) < delta * MAX_SCORE:
        return 0
    hs = _span(src, cache)
    hd = _span(dst, cache)
    copied = 0
    for h, sc in hs.items():
        dc = hd.get(h, 0)
        copied += min(sc, dc)
    if not dst:
        return 0
    return copied * MAX_SCORE // max_size


def _span(data, cache):
    if cache is None:
        return _hash_chars(data)
    k = id(data)
    got = cache.get(k)
    if got is None:
        got = cache[k] = (_hash_chars(data), data)
    return got[0]


def _basename(p):
    return p.rsplit('/', 1)[-1]


def detect_renames(changes, reader, min_score=MIN_SCORE):
    """diffcore_rename (no copies, no break): pair deletions with additions. Ties git would
    break by an unspecified sort order are Unsupported."""
    srcs = [c for c in changes if c.status == 'D' and (c.a_mode & 0o170000) == 0o100000]
    dsts = [c for c in changes if c.status == 'A' and (c.b_mode & 0o170000) == 0o100000]
    if not srcs or not dsts:
        return changes
    pairs = {}                # id(dst change) -> (src change, score)
    used = set()
    # exact renames: same blob
    by_oid = {}
    for s in srcs:
        by_oid.setdefault(s.a_oid, []).append(s)
    for d in dsts:
        oid = reader.oid(d.b_path, d.b_oid)
        cands = [s for s in by_oid.get(oid, []) if id(s) not in used]
        if not cands:
            continue
        if len(cands) > 1:
            same = [s for s in cands if _basename(s.a_path) == _basename(d.b_path)]
            if len(same) != 1:
                raise Unsupported('exact rename with several equal sources (%s)' % d.b_path)
            cands = same
        if sum(1 for x in dsts if x is not d and id(x) not in pairs and reader.oid(x.b_path, x.b_oid) == oid):
            raise Unsupported('exact rename with several equal destinations (%s)' % d.b_path)
        pairs[id(d)] = (cands[0], MAX_SCORE)
        used.add(id(cands[0]))
    left_src = [s for s in srcs if id(s) not in used]
    left_dst = [d for d in dsts if id(d) not in pairs]
    if left_src and left_dst:
        cache = {}
        data = {}

        def blob(c, side):
            key = (id(c), side)
            if key not in data:
                if side == 'a':
                    data[key] = reader.data(c.a_path, c.a_oid)
                else:
                    data[key] = reader.data(c.b_path, c.b_oid)
            return data[key]
        if any(not blob(s, 'a') for s in left_src) or any(not blob(d, 'b') for d in left_dst):
            # git's handling of empty files as rename candidates is not modelled
            raise Unsupported('rename detection with an empty file')
        # same basename, each unique among the remaining sources and destinations
        basename_score = min_score + (MAX_SCORE - min_score) // 2
        sb, db = {}, {}
        for s in left_src:
            sb.setdefault(_basename(s.a_path), []).append(s)
        for d in left_dst:
            db.setdefault(_basename(d.b_path), []).append(d)
        for name, ss in sb.items():
            ds = db.get(name)
            if len(ss) != 1 or not ds or len(ds) != 1:
                continue
            s, d = ss[0], ds[0]
            score = _similarity(blob(s, 'a'), blob(d, 'b'), basename_score, cache)
            if score >= basename_score:
                pairs[id(d)] = (s, score)
                used.add(id(s))
        left_src = [s for s in left_src if id(s) not in used]
        left_dst = [d for d in left_dst if id(d) not in pairs]
        cands = []
        for di, d in enumerate(left_dst):
            row = []
            for si, s in enumerate(left_src):
                score = _similarity(blob(s, 'a'), blob(d, 'b'), min_score, cache)
                if score >= min_score:
                    name_score = 1 if _basename(s.a_path) == _basename(d.b_path) else 0
                    row.append((score, name_score, si, di))
            row.sort(key=lambda t: (-t[0], -t[1]))
            cands.extend(row[:4])
        cands.sort(key=lambda t: (-t[0], -t[1]))
        for i, (score, ns, si, di) in enumerate(cands):
            d, s = left_dst[di], left_src[si]
            if id(d) in pairs or id(s) in used:
                continue
            for other in cands[i + 1:]:
                if (other[0], other[1]) != (score, ns):
                    break
                od, os_ = left_dst[other[3]], left_src[other[2]]
                if (other[3] == di or other[2] == si) and id(od) not in pairs and id(os_) not in used:
                    raise Unsupported('rename candidates with equal scores (%s)' % d.b_path)
            pairs[id(d)] = (s, score)
            used.add(id(s))
    if not pairs:
        return changes
    out = []
    for c in changes:
        if c.status == 'D' and id(c) in used:
            continue
        if c.status == 'A' and id(c) in pairs:
            s, score = pairs[id(c)]
            r = Change('R', s.a_path, c.b_path, (s.a_mode, s.a_oid), (c.b_mode, c.b_oid), score)
            out.append(r)
            continue
        out.append(c)
    return out


def apply_filter(changes, spec):
    """--diff-filter: upper-case letters keep, lower-case drop."""
    if not spec:
        return changes
    include = {ch for ch in spec if ch.isupper()}
    exclude = {ch.upper() for ch in spec if ch.islower()}
    for ch in include | exclude:
        if ch not in 'ACDMRTUXB*':
            raise Unsupported('--diff-filter=%s' % spec)
    if '*' in include:
        raise Unsupported('--diff-filter=*')
    out = []
    for c in changes:
        if include and c.status not in include:
            continue
        if c.status in exclude:
            continue
        out.append(c)
    return out


# -- output ---------------------------------------------------------------------------------


def _q(path, quote, z):
    if z:
        return path
    return _u.cquote(path, quote)


def name_only(changes, quote, z):
    term = '\0' if z else '\n'
    return ''.join(_q(c.path, quote, z) + term for c in changes)


def name_status(changes, quote, z):
    out = []
    for c in changes:
        if c.status == 'R':
            st = 'R%03d' % (c.score * 100 // MAX_SCORE)
            if z:
                out.append('%s\0%s\0%s\0' % (st, c.a_path, c.b_path))
            else:
                out.append('%s\t%s\t%s\n' % (st, _q(c.a_path, quote, False), _q(c.b_path, quote, False)))
        else:
            if z:
                out.append('%s\0%s\0' % (c.status, c.path))
            else:
                out.append('%s\t%s\n' % (c.status, _q(c.path, quote, False)))
    return ''.join(out)


def _lines(reader, c):
    a = reader.data(c.a_path, c.a_oid) if c.status not in ('A',) else b''
    b = reader.data(c.b_path, c.b_oid) if c.status not in ('D',) else b''
    return a, b


def pprint_rename(a, b, quote=True):
    if _u.cquote(a, quote) != a or _u.cquote(b, quote) != b:
        return '%s => %s' % (_u.cquote(a, quote), _u.cquote(b, quote))
    pfx = 0
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        if a[i] == '/':
            pfx = i + 1
        i += 1
    sfx = 0
    oa, ob = len(a) - 1, len(b) - 1
    adj = 1 if pfx else 0
    while pfx - adj <= oa and pfx - adj <= ob and oa >= 0 and ob >= 0 and a[oa] == b[ob]:
        if a[oa] == '/':
            sfx = len(a) - oa
        oa -= 1
        ob -= 1
    am = max(len(a) - pfx - sfx, 0)
    bm = max(len(b) - pfx - sfx, 0)
    if pfx + sfx:
        return '%s{%s => %s}%s' % (a[:pfx], a[pfx:pfx + am], b[pfx:pfx + bm], a[len(a) - sfx:])
    return '%s => %s' % (a[pfx:pfx + am], b[pfx:pfx + bm])


def numstat(changes, reader, quote, z):
    out = []
    for c in changes:
        if c.status == 'U':
            out.append('0\t0\t%s%s' % (_q(c.path, quote, z), '\0' if z else '\n'))
            continue
        a, b = _lines(reader, c)
        if is_binary(a) or is_binary(b):
            nums = '-\t-\t'
        else:
            added, deleted = _xdiff.counts(_xdiff.split_lines(a), _xdiff.split_lines(b))
            nums = '%d\t%d\t' % (added, deleted)
        if c.status == 'R':
            if z:
                out.append('%s\0%s\0%s\0' % (nums, c.a_path, c.b_path))
            else:
                out.append('%s%s\n' % (nums, pprint_rename(c.a_path, c.b_path, quote)))
        else:
            out.append('%s%s%s' % (nums, _q(c.path, quote, z), '\0' if z else '\n'))
    return ''.join(out)


def shortstat(changes, reader):
    files = ins = dels = 0
    for c in changes:
        if c.status == 'U':
            continue
        a, b = _lines(reader, c)
        if c.status == 'R' and a == b and c.a_mode == c.b_mode:
            files += 1
            continue
        if c.status == 'M' and a == b and c.a_mode == c.b_mode:
            continue
        files += 1
        if is_binary(a) or is_binary(b):
            continue
        added, deleted = _xdiff.counts(_xdiff.split_lines(a), _xdiff.split_lines(b))
        ins += added
        dels += deleted
    if not files:
        return ''
    s = ' %d file%s changed' % (files, '' if files == 1 else 's')
    if ins or dels == 0:
        s += ', %d insertion%s(+)' % (ins, '' if ins == 1 else 's')
    if dels or ins == 0:
        s += ', %d deletion%s(-)' % (dels, '' if dels == 1 else 's')
    return s + '\n'


def patch(changes, reader, repo, ctx_lines=3, src_prefix='a/', dst_prefix='b/', abbrev_fn=None):
    """Unified patches as `git diff` prints them (no color, no rename patches). A name is prefix
    and path C-quoted together per core.quotePath (git's quote_two); a `---`/`+++` name with a
    space gets a trailing tab, as git writes it."""
    out = []
    quote = _u.quotepath(repo)
    for c in changes:
        if c.status == 'R':
            raise Unsupported('a patch for a rename')
        if c.status == 'U':
            raise Unsupported('a patch for an unmerged path')
        a_name = _u.cquote(src_prefix + c.a_path, quote)
        b_name = _u.cquote(dst_prefix + c.b_path, quote)
        a, b = _lines(reader, c)
        a_oid = reader.oid(c.a_path, c.a_oid) if c.status != 'A' else None
        b_oid = reader.oid(c.b_path, c.b_oid) if c.status != 'D' else None
        zero = '0' * 7
        ab = (lambda o: abbrev_fn(o)) if abbrev_fn else (lambda o: o[:7])
        head = ['diff --git %s %s\n' % (a_name, b_name)]
        if c.status == 'A':
            head.append('new file mode %o\n' % c.b_mode)
            head.append('index %s..%s\n' % (zero, ab(b_oid)))
        elif c.status == 'D':
            head.append('deleted file mode %o\n' % c.a_mode)
            head.append('index %s..%s\n' % (ab(a_oid), zero))
        else:
            if c.a_mode != c.b_mode:
                head.append('old mode %o\nnew mode %o\n' % (c.a_mode, c.b_mode))
                if a_oid != b_oid:
                    head.append('index %s..%s\n' % (ab(a_oid), ab(b_oid)))
            else:
                if a_oid == b_oid:
                    continue
                head.append('index %s..%s %o\n' % (ab(a_oid), ab(b_oid), c.a_mode))
        if a_oid == b_oid:
            out.append(''.join(head).encode('utf-8', 'surrogateescape'))
            continue
        if is_binary(a) or is_binary(b):
            out.append(''.join(head).encode('utf-8', 'surrogateescape'))
            out.append(('Binary files %s and %s differ\n' % (
                '/dev/null' if c.status == 'A' else a_name,
                '/dev/null' if c.status == 'D' else b_name)).encode('utf-8', 'surrogateescape'))
            continue
        hunks = _xdiff.unified(_xdiff.split_lines(a), _xdiff.split_lines(b), ctx_lines)
        if not hunks:
            out.append(''.join(head).encode('utf-8', 'surrogateescape'))
            continue
        for mark, name in (('---', '/dev/null' if c.status == 'A' else a_name),
                           ('+++', '/dev/null' if c.status == 'D' else b_name)):
            head.append('%s %s%s\n' % (mark, name, '\t' if ' ' in name else ''))
        out.append(''.join(head).encode('utf-8', 'surrogateescape'))
        out.extend(hunks)
    return b''.join(out)


# -- shared argument handling ----------------------------------------------------------------


def ambiguous(arg):
    return GitExit(128, "fatal: ambiguous argument '%s': unknown revision or path not in the working "
                        "tree.\nUse '--' to separate paths from revisions, like this:\n"
                        "'git <command> [<revision>...] -- [<file>...]'\n" % arg)


def is_rev(ctx, arg):
    """Whether `arg` names a revision (A, A..B, A...B, ^A) for the rev-or-path split."""
    repo = ctx.repo
    if repo is None:
        return False
    from .plumbing import resolve
    for sep in ('...', '..'):
        if sep in arg:
            a, b = arg.split(sep, 1)
            oa = resolve(ctx, a or 'HEAD')
            ob = resolve(ctx, b or 'HEAD')
            return oa is not None and ob is not None
    if arg.startswith('^'):
        arg = arg[1:]
    return resolve(ctx, arg) is not None


def split_args(ctx, pos, dd):
    """(revisions, paths) as setup_revisions splits them: with '--' everything before is a
    revision; without, revisions until the first argument that is not one, which (and the rest)
    must be existing paths."""
    if dd is not None:
        for a in pos:
            if not is_rev(ctx, a):
                raise GitExit(128, "fatal: bad revision '%s'\n" % a)
        return list(pos), list(dd)
    revs = []
    i = 0
    while i < len(pos) and is_rev(ctx, pos[i]):
        revs.append(pos[i])
        i += 1
    paths = list(pos[i:])
    for p in paths:
        if ctx.repo is None or ctx.repo.worktree is None:
            raise ambiguous(p)
        full = os.path.join(ctx.cwd, p)
        if not os.path.lexists(full):
            raise ambiguous(p)
        if is_rev(ctx, p):
            raise Unsupported('an argument that is both a revision and a path: %s' % p)
    return revs, paths


# -- git diff -------------------------------------------------------------------------------

_DIFF = _u.Spec('diff', flags='cached staged name-only name-status numstat shortstat z no-renames '
                'no-color no-ext-diff quiet exit-code patch|p no-prefix find-renames|M R raw '
                'ignore-submodules ita-invisible-in-index',
                values='diff-filter src-prefix dst-prefix color',
                optional='U|unified')


def _opts_format(opts):
    fmts = [k for k in ('name-only', 'name-status', 'numstat', 'shortstat', 'raw') if opts.get(k)]
    if opts.get('raw'):
        raise Unsupported('diff --raw')
    if opts.get('R'):
        raise Unsupported('diff -R')
    color = opts.get('color')
    if color not in (None, 'never'):
        raise Unsupported('--color=%s' % color)
    return fmts


def emit(ctx, repo, changes, opts, reader, fmts):
    quote = _u.quotepath(repo)
    z = bool(opts.get('z'))
    if 'name-only' in fmts:
        ctx.write(name_only(changes, quote, z))
        return
    if 'name-status' in fmts:
        ctx.write(name_status(changes, quote, z))
        return
    if 'numstat' in fmts:
        ctx.write(numstat(changes, reader, quote, z))
        if 'shortstat' in fmts:
            ctx.write(shortstat(changes, reader))
        return
    if 'shortstat' in fmts:
        ctx.write(shortstat(changes, reader))
        return
    if opts.get('quiet'):
        return
    u = opts.get('U')
    n = 3
    if u is not None and u is not True:
        n = int(u)
    elif u is True:
        n = 3
    else:
        cfg = repo.config.get('diff.context')
        if cfg is not None:
            n = int(cfg)
    if repo.config.get('diff.external') and not opts.get('no-ext-diff'):
        raise Unsupported('diff.external')
    src = opts.get('src-prefix', 'a/')
    dst = opts.get('dst-prefix', 'b/')
    if opts.get('no-prefix'):
        src = dst = ''
    explicit = opts.get('no-prefix') or ('src-prefix' in opts and 'dst-prefix' in opts)
    if not explicit and any(repo.config.get(k) for k in (
            'diff.noprefix', 'diff.mnemonicprefix', 'diff.srcprefix', 'diff.dstprefix')):
        # both prefixes given on the command line override these settings; otherwise unported
        raise Unsupported('diff.noprefix / diff.mnemonicPrefix / diff.srcPrefix / diff.dstPrefix')
    from .plumbing import abbrev
    ctx.write(patch(changes, reader, repo, n, src, dst, lambda o: abbrev(repo, o)))


def renames_wanted(repo, opts):
    if opts.get('no-renames'):
        return False
    if opts.get('find-renames'):    # an explicit option beats diff.renames, as in git
        return True
    v = repo.config.get('diff.renames')
    if v is not None:
        low = v.strip().lower()
        if low in ('copy', 'copies'):
            raise Unsupported('diff.renames=copies')
        from .config import parse_bool
        return parse_bool(v, 'diff.renames')
    return True


def tree_of(ctx, spec):
    from .plumbing import resolve
    oid = resolve(ctx, spec)
    if oid is None:
        return None
    return _revs.peel(ctx.repo, oid, 'tree')


@command('diff')
def diff_cmd(ctx, args):
    opts, pos, dd = _u.parse(_DIFF, args)
    if opts.get('M') and opts.get('M') is not True:
        raise Unsupported('diff -M<n>')
    fmts = _opts_format(opts)
    repo = ctx.need_repo()
    cached = opts.get('cached') or opts.get('staged')
    revs, paths = split_args(ctx, pos, dd)
    spec = _u.Pathspec(ctx.prefix or '', paths) if paths else None
    filt = spec.match if spec else None
    ix = None
    tree = None
    reader = Reader(repo)
    if len(revs) == 1 and '...' in revs[0]:
        a, b = revs[0].split('...', 1)
        from .plumbing import resolve
        oa = _revs.peel(repo, resolve(ctx, a or 'HEAD'), 'commit')
        ob = _revs.peel(repo, resolve(ctx, b or 'HEAD'), 'commit')
        bases = _revs.merge_bases(repo, oa, [ob])
        if not bases:
            raise die('%s: no merge base' % revs[0])
        if len(bases) > 1:
            raise Unsupported('diff A...B with several merge bases')
        old = tree_side(repo, _revs.peel(repo, bases[0], 'tree'))
        new = tree_side(repo, _revs.peel(repo, ob, 'tree'))
    elif len(revs) == 1 and '..' in revs[0]:
        a, b = revs[0].split('..', 1)
        old = tree_side(repo, tree_of(ctx, a or 'HEAD'))
        new = tree_side(repo, tree_of(ctx, b or 'HEAD'))
    elif len(revs) == 2:
        for r in revs:
            if '..' in r or r.startswith('^'):
                raise Unsupported('diff %s' % ' '.join(revs))
        from .plumbing import resolve
        for r in revs:
            o = resolve(ctx, r)
            if o is not None and not repo.objects.has(o):
                raise die('bad object %s' % r)       # a full hex naming no object
        ta, tb = tree_of(ctx, revs[0]), tree_of(ctx, revs[1])
        if ta is None or tb is None:
            raise Unsupported('diff of a non-tree object')
        old, new = tree_side(repo, ta), tree_side(repo, tb)
    elif len(revs) > 2:
        raise Unsupported('diff with %d revisions' % len(revs))
    else:
        if not cached:
            ctx.need_worktree()
        ix =_index.Index(repo).load()
        if revs:
            if revs[0].startswith('^'):
                raise Unsupported('diff ^rev')
            t = tree_of(ctx, revs[0])
            if t is None:
                raise Unsupported('diff against a non-tree')
            old = tree_side(repo, t)
        elif cached:
            from .plumbing import resolve
            h = resolve(ctx, 'HEAD')
            old = tree_side(repo, _revs.peel(repo, h, 'tree')) if h else {}
        else:
            old = None
        if cached:
            new, unmerged = index_side(ix)
        else:
            ctx.need_worktree()
            tree = _wt.Tree(repo, ix)
            reader = Reader(repo, tree)
            new = worktree_side(tree, ix)
            unmerged = ix.unmerged()
            if old is None:
                old, _um = index_side(ix)
                # an intent-to-add entry is new in the work tree
                for (p, s), e in ix.entries.items():
                    if s == 0 and e.intent_to_add:
                        raise Unsupported('diff with an intent-to-add entry')
        changes = compare(old, new, filt)
        changes = _resolve_worktree(changes, reader)
        if unmerged:
            if revs and not cached:
                raise Unsupported('diff <rev> with unmerged paths')
            ums = [p for p in unmerged if filt is None or filt(p)]
            if ums:
                changes = [c for c in changes if c.path not in ums]
                for p in ums:
                    changes.append(Change('U', p, p, None, None))
                changes.sort(key=lambda c: pkey(c.path))
        if renames_wanted(repo, opts) and (cached or revs):
            changes = detect_renames(changes, reader)
        changes = apply_filter(changes, opts.get('diff-filter'))
        emit(ctx, repo, changes, opts, reader, fmts)
        return _exit(opts, changes)
    changes = compare(old, new, filt)
    if renames_wanted(repo, opts):
        changes = detect_renames(changes, reader)
    changes = apply_filter(changes, opts.get('diff-filter'))
    emit(ctx, repo, changes, opts, reader, fmts)
    return _exit(opts, changes)


def _resolve_worktree(changes, reader):
    """Drop work-tree 'changes' whose content turns out the same (git diff refreshes first)."""
    out = []
    for c in changes:
        if c.status == 'M' and c.b_oid is None and c.a_oid is not None:
            if reader.oid(c.b_path, None) == c.a_oid and c.a_mode == c.b_mode:
                continue
        out.append(c)
    return out


def _exit(opts, changes):
    if opts.get('exit-code') or opts.get('quiet'):
        return 1 if changes else 0
    return 0


# -- git diff-tree --------------------------------------------------------------------------

_DTREE = _u.Spec('diff-tree', flags='r root no-commit-id name-only name-status no-renames z stdin '
                 'm t', values='diff-filter')


def commit_changes(repo, commit, opts, reader, root=True):
    """Changes of a commit against its first parent (the empty tree for a root commit)."""
    c = _revs.commit(repo, commit)
    if c.parents:
        old = tree_side(repo, _revs.commit(repo, c.parents[0]).tree)
    elif root:
        old = {}
    else:
        return None
    changes = compare(old, tree_side(repo, c.tree))
    if renames_wanted(repo, opts):
        changes = detect_renames(changes, reader)
    return apply_filter(changes, opts.get('diff-filter'))


@command('diff-tree')
def diff_tree(ctx, args):
    opts, pos, dd = _u.parse(_DTREE, args)
    if dd:
        raise Unsupported('diff-tree with paths')
    if not opts.get('r') or opts.get('t') or opts.get('m'):
        raise Unsupported('diff-tree without -r')
    fmts = [k for k in ('name-only', 'name-status') if opts.get(k)]
    if not fmts:
        raise Unsupported('diff-tree raw output')
    repo = ctx.need_repo()
    quote = _u.quotepath(repo)
    z = bool(opts.get('z'))
    reader = Reader(repo)
    term = '\0' if z else '\n'
    from .plumbing import resolve

    def one(spec, show_id):
        oid = resolve(ctx, spec)
        cm = _revs.peel(repo, oid, 'commit') if oid else None
        if cm is None:
            raise Unsupported('diff-tree of a non-commit %s' % spec)
        c = _revs.commit(repo, cm)
        if len(c.parents) > 1:
            return                       # a merge shows nothing without -m/-c/--cc
        changes = commit_changes(repo, cm, opts, reader, root=bool(opts.get('root')))
        if changes is None:
            return
        if show_id and not opts.get('no-commit-id'):
            ctx.write(cm + term)
        if fmts[0] == 'name-only':
            ctx.write(name_only(changes, quote, z))
        else:
            ctx.write(name_status(changes, quote, z))
    if opts.get('stdin'):
        if pos:
            raise Unsupported('diff-tree --stdin with arguments')
        for line in ctx.stdin_bytes().decode('utf-8', 'surrogateescape').splitlines():
            line = line.strip()
            if not line:
                continue
            if ' ' in line:
                raise Unsupported('diff-tree --stdin with parents on a line')
            one(line, True)
        return 0
    if len(pos) != 1:
        raise Unsupported('diff-tree %s' % ' '.join(pos))
    one(pos[0], False)
    return 0
