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

The write side builds a changed Book (`set_rule`, `drop_rule`, `set_header`) and the reading list
before an add (`conflicts`, with its `digest`); kitlib.ledger.write writes it beside a log entry
whose Decides ends with a `tag`, and `applied` finds a logged change the rulebook does not show.

Pure functions over text, except `load`, which reads files. Nothing here prints or exits.
"""
import collections
import hashlib
import math
import os
import re

from . import read
from .fmt import ID, parse_block, split_entries

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


# The write side (docs/ledger.md "Rulebooks"): kitlib.ledger.write pairs each change below with a
# log entry under the log's write lock and the rulebook's; these functions only build the new Book.

def target(books, repo, book=None):
    """The name of the rulebook a rule for `repo` goes in: `all` is the shared book; else the book
    `for_repo` maps (a checkout folder or a log repo name), else `normal(repo)`, a book not made
    yet. `book` (a name) overrides all that: it must be `all` or a book in `books`, else ValueError."""
    if book is not None:
        name = normal(book)
        if name != SHARED and name not in books:
            raise ValueError('no rulebook %r (the books: %s)' % (book, ', '.join(sorted(books)) or 'none'))
        return name
    if normal(repo) == SHARED:
        return SHARED
    return for_repo(books, repo) or normal(repo)


def new_book(name, repos, budget=None, title=None):
    """An empty Book: the header `rule-set` writes for a rulebook not made yet."""
    return Book(name, one_line(title) or '%s rules' % name.capitalize(), tuple(repos),
                budget or DEFAULT_BUDGET, ())


def one_line(text):
    """`text` with each whitespace run as one space."""
    return ' '.join(str(text or '').split())


def set_rule(book, key, topic, text, sources):
    """`(the new Book, the Rule it replaced or None)`: rule `key` set to `text` under heading
    `topic`, citing `sources`. A key already in the book is replaced, never doubled: in its place
    when the topic stays, else moved. A new rule goes after the last rule of its topic, or under a
    new heading at the end. ValueError on a malformed key, topic or text."""
    topic, text = one_line(topic), one_line(text)
    if not KEY_RE.match(key or ''):
        raise ValueError('key %r is not <topic-slug>.<slug> (lower case, digits, - and .)' % (key,))
    if not topic:
        raise ValueError('the topic heading is empty')
    if not text:
        raise ValueError('the rule text is empty')
    rule = Rule(key, text, tuple(sources), topic, 0)
    rules = list(book.rules)
    old = next((r for r in rules if r.key == key), None)
    if old is not None and old.topic == topic:
        rules[rules.index(old)] = rule
    else:
        if old is not None:
            rules.remove(old)
        last = max((i for i, r in enumerate(rules) if r.topic == topic), default=None)
        rules.insert(len(rules) if last is None else last + 1, rule)
    return book._replace(rules=tuple(rules)), old


def drop_rule(book, key):
    """`(the new Book, the Rule removed)`; KeyError when the book has no rule `key`."""
    old = next((r for r in book.rules if r.key == key), None)
    if old is None:
        raise KeyError(key)
    return book._replace(rules=tuple(r for r in book.rules if r.key != key)), old


def set_header(book, repos=None, budget=None, title=None):
    """`book` with each header field that is given replaced."""
    return book._replace(repos=tuple(repos) if repos is not None else book.repos,
                         budget=budget if budget is not None else book.budget,
                         title=one_line(title) if title is not None else book.title)


# The tag a paired log entry's Decides ends with, so `applied` finds a logged change that never
# reached its rulebook (a crash between the two writes).
TAG_RE = re.compile(r'\[(?:rule ([a-z0-9][a-z0-9._-]*)\.md ([a-z0-9][a-z0-9.-]*) (set|dropped)'
                    r'|rulebook ([a-z0-9][a-z0-9._-]*)\.md header ([0-9a-f]{12}))\]\s*$')


def header_digest(book):
    """12 hex of sha256 over `book`'s header (repos, budget, title): the header tag records the
    header a change left, so `applied` can tell whether it is the one in force."""
    data = '\n'.join(['\t'.join(book.repos), str(book.budget), book.title])
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:12]


def tag(op, name, key=None, head=None):
    """The tag of a change: op 'set' or 'dropped' of rule `key` in book `name`, or 'header' with
    `head`, the header_digest of the header it set."""
    if op == 'header':
        return '[rulebook %s header %s]' % (file_name(name), head)
    return '[rule %s %s %s]' % (file_name(name), key, op)


def applied(books, texts):
    """Error lines for each logged rule change its rulebook does not show. Per rule, the last live
    tagged entry (by its time, then file and log order; `texts` are the log files' texts) must be
    in force: a `set` as a rule citing it, a `dropped` as no rule, a `header` as the book's header
    having the digest its tag records.
    A tombstone keeps no Decides, so which change it was is unknown. Not judged: a rule citing a
    retired entry newer than its last live change (a later set, since retired); and a `set` whose
    rule is absent, or a header, with a retired entry of a repo routed to its book (`target`) newer
    than its last live change (perhaps a later drop or header change, since retired)."""
    last, heads, retired = {}, {}, []
    for f, text in enumerate(texts):
        for n, (rid, block) in enumerate(split_entries(text)[1]):
            e, _ = parse_block(block)
            if not e or not rid:
                continue
            order = (e.get('when', ''), f, n)
            if e.get('retired'):
                retired.append((order, rid, e.get('repo', '')))
                continue
            m = TAG_RE.search(e.get('Decides', ''))
            if not m:
                continue
            if m.group(4):
                if m.group(4) not in heads or heads[m.group(4)][0] < order:
                    heads[m.group(4)] = (order, rid, m.group(5))
            else:
                k = (m.group(1), m.group(2))
                if k not in last or last[k][0] < order:
                    last[k] = (order, rid, m.group(3))
    dead = {rid: o for o, rid, _ in retired}

    def overtaken(name, order):
        return any(o > order and target(books, repo) == name for o, _, repo in retired)
    errs = []
    for (name, key), (order, rid, op) in sorted(last.items()):
        book = books.get(name)
        rule = next((r for r in book.rules if r.key == key), None) if book else None
        if rule is not None and any(s in dead and dead[s] > order for s in rule.sources):
            continue        # a later set, since retired: the rule cites it
        if op == 'set' and rule is None and overtaken(name, order):
            continue        # perhaps a later drop, since retired
        if op == 'set' and (rule is None or rid not in rule.sources):
            why = ('there is no %s' % file_name(name) if book is None else
                   'it has no such rule' if rule is None else 'the rule does not cite it')
            errs.append('logged rule change not applied: %s sets %s %s, but %s; redo it with '
                        'ledger.py add --rule' % (rid, file_name(name), key, why))
        elif op == 'dropped' and rule is not None:
            errs.append('logged rule change not applied: %s drops %s %s, which is still there; '
                        'redo it with ledger.py rule-drop' % (rid, file_name(name), key))
    for name, (order, rid, head) in sorted(heads.items()):
        if overtaken(name, order):
            continue
        if name not in books:
            errs.append('logged rule change not applied: %s sets the header of %s, which is not '
                        'there; redo it with ledger.py rule-set' % (rid, file_name(name)))
        elif header_digest(books[name]) != head:
            errs.append('logged rule change not applied: %s sets the header of %s, which is not '
                        'the header there; redo it with ledger.py rule-set' % (rid, file_name(name)))
    return errs


# Conflicts: what `add --rule` must be read against first (docs/ledger.md "Rulebooks"). Kinds in
# the order they print; a rule shows once, under its first kind.
KINDS = ('same key', 'same key elsewhere', 'same topic', 'same topic slug', 'shared words')
WORD_RE = re.compile(r'[a-z][a-z0-9-]{3,}')
STOP = frozenset('''
    about after also always before been being both could does each every from have into just more
    most must never only other over same should some such than that their them then there these
    they this those under when where which while will with would your
'''.split())
WORD_LIMIT = 8     # the shared-words hits shown at most

Hit = collections.namedtuple('Hit', 'kind book rule words score')
Conflicts = collections.namedtuple('Conflicts', 'name scope hits rare digest')


def scope(books, name):
    """The books a change to `name` is read against, in print order: the book itself, then the
    shared one; for the shared book, it and then every repo book."""
    if name == SHARED:
        return [SHARED] + sorted(n for n in books if n != SHARED)
    return [name, SHARED]


def words(text):
    """The content words of `text`: lower case, four or more letters, not in STOP."""
    return {w for w in WORD_RE.findall(text.lower()) if w not in STOP}


def digest(books, name, topic, key, text):
    """The `checked` hash: the books of `scope` as `render` writes them (an absent one as empty)
    and the proposed rule. A change to any of those books, or to the rule, gives another hash."""
    parts = [name, one_line(topic), key or '', one_line(text)]
    for n in scope(books, name):
        parts += [n, render(books[n]) if n in books else '']
    return hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()[:16]


def conflicts(books, name, topic, key, text):
    """`Conflicts` for a proposed rule `key` (`text` under `topic`) in book `name`. Hits, in KINDS
    order, then `scope` order, then file order: the same key in the book (the add replaces it); the
    same key in another book of the scope (an override, or one a shared rule would shadow); each
    rule under the same heading (any case); the same topic slug (the key's part before the first
    `.`); then up to WORD_LIMIT rules sharing content words, by a score that weighs each shared word
    by its rarity over every rulebook. `rare`: the proposal's two rarest words, for a docs search."""
    order = scope(books, name)
    inside = [(n, r) for n in order if n in books for r in books[n].rules]
    topic_low, slug = one_line(topic).lower(), (key or '').split('.', 1)[0]
    tests = (lambda n, r: n == name and r.key == key,
             lambda n, r: n != name and r.key == key,
             lambda n, r: r.topic.lower() == topic_low,
             lambda n, r: r.key.split('.', 1)[0] == slug)
    hits, seen = [], set()
    for kind, test in zip(KINDS, tests):
        for n, r in inside:
            if (n, r.key) not in seen and test(n, r):
                seen.add((n, r.key))
                hits.append(Hit(kind, n, r, (), 0.0))
    every = [words(r.text + ' ' + r.topic) for b in books.values() for r in b.rules]
    df = collections.Counter(w for ws in every for w in ws)
    total = len(every) + 1
    mine = words(text + ' ' + topic)
    scored = []
    for i, (n, r) in enumerate(inside):
        if (n, r.key) in seen:
            continue
        common = mine & words(r.text + ' ' + r.topic)
        if common:
            score = sum(math.log(total / df[w]) + 1 for w in common)
            scored.append((-score, i, Hit(KINDS[4], n, r, tuple(sorted(common)), round(score, 2))))
    hits += [h for _, _, h in sorted(scored, key=lambda s: s[:2])[:WORD_LIMIT]]
    rare = sorted(mine, key=lambda w: (df[w], w))[:2]
    return Conflicts(name, order, hits, rare, digest(books, name, topic, key, text))
