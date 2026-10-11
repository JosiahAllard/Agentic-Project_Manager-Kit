"""kitlib.pieces: the path-to-piece table; docs/repo-checks.md.

A repository's pieces table says where each piece of it lives, so a tool names a piece by id
(`table.path('app-scripts', 'src/test/resources/')`), never by a path literal, and moving a
folder is an edit to the table and nothing else. The table is repository data: `.kit.json`
"pieces" (inline, or a repo-relative JSON path the config loader reads, kitlib.config). A
repository without one has no pieces (`has_table`); a kit tool that only refines its answer by
piece then skips that refinement.

The json: `schema` 1; `root` (paths that are no piece and never move); `dotnet` (the solution
file, the files beside it that count as .NET changes, and their `was` map); `removed` (folders a
move deleted); and `pieces`, each with `id`, `group`, `kind` (one of KINDS), `dir` and, as the
kind needs: `gradle` (the project path, `:name`), `dotnet` (a change here is a .NET change),
`wire` and `forces` (a change here is a protocol change and force-tests those Gradle modules),
`outputs` (build products, relative to `dir`), `artifact`, `optional`, `owner_only`,
`transitional`, `was` (the dir before a move; `repath` maps an old path through it) and `deps`
(the pieces this one builds against, transitive through `dependents`).

Pure: nothing here runs git. Errors are TableError (a malformed table, a missing one) and
KeyError (an unknown piece id); ConfigError from kitlib.config passes through.
"""
import json
import posixpath

from kitlib.config import load as load_config

SECTION = 'pieces'

KINDS = frozenset(('gradle', 'dotnet', 'spec', 'docker', 'asset', 'docs', 'builder', 'file', 'planned'))
FIELDS = frozenset(('id', 'group', 'kind', 'dir', 'gradle', 'dotnet', 'wire', 'forces', 'outputs',
                    'artifact', 'optional', 'owner_only', 'transitional', 'was', 'deps'))
TOP = frozenset(('schema', 'about', 'root', 'dotnet', 'pieces', 'removed'))


class TableError(ValueError):
    """A pieces table is malformed or missing; the message names the entry."""


def _rel_ok(p):
    return (isinstance(p, str) and p and '\\' not in p and not p.startswith('/')
            and not p.endswith('/') and not ({'', '.', '..'} & set(p.split('/'))))


def _under(path, d):
    """`path` is `d` itself or inside it."""
    return path == d or path.startswith(d + '/')


class Table(object):
    """One loaded table. Every method is pure and answers in repo-relative `/` paths."""

    def __init__(self, data):
        self.data = data
        if not isinstance(data, dict) or data.get('schema') != 1:
            raise TableError('schema must be 1')
        extra = set(data) - TOP
        if extra:
            raise TableError('unknown top-level key(s): %s' % ', '.join(sorted(extra)))
        self.root = tuple(data.get('root', ()))
        self.removed = tuple(data.get('removed', ()))
        dn = data.get('dotnet') or {}
        self._solution = dn.get('solution', '')
        self.root_files = tuple(dn.get('root_files', ()))
        self._root_was = dict(dn.get('was', {}))
        self.pieces = []
        self._by_id = {}
        for p in data.get('pieces', ()):
            pid = p.get('id')
            if not pid or pid in self._by_id:
                raise TableError('piece id missing or repeated: %r' % pid)
            bad = set(p) - FIELDS
            if bad:
                raise TableError('%s: unknown field(s) %s' % (pid, ', '.join(sorted(bad))))
            for need in ('group', 'kind', 'dir'):
                if not p.get(need):
                    raise TableError('%s: no %s' % (pid, need))
            if p['kind'] not in KINDS:
                raise TableError('%s: unknown kind %r' % (pid, p['kind']))
            if not _rel_ok(p['dir']) or ('was' in p and not _rel_ok(p['was'])):
                raise TableError('%s: dir/was must be repo-relative, `/`-separated, no trailing /, '
                                 'no empty, `.` or `..` part (dir %r, was %r)' % (pid, p['dir'], p.get('was')))
            if (p['kind'] == 'gradle') != bool(p.get('gradle')) or \
                    (p.get('gradle') and not p['gradle'].startswith(':')):
                raise TableError('%s: a gradle piece, and only one, names its project as `:name`' % pid)
            self.pieces.append(p)
            self._by_id[pid] = p
        for p in self.pieces:
            deps = p.get('deps', [])
            if not isinstance(deps, list) or len(set(deps)) != len(deps):
                raise TableError('%s: deps must be a list of distinct piece ids' % p['id'])
            for d in deps:
                if d == p['id'] or d not in self._by_id:
                    raise TableError('%s: dep %r is itself or no piece in the table' % (p['id'], d))
        for p in self.pieces:
            if p['id'] in self.closure_deps(p['id'], strict=True):
                raise TableError('%s: deps form a cycle through it' % p['id'])
        for f in self.root + self.root_files + ((self._solution,) if self._solution else ()):
            if not _rel_ok(f):
                raise TableError('root path %r must be repo-relative, `/`-separated' % f)

    # ---- by id ----------------------------------------------------------------------------------
    def ids(self):
        return [p['id'] for p in self.pieces]

    def get(self, pid):
        try:
            return self._by_id[pid]
        except KeyError:
            raise KeyError('no piece %r in the table (.kit.json "pieces")' % pid)

    def dir(self, pid):
        return self.get(pid)['dir']

    def path(self, pid, rel=''):
        """`<dir of pid>/<rel>`; `rel` keeps its trailing `/` (a prefix stays a prefix)."""
        d = self.dir(pid)
        return d + '/' + rel.lstrip('/') if rel else d

    def deps(self, pid):
        """The pieces `pid` builds against directly (`deps`), in table order of the json."""
        return list(self.get(pid).get('deps', ()))

    def closure_deps(self, pid, strict=False):
        """Every piece `pid` reaches through `deps`; with `strict`, `pid` only if a cycle."""
        seen, todo = set(), list(self.deps(pid))
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(self.deps(d))
        return seen if strict else seen - {pid}

    def dependents(self, pids):
        """`pids` plus every piece that reaches one of them through `deps` (a change to a piece
        is a change to everything built on it), as a set."""
        out = set(pids)
        grew = True
        while grew:
            grew = False
            for p in self.pieces:
                if p['id'] not in out and any(d in out for d in p.get('deps', ())):
                    out.add(p['id'])
                    grew = True
        return out

    def output(self, pid, key):
        return self.path(pid, self.get(pid)['outputs'][key])

    # ---- by path --------------------------------------------------------------------------------
    def piece_of(self, path):
        """The id of the piece holding `path`; the longest dir wins (model-sums inside
        app-scripts). None for a root path or one no piece holds."""
        path = path.replace('\\', '/')
        best = None
        for p in self.pieces:
            if _under(path, p['dir']) and (best is None or len(p['dir']) > len(best['dir'])):
                best = p
        return best['id'] if best else None

    def owner(self, path):
        """('piece', id) / ('dotnet-root', file) / ('root', entry) for a repo-relative path, or
        None when nothing in the table holds it."""
        path = path.replace('\\', '/')
        if path in self.root_files:
            return ('dotnet-root', path)
        pid = self.piece_of(path)
        if pid:
            return ('piece', pid)
        for r in self.root:
            if _under(path, r):
                return ('root', r)
        return None

    def gradle_pieces(self):
        """[(module name as settings.gradle.kts includes it, dir)] in table order."""
        return [(p['gradle'].lstrip(':').replace(':', '/'), p['dir']) for p in self.pieces
                if p['kind'] == 'gradle']

    def gradle_module_of(self, path):
        """The Gradle module whose folder holds `path` (the longest such folder), or None. Only
        Gradle pieces count: a file piece nested in a module folder is still that module's."""
        path = path.replace('\\', '/')
        best = None
        for module, d in self.gradle_pieces():
            if path.startswith(d + '/') and (best is None or len(d) > len(best[1])):
                best = (module, d)
        return best[0] if best else None

    def dotnet_triggers(self):
        """(dirs, files): a path under one of the dirs, or equal to one of them or of the files,
        is a .NET change (the gate's dotnet steps; .md excepted by the gate)."""
        return (tuple(p['dir'] for p in self.pieces if p.get('dotnet')), self.root_files)

    def is_dotnet(self, path):
        """A .NET change: a path of a `dotnet` piece or a solution root file, or a path under the
        solution's folder that nothing in the table holds yet (a new project, a new
        Directory.Packages.props or nuget.config) - dotnet may read it, so it counts. With the
        solution at the repository root, that is any path the table does not hold."""
        path = path.replace('\\', '/')
        dirs, files = self.dotnet_triggers()
        if path in files or any(_under(path, d) for d in dirs):
            return True
        sdir = self.solution_dir()
        return (sdir == '' or _under(path, sdir)) and self.owner(path) is None

    def wire_prefixes(self):
        """`<dir>/` for every piece whose change is a wire (protocol) change."""
        return tuple(p['dir'] + '/' for p in self.pieces if p.get('wire'))

    def forces_of(self, path):
        """The Gradle modules a change to `path` force-tests through its piece's `forces`; none for
        a `.md` (a doc forces no module's tests)."""
        out = []
        if path.lower().endswith('.md'):
            return out
        for p in self.pieces:
            if p.get('forces') and path.startswith(p['dir'] + '/'):
                out += [m for m in p['forces'] if m not in out]
        return out

    def solution(self):
        return self._solution

    def solution_dir(self):
        """The folder dotnet runs in ('' for the repository root)."""
        return posixpath.dirname(self._solution)

    # ---- moves ----------------------------------------------------------------------------------
    def legacy_dirs(self):
        """Every `was` dir: where pieces lived before the last move."""
        return sorted({p['was'] for p in self.pieces if 'was' in p} | set(self._root_was))

    def repath(self, old):
        """Where the path `old` (before the move) lives now: through the root files' `was` map,
        then the longest piece `was`; None for a path under `removed`; unchanged otherwise."""
        old = old.replace('\\', '/')
        if old in self._root_was:
            return self._root_was[old]
        for r in self.removed:
            if _under(old, r):
                return None
        best = None
        for p in self.pieces:
            w = p.get('was')
            if w and _under(old, w) and (best is None or len(w) > len(best['was'])):
                best = p
        if best is None:
            return old
        return best['dir'] + old[len(best['was']):]


_CACHE = {}


def load(source):
    """A Table from an already-parsed dict or a JSON file path."""
    if isinstance(source, dict):
        return Table(source)
    with open(source, encoding='utf-8') as fh:
        return Table(json.load(fh))


def has_table(root, cfg=None):
    """Whether the repository at `root` keeps a pieces table (`.kit.json` "pieces")."""
    cfg = cfg if cfg is not None else load_config(root)
    return bool(cfg.section(SECTION))


def load_repo(root, cfg=None):
    """The repository's own table, from `.kit.json` "pieces"; TableError when it has none."""
    cfg = cfg if cfg is not None else load_config(root)
    data = cfg.section(SECTION)
    if not data:
        raise TableError('this repository has no pieces table (.kit.json "pieces")')
    return Table(data)


def table(root):
    """The repository's table, read once per process per root."""
    key = posixpath.normpath(str(root).replace('\\', '/')).lower()
    if key not in _CACHE:
        _CACHE[key] = load_repo(root)
    return _CACHE[key]


def clear_cache():
    _CACHE.clear()
