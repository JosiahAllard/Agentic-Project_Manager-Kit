"""Which of the kit's own test packages a branch's change can break: the kit's tree laid out as
`kitlib/<pkg>/` (logic), `tools/<x>.py` (entry points) and `tests/<pkg>/` (unittest packages).
`deps` maps a flat tools folder; this maps those packages. tools/kit_tests.py is the entry point,
docs/test-tools.md ("kit_tests") the doc.

The graph is derived (`scan`), never hand-written:
  - a kitlib package reaches kitlib.X by `import kitlib.X`, `from kitlib.X import`, `from kitlib
    import X`, a relative import that climbs out of its package, or a non-docstring string that
    imports it in a child (`-c "from kitlib.X import ..."`, `-m kitlib.X`); a string that only
    names a package (a label, a message) is not a reference;
  - a kitlib package runs a tool by a non-docstring string holding `<tool>.py`;
  - a tool reaches the kitlib packages the same way, and another tool by `import <tool>`;
  - a test package reaches every kitlib package it imports and every `kitlib.X` / `kitlib/X` its
    files' text names (comments too: a test only ever selects more), every `tests.<pkg>` it
    imports, and every tool it imports or names as `<tool>.py`.

What a change reaches (`Tree.reach`) is closed transitively over: packages importing a reached
package, tools importing a reached package or tool, and packages running a reached tool.

`selection(changed, tree, cfg)` for each changed repo-relative path:
  - `full` (globs: the test helpers, the selector and its table) -> ALL;
  - kitlib/<m>/...: ALL when m is in `shared_core` or is not a package of the tree; else what m
    reaches: the reached packages' own tests/<p>, every test package that uses one of them, and
    every test package that names a reached tool;
  - tools/<x>.py: tests/<p> of each kitlib package x imports (the shared core left out), and
    what x reaches, as above (a deleted x too: its importers and runners still name it);
  - tests/<t>/...: t and every test package importing tests.<t>;
  - `paths` [glob, [packages]]: those packages, on top of any rule above (a test package that
    reads another folder, such as a scrub test); with no rule above, plus the test packages
    naming the file;
  - `by_name` globs: the test packages naming the file (its base name);
  - `prose` globs: the same, but a file no test names selects nothing (it is read only by name);
  - anything else -> ALL.
After the paths, the `always` packages (test packages that read every tracked file, such as
tests/scrub) join any non-empty change that is not ALL, whatever the paths are.
A path that maps to no test package (but a `prose` one) is unmapped and selects ALL: unmapped
never means "skip". Each selected package carries the reasons it was picked.
The table's `serial` list, [module, why] pairs, names the test modules that cannot run beside
others; kitlib.tooldeps.selfrun runs them alone after its parallel phase. Its `long` list names
the modules selfrun starts first and splits by class while it has no recorded time for them.
"""
import ast
import json
import os
import re

from kitlib.git import cmd as gitcmd
from kitlib.tooldeps.deps import ALL, glob_regex

CONFIG_REL = 'tests/selection.json'
DEFAULTS = {
    'shared_core': ['git', 'config', 'proc', 'lock'],
    'full': [],
    'paths': [],
    'by_name': [],
    'prose': [],
    'always': [],
    'serial': [],
    'long': [],
}
_KITLIB_REF = re.compile(r'\bkitlib[./\\]+([A-Za-z_]\w*)')
_TESTS_REF = re.compile(r'\btests\.([A-Za-z_]\w*)')
# A string that runs kitlib code in a child (`-c "from kitlib.X import ..."`, `-m kitlib.X`);
# a string that only names a package (a label, a message) is not a reference.
_CHILD_IMPORT = re.compile(r'\b(?:import|from)\s+kitlib\b|-m\s+kitlib\.')
_TOOL_FILE = re.compile(r'(?<![\w.])([A-Za-z_]\w*)\.py\b')


class ConfigError(ValueError):
    """The selection table is not of the shape this module reads."""


def config(root, data=None):
    """The selection table: `data`, else `<root>/tests/selection.json`, over DEFAULTS. Raises
    ConfigError on an unknown key or a value of the wrong shape."""
    if data is None:
        path = os.path.join(root, *CONFIG_REL.split('/'))
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding='utf-8') as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as e:
                raise ConfigError('%s: %s' % (CONFIG_REL, e))
    if not isinstance(data, dict):
        raise ConfigError('%s must be an object' % CONFIG_REL)
    unknown = sorted(set(data) - set(DEFAULTS) - {'comment'})
    if unknown:
        raise ConfigError('%s: unknown key(s) %s' % (CONFIG_REL, ', '.join(unknown)))
    out = dict(DEFAULTS)
    out.update({k: v for k, v in data.items() if k in DEFAULTS})
    for k in ('shared_core', 'full', 'by_name', 'prose', 'always'):
        v = out[k]
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ConfigError('%s: %s must be a list of non-empty strings' % (CONFIG_REL, k))
    pairs = []
    for i, e in enumerate(out['paths']):
        ok = (isinstance(e, list) and len(e) == 2 and isinstance(e[0], str) and e[0]
              and isinstance(e[1], list) and all(isinstance(x, str) and x for x in e[1]))
        if not ok:
            raise ConfigError('%s: paths[%d] must be [glob, [test package, ...]]' % (CONFIG_REL, i))
        pairs.append((e[0], list(e[1])))
    out['paths'] = pairs
    serial = []
    for i, e in enumerate(out['serial']):
        ok = (isinstance(e, list) and len(e) == 2 and all(isinstance(x, str) and x.strip() for x in e)
              and e[0].startswith('tests.'))
        if not ok:
            raise ConfigError('%s: serial[%d] must be ["tests.<pkg>.<module>", "why it cannot run '
                              'beside other tests"]' % (CONFIG_REL, i))
        serial.append((e[0], e[1]))
    out['serial'] = serial
    v = out['long']
    if not isinstance(v, list) or not all(isinstance(x, str) and x.startswith('tests.') for x in v):
        raise ConfigError('%s: long must be a list of "tests.<pkg>.<module>" names' % CONFIG_REL)
    out['long'] = list(v)
    return out


def _matches(path, globs):
    return any(glob_regex(g).match(path) for g in globs)


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return ''


def _py_files(folder):
    for d, dirs, files in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if x != '__pycache__')
        for n in sorted(files):
            if n.endswith('.py'):
                yield os.path.join(d, n)


def _docstring_ids(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def tool_runs(text):
    """The names a kitlib file runs by path: `<name>` for each non-docstring string holding
    `<name>.py`. Every name is kept, not only tools that exist, so a deleted tool's runners
    still name it. A file that does not parse falls back to its whole text."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set(_TOOL_FILE.findall(text))
    docs, out = _docstring_ids(tree), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.update(_TOOL_FILE.findall(node.value))
    return out


def code_refs(text, package=None):
    """(kitlib packages, top-level names) a code file (kitlib or tools) reaches: its imports, its
    relative imports out of kitlib/`package`, and `kitlib.X` in a non-docstring string that
    imports it in a child (_CHILD_IMPORT). A file
    that does not parse falls back to its whole text (it only selects more)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set(_KITLIB_REF.findall(text)), set()
    pkgs, tops, docs = set(), set(), _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split('.')
                if parts[0] == 'kitlib' and len(parts) > 1:
                    pkgs.add(parts[1])
                tops.add(parts[0])
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or '').split('.')
            if node.level == 0:
                if mod[0] == 'kitlib':
                    if len(mod) > 1:
                        pkgs.add(mod[1])
                    else:
                        pkgs.update(a.name for a in node.names)
                tops.add(mod[0])
            elif node.level >= 2 and package is not None:
                # from ..X import y inside kitlib/<package>/: X (or y with no X) is a kitlib package
                pkgs.update([mod[0]] if mod[0] else [a.name for a in node.names])
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docs and _CHILD_IMPORT.search(node.value)):
            pkgs.update(_KITLIB_REF.findall(node.value))
    return pkgs, tops


class Tree(object):
    """The kit's tree at `root`: `kitlib` {package: kitlib packages it reaches}, `tools` {tool:
    (kitlib packages, top-level names it imports)}, `tests` {test package: TestPackage}, `runs`
    {kitlib package: tools it runs by path}."""

    def __init__(self, root, kitlib, tools, tests, runs=None):
        self.root = root
        self.kitlib = kitlib
        self.tools = tools
        self.tests = tests
        self.runs = runs or {}

    def reach(self, packages=(), tools=(), core=()):
        """(kitlib packages, tools, core runners) a change to `packages` and `tools` can break,
        transitively: importers of a package, tools importing a package or a tool, and packages
        running a reached tool by path, with their importers. A `core` package that runs a
        reached tool is returned in core runners and not closed over: its own tests cover what
        it does with the tool, and its importers are the whole tree."""
        pk, tl, cr = set(), set(tools), set()
        todo = set(packages)
        while True:
            for p in todo - pk:
                pk |= self.importers(p)
            for x, (pkgs, _tops) in self.tools.items():
                if pkgs & pk:
                    tl.add(x)
            for t in list(tl):
                tl |= self.tool_importers(t)
            runners = {k for k, ran in self.runs.items() if ran & tl}
            cr |= runners & set(core)
            todo = runners - set(core) - pk
            if not todo:
                return pk, tl, cr

    def importers(self, package):
        """`package` and every kitlib package that reaches it, transitively."""
        out, todo = {package}, [package]
        while todo:
            m = todo.pop()
            for p, refs in self.kitlib.items():
                if m in refs and p not in out:
                    out.add(p)
                    todo.append(p)
        return out

    def tool_importers(self, tool):
        """`tool` and every tool that imports it, transitively."""
        out, todo = {tool}, [tool]
        while todo:
            t = todo.pop()
            for x, (_pkgs, tools) in self.tools.items():
                if t in tools and x not in out:
                    out.add(x)
                    todo.append(x)
        return out

    def naming(self, word):
        """The test packages whose files' text holds `word`."""
        return {t for t, tp in self.tests.items() if word in tp.text}


class TestPackage(object):
    def __init__(self, kitlib, tests, tools, text):
        self.kitlib = kitlib      # kitlib packages it names
        self.tests = tests        # other test packages it imports (tests.<pkg>)
        self.tools = tools        # tools it imports by module name
        self.text = text          # every .py file's text, joined


def scan(root):
    """The Tree of the checkout at `root`."""
    tools = {}
    tdir = os.path.join(root, 'tools')
    names = {f[:-3] for f in os.listdir(tdir) if f.endswith('.py')} if os.path.isdir(tdir) else set()
    for name in sorted(names):
        pkgs, tops = code_refs(_read(os.path.join(tdir, name + '.py')))
        # every top-level name, not only tools that exist: a deleted tool's importers still name it
        tools[name] = (pkgs, tops - {name})
    kitlib, runs = {}, {}
    kdir = os.path.join(root, 'kitlib')
    for name in sorted(os.listdir(kdir)) if os.path.isdir(kdir) else ():
        folder = os.path.join(kdir, name)
        if not os.path.isfile(os.path.join(folder, '__init__.py')):
            continue
        refs, ran = set(), set()
        for f in _py_files(folder):
            text = _read(f)
            refs |= code_refs(text, package=name)[0]
            ran |= tool_runs(text)
        kitlib[name] = refs - {name}
        if ran:
            runs[name] = ran
    tests = {}
    sdir = os.path.join(root, 'tests')
    for name in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else ():
        folder = os.path.join(sdir, name)
        if not os.path.isfile(os.path.join(folder, '__init__.py')):
            continue
        texts, pkgs, tops = [], set(), set()
        for f in _py_files(folder):
            text = _read(f)
            texts.append(text)
            p, t = code_refs(text)
            pkgs |= p
            tops |= t
        text = '\n'.join(texts)
        # imports (`from kitlib import X` too) and every `kitlib.X` / `kitlib/X` the text names
        tests[name] = TestPackage(pkgs | set(_KITLIB_REF.findall(text)),
                                  set(_TESTS_REF.findall(text)) - {name}, tops, text)
    return Tree(root, kitlib, tools, tests, runs)


class _Picks(object):
    def __init__(self, tree):
        self.tree = tree
        self.why = {}

    def add(self, pkg, reason):
        if pkg in self.tree.tests:
            self.why.setdefault(pkg, [])
            if reason not in self.why[pkg]:
                self.why[pkg].append(reason)
            return True
        return False

    def naming_tools(self, tools, path):
        found = False
        for tool in sorted(tools):
            for t in sorted(self.tree.tests):
                tp = self.tree.tests[t]
                if tool in tp.tools or (tool + '.py') in tp.text:
                    found |= self.add(t, 'names tools/%s.py, which %s reaches' % (tool, path)
                                      if ('tools/%s.py' % tool) != path else 'names %s' % path)
        return found


def _kitlib_change(path, pkg, tree, cfg, picks):
    if pkg in cfg['shared_core']:
        return 'shared core: kitlib/%s changed (%s)' % (pkg, path)
    if pkg not in tree.kitlib:
        return '%s is not in a kitlib package of this tree' % path
    found = _reached(path, pkg, tree.reach(packages={pkg}, core=cfg['shared_core']), tree, picks)
    return None if found else 'nothing maps %s to a test package' % path


def _reached(path, pkg, reached, tree, picks):
    """Pick the tests of every reached kitlib package, the test packages using one, those naming
    a reached tool, and the own tests of a shared-core package running a reached tool."""
    reach, tools, core = reached
    found = False
    for p in sorted(core):
        found |= picks.add(p, 'kitlib/%s (shared core) runs tools/%s.py, which %s reaches' % (
            p, sorted(tree.runs[p] & tools)[0], path))
    for p in sorted(reach):
        if p == pkg:
            why = 'own tests of changed kitlib/%s' % p
        elif tree.runs.get(p, set()) & tools:
            why = 'kitlib/%s runs tools/%s.py, which %s reaches' % (
                p, sorted(tree.runs[p] & tools)[0], path)
        elif pkg is not None and p in tree.importers(pkg):
            why = 'kitlib/%s imports kitlib/%s (%s)' % (p, pkg, path)
        else:
            why = 'kitlib/%s imports what %s reaches' % (p, path)
        found |= picks.add(p, why)
    for t, tp in sorted(tree.tests.items()):
        hit = sorted(tp.kitlib & reach)
        if hit:
            found |= picks.add(t, 'uses kitlib/%s (%s)' % (hit[0], path))
    found |= picks.naming_tools(tools, path)
    return found


def _tool_change(path, name, tree, cfg, picks):
    found = False
    # A tool's change cannot break the shared core it calls; that core's tests test the core.
    for p in sorted(tree.tools.get(name, (set(), set()))[0] - set(cfg['shared_core'])):
        found |= picks.add(p, 'tests of kitlib/%s, which %s imports' % (p, path))
    # A deleted tool is not in the tree, but its importers and runners still name it.
    found |= _reached(path, None, tree.reach(tools={name}, core=cfg['shared_core']), tree, picks)
    return None if found else 'nothing maps %s to a test package' % path


def _tests_change(path, pkg, tree, picks):
    if pkg not in tree.tests:
        return '%s is not in a test package of this tree' % path
    picks.add(pkg, 'changed in tests/%s (%s)' % (pkg, path))
    for t, tp in sorted(tree.tests.items()):
        if pkg in tp.tests:
            picks.add(t, 'imports tests.%s (%s)' % (pkg, path))
    return None


def _by_name(path, tree, picks):
    base = path.rsplit('/', 1)[-1]
    found = False
    for t in sorted(tree.naming(base)):
        found |= picks.add(t, 'names %s' % base)
    return found


def selection(changed, tree, cfg):
    """(ALL, [why]) or (sorted test packages, {package: [reasons]}, notes) - see the docstring.
    `notes` lists the prose paths no test names. Pure: reads only `tree` and `cfg`."""
    picks, notes = _Picks(tree), []
    for f in sorted({p.replace('\\', '/') for p in changed}):
        why = None
        parts = f.split('/')
        # `paths` adds to every rule below: a test package that reads another folder.
        mapped = [mods for g, mods in cfg['paths'] if glob_regex(g).match(f)]
        found = False
        for mods in mapped:
            for m in mods:
                found |= picks.add(m, 'reads %s (%s)' % (f, CONFIG_REL))
        if _matches(f, cfg['full']):
            why = '%s changed (a test helper or the selection itself)' % f
        elif parts[0] == 'kitlib' and len(parts) > 2:
            why = _kitlib_change(f, parts[1], tree, cfg, picks)
        elif parts[0] == 'tools' and len(parts) == 2 and f.endswith('.py'):
            why = _tool_change(f, parts[1][:-3], tree, cfg, picks)
        elif parts[0] == 'tests' and len(parts) > 2:
            why = _tests_change(f, parts[1], tree, picks)
        else:
            if mapped:
                found |= _by_name(f, tree, picks)
                why = None if found else 'its %s packages do not exist (%s)' % (CONFIG_REL, f)
            elif _matches(f, cfg['by_name']):
                why = None if _by_name(f, tree, picks) else 'no test package names %s' % f
            elif _matches(f, cfg['prose']):
                if not _by_name(f, tree, picks):
                    notes.append(f)
            else:
                why = '%s is not mapped (%s)' % (f, CONFIG_REL)
        if why is not None:
            return ALL, ['full suite: ' + why], notes
    if changed:
        for pkg in cfg['always']:
            picks.add(pkg, 'reads every tracked file (always, %s)' % CONFIG_REL)
    return sorted(picks.why), picks.why, notes


def changed_paths(base, root):
    """(paths, None) changed against the merge-base of `base` and HEAD in `root` - committed, in
    the work tree, and untracked - or (None, why) when git could not say."""
    mb, why = gitcmd.run_text(['merge-base', base, 'HEAD'], root)
    if mb is None or not mb:
        return None, 'git merge-base %s HEAD: %s' % (base, why or 'no merge-base printed')
    diff, why = gitcmd.run_text(['diff', '--name-only', '--no-renames', '-z', mb], root, strip=False)
    if diff is None:
        return None, 'git diff %s: %s' % (mb[:12], why)
    new, why = gitcmd.run_text(['ls-files', '-z', '--others', '--exclude-standard'], root, strip=False)
    if new is None:
        return None, 'git ls-files: %s' % why
    return sorted({p for p in (diff + new).split('\0') if p.strip()}), None


def test_modules(root, packages):
    """The dotted test modules `python -m unittest discover -s tests -t .` would load from the
    named test packages: `test*.py` files in folders that are packages, in sorted order."""
    return _walk_modules(root, [os.path.join(root, 'tests', pkg) for pkg in packages])


def all_test_modules(root):
    """Every dotted test module `python -m unittest discover -s tests -t .` loads, in sorted
    order (`tests/test*.py` included)."""
    return _walk_modules(root, [os.path.join(root, 'tests')])


def _walk_modules(root, tops):
    out = []
    for top in tops:
        for d, dirs, files in os.walk(top):
            if not os.path.isfile(os.path.join(d, '__init__.py')):
                dirs[:] = []
                continue
            dirs[:] = sorted(x for x in dirs if x != '__pycache__')
            rel = os.path.relpath(d, root).replace(os.sep, '.')
            for n in sorted(files):
                if n.startswith('test') and n.endswith('.py') and n[:-3].isidentifier():
                    out.append('%s.%s' % (rel, n[:-3]))
    return out
