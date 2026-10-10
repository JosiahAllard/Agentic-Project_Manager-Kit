"""Revisions: naming (sha1-name.c get_oid) and walking (revision.c), for the forms the kit uses.

Names: a full or abbreviated hash (refs win over a short hash, as in git), a ref by git's DWIM
rules, HEAD and `@`, `<rev>~N`, `^N`, `^{}`, `^{commit|tree|blob|tag|object}`, `<rev>:<path>`,
`:<path>` and `:<n>:<path>` (the index), `<branch>@{upstream}` / `@{u}` / `@{push}` (as
upstream). Reflog forms (`@{1}`, `@{yesterday}`, `@{-1}`) and `^{/text}` are Unsupported.

Walks follow git's default order: commits by committer date, newest first, equal dates in the
order they were queued (commit_list_insert_by_date), the start list stable-sorted by date.
"""
import re

from . import refs as _refs
from .errors import GitExit, Unsupported, die
from .objects import EMPTY_TREE, Commit, Tag, parse_tree

_HEX = re.compile(r'^[0-9a-f]+$')


class Ambiguous(Exception):
    pass


def read_object(repo, oid):
    got = repo.objects.read(oid)
    if got is None:
        return None, None
    return got


def commit(repo, oid):
    cache = repo.__dict__.setdefault('_commits', {})
    c = cache.get(oid)
    if c is None:
        kind, data = read_object(repo, oid)
        if kind != 'commit':
            return None
        c = cache[oid] = Commit(oid, data)
    return c


def tag(repo, oid):
    kind, data = read_object(repo, oid)
    if kind != 'tag':
        return None
    return Tag(oid, data)


def obj_type(repo, oid):
    got = repo.objects.read(oid)
    return got[0] if got else None


def peel(repo, oid, want=None):
    """Follow tags (and a commit to its tree for want 'tree'). None when impossible."""
    seen = 0
    while oid is not None and seen < 20:
        seen += 1
        kind = obj_type(repo, oid)
        if kind is None:
            return None
        if want in (None, 'object') and kind != 'tag':
            return oid
        if kind == want:
            return oid
        if kind == 'tag':
            t = tag(repo, oid)
            oid = t.object
            continue
        if kind == 'commit' and want == 'tree':
            return commit(repo, oid).tree
        return None
    return None


def tree_entries(repo, tree_oid):
    """[(mode, name, oid)] of a tree; None when the object is not a tree. A tree object that is
    missing dies with git's words ("fatal: unable to read tree (<oid>)", exit 128: git 2.55
    read-tree with a subtree object removed, probed 2026-10-10): taking it for an empty tree would
    drop every path under it from what is written next. The empty tree needs no object, as in
    git."""
    kind, data = read_object(repo, tree_oid)
    if kind is None:
        if tree_oid == EMPTY_TREE.get(repo.algo):
            return []
        raise die('unable to read tree (%s)' % tree_oid)
    if kind != 'tree':
        return None
    return parse_tree(data, repo.hexlen)


def tree_lookup(repo, tree_oid, path):
    """(mode, oid) of `path` ('a/b') inside a tree, or None. A tree on the way that is missing
    gives None too: git names such a `<rev>:<path>` as missing (rev-parse --verify -q exits 1,
    cat-file --batch-check says "<name> missing") rather than dying as read-tree does."""
    if path in ('', '.'):
        return ('40000', tree_oid)
    parts = [p for p in path.split('/') if p]
    cur = tree_oid
    mode = '40000'
    for i, part in enumerate(parts):
        if read_object(repo, cur)[0] is None and cur != EMPTY_TREE.get(repo.algo):
            return None
        ents = tree_entries(repo, cur)
        if ents is None:
            return None
        bpart = part.encode('utf-8')
        for m, name, oid in ents:
            if name == bpart:
                mode, cur = m, oid
                break
        else:
            return None
        if i < len(parts) - 1 and mode != '40000':
            return None
    return (mode, cur)


def flatten_tree(repo, tree_oid, prefix='', recurse=True, out=None):
    """{path: (mode, oid)} for every blob (and, unless recurse, tree) under a tree."""
    if out is None:
        out = {}
    ents = tree_entries(repo, tree_oid)
    if ents is None:
        return out
    for mode, name, oid in ents:
        p = prefix + name.decode('utf-8', 'surrogateescape')
        if mode == '40000' and recurse:
            flatten_tree(repo, oid, p + '/', True, out)
        else:
            out[p] = (mode, oid)
    return out


# -- naming ---------------------------------------------------------------------------------


def resolve_short(repo, hexpart):
    """The one oid starting with `hexpart`, None when there is none; raises Ambiguous."""
    if len(hexpart) == repo.hexlen:
        return hexpart if repo.objects.has(hexpart) else None
    found = repo.objects.prefix_matches(hexpart)
    if len(found) > 1:
        raise Ambiguous(hexpart)
    return next(iter(found)) if found else None


def upstream_of(repo, branch, push=False):
    """(remote-tracking ref) for refs/heads/<branch>; raises GitExit as git words it."""
    cfg = repo.config
    remote = cfg.get('branch.%s.remote' % branch)
    merge = cfg.get('branch.%s.merge' % branch)
    if remote is None or merge is None:
        if not _refs.exists(repo, 'refs/heads/' + branch):
            raise die("no such branch: '%s'" % branch)
        raise die("no upstream configured for branch '%s'" % branch)
    if remote == '.':
        return merge
    tracking = map_ref(repo, remote, merge)
    if tracking is None:
        raise die("upstream branch '%s' not stored as a remote-tracking branch" % merge)
    if not _refs.exists(repo, tracking):
        raise die("upstream branch '%s' not stored as a remote-tracking branch" % merge)
    return tracking


def map_ref(repo, remote, src):
    """The local ref `src` on `remote` maps to by remote.<name>.fetch, or None."""
    for spec in repo.config.get_all('remote.%s.fetch' % remote):
        spec = spec.lstrip('+')
        a, _, b = spec.partition(':')
        if '*' in a:
            pa, sa = a.split('*', 1)
            pb, sb = b.split('*', 1)
            if src.startswith(pa) and src.endswith(sa) and len(src) >= len(pa) + len(sa):
                mid = src[len(pa):len(src) - len(sa)] if sa else src[len(pa):]
                return pb + mid + sb
        elif a == src:
            return b or None
    return None


def current_branch(repo):
    """The short branch name HEAD points to, or None when detached."""
    raw = _refs.read_raw(repo, 'HEAD')
    if raw and raw[0] == 'sym' and raw[1].startswith('refs/heads/'):
        return raw[1][len('refs/heads/'):]
    return None


def dwim_ref(repo, name):
    """(full ref name, oid) for a short or full ref name, or (None, None)."""
    if name in ('HEAD', '@'):
        oid, _ = _refs.resolve(repo, 'HEAD')
        return ('HEAD', oid) if oid else (None, None)
    found = _refs.dwim(repo, name)
    if not found:
        return None, None
    full = found[0]
    oid, _ = _refs.resolve(repo, full)
    return full, oid


def _base_name(repo, name, index_reader=None):
    """oid for a name with no ~ ^ suffix (may carry @{...})."""
    m = re.match(r'^(.*)@\{([^}]*)\}$', name)
    if m:
        what = m.group(2)
        base = m.group(1)
        if what.lower() in ('u', 'upstream', 'push'):
            if base in ('', 'HEAD', '@'):
                br = current_branch(repo)
                if br is None:
                    raise die('HEAD does not point to a branch')
            else:
                br = base[len('refs/heads/'):] if base.startswith('refs/heads/') else base
            tracking = upstream_of(repo, br)
            oid, _ = _refs.resolve(repo, tracking)
            return oid
        raise Unsupported('reflog revision %s' % name)
    if name == '@':
        name = 'HEAD'
    if not name:
        return None
    if len(name) == repo.hexlen and _HEX.match(name):
        return name
    full, oid = dwim_ref(repo, name)
    if full is not None:
        return oid
    if 4 <= len(name) < repo.hexlen and _HEX.match(name):
        return resolve_short(repo, name)
    # git describe output: <tag>-<n>-g<hex>
    m = re.match(r'^.*-g([0-9a-f]{4,})$', name)
    if m:
        return resolve_short(repo, m.group(1))
    return None


_SUFFIX = re.compile(r'(\^\{[^}]*\}|\^[0-9]*|~[0-9]*)$')


def get_oid(repo, spec, index_reader=None):
    """The oid a revision expression names, or None. Raises Ambiguous, GitExit, Unsupported."""
    if spec.startswith(':'):
        rest = spec[1:]
        stage = 0
        m = re.match(r'^([0-3]):(.*)$', rest)
        if m:
            stage, rest = int(m.group(1)), m.group(2)
        if rest.startswith('/') or rest.startswith('!'):
            raise Unsupported('revision %s' % spec)
        if index_reader is None:
            return None
        return index_reader(rest, stage)
    colon = _path_colon(spec)
    if colon is not None:
        rev, path = spec[:colon], spec[colon + 1:]
        base = get_oid(repo, rev, index_reader)
        if base is None:
            return None
        tree = peel(repo, base, 'tree')
        if tree is None:
            return None
        if path.startswith('./') or path.startswith('../'):
            raise Unsupported('relative tree path %s' % spec)
        hit = tree_lookup(repo, tree, path.rstrip('/'))
        return hit[1] if hit else None
    m = _SUFFIX.search(spec)
    if m and m.start() > 0:
        base = get_oid(repo, spec[:m.start()], index_reader)
        if base is None:
            return None
        suf = m.group(1)
        if suf.startswith('^{'):
            want = suf[2:-1]
            if want == '':
                return peel(repo, base, None)
            if want.startswith('/'):
                raise Unsupported('revision %s' % spec)
            if want not in ('commit', 'tree', 'blob', 'tag', 'object'):
                return None
            if want == 'tag':
                return base if obj_type(repo, base) == 'tag' else None
            if want == 'object':
                return base
            return peel(repo, base, want)
        if suf.startswith('^'):
            n = int(suf[1:]) if len(suf) > 1 else 1
            c = peel(repo, base, 'commit')
            if c is None:
                return None
            if n == 0:
                return c
            ps = commit(repo, c).parents
            return ps[n - 1] if n <= len(ps) else None
        n = int(suf[1:]) if len(suf) > 1 else 1
        c = peel(repo, base, 'commit')
        for _ in range(n):
            if c is None:
                return None
            ps = commit(repo, c).parents
            c = ps[0] if ps else None
        return c
    return _base_name(repo, spec, index_reader)


def _path_colon(spec):
    """Index of the ':' that splits `<rev>:<path>`, or None (a '@{...}' or '^{...}' is skipped)."""
    depth = 0
    for i, ch in enumerate(spec):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        elif ch == ':' and depth == 0 and i > 0:
            return i
    return None


def commit_oid(repo, spec, index_reader=None):
    oid = get_oid(repo, spec, index_reader)
    return peel(repo, oid, 'commit') if oid else None


# -- walking --------------------------------------------------------------------------------


def _date(repo, oid):
    return commit(repo, oid).commit_time


def insert_by_date(repo, lst, oid):
    d = _date(repo, oid)
    i = 0
    n = len(lst)
    while i < n and _date(repo, lst[i]) >= d:
        i += 1
    lst.insert(i, oid)


def sort_by_date(repo, oids):
    return sorted(oids, key=lambda o: -_date(repo, o))


def ancestors(repo, starts, first_parent=False):
    """Every commit reachable from `starts` (inclusive)."""
    seen = set()
    stack = list(starts)
    while stack:
        o = stack.pop()
        if o in seen:
            continue
        seen.add(o)
        c = commit(repo, o)
        if c is None:
            raise die('bad object %s' % o)
        ps = c.parents[:1] if first_parent else c.parents
        stack.extend(p for p in ps if p not in seen)
    return seen


def walk(repo, include, exclude=(), first_parent=False, no_walk=False):
    """Commits reachable from `include` and not from `exclude`, in git's default order."""
    if no_walk:
        out = []
        for o in sort_by_date(repo, _dedup(include)):
            if o not in exclude:
                out.append(o)
        return out
    hidden = ancestors(repo, exclude, False) if exclude else set()
    queue = sort_by_date(repo, [o for o in _dedup(include)])
    seen = set(queue)
    out = []
    while queue:
        o = queue.pop(0)
        c = commit(repo, o)
        if c is None:
            raise die('bad object %s' % o)
        ps = c.parents[:1] if first_parent else c.parents
        for p in ps:
            if p not in seen:
                seen.add(p)
                insert_by_date(repo, queue, p)
        if o not in hidden:
            out.append(o)
    return out


def _dedup(oids):
    out = []
    seen = set()
    for o in oids:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


def is_ancestor(repo, a, b):
    """True when commit a is reachable from b."""
    if a == b:
        return True
    da = _date(repo, a)
    seen = set()
    stack = [b]
    while stack:
        o = stack.pop()
        if o == a:
            return True
        if o in seen:
            continue
        seen.add(o)
        stack.extend(commit(repo, o).parents)
    return False


def merge_bases(repo, a, others):
    """Best common ancestors of `a` and each of `others` (git merge-base A B...), newest first."""
    anc_a = ancestors(repo, [a])
    anc_b = ancestors(repo, others)
    common = anc_a & anc_b
    if not common:
        return []
    # drop every common ancestor that is an ancestor of another common ancestor
    redundant = set()
    for c in common:
        for p in commit(repo, c).parents:
            if p in common:
                redundant |= ancestors(repo, [p])
    best = [c for c in common if c not in redundant]
    return sort_by_date(repo, best)


def parse_range_args(repo, specs, index_reader=None, error=None):
    """(include, exclude, symmetric pairs) for rev-list style arguments (A, ^A, A..B, A...B).
    Raises GitExit via `error(spec)` when one does not resolve."""
    include, exclude, sym = [], [], []
    neg = False

    def need(s):
        o = commit_oid(repo, s or 'HEAD', index_reader)
        if o is None:
            raise error(s)
        return o
    for spec in specs:
        if spec == '--not':
            neg = not neg
            continue
        if '...' in spec:
            a, b = spec.split('...', 1)
            oa, ob = need(a), need(b)
            include += [oa, ob]
            for mb in merge_bases(repo, oa, [ob]):
                exclude.append(mb)
            sym.append((oa, ob))
            continue
        if '..' in spec:
            a, b = spec.split('..', 1)
            oa, ob = need(a), need(b)
            if neg:
                include.append(oa)
                exclude.append(ob)
            else:
                exclude.append(oa)
                include.append(ob)
            continue
        if spec.startswith('^'):
            o = need(spec[1:])
            (include if neg else exclude).append(o)
            continue
        o = need(spec)
        (exclude if neg else include).append(o)
    return include, exclude, sym


__all__ = ['Ambiguous', 'GitExit']
