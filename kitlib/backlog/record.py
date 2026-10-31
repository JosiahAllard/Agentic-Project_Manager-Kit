"""One record: parse, the schema, record-local validation, rendering.

A record is a `key: value` header, one blank line, then a free Markdown body. The header ends at
the first whitespace-only line. Nothing is defaulted and nothing is inferred.
"""
import collections
import datetime
import re

from . import conditions as condmod
from . import scope as scopemod
from .errors import Usage

Finding = collections.namedtuple('Finding', 'check path line message')

# Check names (`check` prints one line per check).
SCHEMA = 'schema'
BLOCKED_ON = 'blocked_on set'
ID_FILENAME = 'id/filename'
ITEM_TARGETS = 'item: targets'
PIN_UNIQUENESS = 'pin uniqueness'
UNREADABLE = 'unreadable'

ID_GRAMMARS = {'upper': r'[A-Z][A-Z0-9-]*', 'mixed': r'[A-Za-z0-9][A-Za-z0-9.-]*'}


class Rules(object):
    """The repo-specific parts of the schema: the id grammar (`upper` or `mixed`) and the phase
    names a `phase:` blocker may name (`.kit.json` "backlog")."""

    def __init__(self, id_grammar='upper', phases=()):
        if id_grammar not in ID_GRAMMARS:
            raise ValueError('unknown id grammar %r' % id_grammar)
        self.id_grammar = id_grammar
        self.phases = tuple(phases)
        self.id_pattern = ID_GRAMMARS[id_grammar]
        self.id_re = re.compile('^%s$' % self.id_pattern)
        self.displaced_re = re.compile(r'^(%s) rank (\d+|-) priority ([1-5]|-)$' % self.id_pattern)

    def id_error(self, v):
        if self.id_re.match(v or ''):
            return None
        return '`%s` is not an id - `%s`' % (v, self.id_pattern)


# --- the blocked_on closed set --------------------------------------------------------------------

BLOCKED_ON_PLAIN = ('owner', 'live-client', 'deliberate', 'unmapped', '-')
BLOCKED_ON_PREFIXES = ('item:', 'tool:', 'upstream:', 'phase:')
HELD_ORDER = ('owner', 'item', 'phase', 'tool', 'live-client', 'upstream', 'deliberate', 'unmapped')


def blocked_on_error(v, rules):
    """None if `v` is in the closed set, else why not. A blocker the set cannot express is filed
    as its own item and blocked on `item:<ID>`."""
    if v in BLOCKED_ON_PLAIN:
        return None
    for prefix in BLOCKED_ON_PREFIXES:
        if v.startswith(prefix):
            name = v[len(prefix):]
            if not name:
                return '`%s` names nothing after `%s`' % (v, prefix)
            if prefix == 'item:':
                if not rules.id_re.match(name):
                    return ('`%s` is not an item id - `item:<ID>`, an id is `%s`'
                            % (v, rules.id_pattern))
            elif prefix == 'phase:':
                if name not in rules.phases:
                    return ('`%s` is not a phase in .kit.json "backlog".phases (%s)'
                            % (v, ', '.join(rules.phases) or 'none configured'))
            elif name != name.strip():
                return '`%s` has leading or trailing whitespace after `%s`' % (v, prefix)
            return None
    return ('`%s` is not in the closed set: %s, %s' % (
        v, ', '.join(BLOCKED_ON_PLAIN), ', '.join(p + '<name>' for p in BLOCKED_ON_PREFIXES)))


# --- hold citations (a hold cites the ruling, question or item it waits on, or it is void) --------

UNCITED_KINDS = ('deliberate', 'owner')     # members that name nothing they wait on
CITE_RE = re.compile(r'(?<![A-Za-z0-9-])(?:R-[0-9]{4,}|[QTA]-[0-9]{3,}|item:[A-Za-z0-9][A-Za-z0-9.-]*)')
BLOCK_NOTE = 'Blocked on '                  # the body line plan_block writes for `--reason`


def cites(text):
    """The R-nnnn, Q-nnn, T-nnn, A-nnn and item:<ID> citations in `text`, in order."""
    return CITE_RE.findall(text or '')


def hold_citations(rec):
    """What a record's hold cites: ids in `q`, `owner_ask`, the `blocked_on` members and the body
    lines `block --reason` writes (`Blocked on ...`). `hold_ruling` and `resume` are not read:
    they are the rulings that released a hold or unparked the record, not why it is held, and
    `ref` is where the record came from."""
    out = []
    for key in ('q', 'owner_ask', 'blocked_on'):
        for v in rec.all(key):
            out.extend(cites(v))
    for line in (rec.body or '').split('\n'):
        if line.startswith(BLOCK_NOTE):
            out.extend(cites(line))
    return out


def block_citation_error(members, reason=None, owner_ask=None):
    """None, or why `block --on <members>` may not be written: a `deliberate` or `owner` member
    names nothing it waits on, so `--reason` or `--owner-ask` must cite an R-nnnn, Q-nnn, T-nnn, A-nnn
    or item:<ID> (an uncited hold is void; `block` takes no ruling flag, so the reason carries it)."""
    bare = [m for m in members if m in UNCITED_KINDS]
    if not bare or cites(reason) or cites(owner_ask):
        return None
    return ('`block --on %s` cites nothing it waits on: name the ruling, question or item and its '
            'end condition in `--reason` (or `--owner-ask`), e.g. `--reason "R-nnnn: until <end '
            'condition>"`, a Q-nnn, a T-nnn, an A-nnn or an item:<ID> (an uncited hold is void)'
            % ' --on '.join(bare))


def blocker_kind(member):
    """The held-breakdown group of a `blocked_on` member, or None for `-` or an unknown one."""
    for prefix in BLOCKED_ON_PREFIXES:
        if member.startswith(prefix):
            return prefix[:-1]
    return member if member in HELD_ORDER else None


# --- the schema -----------------------------------------------------------------------------------

ONE, AT_MOST_ONE, ONE_PLUS, ANY = '1', '0-1', '1+', '0+'
# `priority: -1` once marked a record awaiting the owner; `blocked_on: owner` is the only owner
# hold now, and -1 is refused with this pointer (docs/backlog.md "Fields").
RETIRED_PRIORITY = '-1'
RETIRED_PRIORITY_WHY = ('`priority: -1` is retired: a record awaiting the owner is `block ID --on '
                        'owner --owner-ask "<the question>"`')
RULING_RE = re.compile(r'^R-[0-9]{4,}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
QUESTION_RE = re.compile(r'^Q-[0-9]{3,}$')
KEY_RE = re.compile(r'^[a-z][a-z0-9_]*$')
SESSION_RE = re.compile(r'^[a-z][a-z0-9-]*$')
RESOURCE_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


def is_band(v):
    """True iff `v` is a band `1`-`5` (1 the top). `-` is not a band and has no rank."""
    return v is not None and bool(re.match(r'^[1-5]$', v))


def _free(v, rules):
    return None if v.strip() else 'an empty value'


def _id(v, rules):
    return rules.id_error(v)


def _priority(v, rules=None):
    if v == '-' or is_band(v):
        return None
    if v == RETIRED_PRIORITY:
        return RETIRED_PRIORITY_WHY
    return ('`%s` is not a priority - a band `1`-`5` (1 the top), or `-` for not prioritised'
            % v)


def _priority_by(v, rules=None):
    if v in ('owner', 'pm', 'agent', '-'):
        return None
    return '`%s` is not `owner`, `pm`, `agent` or `-`' % v


def _ruling(v, rules=None):
    return None if RULING_RE.match(v) else '`%s` is not a ruling id - `R-nnnn`' % v


def _question(v, rules=None):
    return None if QUESTION_RE.match(v) else '`%s` is not an owner question id - `Q-nnn`' % v


def _effort(v, rules=None):
    return None if v in ('s', 'm', 'l', '-') else '`%s` is not `s`, `m`, `l` or `-`' % v


STATES = ('open', 'blocked', 'parked', 'pulled')


def _state(v, rules=None):
    if v in STATES:
        return None
    return ('`%s` is not `open`, `blocked`, `parked` or `pulled`. There is no `closed`: a closed '
            'item has no record' % v)


def _date(v, rules=None):
    return None if DATE_RE.match(v) else '`%s` is not a date (YYYY-MM-DD)' % v


def _path(v, rules=None):
    return scopemod.path_shape_error(v)


def _scope(v, rules=None):
    return scopemod.scope_member_error(v)


def _displaced(v, rules):
    if v == '-' or rules.displaced_re.match(v):
        return None
    return ('`%s` is not a displaced line - `<ID> rank <n> priority <p>`, or `-`; written only '
            'by `pull`' % v)


def _pin(v, rules=None):
    return None if re.match(r'^[1-9][0-9]*$', v) else '`%s` is not a pin - a positive integer' % v


def _session(v, rules=None):
    if SESSION_RE.match(v):
        return None
    return '`%s` is not a session name - lower-case `[a-z][a-z0-9-]*`' % v


def _uses(v, rules=None):
    """A resource the record needs while it is pulled (docs/backlog.md "Resources"). Only the
    grammar: which names exist is the repository's `.kit.json`, which `check` reads."""
    if RESOURCE_RE.match(v):
        return None
    return '`%s` is not a resource name - lower-case `[a-z0-9][a-z0-9-]*`' % v


def _review(v, rules=None):
    """`<source> <YYYY-MM-DD>`: who marked the record for review, and when (docs/backlog.md,
    "Records")."""
    source, _, date = v.partition(' ')
    if not SESSION_RE.match(source) or not date or ' ' in date:
        return ('`%s` is not a review mark - `<source> <YYYY-MM-DD>`, the source lower-case '
                '`[a-z][a-z0-9-]*`' % v)
    return _date(date)


# The one spelling of an answer's `at`: no `Z`, no fractional seconds, no compact form, so a
# repeat filing of one answer matches its line exactly (plan.py plan_answer).
ANSWER_AT_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                          r'[+-][0-9]{2}:[0-9]{2}$')


def _answer(v, rules=None):
    """`<Q-nnn> <at>`: the owner's answer to that question, filed by the Action Hub at `at`,
    exactly `YYYY-MM-DDTHH:MM:SS+HH:MM` (ANSWER_AT_RE, then a real moment; docs/backlog.md
    "Records"). One line per question (_cross_field)."""
    qid, _, at = v.partition(' ')
    shape = ('`%s` is not an answer line - `<Q-nnn> <at>`, `at` a timestamp with its offset '
             '(YYYY-MM-DDTHH:MM:SS+HH:MM)' % v)
    if not QUESTION_RE.match(qid) or not ANSWER_AT_RE.match(at):
        return shape
    try:
        when = datetime.datetime.fromisoformat(at)
    except ValueError:
        return shape
    return shape if when.tzinfo is None else None


def _until(v, rules):
    """`<member> <condition>`: the end condition of one `blocked_on` member, written by `block
    --on M --until C` (kitlib/backlog/conditions.py says which conditions a program checks; any
    other text is kept as free text). The member is one word, in the closed set and not `-`; one
    line per member, and only a member the record is blocked on (_cross_field)."""
    member, cond = condmod.split(v)
    if not member or not cond.strip():
        return '`%s` is not an until line - `<member> <condition>`' % v
    if member == '-':
        return '`-` is not a blocker, so nothing ends on `%s`' % cond
    return blocked_on_error(member, rules)


def until_lines(rec):
    """[(member, condition)] of the record's `until` lines, in order."""
    return [condmod.split(v) for v in rec.all('until')]


def prune_until(pairs):
    """`pairs` without the `until` lines of a member that is not in their `blocked_on`: a write
    that clears a blocker (unblock, an answer, a close's dependents, set) drops its condition
    with it."""
    members = set(values(pairs, 'blocked_on'))
    return [(k, v) for k, v in pairs if k != 'until' or condmod.split(v)[0] in members]


def answered(rec):
    """{question id: at} of the record's `answer:` lines (a malformed line is left out)."""
    out = {}
    for v in rec.all('answer'):
        if _answer(v) is None:
            qid, _, at = v.partition(' ')
            out.setdefault(qid, at)
    return out


# key -> (cardinality, validator), in the order a new key is inserted.
FIELDS = collections.OrderedDict([
    ('id', (ONE, _id)),
    ('area', (ONE, _free)),
    ('priority', (ONE, _priority)),
    ('priority_by', (ONE, _priority_by)),
    ('priority_per', (ANY, _ruling)),
    ('priority_ruling', (AT_MOST_ONE, _ruling)),
    ('effort', (ONE, _effort)),
    ('state', (ONE, _state)),
    ('pulled_at', (AT_MOST_ONE, _date)),
    ('pulled_by', (AT_MOST_ONE, _free)),
    ('blocked_on', (ONE_PLUS, None)),            # the closed set, under its own check name
    ('until', (ANY, _until)),
    ('hold_ruling', (AT_MOST_ONE, _ruling)),
    ('q', (AT_MOST_ONE, _question)),
    ('parked_at', (AT_MOST_ONE, _date)),
    ('resume', (AT_MOST_ONE, _ruling)),
    ('review', (AT_MOST_ONE, _review)),
    ('answer', (ANY, _answer)),
    ('scope', (ONE_PLUS, _scope)),
    ('uses', (ANY, _uses)),
    ('files', (ANY, _path)),
    ('files_at', (AT_MOST_ONE, _free)),
    ('requires', (ANY, _free)),
    ('expect', (ANY, _free)),
    ('owner_ask', (AT_MOST_ONE, _free)),
    ('preempt', (AT_MOST_ONE, _free)),
    ('preempt_by', (AT_MOST_ONE, _free)),
    ('preempt_at', (AT_MOST_ONE, _date)),
    ('displaced', (ANY, _displaced)),
    ('pin', (AT_MOST_ONE, _pin)),
    ('session', (AT_MOST_ONE, _session)),
    ('ref', (AT_MOST_ONE, _free)),
    ('summary', (ONE, _free)),
])
SINGLE_VALUED = frozenset(k for k, (c, _) in FIELDS.items() if c in (ONE, AT_MOST_ONE))
REQUIRED = frozenset(k for k, (c, _) in FIELDS.items() if c in (ONE, ONE_PLUS))
PRIORITY_KEYS = ('priority', 'priority_by', 'priority_per', 'priority_ruling')
PREEMPT_KEYS = ('preempt', 'preempt_by', 'preempt_at', 'displaced')
PULL_KEYS = ('pulled_at', 'pulled_by')
PARK_KEYS = ('q', 'parked_at')


def parked_days(rec, today):
    """Whole days since `parked_at` (`today` YYYY-MM-DD); None without a readable date."""
    at = rec.get('parked_at')
    if at is None or _date(at) is not None:
        return None
    try:
        return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(at)).days
    except ValueError:
        return None


# --- the record -----------------------------------------------------------------------------------

class Record(object):
    """One parsed record. `structural` holds why it is unreadable; an unreadable record is never
    validated and never dropped."""

    def __init__(self, path, stem):
        self.path = path
        self.stem = stem
        self.entries = []        # [(line_no, key, value)] in file order
        self.body = ''
        self.structural = []
        self.text = None         # the text it was parsed from

    @property
    def readable(self):
        return not self.structural

    def all(self, key):
        return [v for _, k, v in self.entries if k == key]

    def get(self, key):
        vs = self.all(key)
        return vs[0] if vs else None

    def has(self, key):
        return any(k == key for _, k, _v in self.entries)

    def line_of(self, key):
        for n, k, _ in self.entries:
            if k == key:
                return n
        return 1

    @property
    def id(self):
        return self.get('id') or self.stem

    def pairs(self):
        return [(k, v) for _, k, v in self.entries]

    def __repr__(self):
        return 'Record(%r, readable=%r)' % (self.path, self.readable)


def parse(path, text, stem):
    """Split `text` into header and body. Structural problems land in `structural`."""
    rec = Record(path, stem)
    rec.text = text
    if not text.strip():
        rec.structural.append(Finding(UNREADABLE, path, 1, 'the file is empty'))
        return rec
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    end = next((i for i, line in enumerate(lines) if not line.strip()), len(lines))
    if end == 0:
        rec.structural.append(Finding(UNREADABLE, path, 1, 'the header is empty - a record '
                                      'starts with `id: <ID>` on line 1'))
        return rec
    for i in range(end):
        line, n = lines[i], i + 1
        if ': ' not in line:
            rec.structural.append(Finding(UNREADABLE, path, n, 'no `: ` separator - the header '
                                          'is one `key: value` per line and the first blank line '
                                          'ends it'))
            continue
        key, value = line.split(': ', 1)
        if not KEY_RE.match(key):
            rec.structural.append(Finding(UNREADABLE, path, n, '`%s` is not a header key - '
                                          'lower-case `[a-z][a-z0-9_]*`' % key))
            continue
        rec.entries.append((n, key, value))
    rec.body = '\n'.join(lines[end + 1:]) if end < len(lines) else ''
    return rec


def unreadable(path, stem, why):
    """A record that could not even be decoded: counted and named like any unreadable one."""
    rec = Record(path, stem)
    rec.structural.append(Finding(UNREADABLE, path, 1, why))
    return rec


def validate(rec, rules):
    """Every finding about one record on its own."""
    if not rec.readable:
        return []
    out = []
    add = out.append
    path = rec.path
    seen = {}
    for n, key, value in rec.entries:
        if key not in FIELDS:
            add(Finding(SCHEMA, path, n, 'unknown key `%s` - the key set is fixed' % key))
            continue
        if not value.strip():
            add(Finding(SCHEMA, path, n, '`%s` has an empty value' % key))
            continue
        if key in SINGLE_VALUED and key in seen:
            add(Finding(SCHEMA, path, n, '`%s` is single-valued and is repeated (first on line '
                        '%d)' % (key, seen[key])))
            continue
        seen.setdefault(key, n)
        check = FIELDS[key][1]
        err = check(value, rules) if check is not None else None
        if err is not None:
            add(Finding(SCHEMA, path, n, '`%s`: %s' % (key, err)))
    for key in sorted(REQUIRED):
        if not rec.has(key):
            add(Finding(SCHEMA, path, 1, '`%s` is missing. Nothing is defaulted' % key))
    for key, (card, _) in FIELDS.items():
        if card in (ONE_PLUS, ANY):
            vals = rec.all(key)
            for v in sorted({v for v in vals if vals.count(v) > 1}):
                add(Finding(SCHEMA, path, rec.line_of(key),
                            '`%s: %s` appears more than once - a set holds it once' % (key, v)))
    for n, key, value in rec.entries:
        if key == 'blocked_on' and value.strip():
            err = blocked_on_error(value, rules)
            if err is not None:
                add(Finding(BLOCKED_ON, path, n, err))
    out.extend(_cross_field(rec))
    rid = rec.get('id')
    if rid is not None and rules.id_re.match(rid) and rid != rec.stem:
        add(Finding(ID_FILENAME, path, rec.line_of('id'),
                    '`id: %s` in a file named `%s.md` - the filename is the id' % (rid, rec.stem)))
    return out


def _both_or_neither(rec, key, condition, because):
    here = rec.has(key)
    if condition and not here:
        return [Finding(SCHEMA, rec.path, 1, '`%s` is required on %s' % (key, because))]
    if here and not condition:
        return [Finding(SCHEMA, rec.path, rec.line_of(key), '`%s` is set, and this is not %s'
                        % (key, because))]
    return []


def _cross_field(rec):
    out = []
    add = out.append
    path = rec.path
    priority, by = rec.get('priority'), rec.get('priority_by')
    state, blockers, scopes = rec.get('state'), rec.all('blocked_on'), rec.all('scope')
    if priority is not None and by is not None:
        if priority == '-' and by != '-':
            add(Finding(SCHEMA, path, rec.line_of('priority_by'), '`priority: %s` is not a band, '
                        'so `priority_by` must be `-`, not `%s`' % (priority, by)))
        if is_band(priority) and by == '-':
            add(Finding(SCHEMA, path, rec.line_of('priority_by'), '`priority: %s` was set by '
                        'somebody - `priority_by` is `owner`, `pm` or `agent`' % priority))
    if rec.has('priority_per') and not is_band(priority):
        add(Finding(SCHEMA, path, rec.line_of('priority_per'), '`priority_per` cites the '
                    'decision a band was taken from, and this record carries no band'))
    if rec.has('priority_ruling') and by == 'agent':
        add(Finding(SCHEMA, path, rec.line_of('priority_ruling'), '`priority_ruling` beside '
                    '`priority_by: agent` - the id is stale; `set` drops it'))
    if state is not None and blockers:
        if state == 'blocked' and blockers == ['-']:
            add(Finding(BLOCKED_ON, path, rec.line_of('blocked_on'),
                        '`state: blocked` with `blocked_on: -` says blocked on nothing'))
        if state in ('open', 'parked', 'pulled') and '-' not in blockers:
            add(Finding(BLOCKED_ON, path, rec.line_of('state'), '`state: %s` with `blocked_on: '
                        '%s` - an item held on something is `state: blocked`'
                        % (state, ', '.join(blockers))))
    if '-' in blockers and len(blockers) > 1:
        add(Finding(BLOCKED_ON, path, rec.line_of('blocked_on'), '`blocked_on: -` means not '
                    'blocked, so it cannot sit beside other members'))
    out.extend(_both_or_neither(rec, 'owner_ask', 'owner' in blockers, 'an item awaiting the '
                                'owner - `blocked_on: owner`'))
    for key in PULL_KEYS:
        out.extend(_both_or_neither(rec, key, state == 'pulled', 'an item with `state: pulled`'))
    out.extend(_both_or_neither(rec, 'files_at', rec.has('files'), 'a record with `files`'))
    for key in ('preempt_by', 'preempt_at'):
        out.extend(_both_or_neither(rec, key, rec.has('preempt'), 'a record with `preempt`'))
    if rec.has('displaced') and not rec.has('preempt'):
        add(Finding(SCHEMA, path, rec.line_of('displaced'), '`displaced` without `preempt` - '
                    'written only by an out-of-order `pull`'))
    seen_q = set()
    for n, key, value in rec.entries:
        qid = value.partition(' ')[0]
        if key == 'answer' and _answer(value) is None:
            if qid in seen_q:
                add(Finding(SCHEMA, path, n, '`answer` holds one line per question, and %s has '
                            'more than one; a re-answer replaces its line' % qid))
            seen_q.add(qid)
    seen_member = set()
    for n, key, value in rec.entries:
        if key != 'until':
            continue
        member, cond = condmod.split(value)
        if not member or not cond.strip():
            continue                    # the shape is _until's finding
        if member not in blockers:
            add(Finding(SCHEMA, path, n, '`until: %s` ends `%s`, and the record is not blocked on '
                        'it; `unblock` drops its line' % (value, member)))
        if member in seen_member:
            add(Finding(SCHEMA, path, n, '`until` holds one line per member, and `%s` has more '
                        'than one; a re-block with `--until` replaces its line' % member))
        seen_member.add(member)
        why = condmod.owner_error(member, cond)
        if why:
            add(Finding(SCHEMA, path, n, '`until: %s`: %s' % (value, why)))
    if scopemod.NONE in scopes and len(scopes) > 1:
        add(Finding(SCHEMA, path, rec.line_of('scope'), '`scope: none` touches no tracked file, '
                    'so it cannot sit beside other members'))
    return out


# --- writing --------------------------------------------------------------------------------------

def _control_char(text):
    """True when `text` holds a control character other than a tab."""
    return any((ord(ch) < 32 and ch != '\t') or ord(ch) == 127 for ch in str(text))


def render(pairs, body):
    """`key: value` header, one blank line, body. The only place a record is serialised.

    Usage when a header key or value holds a control character: a line break would split one
    header line into two (`summary: x` + an injected `pin: 1`), and no header value is multi-line.
    A tab is let through: it splits no line, and a record that already holds one in a pasted
    summary must still take writes it did not cause (a path with one is still a new FAIL finding
    at store.check_change, through scope.path_shape_error). The body is not checked."""
    for k, v in pairs:
        if _control_char(k) or _control_char(v):
            raise Usage('`%s: %r` holds a line break or other control character; a header value '
                        'is one line' % (k, v))
    head ='\n'.join('%s: %s' % (k, v) for k, v in pairs)
    body = (body or '').rstrip('\n')
    return head + '\n\n' + (body + '\n' if body else '')


def apply_changes(pairs, changes):
    """`changes` is `[(key, [values])]`. A present key is replaced in place (first position); a
    new one goes after the last key that comes before it in FIELDS order. `[]` removes a key."""
    order = list(FIELDS)
    out = list(pairs)
    for key, vals in changes:
        first = next((i for i, (k, _) in enumerate(out) if k == key), None)
        out = [(k, v) for k, v in out if k != key]
        if first is None:
            rank = order.index(key) if key in order else len(order)
            after = [i for i, (k, _) in enumerate(out) if k in order and order.index(k) < rank]
            first = (max(after) + 1) if after else len(out)
        out[first:first] = [(key, v) for v in vals]
    return out


def values(pairs, key):
    return [v for k, v in pairs if k == key]


def is_owner_held(rec):
    """Awaiting the owner: `blocked_on: owner`."""
    return 'owner' in rec.all('blocked_on')


def is_owner_prioritised(rec):
    """The owner's price: `priority_by: owner`, or `pm` (priced on the owner's ruling)."""
    return rec.get('priority_by') in ('owner', 'pm')


def owner_hold_label(rec):
    return 'blocked_on: owner'
