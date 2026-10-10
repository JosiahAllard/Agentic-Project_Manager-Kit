"""kitlib.rulingcites: fail a branch that ADDS a citation of a ruling the PM's rulings ledger
cannot back, so a stale or mistyped citation never lands; docs/repo-checks.md.

A citation is `PM R-nnnn` plus the ids continuing it on the same line (`PM R-nnnn/R-mmmm`,
`PM R-nnnn, R-mmmm and R-pppp`); a bare `R-nnnn` is not read. Each id must be a ledger header
whose repo column is this repository's ledger name or `all`, and not a tombstone (the tombstone's
`see` chain names what to cite instead). Only a citation on a line the branch adds fails; the
same finding on an untouched line is a warning, so tombstoning an id the main branch still cites
does not turn every open branch red at once.

The ledger is read through kitlib.backlog.ledger, which re-exports kitlib.ledger.read (the one
reader the backlog's own ruling checks use), from the path the machine config names
(kitlib.config.ledger_path); this module names no path. No configured or readable ledger: one warning, nothing checked, exit 0.
"""
import collections
import fnmatch
import os
import re

from kitlib.backlog import ledger as ledgermod
from kitlib.config import ConfigError, ledger_path as configured_ledger, load as load_config
from kitlib.git import batch as gitbatch
from kitlib.git import cmd as gitcmd
from kitlib.git import refs as gitrefs

CITE_RE = re.compile(r'\bPM (R-[0-9]{4,}(?:\s*(?:,|/|&|and)\s*R-[0-9]{4,})*)')
ID_RE = re.compile(r'R-[0-9]{4,}')
HUNK_RE = re.compile(r'^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
MAX_BYTES = 4 * 1024 * 1024
SECTION = 'ruling_cites'
OK, FAILED, REFUSED = 0, 1, 2

Outcome = collections.namedtuple('Outcome', 'code lines')


def settings(root, cfg=None):
    """`(ledger repo names, skip globs)` from `.kit.json` "ruling_cites" (the schema has checked
    its types): the name is "ruling_cites".repo, else "repo".name, else the folder, lower-cased;
    skip unset is `<tools_dir>/test_*.py` and `<tools_dir>/fixtures/*`."""
    cfg = cfg if cfg is not None else load_config(root)
    sec = cfg.section(SECTION)
    name = sec.get('repo') or cfg.repo.get('name') or os.path.basename(os.path.normpath(root))
    skip = sec.get('skip')
    if skip is None:
        tools = cfg.tools_dir.replace('\\', '/').strip('/')
        skip = ['%s/test_*.py' % tools, '%s/fixtures/*' % tools]
    return (name.strip().lower(), 'all'), tuple(skip)


def tree_files(root):
    """`(tracked paths, set of untracked non-ignored paths)` from one `ls-files -t`, or
    `(None, None)` when git cannot list them. `-t` tags an untracked path `?`."""
    out, _why = gitcmd.run(['ls-files', '-z', '-t', '--cached', '--others', '--exclude-standard'],
                           root)
    if out is None:
        return None, None
    tracked, untracked = [], set()
    for entry in out.decode('utf-8', 'replace').split('\0'):
        if len(entry) < 3:
            continue
        tag, path = entry[0], entry[2:]
        if tag == '?':
            untracked.add(path)
        else:
            tracked.append(path)
    return tracked, untracked


def added_lines(root, base):
    """`{path: set(line numbers)}` the branch adds against the merge base of `base` and HEAD
    (working tree included), or None when git failed. Untracked files are the caller's.

    The prefixes are explicit (`diff.noprefix`, `diff.mnemonicPrefix` would change them) and
    `core.quotePath` is off; a path git still quotes is unquoted. Header lines are read only
    between hunks: inside one, its counts say how many lines follow, so an added line whose text
    starts `++ ` is content, not a new file."""
    mb, _why = gitrefs.merge_base(base, 'HEAD', root)
    if not mb:
        return None
    out, _why = gitcmd.run(['-c', 'core.quotePath=false', 'diff', '-U0', '--no-color',
                            '--no-ext-diff', '--no-renames', '--src-prefix=a/', '--dst-prefix=b/',
                            mb], root)
    if out is None:
        return None
    found, path, n, old_left, new_left = {}, None, 0, 0, 0
    for line in out.decode('utf-8', 'replace').split('\n'):
        if old_left > 0 or new_left > 0:
            if line.startswith('+'):
                if path:
                    found.setdefault(path, set()).add(n)
                n += 1
                new_left -= 1
            elif line.startswith('-'):
                old_left -= 1
            continue                                    # `\ No newline at end of file`
        if line.startswith('+++ '):
            name = line[4:]
            if name.endswith('\t'):                     # git's tab after a path with a space
                name = name[:-1]
            name = gitbatch.unquote_path(name)
            path = name[2:] if name.startswith('b/') else None      # `/dev/null`: a deletion
            continue
        m = HUNK_RE.match(line)
        if m:
            old_left = 1 if m.group(1) is None else int(m.group(1))
            n = int(m.group(2))
            new_left = 1 if m.group(3) is None else int(m.group(3))
    return found


def judge(entries, rid, repos):
    """None when `rid` may be cited in a repository whose ledger names are `repos`, else why not."""
    got = entries.get(rid)
    if got is None:
        return 'is not a ruling header in the PM\'s rulings ledger'
    if got.repo not in repos:
        return 'is a ruling for `%s`, not for %s' % (got.repo, ' or '.join(repos))
    if got.retired:
        target = ledgermod.successor(entries, rid)
        return ('is retired in the PM\'s rulings ledger (%s): %s'
                % (got.retired[0], 'cite %s instead' % target if target else 'drop the citation'))
    return None


def scan(root, files, entries, added, untracked, repos, skip):
    """`(failures, warnings, count)`: every citation read, judged."""
    fails, warns, count = [], [], 0
    for rel in files:
        if any(fnmatch.fnmatchcase(rel, g) for g in skip):
            continue
        path = os.path.join(root, rel)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
                continue
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError:
            continue
        if b'\0' in data[:8192] or b'PM R-' not in data:
            continue
        mine = added.get(rel, set())
        for n, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            for m in CITE_RE.finditer(line):
                for rid in ID_RE.findall(m.group(1)):
                    count += 1
                    why = judge(entries, rid, repos)
                    if why is None:
                        continue
                    msg = '%s:%d: `PM %s` %s' % (rel, n, rid, why)
                    (fails if rel in untracked or n in mine else warns).append(msg)
    return fails, warns, count


def check(root, base=None, ledger=None, cfg=None):
    """Outcome(code, lines). `ledger` None: the machine config's ledger_path. A ConfigError from
    `.kit.json` or the machine config propagates (the caller refuses, exit 2)."""
    cfg = cfg if cfg is not None else load_config(root)
    repos, skip = settings(root, cfg)
    note = None
    if not base:
        base, note = gitrefs.default_base(root, cfg.remote, cfg.main_branch)
    if ledger is None:
        ledger = configured_ledger()
    if not ledger:
        return Outcome(OK, ['WARNING: ruling citations NOT CHECKED - no rulings ledger is '
                            'configured (ledger_path in the machine config)'])
    entries, why = ledgermod.read_entries(ledger)
    if entries is None:
        return Outcome(OK, ['WARNING: ruling citations NOT CHECKED - the rulings ledger is '
                            'unreadable (%s)' % why])
    files, untracked = tree_files(root)
    added = added_lines(root, base)
    if files is None or added is None:
        return Outcome(REFUSED, ['RULING CITES NOT CHECKED: git could not list the tree or diff '
                                 'it against %s' % base])
    fails, warns, count = scan(root, sorted(set(files) | untracked), entries, added, untracked,
                               repos, skip)
    lines = (['ruling cites: %s' % note] if note else []) + [
        'WARNING: %s (a line this branch did not add)' % w for w in warns] + fails
    if fails:
        lines.append('RULING CITES FAILED: %d added citation(s) the ledger cannot back. '
                     'Cite the live ruling the finding names, or drop the citation.' % len(fails))
        return Outcome(FAILED, lines)
    lines.append('RULING CITES OK (%d `PM R-` citation(s) read, %d on lines not added here WARN)'
                 % (count, len(warns)))
    return Outcome(OK, lines)


__all__ = ['CITE_RE', 'ConfigError', 'Outcome', 'added_lines', 'check', 'judge', 'scan',
           'settings', 'tree_files']
