"""csharp-using: a C# `using` of another module's namespace (docs/module-map.md "Detectors").

Every `.cs` file a module holds is read for `using X.Y;`, `using static X.Y.Z;`, `using A = X.Y;`
and the `global` form of each: one build crossing per line whose namespace maps to another
module. Namespaces map to modules one way: the map's `namespaces` (prefix -> module) when it has
any, else each module's csproj `RootNamespace` (the csproj file name when it has none). A prefix
matches a whole name (`A.B` matches `A.B` and `A.B.C`, not `A.Bx`); the longest prefix wins; a
namespace no prefix matches is ignored, and so is a using of the file's own module. A RootNamespace
two modules both claim is dropped (it names no one module).

Block comments are skipped, and a `using` must start its line, so `//` lines and `using` inside a
one-line string are not read. An alias may name a generic type (`using A = X.Y.Box<int>;`).
Known misses: a second using on the same line, a using after code on the same line
(`namespace X { using A; }`), a `/*` inside a `//` comment or a string (it can blank later
usings), a using inside a multi-line verbatim string, fully qualified names used without a using,
and namespaces a csproj sets by other means (`$(...)`, Directory.Build.props). A RootNamespace
claims every namespace under it, including those of folders with no csproj.
"""
import posixpath
import re

from . import ROOT, Crossing, line_of

NAME = 'csharp-using'
USING = re.compile(r'^[ \t]*(?:global[ \t]+)?using[ \t]+(?:static[ \t]+)?'
                   r'(?:[A-Za-z_]\w*[ \t]*=[ \t]*)?(?:global::)?([A-Za-z_][\w.]*)(?:<[^;\r\n]*>)?\??[ \t]*;[^\r\n]*', re.M)
BLOCK = re.compile(r'/\*.*?\*/', re.S)
ROOT_NS = re.compile(r'<RootNamespace>\s*([^<\s]+)\s*</RootNamespace>')


def _blank(m):
    return re.sub(r'[^\n]', ' ', m.group(0))


def root_namespaces(files, mm, read):
    """`{namespace prefix: module}` from each module's csproj files."""
    found, clash = {}, set()
    for f in files:
        if not f.lower().endswith('.csproj'):
            continue
        mod = mm.module_of(f)[0]
        if mod == ROOT:
            continue
        text = read(f) or ''
        m = ROOT_NS.search(re.sub(r'<!--.*?-->', _blank, text, flags=re.S))
        ns = m.group(1) if m else posixpath.basename(f)[:-len('.csproj')]
        if found.setdefault(ns, mod) != mod:
            clash.add(ns)
    return {ns: mod for ns, mod in found.items() if ns not in clash}


def module_of_namespace(prefixes, ns):
    """The module of the longest prefix of `prefixes` that names `ns` or a parent of it, or None."""
    best = None
    for p in prefixes:
        if (ns == p or ns.startswith(p + '.')) and (best is None or len(p) > len(best)):
            best = p
    return prefixes[best] if best is not None else None


def detect(files, mm, read):
    prefixes = dict(mm.namespaces) or root_namespaces(files, mm, read)
    if not prefixes:
        return
    for f in files:
        if not f.lower().endswith('.cs'):
            continue
        src = mm.module_of(f)[0]
        if src == ROOT:
            continue
        text = read(f)
        if text is None:
            continue
        text = BLOCK.sub(_blank, text)
        for m in USING.finditer(text):
            dst = module_of_namespace(prefixes, m.group(1))
            if dst is None or dst == src:
                continue
            yield Crossing(src, dst, 'build', NAME, f, line_of(text, m.start(1)),
                           m.group(0).strip())
