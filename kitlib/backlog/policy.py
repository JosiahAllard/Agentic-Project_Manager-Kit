"""Who may run a backlog command: a command sorted into kitlib.backlog.roles ACTIONS rows, and the
refusal for a role that may not take one (docs/backlog.md "Roles"). roles.py is the owner's map;
this module reads it and never widens it.

Two places ask. tools/backlog.py asks for its own session (cli_role: the session mark; unmarked is
the owner) before a write plans, with the record the write reads. The guard asks for a subagent
(its role is the hook payload's agent type, so a PM_KIT_ROLE the command sets changes nothing),
from the command's words (read_argv) and, only when a builder's own pull could decide, the record
as the store holds it locally.

  sort(verb, given, rec)        the row names a command takes, after each row's `over` narrowing
  refusal(role, rows, facts)    the text naming the first row `role` may not take, or None
  own_pull(rec, branch)         the OWN_PULL fact: `rec` is pulled by `branch`
  cli_role(environ)             the role of the session tools/backlog.py runs in
  given_of(args)                what tools/backlog.py's parsed arguments give, for sort
  read_argv(words)              what a backlog.py command line gives (the guard's reader), as an Argv
  check(args, ctx, snap, environ)
                                tools/backlog.py's check: raises Refusal when its role may not
                                take a row; else the rows it sorted
  may(name, environ)            True when the session tools/backlog.py runs in may take the row
                                `name`: a row the plan finds (pull-collision, set-widen-collision)
  top_only(rows)                the rows of `rows` only the owner and the PM may take, which a
                                plan run without the role check refuses (kitlib/backlog/plan.py)

`given` is a dict: 'fields' (the set fields given or unset, `body` for --body-file), 'priority'
(each priority value given; '-' for --unset priority), 'priority_by' and 'blocked_on' (the values
given), 'owner_ask', 'apply' (each true when given), 'on' (the --on members), and 'unknown' (true
when a word could not be read: every row the verb can take counts). A verb of None (not read)
counts as every row but `read`.

A pull's collision with another branch's live pull, and a pulled record's scope widened into one,
are found by the plan (they need the offer and git), not here: the plan refuses them unless the
caller says the role may take the row (`may`), and the Change names the row for the audit log.
"""
import collections
import re

from . import record as rmod
from . import roles
from .errors import Refusal

READS = roles.ACTIONS['read'].verbs
UNKNOWN = 'unknown'
# read_argv's readable mark for a word the shell passes as one word whose text the line does not
# show (a double-quoted variable): read only as the value of an option whose value sorts nothing
# (not DECIDING), which then reads as UNREAD; anywhere else it is unreadable
ONE = 'one'
DECIDING = ('--priority', '--unset', '--on', '--priority-by', '--priority_by', '--blocked-on',
            '--blocked_on')
UNREAD = object()


def _held(rec):
    return rec is not None and rmod.is_owner_held(rec)


def _owner_priced(rec):
    return rec is not None and rec.get('priority_by') in roles.PRICED_BY_OWNER


def _owner_blocked(rec):
    """`rec` is blocked on the owner, or not read (None), so it could be."""
    return rec is None or 'owner' in rec.all('blocked_on')


def _price_rows(values, rec):
    """The rows a `set` of priority to each of `values` takes on `rec` (None: not read, so the
    strictest: set-price-owner). A retired `-1` sorts as any low price, so the plan's refusal
    (pointing at `block --on owner`) is what the caller reads, not a role refusal."""
    out = set()
    for value in values:
        if value in roles.PRICE_TOP or _owner_priced(rec) or _owner_blocked(rec):
            out.add('set-price-owner')
        else:
            out.add('set-price')
    return out


# The price fields beside `priority`: on an owner- or PM-priced record they are the owner's price.
PRICE_FIELDS = ('priority_by', 'priority_per')
# Fields a `set` may only clear (kitlib/backlog/plan.py UNWRITTEN) whose clearing is the owner's
# and the PM's: `resume` ranks the record in the offer. Clearing `priority_ruling` or
# `hold_ruling` changes no rank, so it is set-other.
OWNER_CLEARS = ('resume',)


def _price_field_row(key, given, rec):
    """The row a `set` of `key` (a PRICE_FIELDS one) takes: set-price-owner when it changes an
    owner's or the PM's price (the record PRICED_BY_OWNER, or not read) or names one
    (`priority_by` owner or pm, or a value not read), else set-other."""
    if rec is None or _owner_priced(rec):
        return 'set-price-owner'
    if key == 'priority_by' and any(v is UNREAD or v in roles.PRICED_BY_OWNER
                                    for v in given.get('priority_by') or ()):
        return 'set-price-owner'
    return 'set-other'


def top_only(rows):
    """The rows of `rows` (sort's names) only the owner and the PM may take (roles.TOP, with no
    `when` grant), in order: a plan run without the role check refuses a write that takes one."""
    top = set(roles.TOP)
    return [n for n in rows if roles.ACTIONS[n].allowed and not roles.ACTIONS[n].when
            and set(roles.ACTIONS[n].allowed) <= top]


def _verb_rows(verb):
    return [name for name, row in roles.ACTIONS.items() if verb in row.verbs]


def _narrow(names):
    """`names` without each row another of them is `over`, in ACTIONS order."""
    over = set()
    for name in names:
        over.update(roles.ACTIONS[name].over)
    return [n for n in roles.ACTIONS if n in names and n not in over]


def _until_rows(verb, given):
    """`block --on owner --until C` sets the owner's hold to end on its own (the Action Hub's
    unblock-conditions job clears it when C is met), so it is also the `unblock-owner` row, the
    owner's and the PM's: a lead may block on the owner, never schedule the owner's hold to end.
    An `--on` not read counts as `owner`."""
    on = list(given.get('on') or ())
    if verb == 'block' and given.get('until') and ('owner' in on or UNREAD in on or not on):
        return {'unblock-owner'}
    return set()


def sort(verb, given=None, rec=None):
    """The ACTIONS rows (names) the command `verb` with `given` takes on the record `rec` (None: not
    read, so the strictest the record could make it), narrowed by `over`. A verb no row names takes
    none, and refusal() refuses that for every role; a dry run (retarget or uncite without
    --apply, catch-up without --apply) is a read, but not with a word unread (`given` UNKNOWN),
    which could be the --apply."""
    given = given or {}
    if verb is None:
        return [n for n in roles.ACTIONS if n != 'read']
    if verb in READS:
        return ['read']
    if given.get(UNKNOWN):                      # an unread word may be the --apply a dry run lacks
        extra = _until_rows(verb, given)
        return [n for n in roles.ACTIONS if n in extra or n in _verb_rows(verb)]
    if (verb in ('retarget', 'uncite') and not given.get('apply')) or \
            (verb == 'catch-up' and not given.get('apply')):
        return ['read']
    rows = set()
    if verb == 'set':
        fields = list(given.get('fields') or ())
        for key in fields:
            if key in roles.TEXT_FIELDS:
                rows.add('set-text')
            elif key in PRICE_FIELDS:
                rows.add(_price_field_row(key, given, rec))
            elif key in OWNER_CLEARS:
                rows.add('set-price-owner')
            elif key != 'priority':
                rows.add('set-other')
            if key == 'blocked_on' and _owner_blocked(rec) and \
                    'owner' not in (given.get('blocked_on') or ()):
                rows.add('unblock-owner')       # the owner's blocker cleared, or it could be
        prices = list(given.get('priority') or ())
        if 'priority' in fields and not prices:
            prices = ['-']
        rows |= _price_rows(prices, rec)
        if not rows:
            rows.add('set-other')               # nothing to set: the tool's usage error
    elif verb == 'pull':
        # a collision with another branch's live pull is the plan's to find (pull-collision)
        rows.add('pull-held' if _held(rec) else 'pull')
    elif verb == 'block':
        on = list(given.get('on') or ())
        rows.add('block-owner' if given.get('owner_ask') or 'owner' in on else 'block')
        rows |= _until_rows(verb, given)
    elif verb == 'unblock':
        on = list(given.get('on') or ())
        owner = 'owner' in on or UNREAD in on or (not on and _owner_blocked(rec))
        rows.add('unblock-owner' if owner else 'block')
    elif verb in ('close', 'retire', 'delete'):
        if rec is None or _held(rec):
            rows.add('close-held')
        elif verb != 'close' and _owner_priced(rec):
            rows.add('retire-priced')           # the owner's or the PM's price
        else:
            rows.add(verb)
    else:
        rows.update(_verb_rows(verb))
    return _narrow(rows)


def own_pull(rec, branch):
    """True when `rec` is pulled and its `pulled_by` is `branch`, as written."""
    return bool(rec is not None and branch and rec.get('state') == 'pulled'
                and rec.get('pulled_by') == branch)


def _who(row):
    names = list(row.allowed) + ['%s (%s)' % (r, f) for r, f in row.when]
    return ', '.join(names) if names else 'nobody'


def row_text(name):
    """The row `name` in words, for a plan's refusal: the action, its risk and who may take it."""
    row = roles.ACTIONS[name]
    return 'the backlog action `%s` (risk %s), which is for %s' % (name, row.risk, _who(row))


def refusal(role, rows, facts=()):
    """The text for the first of `rows` (sort's names) `role` (role_of's answer, None: unlisted)
    may not take with `facts`, or None when it may take each. No row at all (a verb no row names)
    is refused for every role: a role fails closed."""
    if not rows:
        return 'no backlog action names this command; it is refused for every role'
    for name in rows:
        if roles.allows(name, role, facts):
            continue
        row = roles.ACTIONS[name]
        return ('the backlog action `%s` (risk %s) is for %s; this session\'s role is %s'
                % (name, row.risk, _who(row),
                   role if role else 'unlisted (only risk %s)' % ', '.join(roles.UNLISTED_MAY)))
    return None


def cli_role(environ=None):
    """The role of the session tools/backlog.py runs in: its mark (kitlib.sessionmark), the
    owner when nothing marks it."""
    from kitlib import sessionmark
    mark = sessionmark.mark(environ)
    return roles.OWNER if mark is None else roles.role_of(mark, False)


def given_of_set(changes, unset=(), body=None):
    """sort's `given` for a `set` of `changes` ([(key, [values])]), `unset` (keys) and a body
    (None: not given): what kitlib/backlog/plan.py plan_set is asked to write."""
    fields, values = [], {}
    for key, vals in changes:
        fields.append(key)
        values.setdefault(key, []).extend(vals)
    for key in unset:
        fields.append(key)
        if key == 'priority':
            values.setdefault('priority', []).append('-')
    if body is not None:
        fields.append('body')
    return {'fields': fields, 'priority': values.get('priority', []),
            'priority_by': values.get('priority_by', []), 'blocked_on': values.get('blocked_on', [])}


def given_of(args):
    """sort's `given` from tools/backlog.py's parsed arguments (an argparse Namespace)."""
    from .opener import FIELD_FLAGS
    changes = [(key, getattr(args, 'f_' + key)) for key in FIELD_FLAGS
               if getattr(args, 'f_' + key, None) is not None]
    given = given_of_set(changes, list(getattr(args, 'unset', None) or ()),
                         '' if getattr(args, 'body_file', None) else None)
    on = getattr(args, 'on', None)
    given.update({
        'owner_ask': getattr(args, 'owner_ask', None) is not None,
        'until': getattr(args, 'until', None) is not None,
        'on': on if isinstance(on, list) else [on] if on else [],
        'apply': (getattr(args, 'apply', None) is not None if args.verb == 'catch-up'
                  else bool(getattr(args, 'apply', None)))})     # `catch-up --apply` alone counts
    return given


def check(args, ctx, snap, environ=None):
    """tools/backlog.py's own check before a write plans: Refusal when the session's role
    (cli_role) may not take a row the write sorts into. `snap` (None for catch-up) gives the
    record; OWN_PULL holds when the record is pulled by the branch the write acts as. Returns the
    rows it sorted (sort's names), for the audit log (kitlib.backlog.audit)."""
    role = cli_role(environ)
    rid = getattr(args, 'id', None)
    rec = snap.get(rid) if snap is not None and rid else None
    # a record the store does not hold: the plan refuses it by its own words (NOT FOUND), or
    # `add` makes it, so it sorts as unheld and unpriced
    rows = sort(args.verb, given_of(args), _Absent() if rec is None and snap is not None else rec)
    facts = ()
    if rec is not None:
        try:
            branch = ctx.current_branch()
        except Refusal:
            branch = None
        if own_pull(rec, branch):
            facts = (roles.OWN_PULL,)
    text = refusal(role, rows, facts)
    if text:
        raise Refusal('%s (kitlib/backlog/roles.py); ask a session whose role may' % text)
    return rows


# The rows a plan finds (a collision with another branch's live pull), by verb, and the words a
# plan that took one ends its commit subject with, so the audit check (audit_check.rows_of)
# rebuilds the row from the store's history.
COLLISION_ROWS = {'pull': 'pull-collision', 'set': 'set-widen-collision'}
COLLISION_MARK = ' (over a live pull)'


def may(name, environ=None):
    """True when the session tools/backlog.py runs in (cli_role) may take the row `name`: a row
    the plan finds rather than sort (pull-collision, set-widen-collision; no fact grants one)."""
    return roles.allows(name, cli_role(environ))


class _Absent(object):
    """A record the store does not hold, read as unheld and unpriced: the plan refuses the write
    by its own words (NOT FOUND), or `add` makes it."""

    def get(self, key):
        return None

    def all(self, key):
        return []


# --- the guard's reader ---------------------------------------------------------------------------

# Each verb's options as tools/backlog.py spells them (tests/backlog/test_policy.py pins them to its
# parser): True for one that takes a value. A spelling not here (an abbreviation argparse would
# take, a misspelling) reads as unknown.
def _set_options():
    from .opener import FIELD_FLAGS
    opts = {'--unset': True, '--body-file': True}
    for key in FIELD_FLAGS:
        opts['--' + key] = True
        opts['--' + key.replace('_', '-')] = True
    return opts


OPTIONS = {
    'next': None, 'show': None, 'find': None, 'list': None, 'check': None, 'stale': None, 'landable': None,
    'add': {'--summary': True, '--scope': True, '--effort': True, '--ref': True, '--reopen': False,
            '--found-on': True, '--why-not-folded': True, '--body-file': True},
    'set': None,                                # _set_options(), read when asked
    'block': {'--on': True, '--reason': True, '--owner-ask': True, '--until': True},
    'unblock': {'--on': True, '--reason': True},
    'retarget': {'--apply': False},
    'uncite': {'--apply': False},
    'pull': {'--reason': True, '--blocker': True},
    'release': {'--reason': True},
    'close': {'--reason': True},
    'retire': {'--why': True},
    'delete': {'--why': True},
    'clear-review': {'--reason': True},
    'set-status': {'--q': True},
    'unpark': {},
    'catch-up': {'--apply': False},             # its paths are positionals here
}
GLOBAL = {'-C': True, '--session': True, '--as': True, '--no-fetch': False}
HELP = ('-h', '--help')

Argv = collections.namedtuple('Argv', 'root branch verb rid given help')


def verb_options(verb):
    return _set_options() if verb == 'set' else OPTIONS.get(verb)


def read_argv(words):
    """What the backlog.py command line `words` (each (text, readable: True, False or ONE)) gives:
    Argv with -C's folder, --as's branch (UNREAD: a ONE word), the verb (None: not read), the
    record id (None: none or not read), sort's `given` and `help` (true: -h or --help, so nothing
    runs)."""
    root = branch = verb = rid = None
    given = {'fields': [], 'priority': [], 'on': [], 'priority_by': [], 'blocked_on': []}
    k, n = 0, len(words)
    plain = False                               # after `--` past the verb: every word is an operand

    def value_at(j, name):
        """The value word `j` gives option `name`, or None when it gives none that reads."""
        if j >= n or not (words[j][1] is True or (words[j][1] == ONE and name not in DECIDING)):
            return None
        return words[j][0] if words[j][1] is True else UNREAD

    while k < n:
        text, ok = words[k]
        if ok is not True:
            if verb is None:
                return Argv(root, branch, None, None, {UNKNOWN: True}, False)
            given[UNKNOWN] = True
            k += 1
            continue
        if not plain and text in HELP:
            return Argv(root, branch, verb, rid, given, True)
        if not plain and text == '--':
            plain = True
            k += 1
            continue
        if verb is None:
            if not plain and text.startswith('-'):
                name, eq, value = text.partition('=')
                if name.startswith('-C') and not name.startswith('--') and len(name) > 2:
                    root, k = text[2:], k + 1
                    continue
                if name not in GLOBAL:
                    return Argv(root, branch, None, None, {UNKNOWN: True}, False)
                if GLOBAL[name] and not eq:
                    value = value_at(k + 1, name)
                    if value is None:
                        return Argv(root, branch, None, None, {UNKNOWN: True}, False)
                    k += 1
                if name == '-C':
                    root = value
                elif name == '--as':
                    branch = value
                k += 1
                continue
            verb, plain = text, False           # the parser drops a `--` right before the verb
            if verb not in OPTIONS:
                return Argv(root, branch, verb, None, {UNKNOWN: True}, False)
            k += 1
            continue
        opts = verb_options(verb)
        if opts is None or plain or not text.startswith('-') or text == '-' or re.match(r'^-\d', text):
            if rid is None:
                rid = text
            k += 1
            continue
        name, eq, value = text.partition('=')
        if name not in opts:
            given[UNKNOWN] = True
            k += 1
            continue
        if opts[name] and not eq:
            value = value_at(k + 1, name)
            if value is None:                   # the option counts, its value not read
                given[UNKNOWN] = True
                _given_add(given, verb, name, UNREAD)
                k += 1
                continue
            k += 1
        _given_add(given, verb, name, value)
        k += 1
    return Argv(root, branch, verb, rid, given, False)


def _given_add(given, verb, name, value):
    key = name[2:].replace('-', '_')
    if key in ('owner_ask', 'apply', 'until'):
        given[key] = True
    elif key == 'on':
        given['on'].append(value)
    elif verb == 'set' and key == 'unset':
        given['fields'].append(value)
        if value == 'priority':
            given['priority'].append('-')
    elif verb == 'set' and key == 'body_file':
        given['fields'].append('body')
    elif verb == 'set':
        given['fields'].append(key)
        if key in ('priority', 'priority_by', 'blocked_on'):
            given[key].append(value)
