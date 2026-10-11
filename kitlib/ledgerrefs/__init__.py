"""Where is each ruling id cited? For ledger cleanup: before an entry is retired, list every
reference to it so a citation is never broken silently (docs/ledger.md "ledger_refs").

The places come from the machine config's `ledger_refs` (kitlib.config.user): `repos`, each
searched with `git grep` at its committed `ref`, and `folders` (a folder or one file), searched on
disk. The ledger file itself is counted apart: its mentions of an id, less the entry's own header;
a ledger path the ledger's path checks refuse (kitlib.ledger.safepath) counts none and is an error.

An id `R-nnnn` matches `R-nnnn` and `Rnnnn` not followed by a digit (`pattern`). Nothing here
prints or exits; a repository git cannot search is an error line in the result, never "no hits".
"""
import collections
import os
import re

from kitlib.git import cmd
from kitlib.ledger import safepath

ID_RE = re.compile(r'^R-(\d{4,})$')
# The file types searched on disk.
EXTENSIONS = ('.md', '.py', '.ps1', '.json', '.txt', '.cs', '.java', '.yml')
GREP_TIMEOUT = 120      # seconds

Refs = collections.namedtuple('Refs', 'id hits ledger_mentions errors')


def pattern(rid):
    """The extended regex (git grep -E and Python re alike) that finds `rid`."""
    m = ID_RE.match(rid)
    if not m:
        raise ValueError('not a ruling id R-nnnn: %r' % (rid,))
    return r'R-?%s([^0-9]|$)' % m.group(1)


def git_grep(path, ref, pat):
    """`(['file:line', ...], None)` or `(None, why)`."""
    r = cmd.call(['grep', '-n', '-E', pat, ref], path, timeout=GREP_TIMEOUT)
    if r.rc == 1:
        return [], None
    if r.rc != 0:
        return None, (r.err or 'git grep exited %s' % r.rc).strip()
    hits = []
    for line in cmd.text(r.out).splitlines():
        parts = line.split(':', 3)          # ref:file:line:text
        if len(parts) == 4:
            hits.append('%s:%s' % (parts[1], parts[2]))
    return hits, None


def _files(root):
    if os.path.isfile(root):
        return [root]
    return sorted(os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs)


def file_grep(root, rx, any_type=False):
    """`['relative path:line', ...]` for the EXTENSIONS files (every file with `any_type`) at or
    under `root` (a missing root has none); paths relative to `root`, or the file's name when
    `root` is a file."""
    hits = []
    base = os.path.dirname(root) if os.path.isfile(root) else root
    for p in _files(root):
        if not any_type and not p.lower().endswith(EXTENSIONS):
            continue
        rel = os.path.relpath(p, base).replace('\\', '/')
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        hits.append('%s:%d' % (rel, i))
        except OSError:
            pass
    return hits


def ledger_mentions(ledger_path, rx):
    """The ledger's lines naming the id, less the entry's own header (never below 0). The ledger
    is read whatever its file name ends in."""
    if not ledger_path or not os.path.isfile(ledger_path):
        return 0
    return max(0, len(file_grep(ledger_path, rx, any_type=True)) - 1)


def find(rid, settings, ledger_path):
    """Refs(id, ['place:file:line', ...], ledger mentions, [error, ...]) for one id. `settings` is
    the machine config's ledger_refs object (or None)."""
    pat = pattern(rid)
    rx = re.compile(pat)
    settings = settings or {}
    hits, errors = [], []
    for name, repo in sorted((settings.get('repos') or {}).items()):
        got, why = git_grep(repo['path'], repo['ref'], pat)
        if got is None:
            errors.append('%s: cannot search %s at %s: %s' % (name, repo['path'], repo['ref'], why))
            continue
        hits += ['%s:%s' % (name, h) for h in got]
    for name, folder in sorted((settings.get('folders') or {}).items()):
        if not os.path.exists(folder):
            errors.append('%s: %s does not exist' % (name, folder))
            continue
        hits += ['%s:%s' % (name, h) for h in file_grep(folder, rx)]
    why = safepath.refusal(ledger_path) if ledger_path else None
    if why is not None:
        errors.append('ledger: %s' % why)
        return Refs(rid, hits, 0, errors)
    return Refs(rid, hits, ledger_mentions(ledger_path, rx), errors)
