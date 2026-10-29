"""paths: test sources that read another module's files, or climb to the repository root.

Reads the files the map's `test_sources` globs name that are code (CODE_EXT), in a module.
- A string literal holding `/` (or `\\`) that names a file or folder in the tree is a data
  crossing when that path is in another module (or no module: ROOT). It is resolved, first match
  wins: from the file's own folder when it starts `./` or `../`; from the file's folder, then its
  module's folder (a path inside the module is no crossing); from the repository root, with any
  leading `./` and `../` dropped.
- A literal naming a root marker (`.git`, a root-level .sln, .slnx or settings.gradle(.kts) file,
  or a name in the map's `root_markers`), or `ls-files`, is a tree crossing into ROOT: the test
  searches upward for the root or lists the whole tree.

Known misses: paths assembled from pieces (`Path.Combine(root, "docs", name)`, one segment per
literal), paths built from variables or configuration, reflection and classpath resources, text
blocks and raw strings, and paths outside the repository. Literals in comments count.
"""
import posixpath
import re

from . import ROOT, Crossing, inside_repo, line_of

NAME = 'paths'
CODE_EXT = ('.cs', '.java', '.kt', '.kts', '.groovy', '.py', '.js', '.mjs', '.cjs', '.ts', '.gd')
SINGLE_QUOTED = ('.py', '.js', '.mjs', '.cjs', '.ts', '.gd')
DQ = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
SQ = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
ROOT_FILE = re.compile(r'(?i)^[^/]+\.(sln|slnx)$|^settings\.gradle(\.kts)?$')
TREE_WORDS = ('ls-files',)


def root_markers(files, mm):
    """Lower-cased names a root climb looks for."""
    out = {'.git'}
    out.update(f.lower() for f in files if '/' not in f and ROOT_FILE.match(f))
    out.update(m.lower() for m in mm.root_markers)
    return out


def literals(text, ext):
    """(offset, unescaped text) of each string literal."""
    pats = (DQ, SQ) if ext in SINGLE_QUOTED else (DQ,)
    for pat in pats:
        for m in pat.finditer(text):
            yield m.start(), m.group(1).replace('\\\\', '\\')


class Tree(object):
    """The tree's files and folders, lower-cased, for existence checks."""

    def __init__(self, files):
        self.paths = set()
        for f in files:
            p = f.lower()
            while p and p not in self.paths:
                self.paths.add(p)
                p = posixpath.dirname(p)

    def has(self, path):
        return path is not None and path.lower() in self.paths


def resolve(lit, here, modroot, tree):
    """The tree path `lit` names, read from file folder `here` in a module at `modroot`, or None."""
    p = lit.replace('\\', '/').strip()
    if '/' not in p.strip('/') or '://' in p or p.startswith('/') or ':' in p or re.search(r'[*?{}$<>|"]', p):
        return None
    p = p.rstrip('/')
    if p.startswith('./') or p.startswith('../'):
        hit = inside_repo(posixpath.join(here, p))
        if tree.has(hit):
            return hit
    for base in (here, modroot):
        hit = inside_repo(posixpath.join(base, p))
        if tree.has(hit):
            return hit
    bare = re.sub(r'^(?:\.\.?/)+', '', p)
    return bare if tree.has(bare) else None


def detect(files, mm, read):
    tree = Tree(files)
    markers = root_markers(files, mm)
    for f in files:
        low = f.lower()
        ext = posixpath.splitext(low)[1]
        if ext not in CODE_EXT or not mm.is_test_source(f):
            continue
        src, modroot = mm.module_of(f)
        if src == ROOT:
            continue
        text = read(f)
        if text is None:
            continue
        here = posixpath.dirname(f)
        seen = set()
        for off, lit in literals(text, ext):
            line = line_of(text, off)
            norm = lit.replace('\\', '/').strip().rstrip('/').lower()
            last = norm.rsplit('/', 1)[-1]
            if norm in TREE_WORDS or (last and last in markers and (norm == last or not tree.has(norm))):
                key = (line, lit, ROOT)
                if key not in seen:
                    seen.add(key)
                    yield Crossing(src, ROOT, 'tree', NAME, f, line, lit)
                continue
            target = resolve(lit, here, modroot, tree)
            if target is None:
                continue
            dst, _ = mm.module_of(target)
            if dst == src or (line, lit, dst) in seen:
                continue
            seen.add((line, lit, dst))
            yield Crossing(src, dst, 'data', NAME, f, line, lit)
