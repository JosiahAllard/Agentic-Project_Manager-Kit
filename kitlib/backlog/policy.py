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

`given` is a dict: 'fields' (the set fields given or unset, `body` for --body-file), 'priority'
(each priority value given; '-' for --unset priority), 'hold_ruling', 'ruling', 'owner_ask',
'apply', 'dry_run' (each true when given), 'on' (the --on members), and 'unknown' (true when a
word could not be read: every row the verb can take counts). A verb of None (not read) counts as
every row but `read`.
"""
import collections
import re

from . import record as rmod
from . import roles
from .errors import Refusal

READS = roles.ACTIONS['read'].verbs
UNKNOWN = 'unknown'


def _held(rec):
    return rec is not None and rmod.is_owner_held(rec)


def _owner_priced(rec):
    return rec is not None and rec.get('priority_by') in roles.PRICED_BY_OWNER


def _price_rows(values, rec):
    """The rows a `set` of priority to each of `values` takes on `rec` (None: not read)."""
    out = set()
    for value in values:
        if value == roles.HOLD or rec is None or rec.get('priority') == roles.HOLD:
            out.add('set-hold')                 # a hold set, a hold released, or a record not read
        elif value in roles.PRICE_TOP or _owner_priced(rec):
            out.add('set-price-owner')
        else:
            out.add('set-price')
    return out


def _verb_rows(verb):
    return [name for name, row in roles.ACTIONS.items() if verb in row.verbs]


def _narrow(names):
    """`names` without each row another of them is `over`, in ACTIONS order."""
    over = set()
    for name in names:
        over.update(roles.ACTIONS[name].over)
    return [n for n in roles.ACTIONS if n in names and n not in over]


def sort(verb, given=None, rec=None):
    """The ACTIONS rows (names) the command `verb` with `given` takes on the record `rec` (None: not
    read, so the strictest the record could make it), narrowed by `over`. A verb no row names
    (catch-up) takes none; a dry run (retarget without --apply, migrate --dry-run) is a read."""
    given = given or {}
    if verb is None:
        return [n for n in roles.ACTIONS if n != 'read']
    if verb in READS or (verb == 'retarget' and not given.get('apply')) or \
            (verb == 'migrate' and given.get('dry_run')):
        return ['read']
    if given.get(UNKNOWN):
        return _verb_rows(verb)
    rows = set()
    if verb == 'set':
        fields = list(given.get('fields') or ())
        for key in fields:
            if key in roles.TEXT_FIELDS:
                rows.add('set-text')
            elif key != 'priority':
                rows.add('set-other')
        prices = list(given.get('priority') or ())
        if 'priority' in fields and not prices:
            prices = ['-']
        rows |= _price_rows(prices, rec)
        if given.get('hold_ruling'):
            rows.add('set-hold')
        if not rows:
            rows.add('set-other')               # nothing to set: the tool's usage error
    elif verb == 'pull':
        rows.add('pull-held' if _held(rec) else 'pull-collision' if given.get('ruling') else 'pull')
    elif verb in ('block', 'unblock'):
        on = list(given.get('on') or ())
        owner = given.get('owner_ask') or 'owner' in on or (
            verb == 'unblock' and not on and (rec is None or 'owner' in rec.all('blocked_on')))
        rows.add('block-owner' if owner else 'block')
    elif verb in ('close', 'retire', 'delete'):
        rows.add('close-held' if rec is None or _held(rec) else verb)
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


def refusal(role, rows, facts=()):
    """The text for the first of `rows` (sort's names) `role` (role_of's answer, None: unlisted)
    may not take with `facts`, or None when it may take each."""
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


def given_of(args):
    """sort's `given` from tools/backlog.py's parsed arguments (an argparse Namespace)."""
    from .opener import FIELD_FLAGS
    fields, prices = [], []
    for key in FIELD_FLAGS:
        values = getattr(args, 'f_' + key, None)
        if values is not None:
            fields.append(key)
            if key == 'priority':
                prices.extend(values)
    unset = list(getattr(args, 'unset', None) or ())
    fields.extend(unset)
    if 'priority' in unset:
        prices.append('-')
    if getattr(args, 'body_file', None):
        fields.append('body')
    on = getattr(args, 'on', None)
    return {'fields': fields, 'priority': prices,
            'hold_ruling': getattr(args, 'hold_ruling', None) is not None,
            'ruling': getattr(args, 'ruling', None) is not None,
            'owner_ask': getattr(args, 'owner_ask', None) is not None,
            'on': on if isinstance(on, list) else [on] if on else [],
            'apply': bool(getattr(args, 'apply', None)),
            'dry_run': bool(getattr(args, 'dry_run', None))}


def check(args, ctx, snap, environ=None):
    """tools/backlog.py's own check before a write plans: Refusal when the session's role
    (cli_role) may not take a row the write sorts into. `snap` (None for migrate) gives the
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
    opts = {'--unset': True, '--ruling': True, '--body-file': True, '--hold-ruling': True}
    for key in FIELD_FLAGS:
        opts['--' + key] = True
        opts['--' + key.replace('_', '-')] = True
    return opts


OPTIONS = {
    'next': None, 'show': None, 'list': None, 'check': None, 'stale': None, 'landable': None,
    'add': {'--summary': True, '--scope': True, '--effort': True, '--ref': True, '--reopen': False,
            '--found-on': True, '--why-not-folded': True},
    'set': None,                                # _set_options(), read when asked
    'block': {'--on': True, '--reason': True, '--owner-ask': True, '--ruling': True},
    'unblock': {'--on': True, '--reason': True, '--ruling': True},
    'retarget': {'--apply': False},
    'pull': {'--reason': True, '--ruling': True, '--blocker': True},
    'release': {'--reason': True},
    'close': {'--reason': True, '--ruling': True},
    'retire': {'--why': True, '--ruling': True, '--owner-said': True},
    'delete': {'--why': True, '--ruling': True, '--owner-said': True},
    'clear-review': {'--reason': True},
    'set-status': {'--q': True},
    'unpark': {'--ruling': True},
    'migrate': {'--dry-run': False, '--to': True, '--dir': True},
    'catch-up': {'--apply': False},             # its paths are positionals here
}
GLOBAL = {'-C': True, '--session': True, '--as': True, '--no-fetch': False}
HELP = ('-h', '--help')

Argv = collections.namedtuple('Argv', 'root branch verb rid given help')


def verb_options(verb):
    return _set_options() if verb == 'set' else OPTIONS.get(verb)


def read_argv(words):
    """What the backlog.py command line `words` (each (text, readable)) gives: Argv with -C's
    folder, --as's branch, the verb (None: not read), the record id (None: none or not read),
    sort's `given` and `help` (true: -h or --help, so nothing runs)."""
    root = branch = verb = rid = None
    given = {'fields': [], 'priority': [], 'on': []}
    k, n = 0, len(words)
    plain = False                               # after `--` past the verb: every word is an operand
    while k < n:
        text, ok = words[k]
        if not ok:
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
                    if k + 1 >= n or not words[k + 1][1]:
                        return Argv(root, branch, None, None, {UNKNOWN: True}, False)
                    value, k = words[k + 1][0], k + 1
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
            if k + 1 >= n or not words[k + 1][1]:
                given[UNKNOWN] = True
                k += 1
                continue
            value, k = words[k + 1][0], k + 1
        _given_add(given, verb, name, value)
        k += 1
    return Argv(root, branch, verb, rid, given, False)


def _given_add(given, verb, name, value):
    key = name[2:].replace('-', '_')
    if key in ('ruling', 'hold_ruling', 'owner_ask', 'apply', 'dry_run'):
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
        if key == 'priority':
            given['priority'].append(value)
