"""csproj: what an MSBuild project file pulls in from outside its module.

- `<ProjectReference Include="...">`: a build crossing.
- `<Compile Include="...">` of a path outside the project's module (usually with Link): a source
  crossing, the other module's code compiled into this one.
- `<None|Content|EmbeddedResource Include="...">` outside the module: a data crossing.

One crossing per Include entry (a `;` list is several; a glob is one, its module read from the
part before the first wildcard). Comments are skipped. Known misses: `Update=` items, paths built
from MSBuild properties or item metadata (`$(...)`, `@(...)`, `%(...)`), Directory.Build.props
and imported .props/.targets files, project files no module holds, and paths outside the
repository.
"""
import posixpath
import re

from . import ROOT, Crossing, inside_repo, line_of

NAME = 'csproj'
ITEM = re.compile(r'<(ProjectReference|Compile|None|Content|EmbeddedResource)\b([^>]*)>', re.S)
INCLUDE = re.compile(r'(?<![\w.])Include\s*=\s*"([^"]*)"')
COMMENT = re.compile(r'<!--.*?-->', re.S)
KIND = {'ProjectReference': 'build', 'Compile': 'source'}
WILD = re.compile(r'[*?\[]')


def _blank(m):
    return re.sub(r'[^\n]', ' ', m.group(0))


def literal_prefix(path):
    """The part of `path` before its first segment holding a wildcard."""
    out = []
    for seg in path.split('/'):
        if WILD.search(seg):
            break
        out.append(seg)
    return '/'.join(out)


def detect(files, mm, read):
    for f in files:
        if not f.lower().endswith('.csproj'):
            continue
        src, _ = mm.module_of(f)
        if src == ROOT:
            continue
        text = read(f)
        if text is None:
            continue
        text = COMMENT.sub(_blank, text)
        here = posixpath.dirname(f)
        for m in ITEM.finditer(text):
            inc = INCLUDE.search(m.group(2))
            if not inc:
                continue
            for part in inc.group(1).split(';'):
                part = part.strip()
                if not part or re.search(r'[$@%]\(', part):
                    continue
                target = inside_repo(posixpath.join(here, part.replace('\\', '/')))
                if target is None:
                    continue
                prefix = literal_prefix(target)
                dst = mm.module_of(prefix)[0] if prefix else ROOT     # a glob at the repo root
                if dst == src:
                    continue
                yield Crossing(src, dst, KIND.get(m.group(1), 'data'), NAME, f,
                               line_of(text, m.start()), part)
