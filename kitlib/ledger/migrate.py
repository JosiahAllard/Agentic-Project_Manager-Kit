"""Migrating the standing digest into the rulebooks (docs/ledger.md "Rulebooks"): a proposed tag per
digest rule, a report the PM edits, and the plan `rule-apply` writes from it.

`propose` reads STANDING.md's rule lines (`- <id>: <rule>` under a `## repo` heading, an optional
deeper heading as the topic) and tags each KEEP, CUT or MERGE (`classify`, `merge`). `render` and
`parse` are the report's text, each rule line

    ? KEEP | repo | key | topic | text | from: id, id  # reason

where the leading `? ` means "not reviewed": the PM deletes it from each line it has judged (and
may change the tag, key, topic or text first). `plan` refuses a report with any `?` line left, a
stale basis (the digest or the retired entries changed since the report), a malformed line or a
bad key; a CUT line is never carried. A MERGE group is the lines tagged `MERGE gN`: the first one
carries the rule (its key, topic and text), the rest only name the ids they merge.

The rules are written by kitlib.ledger.write `add` with a RuleChange (`rule_change`), one log entry
per rule: this module never writes a file. Pure over text, except `uncited` and `status`, which
take parsed logs and books. Nothing here prints or exits.
"""
import collections
import hashlib
import re

from . import rulebook, write
from .fmt import ID, RID_RE

TAGS = ('KEEP', 'CUT', 'MERGE')
MERGE_OVERLAP = 0.5           # content-word Jaccard at which two KEEP lines of a topic are one behaviour

# The cut signals (any one cuts), in the order they are tried; each is (reason, pattern). The stale
# and kit-enforced term lists are the places the PM extends as the kit changes.
STALE_TERMS = ('hard-codes the ledger path', 'interactive prompt')
ENFORCED_TERMS = ('co-author trailer', '--no-verify')
_MONTHS = (r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?'
           r'|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)')
CUTS = (
    ('dated', re.compile(r'\b\d{4}-\d{2}-\d{2}\b|\b%s\.? \d{1,2}\b' % _MONTHS)),
    ('one-off', re.compile(r'\b(?:until|for now|this week|today|tomorrow|currently|temporar\w*'
                           r'|at the moment)\b', re.I)),
    ('names a record', re.compile(r'\b[A-Z]{2,}(?:-[A-Z0-9]+){2,}\b')),
    ('names a priority', re.compile(r'\bp[0-4]\b', re.I)),
    ('spec detail', re.compile(r'\b(?:layer|step|phase|slice|stage) \d', re.I)),
    ('stale term', re.compile('|'.join(re.escape(t) for t in STALE_TERMS), re.I)),
    ('the kit enforces it', re.compile('|'.join(re.escape(t) for t in ENFORCED_TERMS), re.I)),
    ('a fact or history, not a behaviour', re.compile(
        r'\b(?:lives? in|is located|are located|was (?:created|added|decided|built)'
        r'|before the ledger|used to)\b', re.I)),
)
BEHAVIOUR = re.compile(r"\b(?:never|always|only|must|may|refuse[sd]?|ask|stop|report|do not|don't"
                       r"|cannot|should)\b", re.I)

# tag ('KEEP', 'CUT' or 'MERGE'), group ('gN' for a MERGE, else ''), reviewed, repo (a log repo name,
# `all` for the shared book), key, topic, text, ids (the digest line's ruling ids), reason, line (the
# report line, 0 before it is rendered).
Item = collections.namedtuple('Item', 'tag group reviewed repo key topic text ids reason line')
Raw = collections.namedtuple('Raw', 'repo topic text ids')
# One rule to write: the carrier line's fields, and every id merged into it.
Change = collections.namedtuple('Change', 'repo key topic text ids line')

HEADING = re.compile(r'^(#{2,})\s+(.*\S)\s*$')
CITES = re.compile(r'\b(?:%s|Q-\d+)\b' % ID)
DIGEST_LINE = re.compile(r'^- ((?:%s)(?:\s*,\s*(?:%s))*)\s*:\s*(.+?)\s*$' % (ID, ID))
SHARED_NAMES = ('shared', 'all')


def rules_of(digest_text):
    """`[Raw]` of the digest's rule lines, in order. The repo is the `##` heading lower-cased via
    rulebook.normal (`shared` and `all` are `all`; lines before any `##` are `all` too); the topic
    is the deepest heading under it, else `General`."""
    out, repo, topic = [], rulebook.SHARED, None
    for line in digest_text.replace('\r\n', '\n').split('\n'):
        m = HEADING.match(line)
        if m:
            if len(m.group(1)) == 2:
                name = rulebook.normal(m.group(2))
                repo, topic = (rulebook.SHARED if name in SHARED_NAMES else name), None
            else:
                topic = m.group(2)
            continue
        if not line.startswith('- '):
            continue
        d = DIGEST_LINE.match(line)
        if d:
            ids, text = tuple(RID_RE.findall(d.group(1))), d.group(2)
        else:
            ids, text = tuple(RID_RE.findall(line)), line[2:].strip()
        out.append(Raw(repo, topic or 'General', rulebook.one_line(text), ids))
    return out


def classify(text, ids=(), retired=frozenset()):
    """`(tag, reason)`, tag KEEP or CUT, for one rule. Cut at the first signal: every cited id
    retired (a newer entry overturned it), then CUTS in order; else KEEP when the text has a
    behaviour word; else CUT. That nothing newer contradicts a kept rule is not judged here."""
    if ids and all(i in retired for i in ids):
        return 'CUT', 'cites only retired entries'
    for reason, pattern in CUTS:
        if pattern.search(text):
            return 'CUT', reason
    if BEHAVIOUR.search(text):
        return 'KEEP', 'a behaviour that holds for future work'
    return 'CUT', 'no behaviour word'


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def key_of(topic, text):
    """`<topic-slug>.<first three content words>`, a valid rule key."""
    seen = []
    for w in rulebook.WORD_RE.findall(text.lower()):
        if w not in rulebook.STOP and w not in seen:
            seen.append(w)
    return '%s.%s' % (slug(topic) or 'general', '-'.join(seen[:3]) or 'rule')


def groups_of(texts):
    """`[[index, ...]]` of the lines whose content words overlap (Jaccard >= MERGE_OVERLAP), each
    group of two or more in the order of its first line."""
    sets = [rulebook.words(t) for t in texts]
    parent = list(range(len(texts)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            union = sets[i] | sets[j]
            if union and len(sets[i] & sets[j]) / len(union) >= MERGE_OVERLAP:
                parent[find(j)] = find(i)
    found = collections.OrderedDict()
    for i in range(len(texts)):
        found.setdefault(find(i), []).append(i)
    return [g for g in found.values() if len(g) > 1]


def propose(digest_text, retired=frozenset()):
    """`[Item]`, all unreviewed, in digest order: each rule tagged by `classify`, the KEEP lines of a
    (repo, topic) grouped by `groups_of` into MERGE groups (the shortest text first, the group at
    its first member's place), and every key unique within its repo."""
    raws = rules_of(digest_text)
    tagged = [classify(r.text, r.ids, retired) for r in raws]
    tag = [t for t, _ in tagged]
    group = [''] * len(raws)
    where = collections.OrderedDict()
    for i, r in enumerate(raws):
        if tag[i] == 'KEEP':
            where.setdefault((r.repo, r.topic), []).append(i)
    made, lead = 0, {}
    for members in where.values():
        for g in groups_of([raws[i].text for i in members]):
            made += 1
            idx = [members[k] for k in g]
            for i in idx:
                tag[i], group[i] = 'MERGE', 'g%d' % made
            lead[min(idx)] = sorted(idx, key=lambda i: (len(raws[i].text), i))
    order = []
    for i in range(len(raws)):
        if i in lead:
            order += lead[i]
        elif not group[i]:
            order.append(i)
    used = collections.defaultdict(set)
    items = []
    for i in order:
        r = raws[i]
        key = base = key_of(r.topic, r.text)
        n = 1
        while key in used[r.repo]:
            n += 1
            key = '%s-%d' % (base, n)
        used[r.repo].add(key)
        reason = tagged[i][1] if tag[i] != 'MERGE' else 'says one behaviour several ways'
        items.append(Item(tag[i], group[i], False, r.repo, key, r.topic, r.text, r.ids, reason, 0))
    return items


def basis(digest_text, retired):
    """16 hex over what the proposals depend on: the digest text and the retired ids. Rulebooks
    and new log entries are not in it, so a half-applied report can be applied again."""
    data = '\0'.join([digest_text.replace('\r\n', '\n')] + sorted(retired))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]


def uncited(entries, digest_text, since):
    """`[(id, decides)]` of the live entries dated `since` (YYYY-MM-DD) or later that no digest line
    cites: a recent rule that may never have reached the digest."""
    cited = set(RID_RE.findall(digest_text))
    return [(e.id, e.decides or '') for e in entries
            if not e.dead and e.id not in cited and e.date >= since]


def _clean(text):
    return rulebook.one_line(text).replace(' | ', ' / ').replace(' # ', ' / ')


def render(items, basis_hash, source, later=()):
    """The report text. `later` is `uncited`'s result, listed as comments the apply ignores."""
    out = ['# migrate-report', 'Basis: %s' % basis_hash, '# source: %s' % source,
           '# Delete the `?` on each line you have judged; tag it KEEP, CUT or MERGE gN. A CUT line',
           '# is never carried. Whether a newer ruling contradicts a KEEP line is for you to judge.']
    for it in items:
        tag = it.tag + (' ' + it.group if it.group else '')
        line = '%s%s | %s | %s | %s | %s | from: %s' % (
            '' if it.reviewed else '? ', tag, it.repo, it.key, _clean(it.topic), _clean(it.text),
            ', '.join(it.ids))
        out.append(line + (' # ' + _clean(it.reason) if it.reason else ''))
    if later:
        out += ['', '# Log entries newer than the digest that no digest line cites:']
        out += ['# uncited: %s | %s' % (rid, _clean(decides)) for rid, decides in later]
    return '\n'.join(out) + '\n'


LINE_RE = re.compile(r'^(\? )?(\S+)(?: (\S+))? \| ')


def parse(text):
    """`(basis hash or None, [Item], [error lines])` of a report. Comment (`#`) and blank lines are
    skipped; every other line must be `Basis:` or a rule line."""
    found, items, errs = None, [], []
    for n, line in enumerate(text.replace('\r\n', '\n').split('\n'), 1):
        if not line.strip() or line.startswith('#'):
            continue
        if line.startswith('Basis:'):
            found = line[len('Basis:'):].strip()
            continue
        m = LINE_RE.match(line)
        body, _, reason = line.partition(' # ')
        parts = [p.strip() for p in body.split(' | ')]
        if not m or len(parts) < 6 or not parts[-1].startswith('from:'):
            errs.append('line %d: not a report line (TAG | repo | key | topic | text | from: ids): %r'
                        % (n, line))
            continue
        tag, group = m.group(2), m.group(3) or ''
        if tag not in TAGS or (tag == 'MERGE') != bool(re.fullmatch(r'g\d+', group)):
            errs.append('line %d: the tag must be KEEP, CUT or MERGE gN, not %r'
                        % (n, (tag + ' ' + group).strip()))
            continue
        ids = tuple(i.strip() for i in parts[-1][len('from:'):].split(',') if i.strip())
        items.append(Item(tag, group, not m.group(1), parts[1], parts[2], parts[3],
                          ' | '.join(parts[4:-1]), ids, reason.strip(), n))
    return found, items, errs


def plan(report_text, current_basis, split=False):
    """`([Change], [error lines])` for `report_text`. Errors, all found before any write: no or a
    stale Basis; a malformed line; a line still marked `?`; a bad or doubled key; a MERGE group
    across repos; with `split` (ledger_dir set) a shared-book rule, which the writer cannot reach
    there. A CUT line is skipped. One Change per KEEP line and per MERGE group (the group's first
    line carries it, every member's ids join it)."""
    found, items, errs = parse(report_text)
    if found is None:
        errs.append('the report has no Basis line')
    elif found != current_basis:
        errs.append('the report is stale: the digest or the retired entries changed since it was '
                    'made; run migrate-report again')
    todo = [i for i in items if not i.reviewed]
    if todo:
        errs.append('%d line(s) still marked `?` (delete the `?` once judged): lines %s'
                    % (len(todo), ', '.join(str(i.line) for i in todo)))
    changes, seen, lead = [], {}, {}
    for it in items:
        if not it.reviewed or it.tag == 'CUT':
            continue
        if it.tag == 'MERGE' and it.group in lead:
            c = lead[it.group]
            if it.repo != c.repo:
                errs.append('line %d: MERGE %s joins repo %s, but its first line (%d) is repo %s'
                            % (it.line, it.group, it.repo, c.line, c.repo))
            lead[it.group] = c._replace(ids=c.ids + tuple(i for i in it.ids if i not in c.ids))
            changes[changes.index(c)] = lead[it.group]
            continue
        if not rulebook.KEY_RE.match(it.key):
            errs.append('line %d: key %r is not <topic-slug>.<slug> (lower case, digits, - and .)'
                        % (it.line, it.key))
        if (it.repo, it.key) in seen:
            errs.append('line %d: key %s of repo %s is already on line %d'
                        % (it.line, it.key, it.repo, seen[(it.repo, it.key)]))
        seen.setdefault((it.repo, it.key), it.line)
        if not it.topic or not it.text:
            errs.append('line %d: the topic and the text must not be empty' % it.line)
        if split and rulebook.normal(it.repo) == rulebook.SHARED:
            errs.append('line %d: shared rule %s: with ledger_dir set no writer path reaches all.md '
                        'yet; leave it for later' % (it.line, it.key))
        if CITES.search(it.text):
            errs.append("line %d: rule %s cites a ruling or question id in its text; the id belongs in "
                        "the entry's question only, so say the reason instead" % (it.line, it.key))
        c = Change(it.repo, it.key, it.topic, it.text, tuple(it.ids), it.line)
        changes.append(c)
        if it.tag == 'MERGE':
            lead[it.group] = c
    return changes, errs


def status(books, change):
    """'new', 'done' (the target book already has this key with this text) or 'conflict' (it has
    the key with other text: the writer would replace it, so apply refuses)."""
    book = books.get(rulebook.target(books, change.repo))
    old = next((r for r in book.rules if r.key == change.key), None) if book else None
    if old is None:
        return 'new'
    return 'done' if old.text == rulebook.one_line(change.text) else 'conflict'


def rule_change(folder, change, books):
    """The RuleChange that sets `change` through kitlib.ledger.write `add`, its `checked` hash
    taken now from `books` (freshly loaded for each rule). The one place a shared-book writer
    would plug in."""
    name = rulebook.target(books, change.repo)
    return write.RuleChange(folder, 'set', change.key, change.topic, change.text,
                            rulebook.digest(books, name, change.topic, change.key, change.text))


def entry_text(change, source_name):
    """`(q, a, decides)` of the log entry that carries `change`: the digest ids go in the question,
    never the Decides (a Decides mention counts as a citation and would pin them against retire)."""
    q = 'Carry the standing rule from %s (%s) into the rulebook?' % (
        source_name, ', '.join(change.ids) or 'no ids')
    return q, 'Yes: %s' % change.text, change.text
