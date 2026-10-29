"""python: imports of another module's code.

Reads every `.py` file a module holds (parsed with ast; a file that does not parse is skipped).
Each `import a.b` and `from a.b import c` that resolves to a file or folder in the tree is a build
crossing when that path is in another module. A relative import (`from . import x`,
`from ..a import b`) resolves from the file's package. An absolute one resolves from, first match
wins: the file's own folder, its top package's parent folder (the folder above the highest
`__init__.py` chain), its module's folder, then the repository root. A name resolves to
`a/b/c.py` or the folder `a/b/c`, trying the longest dotted prefix first (`from a.b import c`:
the submodule `a/b/c`, then `a/b`). A name that resolves nowhere (the standard library, an
installed package) is skipped.

Known misses: `sys.path` changes (an inserted folder is not added to the roots; a climb written
as `dirname(__file__)` pieces is not evaluated), `importlib` and `__import__` with computed names,
imports inside strings or `exec`, and packages installed from the repository (`pip install -e`).
"""
import ast
import posixpath

from . import ROOT, Crossing
from .paths import Tree

NAME = 'python'


def _parse(text, path):
    try:
        return ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return None


def package_root(here, tree):
    """The folder above the highest package (a folder with `__init__.py`) holding `here`."""
    cand = here
    while cand and tree.has(posixpath.join(cand, '__init__.py')):
        cand = posixpath.dirname(cand)
    return cand


def resolve(parts, base, tree):
    """The tree path the dotted `parts` name under folder `base`, longest prefix first, or None."""
    for k in range(len(parts), 0, -1):
        p = posixpath.join(base, *parts[:k]) if base else posixpath.join(*parts[:k])
        if tree.has(p + '.py'):
            return p + '.py'
        if tree.has(p):
            return p
    return None


def imports(node):
    """`(line, level, dotted parts, text)` of each import in the tree `node`."""
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield n.lineno, 0, a.name.split('.'), a.name
        elif isinstance(n, ast.ImportFrom):
            mod = n.module.split('.') if n.module else []
            text = '.' * n.level + (n.module or '')
            for a in n.names:
                if a.name == '*':
                    yield n.lineno, n.level, mod, text
                else:
                    yield n.lineno, n.level, mod + [a.name], '%s import %s' % (text, a.name)


def detect(files, mm, read):
    tree = Tree(files)
    for f in files:
        if not f.lower().endswith('.py'):
            continue
        src, modroot = mm.module_of(f)
        if src == ROOT:
            continue
        text = read(f)
        node = _parse(text, f) if text is not None else None
        if node is None:
            continue
        here = posixpath.dirname(f)
        roots = []
        for r in (here, package_root(here, tree), modroot, ''):
            if r not in roots:
                roots.append(r)
        seen = set()
        for line, level, parts, what in imports(node):
            if not parts:
                continue
            if level:
                base = here
                for _ in range(level - 1):
                    base = posixpath.dirname(base)
                target = resolve(parts, base, tree)
            else:
                target = next((t for t in (resolve(parts, r, tree) for r in roots) if t), None)
            if target is None:
                continue
            dst, _ = mm.module_of(target)
            if dst == src or (line, dst) in seen:
                continue
            seen.add((line, dst))
            yield Crossing(src, dst, 'build', NAME, f, line, what)
