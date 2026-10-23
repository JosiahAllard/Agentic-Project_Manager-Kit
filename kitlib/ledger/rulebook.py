"""The rulebooks: the standing rules, one file per repository and one shared (docs/ledger.md
"Rulebooks"), in `<folder of the log>/rules/` (kitlib.ledger.files `Files.rules`).

```
# Alpha rules
Repos: alpha-checkout
Budget: 40

## Landing
- landing.batch: Landers batch up to three branches; a high-risk branch lands alone. (from <log id>, <log id>)
```

The header has a `# title`, a `Repos:` line (checkout folder names, comma-separated) and an optional
`Budget: N` (default 40, a soft limit). Each `##` heading is a topic; each rule is one
`- <key>: <text> (from <log id>, ...)` line. A key is `<topic-slug>.<slug>`, unique in its file; a
repo rulebook rule with the key of a shared rule overrides it. A source is a log entry id and
nothing else. Unknown lines, a duplicate key, a rule with no source and a header with no `Repos:`
are errors; going over budget is a note.

Pure functions over text, except `load`, which reads files. Nothing here prints or exits.
"""
import collections
import os
import re

from . import read
from .fmt import ID

SHARED = 'all'
FOLDER = 'rules'
DEFAULT_BUDGET = 40

KEY_RE = re.compile(r'^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9.-]*$')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
RULE_RE = re.compile(r'^- ([^\s:]+): (.+?)(?: \(from ([^()]*)\))?\s*$')
ID_ONLY = re.compile(r'^%s$' % ID)

# key, text, sources (a tuple of log ids), topic (the heading text), line (1-based).
Rule = collections.namedtuple('Rule', 'key text sources topic line')
# name (the file stem), title, repos (a tuple of folder names), budget, rules (file order).
Book = collections.namedtuple('Book', 'name title repos budget rules')
# errors: findings that fail `check`; notes: overrides and budget, which do not.
Findings = collections.namedtuple('Findings', 'errors notes')


def file_name(name):
    return name + '.md'


def paths(folder):
    """`{name: path}` of the rulebooks present in `folder` (every `<name>.md` with a lower-case
    name), sorted by name with the shared one last."""
    try:
        found = os.listdir(folder)
    except OSError:
        return {}
    names = sorted(f[:-3] for f in found
                   if f.endswith('.md') and NAME_RE.match(f[:-3]) and f[:-3] == f[:-3].lower()
                   and os.path.isfile(os.path.join(folder, f)))
    names.sort(key=lambda n: n == SHARED)
    return {n: os.path.join(folder, file_name(n)) for n in names}


def normal(folder_name):
    """The name a checkout folder maps to: lower-cased, every '_' as '-' (the same rule as the
    ledger_repos match in kitlib.coordinatorstart `ledger_name`)."""
    return str(folder_name or '').lower().replace('_', '-')


def parse(text, name):
    """`(Book, [error lines])` of one rulebook's text. `name` is the file stem; errors say
    `<name>.md:<line>: ...`."""
    where = file_name(name)
    errs = []
    title, repos, budget = None, None, DEFAULT_BUDGET
    rules, seen, topic = [], {}, None
    for n, line in enumerate(text.replace('\r\n', '\n').split('\n'), 1):
        if not line.strip():
            continue
        at = '%s:%d' % (where, n)
        if topic is None and line.startswith('# ') and title is None:
            title = line[2:].strip()
        elif topic is None and line.startswith('Repos:'):
            if repos is not None:
                errs.append('%s: a second Repos line (the first is a book\'s only one)' % at)
            repos = tuple(r.strip() for r in line[len('Repos:'):].split(',') if r.strip())
            bad =[r for r in repos if not NAME_RE.match(r)]
            if not repos or bad:
                errs.append('%s: Repos needs checkout folder names, comma-separated%s'
                            % (at, ' (bad: %s)' % ', '.join(bad) if bad else ''))
        elif topic is None and line.startswith('Budget:'):
            value = line[len('Budget:'):].strip()
            if re.fullmatch(r'[0-9]+', value) and int(value) > 0:
                budget = int(value)
            else:
                errs.append('%s: Budget needs a positive number, not %r' % (at, value))
        elif line.startswith('## '):
            topic = line[3:].strip()
            if not topic:
                errs.append('%s: an empty topic heading' % at)
        elif line.startswith('- '):
            m = RULE_RE.match(line)
            if topic is None:
                errs.append('%s: a rule before any `## topic` heading' % at)
            elif not m:
                errs.append('%s: not a rule line (- key: text (from ID, ...)): %r' % (at, line))
            else:
                _rule(m, topic, n, at, rules, seen, errs)
        else:
            errs.append('%s: unknown line %r' % (at, line))
    if repos is None:
        errs.append('%s: no `Repos:` header line' % where)
    return Book(name, title or '', repos or (), budget, tuple(rules)), errs


def _rule(m, topic, n, at, rules, seen, errs):
    key, text, cited = m.group(1), m.group(2).strip(), m.group(3)
    if not KEY_RE.match(key):
        errs.append('%s: key %r is not <topic-slug>.<slug> (lower case, digits, - and .)' % (at, key))
    if key in seen:
        errs.append('%s: duplicate key %s (first at line %d)' % (at, key, seen[key]))
    seen.setdefault(key, n)
    sources = tuple(s.strip() for s in cited.split(',')) if cited is not None else ()
    if not sources:
        errs.append('%s: rule %s has no source (end the line with (from <log id>, ...))' % (at, key))
    for s in sources:
        if not ID_ONLY.match(s):
            errs.append('%s: rule %s cites %r, which is not a log entry id' % (at, key, s))
    rules.append(Rule(key, text, sources, topic, n))


def render(book):
    """The text of `book`: parse(render(book)) gives the same Book. A heading is written whenever
    the topic changes from the rule before."""
    out = ['# %s' % book.title, 'Repos: %s' % ', '.join(book.repos), 'Budget: %d' % book.budget]
    topic = None
    for r in book.rules:
        if r.topic != topic:
            topic = r.topic
            out += ['', '## %s' % topic]
        out.append('- %s: %s (from %s)' % (r.key, r.text, ', '.join(r.sources)))
    return '\n'.join(out) + '\n'


def load(folder):
    """`({name: Book}, [error lines])` of the rulebooks present in `folder`; no file is no book and
    no error, an unreadable one is an error."""
    books, errs = {}, []
    for name, path in paths(folder).items():
        text, why = read.read_file(path)
        if text is None:
            errs.append(why)
            continue
        book, more = parse(text, name)
        books[name] = book
        errs += more
    return books, errs


def check(books, log_ids):
    """`Findings` over the parsed `books` ({name: Book}): a cited id absent from `log_ids` (every
    id of the log files, retired ones included) is an error; each override of a shared key and each
    book over its budget is a note. Parse errors come from `parse`."""
    errors, notes = [], []
    shared = {r.key for r in books[SHARED].rules} if SHARED in books else set()
    claimed = {}
    for name, book in books.items():
        if name == SHARED:
            continue
        for repo in sorted({normal(r) for r in book.repos}):
            if repo in claimed:
                errors.append('%s: Repos names %s, which %s already claims'
                              % (file_name(name), repo, file_name(claimed[repo])))
            else:
                claimed[repo] = name
    for name, book in books.items():
        where = file_name(name)
        for r in book.rules:
            for s in r.sources:
                if ID_ONLY.match(s) and s not in log_ids:
                    errors.append('%s:%d: rule %s cites %s, which is not in the log'
                                  % (where, r.line, r.key, s))
            if name != SHARED and r.key in shared:
                notes.append('%s:%d: rule %s overrides the shared rule' % (where, r.line, r.key))
        if len(book.rules) > book.budget:
            notes.append('%s: %d rules, over its budget of %d' % (where, len(book.rules), book.budget))
    return Findings(errors, notes)


def for_repo(books, folder):
    """The name of the repo rulebook for checkout folder `folder`, or None. A book's `Repos:` line
    names folders (matched by `normal`); with no such line naming it, the file stem equal to
    `normal(folder)` is the fallback. The shared book is never a repo's own."""
    key = normal(folder)
    for name, book in books.items():
        if name != SHARED and key in {normal(r) for r in book.repos}:
            return name
    return key if key in books and key != SHARED else None


def view(books, folder):
    """`[(Book, overridden keys)]` for a reader in `folder`: the shared book, then the repo's own
    (each only when present). The repo's own rules that share a key with a shared rule are its
    overrides; the keys are returned with the repo book and are dropped from the shared one."""
    own = for_repo(books, folder)
    mine = books.get(own) if own else None
    over = {r.key for r in mine.rules} & {r.key for r in books[SHARED].rules} \
        if mine and SHARED in books else set()
    out = []
    if SHARED in books:
        out.append((books[SHARED], over))
    if mine:
        out.append((mine, over))
    return out


def lines(book, over, sources=False, topic=None):
    """The printable lines of one book: a count line, then each topic heading once before its first
    rule and `- key: text` (with `(from ...)` when `sources`; `[overrides shared]` for a key in
    `over` on a repo book). `over` is the second item of `view`; the shared book skips those keys.
    `topic` keeps the rules whose heading or key holds the word (any case)."""
    shared = book.name == SHARED
    word = (topic or '').lower()
    shown = [r for r in book.rules
             if not (shared and r.key in over)
             and (not word or word in r.topic.lower() or word in r.key or word in r.text.lower())]
    label = 'shared' if shared else book.name
    count = '%d rules' % len(book.rules)
    if len(book.rules) > book.budget:
        count += ', over its budget of %d' % book.budget
    out = ['# %s (%s)' % (label, count)]
    heading = None
    for r in shown:
        if r.topic != heading:
            heading = r.topic
            out.append('## %s' % heading)
        tail = ' (from %s)' % ', '.join(r.sources) if sources else ''
        mark = ' [overrides shared]' if not shared and r.key in over else ''
        out.append('- %s: %s%s%s' % (r.key, r.text, mark, tail))
    return out, len(shown)
