"""The module map file and the ratchet ceilings file (docs/module-map.md "The map file").

The map names the modules (folders), the messaging module or modules, the allowed edges with a
reason for each, and the ratchets: the couplings that exist today, each with a floor goal. It is
an owner path: `.kit.json` "modules".map names it, and `--check` refuses to run unless the owner
rules lock it. The ceilings file holds each ratchet's current ceiling; it is a plain file, so an
agent may lower a ceiling.
"""
import fnmatch
import posixpath
import re

from kitlib.config.types import (ConfigError, Enum, Field, Int, ListOf, MapOf, Obj, Str, is_note,
                                 parse_json)

ROOT = '(root)'              # a path no module holds: the repository's own files
NAME_RE = r'[A-Za-z0-9][A-Za-z0-9._-]*'
KINDS = ('build', 'source', 'data', 'tree')
EDGE_KINDS = ('build', 'data', 'tree')
DETECTORS = ('csproj', 'paths', 'gradle', 'python', 'java-imports')
JAVA_IMPORTS = 'java-imports'      # the detector a ratchet drives itself (its files and imports)
DEFAULT_TEST_SOURCES = ['*/src/test/*', '*Tests/*', '*.Tests/*', 'tests/*', '*/tests/*',
                        '*/test/*', 'test/*', '*/test_*.py', 'test_*.py', '*_test.py', '*Test.java',
                        '*Tests.java', '*Test.kt', '*Tests.cs', '*.test.js', '*.test.ts']

Name = Str(pattern=NAME_RE, what='a module name')
ModuleRef = Str(pattern=r'%s|\(root\)' % NAME_RE, what='a module name or (root)')

MODULE = Obj({
    'paths': Field(ListOf(Str(), nonempty=True), None,
                   'repo-relative folders (or files) the module holds; fnmatch globs allowed'),
    'why': Field(Str(), None, 'what the module is'),
}, required=('paths',), what='{paths, why}')

EDGE = Obj({
    'from': Field(ModuleRef, None, 'the module that uses'),
    'to': Field(ModuleRef, None, 'the module it uses'),
    'kind': Field(Enum(*EDGE_KINDS), None, 'build, data or tree'),
    'why': Field(Str(), None, 'the owner\'s reason'),
}, required=('from', 'to', 'kind', 'why'), what='{from, to, kind, why}')

RATCHET = Obj({
    'id': Field(Str(pattern=NAME_RE, what='a ratchet id'), None, 'unique id; its ceiling is keyed by it'),
    'from': Field(ModuleRef, None, 'the module that crosses'),
    'to': Field(ModuleRef, None, 'the module it crosses into'),
    'kinds': Field(ListOf(Enum(*KINDS), nonempty=True), None, 'crossing kinds it counts; unset: every kind'),
    'detector': Field(Enum(*DETECTORS), None, 'the detector whose crossings it counts; unset: every one'),
    'goal': Field(Int(0), None, 'the floor goal'),
    'goal_reason': Field(Str(), None, 'the owner\'s reason for a goal above 0'),
    'record': Field(Str(), None, 'the record that drives the count toward the goal'),
    'files': Field(ListOf(Str(), nonempty=True), None,
                   'java-imports only: fnmatch globs (* crosses /) naming the files it reads'),
    'imports': Field(ListOf(Str(), nonempty=True), None,
                     'java-imports only: the package prefixes an import line names'),
}, required=('id', 'from', 'to', 'goal'),
    what='{id, from, to, kinds, detector, goal, goal_reason, record, files, imports}')

RAISE = Obj({
    'ratchet': Field(Str(pattern=NAME_RE, what='a ratchet id'), None, 'the ratchet raised'),
    'ceiling': Field(Int(0), None, 'the highest ceiling the owner allows it'),
    'why': Field(Str(), None, "the owner's reason"),
    'approval': Field(Str(), None, 'the owner approval (A-NNN) that allowed it'),
}, required=('ratchet', 'ceiling', 'why'), what='{ratchet, ceiling, why, approval}')

MAP = Obj({
    'modules': Field(MapOf(MODULE, 'module'), None, 'module name -> its folders'),
    'messaging': Field(ListOf(Name), None, 'the messaging module(s): a crossing into one is always allowed'),
    'edges': Field(ListOf(EDGE), None, 'the allowed edges'),
    'ratchets': Field(ListOf(RATCHET), None, 'the counted couplings'),
    'raises': Field(ListOf(RAISE), None, "ceilings the owner allows above the base's"),
    'test_sources': Field(ListOf(Str(), nonempty=True), None, 'files the paths detector reads'),
    'root_markers': Field(ListOf(Str(), nonempty=True), None, 'names a root climb looks for, added to the defaults'),
}, required=('modules',), what='module map')

CEILINGS = MapOf(Int(0), 'ratchet id')


class MapError(Exception):
    """The map or the ceilings file cannot be used (the run answers nothing: exit 2)."""


def _norm(path):
    return path.replace('\\', '/').strip('/')


class ModuleMap(object):
    """A checked map. `module_of(path)` is `(name, folder)`: the module whose path entry matches
    the deepest of `path` and its parent folders, and that folder; `(ROOT, '')` when none does."""

    def __init__(self, data, where='module map'):
        errs = []
        MAP.check(data, where, errs)
        if errs:
            raise MapError('; '.join(errs))
        self.where = where
        raw = {n: list(m['paths']) for n, m in data['modules'].items() if not is_note(n)}
        self.modules = {n: [_norm(p) for p in ps] for n, ps in raw.items()}
        self.messaging = list(data.get('messaging') or [])
        self.edges = list(data.get('edges') or [])
        self.ratchets = list(data.get('ratchets') or [])
        self.raises = list(data.get('raises') or [])
        self.test_sources = list(data.get('test_sources') or DEFAULT_TEST_SOURCES)
        self.root_markers = list(data.get('root_markers') or [])
        self._cache = {}
        self._validate(raw, errs)
        if errs:
            raise MapError('%s: %s' % (where, '; '.join(errs)))

    def _validate(self, raw, errs):
        known = set(self.modules) | {ROOT}
        named = {}                  # a literal path, case-folded -> the module that names it
        for n in sorted(self.modules):
            if not re.fullmatch(NAME_RE, n):
                errs.append('module name %r must match %s' % (n, NAME_RE))
            for given, p in zip(raw[n], self.modules[n]):
                # The raw entry: _norm strips a leading `/`, which would turn /etc into etc.
                if (not p or given.startswith(('/', '\\')) or '..' in p.split('/')
                        or ':' in p):
                    errs.append('module %s: path %r is not repo-relative' % (n, given))
                    continue
                if re.search(r'[*?\[]', p):
                    continue            # a glob is checked lazily, when a scanned path meets it
                other = named.setdefault(p.lower(), n)
                if other != n:
                    errs.append('path %s is named by more than one module (%s, %s)'
                                % (p, other, n))
        for n in self.messaging:
            if n not in self.modules:
                errs.append('messaging names %s, which is not a module' % n)
        for i, e in enumerate(self.edges):
            for end in ('from', 'to'):
                if e[end] not in known:
                    errs.append('edges[%d].%s names %s, which is not a module' % (i, end, e[end]))
            if e['from'] == e['to']:
                errs.append('edges[%d] joins %s to itself' % (i, e['from']))
        seen = set()
        for r in self.ratchets:
            if r['id'] in seen:
                errs.append('ratchet id %s is written twice' % r['id'])
            seen.add(r['id'])
            for end in ('from', 'to'):
                if r[end] not in known:
                    errs.append('ratchet %s: %s names %s, which is not a module' % (r['id'], end, r[end]))
            if r['goal'] > 0 and not (r.get('goal_reason') or '').strip():
                errs.append('ratchet %s: a goal above 0 needs the owner\'s goal_reason' % r['id'])
            self._validate_detector(r, errs)
        raised = set()
        for x in self.raises:
            if x['ratchet'] not in seen:
                errs.append('raises names %s, which no ratchet has' % x['ratchet'])
            elif x['ratchet'] in raised:
                errs.append('ratchet %s is raised twice' % x['ratchet'])
            raised.add(x['ratchet'])

    @staticmethod
    def _validate_detector(r, errs):
        if r.get('detector') == JAVA_IMPORTS:
            for key in ('files', 'imports'):
                if not r.get(key):
                    errs.append('ratchet %s: a java-imports ratchet needs "%s"' % (r['id'], key))
            if r.get('kinds'):
                errs.append('ratchet %s: a java-imports ratchet counts build crossings: drop '
                            '"kinds"' % r['id'])
            return
        for key in ('files', 'imports'):
            if key in r:
                errs.append('ratchet %s: "%s" is for a java-imports ratchet only' % (r['id'], key))
        if r['from'] == r['to']:
            errs.append('ratchet %s joins %s to itself (only a java-imports ratchet may)'
                        % (r['id'], r['from']))

    def module_of(self, path):
        path = _norm(path)
        hit = self._cache.get(path)
        if hit is not None:
            return hit
        hit = (ROOT, '')
        cand = path
        while cand:
            low, depth = cand.lower(), cand.count('/')
            # An entry names folders at its own depth: fnmatch's * would also cross a `/`.
            names = [n for n, pats in self.modules.items()
                     if any(p.count('/') == depth and fnmatch.fnmatchcase(low, p.lower())
                            for p in pats)]
            if len(names) > 1:
                raise MapError('%s: %s is in more than one module (%s)'
                               % (self.where, cand, ', '.join(sorted(names))))
            if names:
                hit = (names[0], cand)
                break
            cand = posixpath.dirname(cand)
        self._cache[path] = hit
        return hit

    def is_test_source(self, path):
        low = _norm(path).lower()
        return any(fnmatch.fnmatchcase(low, g.lower()) for g in self.test_sources)

    def edge_for(self, src, dst, kind):
        """The edge allowing a `kind` crossing from `src` to `dst`, or None."""
        return next((e for e in self.edges
                     if e['from'] == src and e['to'] == dst and e['kind'] == kind), None)

    def raise_for(self, ratchet_id):
        """The owner's raise entry for `ratchet_id`, or None."""
        return next((x for x in self.raises if x['ratchet'] == ratchet_id), None)

    def ratchet_for(self, crossing):
        """The ratchet that counts `crossing`, or None: the one it names (java-imports), else the
        first whose ends and filters match."""
        if getattr(crossing, 'ratchet', None):
            return next((r for r in self.ratchets if r['id'] == crossing.ratchet), None)
        for r in self.ratchets:
            if r['from'] != crossing.src or r['to'] != crossing.dst:
                continue
            if r.get('kinds') and crossing.kind not in r['kinds']:
                continue
            if r.get('detector') and crossing.detector != r['detector']:
                continue
            return r
        return None


def _read(path, what):
    try:
        with open(path, encoding='utf-8-sig') as fh:
            text = fh.read()
    except FileNotFoundError:
        raise MapError('the %s %s does not exist' % (what, path))
    except OSError as e:
        raise MapError('cannot read the %s %s: %s' % (what, path, e))
    try:
        return parse_json(text, path)
    except ConfigError as e:
        raise MapError(str(e))


def load_map(path):
    """The ModuleMap the file at `path` holds. MapError when it is missing or unusable."""
    return ModuleMap(_read(path, 'module map'), where=path)


def load_ceilings(path):
    """`{ratchet id: ceiling}` from the file at `path`; {} when `path` is None."""
    if path is None:
        return {}
    data = _read(path, 'ceilings file')
    return ceilings_of(data, path)


def ceilings_of(data, where):
    """`{ratchet id: ceiling}` from parsed ceilings `data`, note keys dropped. MapError when the
    shape is wrong."""
    errs = []
    CEILINGS.check(data, where, errs)
    if errs:
        raise MapError('; '.join(errs))
    return {k: v for k, v in data.items() if not is_note(k)}
