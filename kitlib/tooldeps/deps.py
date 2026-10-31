"""Which tools test modules a change can break. tools/tool_deps.py is the entry point, docs/test-tools.md the doc. Every
function takes the checkout root (or a checked configuration) explicitly: there is no module ROOT.

The map is DERIVED, not hand-written: `scan` reads every top-level `*.py` of the repository's
tools directory (`.kit.json` "repo".tools_dir) with `ast` and records an edge X -> Y for each way
a module reaches another:
  - `import Y` / `from Y import ...`, at any depth (a function-level import counts);
  - an `importlib.import_module('Y')` / `__import__('Y')` with a literal name;
  - `import Y` / `from Y import` inside the code string of a `python -c` child (the element after
    a `'-c'` element of an argv list or call, or the string a Name there was assigned): a
    docstring or an assertion message saying 'import Y' is prose and credits nothing;
  - a file name (`mentions` below): a str constant that is exactly `Y.py` or ends `/Y.py`.
Only the import kinds count as a test TESTING a tool (`Graph.tested`): a test that merely spells
'tools/X.py' is selected by X but is not X's test. A test module is a `test*.py` defining a class
whose base is `TestCase`. A tool never depends on a test module (a tool naming one is data).

Selection (`select`, pure) for each changed repo-relative path:
  - a path matching `ignore` selects nothing;
  - a `selector_files` file (tools-relative) selects ALL: it changes what is selected or how it runs;
  - a tools `X.py` selects every test module that reaches X (and X itself if it is one);
  - a tools file in `extra` selects what reaches the modules that read it;
  - any other tools path (deleted, in a subfolder, a new non-.py file): ALL in "path" mode, its
    name-mentions in "tokens" mode;
  - any path: the modules `pinned` names for it and those of every `patterns` regex it matches.
A changed tool that no test reaches selects the `guard_tests` (`unmapped_tool: "guard"`, whose
audit then fails an untested tool) or ALL (`"all"`; also when no guard test exists). Every
non-empty selection, and every one a tools path made, gets the existing `guard_tests`; then
`always_run` is appended (module or module.Class ids, in config order). With `merged` (a landing's
merged-tree gate, over base..merge) the selection is the same, and when a tools path made it the
`whole_tree` classes of existing, unselected modules are appended too; the daily full run and
--full run every module.

The two `mentions` modes (the repositories' two selectors):
  "path"   (default) file-name edges are str constants ending in `Y.py`, followed transitively
           with the imports; an unknown tools path selects ALL.
  "tokens" a `[\\w.-]+` token in a non-docstring str literal of a .py, or in the non-comment text
           of a script, names a file by its base name or by a tools subfolder holding it. Imports
           are followed transitively, a mention through a non-test source ONE hop (runner scripts
           name each other in chains that would otherwise reach every test). `sources` (globs) are
           the non-top-level files that can name others (.ps1, .cmd, hooks, a root test.bat).

`.kit.json` section "tool_tests" (shared with kitlib.pytests; every key optional; the kit schema,
kitlib/config/schema.py, checks keys and types at load, `config` the deeper shapes):
  mentions        "path" | "tokens"            (default "path")
  selector_files  ["tool_deps.py", ...]        tools-relative; their change selects ALL
                                               (default ["tool_deps.py", "pytests.py"])
  extra           {"x.json": ["reader", ...]}  non-.py tools files -> the modules reading them
  pinned          [[WHERE, ["test_x", ...]], ...]  files OUTSIDE tools a tools test reads; WHERE
                  is a repo-relative path (a prefix when it ends in `/`), {"piece": id, "path":
                  rel} resolved through the pieces table, or {"solution": true} (its .NET solution).
                  The table is the caller's `pieces` argument (an object with .path(id, rel) and
                  .solution()); with none, a piece entry is a ConfigError when it is read.
  patterns        [{"regex": "...", "modules": [...]}, ...]  a changed path matching selects them
  whole_tree      {"test_x.Class": "why"}      classes that read the whole real tree: out of the
                                               read audit (pytests --audit), each with a reason;
                                               a merged selection a tools path made adds them
  untested        ["tool", ...]                tools no test imports, on purpose
  dynamic_imports {"module": [["reached", ...], "why"]}  computed import_module calls, declared
  guard_tests     ["test_tool_deps"]           (default) see above; only those that exist
  unmapped_tool   "guard" | "all"              (default "guard")
  always_run      ["test_x", "test_y.Class"]   appended to every selection that is not ALL
  sources         ["tools/**/*.ps1", "test.bat"]  tokens mode only: more files that name others
  skip_dirs       ["__pycache__"]              (default) folders `sources` never walks
  ignore          ["tools/**/*.md"]            changed paths that select nothing
kitlib.pytests reads the rest of the section (its own keys, listed in PYTESTS_KEYS).

--check (`audit`) is the honesty check a repository runs on its real tools: every test module
selects itself; every tool is imported by a test or listed `untested` (and no stale entry);
every table path exists and names real modules; every `whole_tree` class exists; every static
`os.path.join(<tool>.ROOT, ...)` read in a test is covered by a table; every computed import is
declared in `dynamic_imports` (and no stale entry).
"""
import ast
import functools
import os
import re

from kitlib.config import ConfigError, load as load_config
from kitlib.config import schema
from kitlib.config.types import check as check_type

ALL = 'ALL'
SECTION = 'tool_tests'
MODES = ('path', 'tokens')
# The section's keys pytests.py owns; DEFAULTS are the schema's defaults for the rest.
PYTESTS_KEYS = ('shards', 'serial', 'split_classes', 'jobs_max', 'jobs_divisor', 'jobs_env',
                'unit_timeout')
DEFAULTS = {k: v for k, v in schema.defaults(SECTION).items() if k not in PYTESTS_KEYS}
_DYNAMIC = ('import_module', '__import__')
_IMPORT_IN_TEXT = re.compile(r'\b(?:import|from)\s+([A-Za-z_]\w*)')
TOKEN = re.compile(r'[\w.-]+')
SCRIPT_COMMENT = re.compile(r'<#.*?#>|^[ \t]*(?:#|rem\b|::)[^\n]*', re.S | re.I | re.M)


# ---------------------------------------------------------------- config

def _str_list(where, v):
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError('%s must be a list of non-empty strings' % where)
    return list(v)


def config(root=None, data=None, tools_rel='tools'):
    """The "tool_tests" section of the checkout at `root` (kitlib.config.load), or `data` (a raw
    section, checked against the kit schema here), over DEFAULTS and with the deeper shapes
    checked. `out['tools_rel']` is the tools folder, repo-relative with `/`: the checkout's
    "repo".tools_dir, or `tools_rel` with `data`. Raises ConfigError on an unknown key or a value of
    the wrong shape."""
    w = '.kit.json "%s"' % SECTION
    if data is None:
        if root is None:
            raise ValueError('config needs the checkout root or the section data')
        kit = load_config(root)
        data = kit.section(SECTION)
        tools_rel = kit.tools_dir
    else:
        if not isinstance(data, dict):
            raise ConfigError('%s must be an object' % w)
        data = {k: v for k, v in data.items() if v is not None}
        check_type(schema.section_type(SECTION), data, w)
    out = dict(DEFAULTS)
    out.update({k: v for k, v in data.items() if k in DEFAULTS})
    for k in ('mentions', 'unmapped_tool'):
        allowed = MODES if k == 'mentions' else ('guard', 'all')
        if out[k] not in allowed:
            raise ConfigError('%s.%s must be one of %s' % (w, k, ', '.join(allowed)))
    for k in ('selector_files', 'untested', 'guard_tests', 'always_run', 'sources', 'skip_dirs', 'ignore'):
        out[k] = _str_list('%s.%s' % (w, k), out[k])
    if out['sources'] and out['mentions'] != 'tokens':
        raise ConfigError('%s.sources needs "mentions": "tokens" (path mode never reads them)' % w)
    for k in ('extra', 'whole_tree', 'dynamic_imports'):
        if not isinstance(out[k], dict):
            raise ConfigError('%s.%s must be an object' % (w, k))
    out['extra'] = {f: tuple(_str_list('%s.extra["%s"]' % (w, f), m)) for f, m in out['extra'].items()}
    for c, why in out['whole_tree'].items():
        if c.count('.') != 1 or not isinstance(why, str) or not why.strip():
            raise ConfigError('%s.whole_tree: "%s" must be module.Class with a reason' % (w, c))
    dyn = {}
    for m, v in out['dynamic_imports'].items():
        if not (isinstance(v, list) and len(v) == 2 and isinstance(v[1], str)):
            raise ConfigError('%s.dynamic_imports["%s"] must be [[module, ...], "why"]' % (w, m))
        dyn[m] = (tuple(_str_list('%s.dynamic_imports["%s"][0]' % (w, m), v[0])), v[1])
    out['dynamic_imports'] = dyn
    pins = []
    for i, e in enumerate(out['pinned']):
        where = '%s.pinned[%d]' % (w, i)
        if not (isinstance(e, list) and len(e) == 2):
            raise ConfigError('%s must be [WHERE, [module, ...]]' % where)
        loc, mods = e
        ok = (isinstance(loc, str) and loc) or (isinstance(loc, dict) and (
            set(loc) == {'piece', 'path'} and isinstance(loc['piece'], str) and isinstance(loc['path'], str)
            or loc == {'solution': True}))
        if not ok:
            raise ConfigError('%s: WHERE must be a path, {"piece": id, "path": rel} or '
                                   '{"solution": true}' % where)
        pins.append((loc, tuple(_str_list(where + '[1]', mods))))
    out['pinned'] = pins
    pats = []
    for i, p in enumerate(out['patterns']):
        where = '%s.patterns[%d]' % (w, i)
        if not (isinstance(p, dict) and set(p) - {'comment'} == {'regex', 'modules'}):
            raise ConfigError('%s must be {"regex": ..., "modules": [...]}' % where)
        try:
            rx = re.compile(p['regex'])
        except (re.error, TypeError) as e:
            raise ConfigError('%s.regex does not compile: %s' % (where, e))
        pats.append((rx, tuple(_str_list(where + '.modules', p['modules']))))
    out['patterns'] = pats
    out['tools_rel'] = tools_rel.replace('\\', '/').strip('/')
    return out


# ---------------------------------------------------------------- the scan

def _dynamic_call(node):
    """The literal module name of an import_module/__import__ call, '' for a computed one, or
    None when `node` is not such a call."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
    if name not in _DYNAMIC:
        return None
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return ''


def dynamic_imports(tree):
    """The line numbers of every import_module/__import__ call whose module name is computed."""
    return sorted(n.lineno for n in ast.walk(tree) if _dynamic_call(n) == '')


class Graph(object):
    """`deps`: module -> set of modules it references directly (every rule; what "path" mode
    walks). `tests`: the test modules. `imports`: module -> the subset of `deps` reached by an
    import, a literal import_module/__import__, a `python -c` import or a `dynamic_imports` entry -
    not by a file name. Only these count as a test testing a tool (`tested`). In "tokens" mode
    `sources` holds, per repo-relative source path, (tokens it names, top-level names it imports)."""

    def __init__(self, deps, tests, imports=None, cfg=None, tools_rel='tools', sources=None):
        self.deps = deps
        self.tests = frozenset(tests)
        self.imports = deps if imports is None else imports
        self.cfg = cfg
        self.tools_rel = tools_rel
        self.sources = sources or {}
        self.test_paths = frozenset(self.path(t) for t in self.tests)

    def tested(self):
        """The modules some test module reaches by a direct import edge (`imports`)."""
        out = set()
        for t in self.tests:
            out |= self.imports.get(t, set())
        return out

    def __contains__(self, name):
        return name in self.deps

    def path(self, module):
        return '%s/%s.py' % (self.tools_rel, module)

    def stem(self, path):
        """The module name `import` uses for a top-level tools .py path, else None."""
        prefix = self.tools_rel + '/'
        if path.startswith(prefix) and path.endswith('.py') and '/' not in path[len(prefix):]:
            return path[len(prefix):-3]
        return None

    def closure(self, name):
        """Every module `name` reaches, transitively, itself excluded."""
        seen, todo = set(), list(self.deps.get(name, ()))
        while todo:
            m = todo.pop()
            if m not in seen and m != name:
                seen.add(m)
                todo.extend(self.deps.get(m, ()))
        return seen

    def dependents(self, name):
        """The test modules a change to module `name` can break ("path" mode): `name` itself if
        it is one, and every test module whose closure holds it."""
        return {t for t in self.tests if t == name or name in self.closure(t)}

    # ---- "tokens" mode
    def names_of(self, path):
        """The tokens a source can name `path` by: its base name and each tools subfolder holding it."""
        parts = path.split('/')
        prefix = self.tools_rel + '/'
        folders = path[len(prefix):].split('/')[:-1] if path.startswith(prefix) else []
        return {parts[-1]} | set(folders)

    def importers(self, paths):
        """`paths` plus every source that imports one of them, transitively."""
        out, todo = set(paths), list(paths)
        while todo:
            stem = self.stem(todo.pop())
            if stem is None:
                continue
            for src, (_toks, imps) in self.sources.items():
                if stem in imps and src not in out:
                    out.add(src)
                    todo.append(src)
        return out

    def mentioners(self, nodes, tests):
        """Sources naming one of `nodes`: test modules when `tests`, else non-test sources (which
        never count a mention of a test module: a tool naming one is data)."""
        names = set()
        for n in nodes:
            if tests or n not in self.test_paths:
                names |= self.names_of(n)
        return {src for src, (toks, _i) in self.sources.items()
                if (src in self.test_paths) == tests and src not in nodes and toks & names}

    def token_dependents(self, path):
        """The test modules a change to repo-relative `path` reaches in "tokens" mode."""
        reach = self.importers({path})
        reach |= self.importers(self.mentioners(reach, False))
        seen = reach | self.mentioners(reach, True)
        return {self.stem(p) for p in seen if p in self.test_paths}

    def reaches(self, path):
        """The test modules a change to an existing tools module path reaches, in the graph's mode."""
        if self.sources:
            return self.token_dependents(path)
        name = self.stem(path)
        return self.dependents(name) if name in self.deps else set()


def _is_test_module(name, tree):
    if not name.startswith('test'):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for b in node.bases:
                base = b.attr if isinstance(b, ast.Attribute) else getattr(b, 'id', '')
                if base.endswith('TestCase'):
                    return True
    return False


def _str_constants(tree):
    return [c.value for c in ast.walk(tree) if isinstance(c, ast.Constant) and isinstance(c.value, str)]


def python_c_code(tree):
    """The str constants that are a `python -c` argument: the element after a '-c' constant in a
    list, tuple or call, read through a Name to what it was assigned in this module (module-wide,
    scope ignored, plain `name = ...` assignments only). None when some '-c' argument cannot be
    resolved to str constants (a parameter, `+=`, an annotated or tuple/for/with target, an
    attribute, a Name assigned from a Name, `[py, '-c'] + [code]`): the caller then falls back to
    every str constant of the module, which only ever selects more tests."""
    assigned, plain, stores = {}, {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.setdefault(t.id, []).append(node.value)
                    plain[t.id] = plain.get(t.id, 0) + 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores[node.id] = stores.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):
            stores[node.arg] = stores.get(node.arg, 0) + 1
    out = []
    for node in ast.walk(tree):
        seq = (node.elts if isinstance(node, (ast.List, ast.Tuple)) else
               node.args if isinstance(node, ast.Call) else ())
        for i, e in enumerate(seq):
            if isinstance(e, ast.Constant) and e.value == '-c':
                if i + 1 == len(seq):
                    return None
                nxt = seq[i + 1]
                if isinstance(nxt, ast.Name):
                    if stores.get(nxt.id, 0) != plain.get(nxt.id, 0):
                        return None
                    roots = assigned.get(nxt.id, [])
                else:
                    roots = [nxt]
                found = [c for r in roots for c in _str_constants(r)]
                if not found:
                    return None
                out += found
    return out


def module_edges(tree, names, imports_only=False):
    """The modules in `names` (every name when None) that an ast references, by the rules in the
    docstring. With `imports_only`, the file-name rule is left out: what is left are the edges
    that load the module's code into the referencing process or its `python -c` child."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split('.')[0])
        elif _dynamic_call(node):
            out.add(_dynamic_call(node).split('.')[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            base = re.split(r'[\\/]', s)[-1]          # the whole string, or what follows its last /
            if base.endswith('.py') and not imports_only:
                out.add(base[:-3])                    # kept only if it names a module, below
    codes = python_c_code(tree)
    for code in (_str_constants(tree) if codes is None else codes):
        out.update(_IMPORT_IN_TEXT.findall(code))
    return out if names is None else {m for m in out if m in names}


def py_literal_tokens(tree):
    """Tokens inside the string literals a .py file's code uses - not docstrings, which name
    files for the reader, not for the run."""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    toks = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            toks.update(TOKEN.findall(node.value))
    return toks


def source_tokens(path, text, tree=None):
    """The file names a source can act on: string literals for a .py that parses (`tree`), all
    tokens of one that does not, non-comment text for a script."""
    if path.endswith('.py') and tree is not None:
        toks = py_literal_tokens(tree)
    else:
        toks = TOKEN.findall(text if path.endswith('.py') else SCRIPT_COMMENT.sub('', text))
    return {t.rstrip('.') for t in toks}


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return None


def _parse(text):
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


@functools.lru_cache(maxsize=None)
def glob_regex(pattern):
    """A compiled regex for a repo-relative glob: `**/` any folders (or none), `*` within one
    path segment, `?` one character."""
    out, i = '', 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out, i = out + '(?:.*/)?', i + 3
        elif pattern.startswith('**', i):
            out, i = out + '.*', i + 2
        elif pattern[i] == '*':
            out, i = out + '[^/]*', i + 1
        elif pattern[i] == '?':
            out, i = out + '[^/]', i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r'\Z')


def matches_any(path, globs):
    return any(glob_regex(g).match(path) for g in globs)


def _source_files(root, globs, skip_dirs):
    """Repo-relative paths of the files the `sources` globs match, walking only each glob's
    literal leading folders (never the whole repository)."""
    out = set()
    for g in globs:
        lead = []
        for part in g.split('/')[:-1]:
            if any(c in part for c in '*?['):
                break
            lead.append(part)
        if not any(c in g for c in '*?['):
            if os.path.isfile(os.path.join(root, g)):
                out.add(g)
            continue
        rx = glob_regex(g)
        top = os.path.join(root, *lead) if lead else root
        for d, dirs, files in os.walk(top):
            dirs[:] = [x for x in dirs if x not in skip_dirs and x != '.git']
            for n in files:
                rel = os.path.relpath(os.path.join(d, n), root).replace(os.sep, '/')
                if rx.match(rel):
                    out.add(rel)
    return out


def scan(tools_dir, cfg, root):
    """The Graph of every top-level `*.py` in `tools_dir` (None: the checkout's tools folder,
    `cfg['tools_rel']` under `root`). `cfg` is a `config` result (None: read from `root`). A file
    that does not parse is kept as a module with no edges (its own test fails it); a `test*` one
    counts as a test module. In "tokens" mode `sources` is filled too."""
    cfg = config(root) if cfg is None else cfg
    tools_rel = cfg['tools_rel']
    tools_dir = tools_dir or os.path.join(root, *tools_rel.split('/'))
    names = {f[:-3] for f in os.listdir(tools_dir)
             if f.endswith('.py') and os.path.isfile(os.path.join(tools_dir, f))}
    declared_all = cfg['dynamic_imports']
    deps, imports, tests, trees, texts = {}, {}, set(), {}, {}
    for name in names:
        text = _read(os.path.join(tools_dir, name + '.py')) or ''
        tree = _parse(text)
        texts[name], trees[name] = text, tree
        if tree is None:
            deps[name], imports[name] = set(), set()
            if name.startswith('test'):
                tests.add(name)
            continue
        declared = set(declared_all.get(name, ((), ''))[0]) & names
        deps[name] = (module_edges(tree, names) | declared) - {name}
        imports[name] = (module_edges(tree, names, imports_only=True) | declared) - {name}
        if _is_test_module(name, tree):
            tests.add(name)
    for name in deps:
        if name not in tests:
            deps[name] -= tests
            imports[name] -= tests
    sources = None
    if cfg['mentions'] == 'tokens':
        sources = {}
        for name in names:
            tree = trees[name]
            raw = set() if tree is None else module_edges(tree, None, imports_only=True)
            raw |= set(declared_all.get(name, ((), ''))[0])
            sources['%s/%s.py' % (tools_rel, name)] = (source_tokens('x.py', texts[name], tree), raw)
        for rel in _source_files(root, cfg['sources'], cfg['skip_dirs']):
            if rel in sources:
                continue
            text = _read(os.path.join(root, rel))
            if text is None:
                continue
            tree = _parse(text) if rel.endswith('.py') else None
            raw = module_edges(tree, None, imports_only=True) if tree is not None else set()
            sources[rel] = (source_tokens(rel, text, tree), raw)
    return Graph(deps, tests, imports, cfg=cfg, tools_rel=tools_rel, sources=sources)


# ---------------------------------------------------------------- selection

def pinned(cfg, pieces=None):
    """[(repo-relative path or prefix, modules)]: `cfg['pinned']` with each piece entry resolved
    through `pieces` (an object with .path(id, rel) and .solution(); ConfigError when a piece entry
    is read and none was given)."""
    out, table = [], pieces
    for loc, mods in cfg['pinned']:
        if isinstance(loc, str):
            out.append((loc, mods))
            continue
        if table is None:
            raise ConfigError('.kit.json "%s".pinned names %s, but no pieces table was given '
                              '(kitlib.tooldeps reads it from its caller)' % (SECTION, loc))
        out.append((table.solution() if 'solution' in loc else table.path(loc['piece'], loc['path']), mods))
    return out


def pinned_modules(path, pieces=None, cfg=None, pins=None):
    """The test modules `pinned` and `patterns` name for a repo-relative `path` (a pinned
    directory matches its prefix, and the directory itself without its trailing `/`)."""
    if cfg is None:
        raise ValueError('pinned_modules needs the checked configuration')
    out = set()
    for rx, mods in cfg['patterns']:
        if rx.search(path):
            out.update(mods)
    for p, mods in (pinned(cfg, pieces) if pins is None else pins):
        if path == p or (p.endswith('/') and (path.startswith(p) or path + '/' == p)):
            out.update(mods)
    return out


def select(changed, graph, merged=False, pieces=None, cfg=None):
    """The test modules (sorted, then `always_run`) the changed paths select, [] for none, or ALL.
    Pure but for the pieces table a {"piece"} pin reads."""
    return selection(changed, graph, merged, pieces, cfg)[0]


def selection(changed, graph, merged=False, pieces=None, cfg=None):
    """(select's result, one line saying why) - the line a runner prints above its run."""
    cfg = cfg or graph.cfg
    if cfg is None:
        raise ValueError('selection needs the checked configuration (cfg, or the graph\'s)')
    prefix = graph.tools_rel + '/'
    tokens = cfg['mentions'] == 'tokens'
    mods, touched, pins, counted = set(), False, None, 0
    for f in sorted(changed):
        f = f.replace('\\', '/')
        if matches_any(f, cfg['ignore']):
            continue
        counted += 1
        in_tools = f.startswith(prefix)
        is_source = tokens and (f in graph.sources or matches_any(f, cfg['sources']))
        if in_tools or is_source:
            touched = True
            rest = f[len(prefix):] if in_tools else None
            if rest in cfg['selector_files']:
                return ALL, 'full suite: %s changed (it decides the selection)' % f
            name = graph.stem(f)
            if name in graph:
                reached = graph.reaches(f)
                if name not in graph.tests and not reached:
                    if cfg['unmapped_tool'] == 'all' or not _guards(cfg, graph):
                        return ALL, 'full suite: nothing maps %s to a test' % f
                mods |= reached
            elif rest in cfg['extra']:
                for reader in cfg['extra'][rest]:
                    mods |= graph.reaches(graph.path(reader)) if reader in graph else set()
            elif tokens:
                mods |= graph.token_dependents(f)
            else:
                return ALL, 'full suite: %s is not a top-level tools module the scan maps' % f
        if pins is None and (cfg['pinned'] and not in_tools):
            pins = pinned(cfg, pieces)
        mods |= pinned_modules(f, cfg=cfg, pins=pins or [])
    if mods or touched:
        mods |= set(_guards(cfg, graph))
    out = sorted(mods)
    always = [t for t in cfg['always_run'] if t.split('.')[0] not in mods]
    # A landing's merged gate: the whole_tree classes read every file, so no edge says which change
    # reaches them; a tools change runs them all (the daily full run runs every module).
    whole = [c for c in cfg['whole_tree'] if merged and touched and c not in always
             and c.split('.')[0] in graph.tests and not {c.split('.')[0]} & (mods | set(always))]
    return out + always + whole, '%d changed path(s) -> %d test module(s)%s%s' % (
        counted, len(out), ' + %d always-run' % len(always) if always else '',
        ' + %d whole-tree class(es) (merged)' % len(whole) if whole else '')


def _guards(cfg, graph):
    return [t for t in cfg['guard_tests'] if t in graph.tests]


# ---------------------------------------------------------------- the audit

def _join_paths(func, names, bound, loops, constants):
    """The literal repo-relative path an `os.path.join(<tool>.ROOT, ...)` call names, or None."""
    args = func.args
    if not args:
        return None
    first = args[0]
    real = (isinstance(first, ast.Attribute) and first.attr == 'ROOT'
            and isinstance(first.value, ast.Name) and first.value.id in names) or \
           (isinstance(first, ast.Name) and first.id in bound)
    if not real:
        return None
    paths = ['']
    for a in args[1:]:
        if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == 'replace':
            a = a.func.value                     # rel.replace('/', os.sep)
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            parts = [a.value]
        elif isinstance(a, ast.Name) and a.id in loops:
            parts = loops[a.id]
        elif isinstance(a, ast.Attribute) and (a.value.id if isinstance(a.value, ast.Name) else '',
                                               a.attr) in constants:
            parts = [constants[(a.value.id, a.attr)]]
        else:
            return ['?']                         # not statically known: the audit covers it
        paths = [(p + '/' + q).strip('/') for p in paths for q in parts]
    return [p.replace('\\', '/') for p in paths]


def real_tree_joins(test_path, graph_names, tools_dir):
    """[(line, repo-relative path)] for every `os.path.join(<tool module>.ROOT, <literals>)` in a
    test file - also through one local `name = <tool>.ROOT` and a literal tuple iterated into the
    join, and a `<tool>.<CONST>` str constant defined at the tool's top level. '?' marks a join
    whose parts are not all literal. The static half of the pinned-input honesty check."""
    with open(test_path, encoding='utf-8', errors='replace') as fh:
        tree = ast.parse(fh.read())
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in graph_names:
                    aliases[a.asname or a.name] = a.name
    constants = {}
    for alias, mod in aliases.items():
        try:
            with open(os.path.join(tools_dir, mod + '.py'), encoding='utf-8', errors='replace') as fh:
                mtree = ast.parse(fh.read())
        except (OSError, SyntaxError):
            continue
        for node in mtree.body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        constants[(alias, t.id)] = node.value.value
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        bound, loops = set(), {}
        # Local names only inside a function: at module level every function's locals would be
        # pooled, and one test's `root = backlog.ROOT` would mark another's temp `root` real.
        for node in (ast.walk(fn) if not isinstance(fn, ast.Module) else ()):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == 'ROOT' and isinstance(node.value.value, ast.Name)
                    and node.value.value.id in aliases):
                bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
            if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                    and isinstance(node.iter, (ast.Tuple, ast.List))
                    and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                            for e in node.iter.elts)):
                loops[node.target.id] = [e.value for e in node.iter.elts]
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'join' and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == 'path'):
                paths = _join_paths(node, aliases, bound, loops, constants)
                for p in paths or ():
                    out.append((node.lineno, p))
    return sorted(set(out))


def audit(root, graph=None, cfg=None, pieces=None, tools_dir=None):
    """The problems (sorted strings, [] when honest) of the checkout at `root`'s tools folder
    against its tables; the checks are listed in the module docstring."""
    cfg = config(root) if cfg is None else cfg
    tools_dir = tools_dir or os.path.join(root, *cfg['tools_rel'].split('/'))
    graph = graph or scan(tools_dir, cfg, root)
    out = []
    for t in sorted(graph.tests):
        sel = select({graph.path(t)}, graph, pieces=pieces, cfg=cfg)
        if sel != ALL and t not in sel:
            out.append('test module %s is not selected by a change to itself' % t)
    tools = set(graph.deps) - set(graph.tests)
    tested = graph.tested()
    for m in sorted(tools - tested - set(cfg['untested'])):
        out.append('tool %s: no test imports it - give it a test, or list it in "untested"' % m)
    for m in sorted(cfg['untested']):
        if m not in graph.deps or m in tested:
            out.append('"untested" entry %s is stale (%s)' % (m, 'no such tool' if m not in graph.deps else 'it is tested'))
    for name, readers in sorted(cfg['extra'].items()):
        if not os.path.isfile(os.path.join(tools_dir, name)):
            out.append('"extra" file %s does not exist' % name)
        out += ['"extra" %s: reader %s is not a tools module' % (name, r) for r in readers if r not in graph.deps]
    pins = pinned(cfg, pieces)
    for path, mods in pins:
        if not os.path.exists(os.path.join(root, path.rstrip('/'))):
            out.append('"pinned" path %s does not exist' % path)
        out += ['"pinned" %s: %s is not a test module' % (path, m) for m in mods if m not in graph.tests]
    for _rx, mods in cfg['patterns']:
        out += ['"patterns": %s is not a test module' % m for m in mods if m not in graph.tests]
    # guard_tests are not checked: select adds only those that exist (a stub-mode repository has
    # no copy of the kit's test_tool_deps), and an absent one makes an unmapped tool select ALL.
    out += ['"always_run": %s is not a test module' % t for t in cfg['always_run']
            if t.split('.')[0] not in graph.tests]
    for cls in sorted(cfg['whole_tree']):
        module, name = cls.split('.')
        text = _read(os.path.join(tools_dir, module + '.py')) or ''
        if '\nclass %s(' % name not in text:
            out.append('"whole_tree" class %s does not exist' % cls)
    names = set(graph.deps)
    prefix = graph.tools_rel + '/'
    for t in sorted(graph.tests):
        try:
            joins = real_tree_joins(os.path.join(tools_dir, t + '.py'), names, tools_dir)
        except (OSError, SyntaxError):
            continue
        for line, rel in joins:
            if rel in ('?', graph.tools_rel):
                continue
            if rel.startswith(prefix):
                name = rel[len(prefix):]
                ok = (name.endswith('.py') and name[:-3] in names) or t in cfg['extra'].get(name, ())
            else:
                ok = t in pinned_modules(rel, cfg=cfg, pins=pins)
            if not ok:
                out.append('%s.py:%d reads %s, which no table names' % (t, line, rel))
    found = {}
    for name in sorted(graph.deps):
        tree = _parse(_read(os.path.join(tools_dir, name + '.py')) or '')
        lines = dynamic_imports(tree) if tree is not None else []
        if lines:
            found[name] = lines
    for m, lines in sorted(found.items()):
        if m not in cfg['dynamic_imports']:
            out.append('%s: a computed import (line %s) the scan cannot follow - declare it in '
                       '"dynamic_imports"' % (m, ', '.join(map(str, lines))))
    out += ['"dynamic_imports" entry %s: no computed import there' % m
            for m in sorted(cfg['dynamic_imports']) if m not in found]
    return out


def graph_lines(graph):
    """`--graph`: one line per module, `name [test] -> its direct edges` (`-` for none)."""
    return ['%s%s -> %s' % (name, ' [test]' if name in graph.tests else '',
                            ', '.join(sorted(graph.deps[name])) or '-')
            for name in sorted(graph.deps)]
