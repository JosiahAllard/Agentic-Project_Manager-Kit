"""One record: parse, the schema, record-local validation, rendering (ported from an earlier
tools/backlog.py, the keys the kit dropped removed).

A record is a `key: value` header, one blank line, then a free Markdown body. The header ends at
the first whitespace-only line. Nothing is defaulted and nothing is inferred.
"""
import collections
import datetime
import re

from . import scope as scopemod

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


def blocker_kind(member):
    """The held-breakdown group of a `blocked_on` member, or None for `-` or an unknown one."""
    for prefix in BLOCKED_ON_PREFIXES:
        if member.startswith(prefix):
            return prefix[:-1]
    return member if member in HELD_ORDER else None


# --- the schema -----------------------------------------------------------------------------------

ONE, AT_MOST_ONE, ONE_PLUS, ANY = '1', '0-1', '1+', '0+'
OWNER_PRIORITY = '-1'
RULING_RE = re.compile(r'^R-[0-9]{4,}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
QUESTION_RE = re.compile(r'^Q-[0-9]{3,}$')
KEY_RE = re.compile(r'^[a-z][a-z0-9_]*$')
SESSION_RE = re.compile(r'^[a-z][a-z0-9-]*$')


def is_band(v):
    """True iff `v` is a band `1`-`5` (1 the top). `-` and `-1` are not bands and have no rank."""
    return v is not None and bool(re.match(r'^[1-5]$', v))


def _free(v, rules):
    return None if v.strip() else 'an empty value'


def _id(v, rules):
    return rules.id_error(v)


def _priority(v, rules=None):
    if v in ('-', OWNER_PRIORITY) or is_band(v):
        return None
    return ('`%s` is not a priority - a band `1`-`5` (1 the top), `-1` where the judgement of '
            'the owner is required, or `-` for not prioritised' % v)


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
    ('hold_ruling', (AT_MOST_ONE, _ruling)),
    ('q', (AT_MOST_ONE, _question)),
    ('parked_at', (AT_MOST_ONE, _date)),
    ('resume', (AT_MOST_ONE, _ruling)),
    ('scope', (ONE_PLUS, _scope)),
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
        if priority in ('-', OWNER_PRIORITY) and by != '-':
            add(Finding(SCHEMA, path, rec.line_of('priority_by'), '`priority: %s` is not a band, '
                        'so `priority_by` must be `-`, not `%s`' % (priority, by)))
        if is_band(priority) and by == '-':
            add(Finding(SCHEMA, path, rec.line_of('priority_by'), '`priority: %s` was set by '
                        'somebody - `priority_by` is `owner`, `pm` or `agent`' % priority))
        if by == 'pm' and not rec.has('priority_ruling'):
            add(Finding(SCHEMA, path, rec.line_of('priority_by'), '`priority_by: pm` without '
                        '`priority_ruling` - a PM price is written only with `--ruling R-nnnn`'))
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
    awaits = 'owner' in blockers or priority == OWNER_PRIORITY
    out.extend(_both_or_neither(rec, 'owner_ask', awaits, 'an item awaiting the owner - '
                                '`blocked_on: owner` or `priority: -1`'))
    for key in PULL_KEYS:
        out.extend(_both_or_neither(rec, key, state == 'pulled', 'an item with `state: pulled`'))
    out.extend(_both_or_neither(rec, 'files_at', rec.has('files'), 'a record with `files`'))
    for key in ('preempt_by', 'preempt_at'):
        out.extend(_both_or_neither(rec, key, rec.has('preempt'), 'a record with `preempt`'))
    if rec.has('displaced') and not rec.has('preempt'):
        add(Finding(SCHEMA, path, rec.line_of('displaced'), '`displaced` without `preempt` - '
                    'written only by an out-of-order `pull`'))
    if scopemod.NONE in scopes and len(scopes) > 1:
        add(Finding(SCHEMA, path, rec.line_of('scope'), '`scope: none` touches no tracked file, '
                    'so it cannot sit beside other members'))
    return out


# --- writing --------------------------------------------------------------------------------------

def render(pairs, body):
    """`key: value` header, one blank line, body. The only place a record is serialised."""
    head = '\n'.join('%s: %s' % (k, v) for k, v in pairs)
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
    """Awaiting the owner: `blocked_on: owner` or `priority: -1`."""
    return rec.get('priority') == OWNER_PRIORITY or 'owner' in rec.all('blocked_on')


def is_owner_prioritised(rec):
    """The owner's price: `priority_by: owner`, or `pm` (priced on his ruling)."""
    return rec.get('priority_by') in ('owner', 'pm')


def owner_hold_label(rec):
    return 'priority: -1' if rec.get('priority') == OWNER_PRIORITY else 'blocked_on: owner'
