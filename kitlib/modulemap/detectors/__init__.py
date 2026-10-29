"""The crossing detectors (docs/module-map.md "Detectors"). Each is a module with
`detect(files, mm, read)` yielding Crossing: `files` the repo-relative paths in the tree, `mm` the
ModuleMap, `read(path)` the file's text (or None when it cannot be read). A detector reports a
crossing only between two different modules (ROOT counts as one); the check decides whether it is
allowed.
"""
import collections
import posixpath

from kitlib.modulemap.load import ROOT  # noqa: F401  (re-exported for the detectors)

# src, dst: module names; kind: build, source, data or tree; detector: its name; path, line: where
# the crossing is written; target: the text that names what it reaches (an Include, a literal);
# ratchet: the id of the ratchet that counts it, set only by a detector a ratchet drives
# (java-imports), else None.
Crossing = collections.namedtuple('Crossing', 'src dst kind detector path line target ratchet',
                                  defaults=(None,))


def line_of(text, offset):
    return text.count('\n', 0, offset) + 1


def inside_repo(path):
    """`path` normalised (posix, no `.` or `..`), or None when it climbs out of the repository."""
    p = posixpath.normpath(path.replace('\\', '/'))
    if p in ('.', '') or p == '..' or p.startswith('../') or p.startswith('/') or ':' in p:
        return None
    return p


def names():
    return ('csproj', 'paths', 'gradle', 'python', 'java-imports', 'csharp-using')


def all_detectors():
    from . import csharp_using, csproj, gradle, java_imports, paths, python
    return (csproj, paths, gradle, python, java_imports, csharp_using)
