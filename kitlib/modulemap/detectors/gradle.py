"""gradle: what a Gradle build script pulls in from outside its module.

Reads every `*.gradle` and `*.gradle.kts` file a module holds (settings scripts aside):
- `project(":a:b")` (or `project(path = ":a:b")`): a build crossing into that project's folder.
  The folder is the `project(":a:b").projectDir = file("...")` the nearest settings script
  (settings.gradle or settings.gradle.kts in the script's folder or above) writes, else `a/b`
  under that settings script's folder; a project with no folder in the tree is skipped.
- `apply(from = ...)` / `apply from: ...`: a build crossing into the script it names.
- `rootDir.resolve("x")`, `rootProject.file("x")`, `rootProject.layout.projectDirectory.dir/file
  ("x")`, `File(rootDir, "x")` and a `"$rootDir/x"` literal: a data crossing into x, read from
  the root (the settings script's folder); `file("x")` and `layout.projectDirectory.dir/file
  ("x")`: read from the script's folder.

Comments are skipped. Known misses: type-safe project accessors (`projects.foo`), paths built
from variables or other properties, included builds, buildSrc and convention plugins applied by
id, and Copy or Sync tasks whose `from` names a path by other means.
"""
import posixpath
import re

from . import ROOT, Crossing, inside_repo, line_of
from .paths import Tree

NAME = 'gradle'
SETTINGS = ('settings.gradle', 'settings.gradle.kts')
STR = r'''(?:"([^"\\\n]*)"|'([^'\\\n]*)')'''
TOKENS = re.compile(r'''("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(//[^\n]*|/\*.*?\*/)''', re.S)
PROJECT_DIR = re.compile(r'project\(\s*%s\s*\)\s*\.\s*projectDir\s*=\s*(?:file\(\s*)?%s'
                         % (STR, STR))
PROJECT = re.compile(r'(?<![\w.])project\(\s*(?:path\s*[=:]\s*)?%s' % STR)
APPLY = re.compile(r'(?<![\w.])apply\s*\(?\s*from\s*[=:]\s*([^\n]*)')
ROOTED = re.compile(
    r'(?:(?<![\w.])rootDir|rootProject\.(?:rootDir|projectDir))\s*\.\s*resolve\(\s*%s'
    r'|rootProject\s*\.\s*file\(\s*%s'
    r'|rootProject\s*\.\s*layout\s*\.\s*projectDirectory\s*\.\s*(?:dir|file)\(\s*%s'
    r'|(?<![\w.])(?:new\s+)?File\(\s*(?:rootDir|rootProject\.rootDir)\s*,\s*%s' % (STR, STR, STR, STR))
LOCAL = re.compile(r'(?<![\w.])(?:file|layout\s*\.\s*projectDirectory\s*\.\s*(?:dir|file))'
                   r'\(\s*%s' % STR)
ROOT_LIT = re.compile(r'''["']\$\{?(?:rootDir|rootProject\.rootDir)\}?/([^"'$\n]+)["']''')
BARE = re.compile(STR)


def _blank(m):
    return re.sub(r'[^\n]', ' ', m.group(0))


def strip_comments(text):
    """`text` with `//` and `/* */` comments blanked (line numbers kept); strings are kept."""
    return TOKENS.sub(lambda m: m.group(0) if m.group(1) else _blank(m), text)


def _lit(m, first=1):
    """The first non-None group of `m` from `first` on."""
    return next((g for g in m.groups()[first - 1:] if g is not None), None)


def settings_dirs(files, read):
    """`{settings folder: {":project:path": folder}}` from each settings script's overrides."""
    out = {}
    for f in files:
        if posixpath.basename(f).lower() not in SETTINGS:
            continue
        here = posixpath.dirname(f)
        text = read(f)
        dirs = out.setdefault(here, {})
        if text is None:
            continue
        for m in PROJECT_DIR.finditer(strip_comments(text)):
            proj = m.group(1) if m.group(1) is not None else m.group(2)
            rel = m.group(3) if m.group(3) is not None else m.group(4)
            path = inside_repo(posixpath.join(here, rel)) if rel else None
            if proj and path:
                dirs[proj] = path
    return out


def nearest(here, settings):
    """The folder of the settings script nearest `here` (itself or above); '' (the root) when
    none is."""
    cand = here
    while True:
        if cand in settings:
            return cand
        if not cand:
            return ''
        cand = posixpath.dirname(cand)


def project_folder(proj, root_dir, settings, tree):
    dirs = settings.get(root_dir, {})
    if proj in dirs:
        return dirs[proj]
    parts = [p for p in proj.split(':') if p]
    if not parts:
        return None
    path = inside_repo(posixpath.join(root_dir, *parts))
    return path if tree.has(path) else None


def _path_in(arg, root_dir, here):
    """`(path, text)` the first path expression in `arg` names, or None: a rooted call, a local
    call, a `"$rootDir/x"` literal, then (an apply's argument) a bare literal, read from `here`."""
    for pat, base in ((ROOTED, root_dir), (LOCAL, here)):
        m = pat.search(arg)
        if m and '$' not in _lit(m):
            return posixpath.join(base, _lit(m)), _lit(m)
    m = ROOT_LIT.search(arg)
    if m:
        return posixpath.join(root_dir, m.group(1)), m.group(0)
    m = BARE.search(arg)
    if m and _lit(m) and '$' not in _lit(m):
        return posixpath.join(here, _lit(m)), _lit(m)
    return None


def _paths(text, root_dir, here):
    """`(offset, path, text)` of every path expression in `text` (no bare literals)."""
    for pat, base in ((ROOTED, root_dir), (LOCAL, here)):
        for m in pat.finditer(text):
            rel = _lit(m)
            if rel and '$' not in rel:
                yield m.start(), posixpath.join(base, rel), rel
    for m in ROOT_LIT.finditer(text):
        yield m.start(), posixpath.join(root_dir, m.group(1)), m.group(0)


def detect(files, mm, read):
    tree = Tree(files)
    settings = settings_dirs(files, read)
    for f in files:
        low = f.lower()
        if not (low.endswith('.gradle') or low.endswith('.gradle.kts')):
            continue
        if posixpath.basename(low) in SETTINGS:
            continue
        src, _ = mm.module_of(f)
        if src == ROOT:
            continue
        text = read(f)
        if text is None:
            continue
        text = strip_comments(text)
        here = posixpath.dirname(f)
        root_dir = nearest(here, settings)
        found = []                                  # (offset, kind, target path, text)
        applied = []                                # spans of apply statements
        for m in APPLY.finditer(text):
            applied.append((m.start(), m.end()))
            hit = _path_in(m.group(1), root_dir, here)
            if hit:
                found.append((m.start(), 'build') + hit)
        for m in PROJECT.finditer(text):
            proj = _lit(m)
            path = project_folder(proj, root_dir, settings, tree) if proj else None
            if path:
                found.append((m.start(), 'build', path, proj))
        for off, path, what in _paths(text, root_dir, here):
            if not any(a <= off < b for a, b in applied):
                found.append((off, 'data', path, what))
        seen = set()
        for off, kind, path, what in sorted(found):
            target = inside_repo(path)
            if target is None:
                continue
            dst, _ = mm.module_of(target)
            line = line_of(text, off)
            if dst == src or (line, kind, dst, what) in seen:
                continue
            seen.add((line, kind, dst, what))
            yield Crossing(src, dst, kind, NAME, f, line, what)
