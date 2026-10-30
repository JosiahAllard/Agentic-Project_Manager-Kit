"""Code map, generated on demand from the working tree; never committed, so never stale (from the
PM kit's tools/codemap.py; docs/doc-tools.md "codemap").

Modes of `run`:
  (none)        one line per source area (deep Java/Kotlin package trees collapsed)
  dir=<path>    one line per file under that folder (prefix match; the real path)
  find=<Name>   where a type (substring) or member (exact name) is declared
  file=<path>   members of one file, with line numbers
  symbol=<Name> numbered source of one Python def or class (`Class.method` too); `path` narrows it

Reads tracked AND untracked-but-not-ignored source files (`git ls-files -co --exclude-standard`),
so it matches this worktree, new files included. Heuristic regexes, not a parser: good for
"where is X" and "what is in here", not for proving absence. Types and members are read from
C-family sources (Java, Kotlin, C#), Python/PowerShell `def`/`class`/`function` lines and GDScript
`class_name`/`class`/`func` lines; other extensions are counted by folder only. No configuration:
the extension and skip lists are the union every repository needs.
"""
import ast
import os
import re
from collections import OrderedDict

from kitlib.docs import budget
from kitlib.git import cmd
from kitlib.host import registry

EXTS = ('.java', '.kt', '.kts', '.cs', '.razor', '.py', '.ps1', '.js', '.ts', '.tsx', '.go', '.rs',
        '.c', '.cpp', '.h', '.hpp', '.gd', '.gdshader')
TYPE = re.compile(r'^\s*(?:(?:public|internal|protected|private|static|final|abstract|sealed|partial|readonly|data|open)\s+)*'
                  r'(class|interface|enum|record|struct|object)\s+([A-Za-z_]\w*)')
MEMBER = re.compile(r'^\s{2,8}(?:(?:public|internal|protected|private|static|final|abstract|virtual|override|async|'
                    r'sealed|readonly|synchronized|default|new|partial|unsafe)\s+)+'
                    r'[\w<>\[\],.? ]+?\s+([A-Za-z_]\w*)\s*\(')
PYPS = re.compile(r'^\s*(?:def|class|function)\s+([A-Za-z_][\w-]*)', re.I)
GD_TYPE = re.compile(r'^(class_name|class)\s+([A-Za-z_]\w*)')
GD_FUNC = re.compile(r'^\s*(?:static\s+)?func\s+([A-Za-z_]\w*)')
# Build output and tool folders; each host's own folder (registry.host_folders) is skipped too.
SKIP = ('build/', 'bin/', 'obj/', 'node_modules/', 'target/', '.venv/', 'dist/', '.godot/')
NOT_MEMBERS = ('if', 'for', 'while', 'switch', 'catch', 'return', 'using', 'lock')
LIST_TIMEOUT = 120               # seconds; a large tree's listing is slow, not hung


def files(root):
    """`(source files, None)` git lists in `root`, filtered by EXTS, SKIP and the hosts' own
    folders, or `(None, why)` when git could not list them (an empty map would read as "no source
    here")."""
    r = cmd.call(['ls-files', '-co', '--exclude-standard', '-z'], root, timeout=LIST_TIMEOUT)
    if r.rc != 0:
        return None, r.err or 'git ls-files exited %s' % r.rc
    skip = tuple(f + '/' for f in registry.host_folders()) + SKIP
    return [f for f in budget.names_from(cmd.text(r.out))
            if f.endswith(EXTS) and not any(s in f for s in skip)], None


def group(d):
    """Top view key: a Java/Kotlin source root keeps 4 package levels (real paths, so the key
    can be passed straight to --dir), anything else 3 folder levels."""
    m = re.match(r'^((?:.*?/)?src/(?:main|test|testFixtures)/(?:java|kotlin))/(.*)$', d)
    if m:
        return (m.group(1) + '/' + '/'.join(m.group(2).split('/')[:4])).rstrip('/')
    return '/'.join(d.split('/')[:3])


def scan(root, path):
    """(types [(name, kind, line)], members [(name, line)], line count) of `path` under `root`;
    ([], [], 0) when it cannot be read."""
    types, members, n = [], [], 0
    gd = path.endswith('.gd')
    try:
        with open(os.path.join(root, path), encoding='utf-8', errors='replace') as fh:
            for n, line in enumerate(fh, 1):
                if gd:
                    m = GD_TYPE.match(line)
                    if m:
                        types.append((m.group(2), m.group(1), n))
                        continue
                    m = GD_FUNC.match(line)
                    if m:
                        members.append((m.group(1), n))
                    continue
                m = TYPE.match(line)
                if m:
                    types.append((m.group(2), m.group(1), n))
                    continue
                m = MEMBER.match(line) or (PYPS.match(line) if path.endswith(('.py', '.ps1')) else None)
                if m and m.group(1) not in NOT_MEMBERS:
                    members.append((m.group(1), n))
    except OSError:
        return [], [], 0
    return types, members, n


def symbols(root, path):
    """([(file, first line, last line, dotted name, kind)], None) of every Python def and class
    under `path` (a file or folder prefix; None = the whole tree); a file that does not parse is
    skipped. `(None, why)` when git could not list the files."""
    fs, why = files(root)
    if fs is None:
        return None, why
    p = (path or '').replace('\\', '/').strip('/')
    found = []
    for f in fs:
        if not f.endswith('.py') or (p and f != p and not f.startswith(p + '/')):
            continue
        try:
            with open(os.path.join(root, f), encoding='utf-8', errors='replace') as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError, ValueError):
            continue
        stack = [(tree, '')]
        while stack:
            node, prefix = stack.pop()
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = prefix + ch.name
                    found.append((f, ch.lineno, ch.end_lineno, q,
                                  'class' if isinstance(ch, ast.ClassDef) else 'def'))
                    stack.append((ch, q + '.'))
                else:
                    stack.append((ch, prefix))
    found.sort(key=lambda h: (h[0], h[1]))
    return found, None


def show_symbol(root, out, name, path=None):
    """Print the numbered source of the one Python def/class `name` (or `Class.method`); 0, 1
    when it is not found or ambiguous (the matches listed), 2 when git could not list the files."""
    found, why = symbols(root, path)
    if found is None:
        out('codemap: cannot list the files of %s: %s' % (root, why))
        return 2
    hits = [h for h in found if h[3] == name or h[3].endswith('.' + name)]
    if not hits:
        out('codemap: symbol %s not found' % name)
        return 1
    if len(hits) > 1:
        out('codemap: %s is ambiguous, %d matches (name Class.method or pass a path):' % (name, len(hits)))
        for f, a, _b, q, kind in hits:
            out('%s:%d  %s %s' % (f, a, kind, q))
        return 1
    f, a, b, q, kind = hits[0]
    out('%s:%d-%d  %s %s' % (f, a, b, kind, q))
    with open(os.path.join(root, f), encoding='utf-8', errors='replace') as fh:
        lines = fh.read().splitlines()
    for i in range(a, b + 1):
        out('%5d  %s' % (i, lines[i - 1]))
    return 0


def run(root, out, dir=None, find=None, file=None, symbol=None, path=None):
    """One codemap mode over `root`, every line to `out`; 0, or 2 when git could not list the
    files (symbol: 1 when not found or ambiguous)."""
    if symbol:
        return show_symbol(root, out, symbol, path)
    if file:
        path = file.replace('\\', '/')
        types, members, n = scan(root, path)
        out('%s  %d lines' % (path, n))
        for name, kind, ln in types:
            out('  %5d  %s %s' % (ln, kind, name))
        for name, ln in members:
            out('  %5d    %s()' % (ln, name))
        return 0
    fs, why = files(root)
    if fs is None:
        out('codemap: cannot list the files of %s: %s' % (root, why))
        return 2
    if find:
        want = find.lower()
        hits = 0
        for f in fs:
            types, members, _ = scan(root, f)
            for name, kind, ln in types:
                if want in name.lower():
                    out('%s:%d  %s %s' % (f, ln, kind, name))
                    hits += 1
            for name, ln in members:
                if want == name.lower():
                    out('%s:%d  %s()' % (f, ln, name))
                    hits += 1
        out('%d hits' % hits)
        return 0
    if dir:
        d = dir.replace('\\', '/').rstrip('/')
        for f in fs:
            if f.startswith(d + '/') or os.path.dirname(f) == d:
                types, members, n = scan(root, f)
                names = ', '.join(t[0] for t in types[:4]) or '-'
                out('%5d  %-60s %s%s' % (n, f[len(d) + 1:], names,
                                         ' +%d' % (len(types) - 4) if len(types) > 4 else ''))
        return 0
    dirs = OrderedDict()
    for f in fs:
        d = group(os.path.dirname(f) or '.')
        e = dirs.setdefault(d, [0, 0, []])
        _types, _, n = scan(root, f)
        e[0] += 1
        e[1] += n
        e[2].append((n, os.path.basename(f)))
    out('files  lines  folder  | largest files   (next: --dir <folder>, --find <Name>, --file <path>)')
    for d, (count, lines, big) in dirs.items():
        top = ', '.join('%s %d' % (b, n) for n, b in sorted(big, reverse=True)[:3])
        out('%5d %6d  %s  | %s' % (count, lines, d, top))
    return 0
