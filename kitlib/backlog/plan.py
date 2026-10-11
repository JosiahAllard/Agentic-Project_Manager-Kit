"""The write verbs as plans: `plan_<verb>(snap, ctx, ...) -> Change` (ported from earlier
per-repository tools/backlog.py copies, with the keys and verbs the kit dropped cut).

A plan only decides. It reads the snapshot it is given, refuses (Usage, Refusal, NotFound,
Failure) or returns the records to write, and never touches a file, a ref or the screen. The
store runs it again after a lost race, so a plan is a pure function of the snapshot and its
arguments (plus the git probes it names: branch liveness and `requires: commit:`).
"""
import datetime

from kitlib.git import cmd, refs
from kitlib.machinepaths import find as find_machine_paths

from . import ledger as lmod
from . import offer as omod
from . import record as rmod
from . import scope as smod
from .errors import Failure, NotFound, Refusal, Usage
from .store import PLAIN_ID, Change, id_refusal

MIN_REASON = 12
RULED_PRICE_BY = ('owner', 'pm')
ADD_BODY = ('## Why this is here\n\nRecorded by `backlog.py add`. Unprioritised on purpose: '
            'prioritising it is a separate act by somebody not mid-discovery.\n')
# Keys only a verb writes, each with the verb that does.
WRITTEN_BY = {
    'id': 'nothing - the filename is the id; add the record under its new id and retire this one',
    'displaced': '`pull`, which computes it from the offer',
    'preempt': '`pull --reason`', 'preempt_by': '`pull --reason`', 'preempt_at': '`pull --reason`',
    'pulled_at': '`pull`', 'pulled_by': '`pull`',
    'priority_ruling': '`--ruling R-nnnn`, checked before it is written',
    'hold_ruling': '`--ruling R-nnnn` on a release, or `set --hold-ruling R-nnnn`',
    'q': '`set-status parked --q Q-nnn`', 'parked_at': '`set-status parked --q Q-nnn`',
    'resume': '`unpark --ruling R-nnnn`',
}


class Context(object):
    """What a plan needs beyond the snapshot. `branch` None: the branch checked out in `root`."""

    def __init__(self, store, ledger, root, remote='origin', main_branch='main', branch=None,
                 today=None):
        self.store = store
        self.ledger = ledger
        self.root = root
        self.remote = remote
        self.main_branch = main_branch
        self.branch = branch
        self.today = today

    @property
    def session(self):
        return self.store.session

    def date(self):
        return self.today or datetime.date.today().isoformat()

    def current_branch(self):
        if self.branch:
            return self.branch
        name, why = refs.current_branch(self.root)
        if name is None:
            raise Failure('cannot read the branch checked out in %s: %s' % (self.root, why))
        if not name:
            raise Failure('HEAD is detached in %s, so no branch can be named as the one that '
                          'pulls; pass `--as <branch>`' % self.root)
        return name

    def history(self, snap):
        return lambda rid: self.store.history(rid, snap)

    def unmet(self, snap):
        hist = self.history(snap)
        return lambda rec: omod.unmet_requires(rec, snap, hist, self.root, self.main_branch)


# --- shared ---------------------------------------------------------------------------------------

def require(snap, rid):
    rec = snap.get(rid)
    if rec is None:
        raise NotFound('no record with `id: %s` in %s' % (rid, snap.where))
    return rec


def require_shown(snap, ctx, rid):
    """require() for `show`: a missing id's NotFound also says which store commit removed it
    (sha, date, session, subject; never an author name), or that none ever did. An id that is
    not plain (a glob or path character) is not looked up, and a history git cannot read (an
    unborn HEAD, a failing git) still leaves the plain NOT FOUND, with a line saying so."""
    rec = snap.get(rid)
    if rec is not None:
        return rec
    plain = 'no record with `id: %s` in %s' % (rid, snap.where)
    if not PLAIN_ID.match(rid):
        raise NotFound('%s\n  history not searched: `%s` is not a plain id' % (plain, rid))
    try:
        gone = ctx.store.removal(rid, snap)
    except Failure as exc:
        raise NotFound('%s\n  the store\'s history could not be read: %s' % (plain, exc))
    if gone is None:
        then = 'no commit in the store\'s history removed it: the id is unknown'
    else:
        then = 'removed by %s on %s (session %s): %s' % (gone.sha, gone.when, gone.session or '-',
                                                       gone.subject)
    raise NotFound('%s\n  %s' % (plain, then))


def _text(pairs, body):
    return rmod.render(pairs, body)


def clears_owner_blocker(rec, after):
    return 'owner' in rec.all('blocked_on') and 'owner' not in after


def refuse_machine_paths(field, *texts):
    """Usage when free text carries a machine path: it reaches a record or a commit subject, and
    history cannot be fixed once it lands (commit.machine_path_error, same rule)."""
    found = []
    for t in texts:
        found += [f for f in find_machine_paths(t or '') if f not in found]
    if found:
        raise Usage('refused: `%s` carries a machine path: %s. tools/machine_paths.py fails a '
                    'file with one. Write %%VAR%%\\... (the variable that applies), or leave it out.'
                    % (field, ', '.join(found)))


def reason_error(reason, flag):
    text = ' '.join((reason or '').split())
    if len(text) < MIN_REASON:
        return ('`%s` needs at least %d characters of reason: it is the only trace the change '
                'leaves' % (flag, MIN_REASON))
    return None


def current_offer(snap, ctx):
    """`(offers, passed, holding)` as `next` would print them for `ctx.session`."""
    holding, _dead, _why = omod.in_flight_split(snap, ctx.root, ctx.remote)
    result, refusals, _ranks = omod.next_offer(snap, holding, session=ctx.session,
                                               unmet=ctx.unmet(snap))
    if refusals:
        raise Failure('`next` cannot compute an offer, so there is nothing to check this against: '
                      '%s' % '; '.join(refusals))
    return result[0], result[1], holding


def read_body_file(path):
    """The body `--body-file` would write: UTF-8, a BOM dropped, never empty."""
    try:
        with open(path, 'r', encoding='utf-8-sig') as fh:
            text = fh.read()
    except OSError as exc:
        raise Usage('cannot read `--body-file %s`: %s' % (path, exc))
    except UnicodeDecodeError as exc:
        raise Usage('`--body-file %s` is not UTF-8: %s' % (path, exc))
    if not text.strip():
        raise Usage('`--body-file %s` is empty; an empty body is refused rather than written' % path)
    return text.replace('\r\n', '\n')


# --- add ------------------------------------------------------------------------------------------

def plan_add(snap, ctx, rid, summary, scopes, effort=None, ref=None, reopen=False, session=None):
    """One unprioritised record. `priority: -` always: pricing is a separate act."""
    why = snap.rules.id_error(rid)
    if why is not None:
        raise Usage('cannot add: %s' % why)
    why = id_refusal(snap, rid)
    if why is not None:
        raise Refusal('cannot add: %s' % why)
    if session is not None and rmod._session(session) is not None:
        raise Usage('bad `--session %s`: %s' % (session, rmod._session(session)))
    if not (summary or '').strip():
        raise Usage('`add` requires a non-blank `--summary`')
    refuse_machine_paths('--summary', summary)
    if not scopes:
        raise Usage('`add` requires at least one `--scope`; `none` (no tracked files) is written out')
    for s in scopes:
        bad = smod.scope_member_error(s)
        if bad:
            raise Usage('bad `--scope %s`: %s' % (s, bad))
    if effort is not None and rmod._effort(effort) is not None:
        raise Usage('bad `--effort %s`: %s' % (effort, rmod._effort(effort)))
    prior = ctx.store.history(rid, snap)
    if prior and not reopen:
        raise Refusal('`%s` was already closed or retired: %s. Re-adding it under the same id makes '
                      'that history read as though it were about this item: pass `--reopen` if it '
                      'is the same work coming back, or pick a new id' % (rid, prior))
    pairs = [('id', rid), ('area', smod.area_from_scope(scopes)), ('priority', '-'),
             ('priority_by', '-'), ('effort', effort or '-'), ('state', 'open'),
             ('blocked_on', '-')]
    pairs += [('scope', s) for s in scopes]
    if session is not None:
        pairs.append(('session', session))
    if ref:
        pairs.append(('ref', ref))
    pairs.append(('summary', summary.strip()))
    lines = ['ADDED %s' % rid] + ['  %-11s %s' % (k + ':', v) for k, v in pairs]
    lines.append('Unprioritised: not in `next` until somebody prices it (`list --needs-priority`).')
    return Change({rid: _text(pairs, ADD_BODY)}, 'backlog: add %s' % rid, lines)


# --- set ------------------------------------------------------------------------------------------

def plan_set(snap, ctx, rid, changes, unset=(), ruling=None, body=None, hold_ruling=None):
    """Edit fields and/or the body. `changes` is `[(key, [values])]`.

    - Changing a price field on an owner-prioritised or owner-held record, or writing
      `priority_by: owner`, needs `--ruling` (checked, written as `priority_ruling`);
      `priority_by: pm` always needs one (taken on its word, the ledger unread).
    - Releasing an owner hold (dropping `owner` from `blocked_on`, moving `priority` off `-1`)
      needs `--ruling` (written as `hold_ruling`); `-1` -> `-` is a release with no price.
    - A result of `priority_by: agent` drops `priority_ruling`.
    - `state: pulled` is entered only by `pull` and left only by `release`.
    - `hold_ruling` R re-cites the record's hold ruling to R and changes nothing else.
    """
    for key, vals in changes:
        refuse_machine_paths('--' + key, *vals)
    refuse_machine_paths('--body-file', body)
    rec = require(snap, rid)
    if hold_ruling is not None:
        if changes or unset or ruling is not None or body is not None:
            raise Usage('`--hold-ruling` re-cites the hold ruling and changes nothing else; give it '
                        'alone')
        return _plan_recite(snap, ctx, rec, hold_ruling)
    for key in unset:
        if key not in rmod.FIELDS:
            raise Usage('`--unset %s` names no field' % key)
        if key in rmod.REQUIRED:
            raise Usage('`%s` is required in every record, so it cannot be unset' % key)
    if not changes and not unset and body is None:
        raise Usage('`set` was given nothing to change')
    for key, vals in list(changes) + [(k, []) for k in unset]:
        if key not in rmod.FIELDS:
            raise Usage('`%s` is not a field' % key)
        if key in WRITTEN_BY:
            raise Refusal('`%s` is written only by %s' % (key, WRITTEN_BY[key]))
        if key in rmod.SINGLE_VALUED and len(vals) > 1:
            raise Usage('`%s` takes one value and was given %d' % (key, len(vals)))

    keys = [k for k, _ in changes]
    pairs = [(k, v) for k, v in rec.pairs() if k not in unset]
    pairs = rmod.apply_changes(pairs, changes)
    get = lambda k: rmod.values(pairs, k)  # noqa: E731
    was_pulled, now_pulled = rec.get('state') == 'pulled', get('state') == ['pulled']
    if was_pulled != now_pulled:
        raise Refusal('`state: pulled` is entered only by `pull` and left only by `release`, which '
                      'record who has it; `set` does neither')
    if (rec.get('state') == 'parked') != (get('state') == ['parked']):
        raise Refusal('`state: parked` is entered only by `set-status parked --q Q-nnn` and left only '
                      'by `unpark --ruling R-nnnn`, which record the question and its answer')

    new_priority = dict(changes).get('priority', [rec.get('priority')])[0]
    minus_one_release = rec.get('priority') == rmod.OWNER_PRIORITY and new_priority == '-'
    new_by = dict(changes).get('priority_by', [None])[0]
    if 'priority' in keys and rmod.is_owner_held(rec) and not minus_one_release \
            and new_by not in RULED_PRICE_BY:
        raise Refusal('`%s` is owner-held (%s), so its priority changes only with `--priority-by '
                      'owner|pm --ruling R-nnnn`, or a release from `-1` to `-` with `--ruling`'
                      % (rid, rmod.owner_hold_label(rec)))
    if new_by == 'pm' and ruling is None:
        raise Refusal('`--priority-by pm` needs `--ruling R-nnnn`, the ruling the PM priced `%s` on'
                      % rid)

    moved = [k for k in rmod.PRIORITY_KEYS if rec.all(k) != get(k)]
    priced = [k for k in moved if k != 'priority_ruling']
    if minus_one_release and priced == ['priority']:
        priced = []
    owners = rmod.is_owner_prioritised(rec) or rmod.is_owner_held(rec)
    unheld = clears_owner_blocker(rec, get('blocked_on'))
    released_minus_one = rec.get('priority') == rmod.OWNER_PRIORITY and \
        get('priority') != [rmod.OWNER_PRIORITY]
    releases = unheld or released_minus_one
    if ruling is not None:
        if not priced and not releases:
            raise Usage('`--ruling %s` was given, and this write moves no price and releases no '
                        'owner hold: there is nothing for it to record' % ruling)
        if released_minus_one:
            lmod.refuse_unnamed_release(rid, rec, ruling, 'set')
        lmod.check_ruling(ctx.ledger, rid, rec, ruling, 'set', hold_only=not priced,
                          needs_items=releases,
                          pm_price=bool(priced) and get('priority_by') == ['pm'])
        if priced:
            pairs = rmod.apply_changes(pairs, [('priority_ruling', [ruling])])
        if releases:
            pairs = rmod.apply_changes(pairs, [('hold_ruling', [ruling])])
    elif unheld:
        raise Refusal('`%s` is blocked on the owner, and this write clears that hold, so it needs '
                      '`--ruling R-nnnn`' % rid)
    elif moved and (owners or new_by == 'owner'):
        raise Refusal('`%s` is %s, so changing %s needs `--ruling R-nnnn`: an owner\'s price is not '
                      'repriced on an agent\'s word'
                      % (rid, 'owner-held (%s)' % rmod.owner_hold_label(rec)
                         if rmod.is_owner_held(rec) else 'owner-prioritised (`priority_by: %s`)'
                         % rec.get('priority_by') if owners else 'given `priority_by: owner`',
                         ', '.join('`%s`' % k for k in moved)))
    dropped = None
    if get('priority_by') == ['agent'] and get('priority_ruling'):
        dropped = get('priority_ruling')[0]
        pairs = [(k, v) for k, v in pairs if k != 'priority_ruling']

    lines = ['SET %s' % rid]
    for key in sorted(set(keys) | set(unset) | ({'priority_ruling'} if dropped or priced and
                                                ruling else set()) |
                      ({'hold_ruling'} if ruling and releases else set())):
        lines.append('  %-11s %s -> %s' % (key + ':', ' / '.join(rec.all(key)) or '(absent)',
                                           ' / '.join(rmod.values(pairs, key)) or '(absent)'))
    if body is not None:
        lines.append('  body:       replaced (%d bytes)' % len(body.encode('utf-8')))
    if dropped is not None:
        lines.append('  priority_ruling %s dropped: `priority_by: agent` carries no ruling' % dropped)
    acts = [a for a, on in (('priority', priced), ('unblock', unheld),
                            ('hold release', released_minus_one and not priced)) if on]
    subject = ('backlog: %s %s' % (' and '.join(acts), rid) if acts and ruling
               else 'backlog: set %s' % rid)
    text = _text(pairs, body if body is not None else rec.body)
    if text == rec.text:
        return Change({}, subject, lines + ['  (no change)'])
    return Change({rid: text}, subject, lines, ruling=ruling)


def _plan_recite(snap, ctx, rec, ruling):
    rid = rec.id
    if not rmod.RULING_RE.match(ruling or ''):
        raise Usage('`--hold-ruling %s` is not a ruling id - `R-nnnn`' % ruling)
    old = rec.get('hold_ruling')
    if old is None:
        raise Refusal('`%s` carries no `hold_ruling` to re-cite' % rid)
    if ruling == old:
        raise Refusal('`%s` already carries `hold_ruling: %s`' % (rid, ruling))
    if rec.get('priority_ruling') == ruling:
        raise Refusal('`%s` already carries `priority_ruling: %s`. One ruling, one change' % (rid, ruling))
    if ctx.ledger.standing and ruling == ctx.ledger.standing:
        raise Refusal('`%s` is the standing delegation, not the ruling behind any one change' % ruling)
    if ctx.ledger.configured:
        entries, why = ctx.ledger.entries()
        if entries is None:
            raise Failure('`--hold-ruling %s` cannot be checked: %s' % (ruling, why))
        bad = lmod.ruling_refusal(ctx.ledger, entries, ruling, lmod.HOLD_KINDS)
        if bad is None:
            bad = lmod.items_refusal(entries[ruling], ruling, rid)
        if bad is not None:
            raise Refusal(bad)
    pairs = rmod.apply_changes(rec.pairs(), [('hold_ruling', [ruling])])
    return Change({rid: _text(pairs, rec.body)},
                  'backlog: re-cite %s hold_ruling %s -> %s, no price change' % (rid, old, ruling),
                  ['RECITED %s' % rid, '  hold_ruling: %s -> %s' % (old, ruling)], ruling=ruling)


# --- block / unblock ------------------------------------------------------------------------------

def plan_block(snap, ctx, rid, members, owner_ask=None, ruling=None):
    """`block` REPLACES `blocked_on`; leaving `owner` out on a record blocked on him releases his
    hold, as `unblock` does, and takes the same `--ruling`."""
    refuse_machine_paths('--owner-ask', owner_ask)
    rec = require(snap, rid)
    if not members:
        raise Usage('`block` requires at least one `--on <member>`')
    for m in members:
        if m == '-':
            raise Usage('`--on -` is not a blocker; `unblock` writes `-`')
        err = rmod.blocked_on_error(m, snap.rules)
        if err:
            raise Usage('bad `--on %s`: %s' % (m, err))
    if 'item:%s' % rid in members:
        raise Usage('`%s` cannot be blocked on itself' % rid)
    if rec.get('state') == 'pulled':
        raise Refusal('`%s` is in flight (pulled by %s): `release` it first, then block it'
                      % (rid, rec.get('pulled_by')))
    unheld = clears_owner_blocker(rec, members)
    if unheld and ruling is None:
        raise Refusal('`%s` is blocked on the owner, and this block leaves him out, which clears '
                      'his hold - it needs `--ruling R-nnnn`, as `unblock` does' % rid)
    if ruling is not None:
        if not unheld:
            raise Usage('`--ruling %s` was given, and this block releases no hold of the owner\'s'
                        % ruling)
        lmod.check_ruling(ctx.ledger, rid, rec, ruling, 'block', hold_only=True)
    if 'owner' in members and not owner_ask and not rec.has('owner_ask'):
        raise Usage('blocking on the owner needs `--owner-ask "<the question>"`')
    changes = [('state', ['blocked']), ('blocked_on', list(members))]
    if owner_ask:
        changes.append(('owner_ask', [owner_ask]))
    drop = unheld and not owner_ask and rec.get('priority') != rmod.OWNER_PRIORITY
    pairs = [(k, v) for k, v in rec.pairs() if not (drop and k == 'owner_ask')]
    if ruling is not None:
        changes.append(('hold_ruling', [ruling]))
    pairs = rmod.apply_changes(pairs, changes)
    lines = ['BLOCKED %s on %s' % (rid, ', '.join(members))]
    if drop and rec.has('owner_ask'):
        lines.append('  owner_ask dropped: nothing waits on him now. It was: %s' % rec.get('owner_ask'))
    subject = 'backlog: block %s on %s' % (rid, ', '.join(members))
    if ruling is not None:
        subject += ', releasing the owner hold'
    return Change({rid: _text(pairs, rec.body)}, subject, lines, ruling=ruling)


def plan_unblock(snap, ctx, rid, ruling=None):
    rec = require(snap, rid)
    if rec.get('state') != 'blocked':
        raise Usage('`%s` is `state: %s`, not `blocked`: there is nothing to unblock'
                    % (rid, rec.get('state')))
    unheld = clears_owner_blocker(rec, ['-'])
    if unheld and ruling is None:
        raise Refusal('`%s` is blocked on the owner, so unblocking it needs `--ruling R-nnnn`' % rid)
    if ruling is not None:
        if not unheld:
            raise Usage('`--ruling %s` was given, and `%s` is not blocked on the owner' % (ruling, rid))
        lmod.check_ruling(ctx.ledger, rid, rec, ruling, 'unblock', hold_only=True)
    drop = 'owner' in rec.all('blocked_on') and rec.get('priority') != rmod.OWNER_PRIORITY
    pairs = [(k, v) for k, v in rec.pairs() if not (drop and k == 'owner_ask')]
    pairs = rmod.apply_changes(pairs, [('state', ['open']), ('blocked_on', ['-'])]
                               + ([('hold_ruling', [ruling])] if ruling is not None else []))
    lines = ['UNBLOCKED %s - state: open, blocked_on: -' % rid]
    if drop and rec.has('owner_ask'):
        lines.append('  owner_ask dropped: nothing waits on him now. It was: %s' % rec.get('owner_ask'))
    return Change({rid: _text(pairs, rec.body)}, 'backlog: unblock %s' % rid, lines, ruling=ruling)


# --- park / unpark -----------------------------------------------------------------------

def plan_set_status(snap, ctx, rid, status, q=None):
    """Park an open or pulled record on the owner's question `q`. A pull is given up: a parked
    record holds no scope and takes no place in the offer."""
    if status != 'parked':
        raise Usage('`set-status` sets `parked` only; `%s` is reached by its own verb (open: '
                    '`unblock`/`release`/`unpark`, blocked: `block`, pulled: `pull`)' % status)
    if q is None or rmod._question(q) is not None:
        raise Usage('parking needs `--q Q-nnn`, the owner question it waits on%s'
                    % ('' if q is None else ': %s' % rmod._question(q)))
    rec = require(snap, rid)
    state = rec.get('state')
    if state == 'parked':
        raise Refusal('`%s` is already parked on %s'
                      % (rid, rec.get('q') or 'no recorded question'))
    if state not in ('open', 'pulled'):
        raise Refusal('`%s` is `state: %s`; only an open or pulled record is parked. %s first'
                      % (rid, state, 'Unblock it' if state == 'blocked' else 'Open it'))
    pairs = [(k, v) for k, v in rec.pairs() if k not in rmod.PULL_KEYS + rmod.PREEMPT_KEYS]
    pairs = rmod.apply_changes(pairs, [('state', ['parked']), ('q', [q]),
                                       ('parked_at', [ctx.date()])])
    lines = ['PARKED %s on %s' % (rid, q)]
    if state == 'pulled':
        lines.append('  pull by %s given up: its scope is free and it is out of the offer'
                     % rec.get('pulled_by'))
    return Change({rid: _text(pairs, rec.body)}, 'backlog: park %s on %s' % (rid, q), lines)


def plan_unpark(snap, ctx, rid, ruling=None):
    """Open a parked record on the ruling that answered its question; `resume: <ruling>` ranks
    it above priority (below a pin) in `next`."""
    if ruling is None:
        raise Usage('`unpark` needs `--ruling R-nnnn`, the ruling that answered the question')
    rec = require(snap, rid)
    if rec.get('state') != 'parked':
        raise Refusal('`%s` is `state: %s`, not `parked`: there is nothing to unpark'
                      % (rid, rec.get('state')))
    lmod.check_ruling(ctx.ledger, rid, rec, ruling, 'unpark', hold_only=True, needs_items=False)
    pairs = [(k, v) for k, v in rec.pairs() if k not in rmod.PARK_KEYS]
    pairs = rmod.apply_changes(pairs, [('state', ['open']), ('resume', [ruling])])
    lines = ['UNPARKED %s - state: open, resume: %s%s' % (rid, ruling, ' (was parked on %s)'
                                                          % rec.get('q') if rec.get('q') else '')]
    return Change({rid: _text(pairs, rec.body)}, 'backlog: unpark %s' % rid, lines, ruling=ruling)


# --- pull / release -------------------------------------------------------------------------------

def plan_pull(snap, ctx, rid, reason=None, ruling=None, blocker=None):
    """Take an open, priced record and record the branch. One in `next`'s offer pulls plainly;
    one outside it needs `--reason` and records `preempt` and the `displaced` offer, computed
    here. A `priority: -1` record needs `--ruling` (a hold release). An unmet `requires:` refuses."""
    refuse_machine_paths('--reason', reason)
    rec = require(snap, rid)
    if reason is not None:
        reason = ' '.join(reason.split())
        if not reason:
            raise Usage('`--reason` was given blank')
    if ruling is not None and not rmod.RULING_RE.match(ruling):
        raise Usage('`--ruling %s` is not a ruling id - `R-nnnn`' % ruling)
    state = rec.get('state')
    if state == 'pulled':
        raise Refusal('`%s` is already in flight - pulled %s by %s'
                      % (rid, rec.get('pulled_at'), rec.get('pulled_by')))
    if state != 'open':
        raise Refusal('`%s` is `state: %s`, and `pull` takes an open record. %s first, in its own '
                      'step' % (rid, state, 'Unblock it' if state == 'blocked' else 'Open it'))
    minus_one = rec.get('priority') == rmod.OWNER_PRIORITY
    if not minus_one and not rmod.is_band(rec.get('priority')):
        raise Refusal('`%s` is `priority: %s`, and `pull` takes a priced record (`1`-`5`). Price it '
                      'first: `set %s --priority N --priority-by agent`'
                      % (rid, rec.get('priority'), rid))
    if minus_one and ruling is None:
        raise Refusal('`%s` is `priority: -1` - the owner\'s question (%s) - so pulling it takes '
                      '`--ruling R-nnnn`, a ruling that names it' % (rid, rec.get('owner_ask')))
    if blocker is not None:
        other = snap.get(blocker)
        if other is None or 'item:%s' % rid not in other.all('blocked_on'):
            raise Usage('`--blocker %s` is not a record blocked on `item:%s`' % (blocker, rid))
    missing = ctx.unmet(snap)(rec)
    if missing:
        raise Refusal('`%s` requires %s, which is unmet: %s' % ((rid,) + missing[0]))
    branch = ctx.current_branch()
    offers, passed, holding = current_offer(snap, ctx)
    offered = rec in [o.rec for o in offers]
    if not offered and reason is None:
        raise Refusal('`%s` is not in `next`\'s offer - %s. An out-of-order pull is allowed and '
                      'recorded: `pull %s --reason "<why>"`'
                      % (rid, omod.not_offered_why(rec, passed, ctx.session), rid))
    if minus_one:
        lmod.refuse_unnamed_release(rid, rec, ruling, 'pull')
        lmod.check_ruling(ctx.ledger, rid, rec, ruling, 'pull', hold_only=True)
    lines = ['PULLED %s - state: open -> pulled' % rid, '  pulled_at:  %s' % ctx.date(),
             '  pulled_by:  %s' % branch]
    today = ctx.date()
    changes = [('state', ['pulled']), ('pulled_at', [today]), ('pulled_by', [branch])]
    subject = 'backlog: pull %s onto %s' % (rid, branch)
    if offered:
        changes += [(k, []) for k in rmod.PREEMPT_KEYS]
    else:
        recorded = reason
        if ruling is not None and not minus_one:
            bad = None
            if ctx.ledger.configured:
                entries, why = ctx.ledger.entries()
                bad = why if entries is None else lmod.ruling_refusal(ctx.ledger, entries, ruling,
                                                                     lmod.HOLD_KINDS)
            recorded = ('%s (context, unchecked: %s)' % (reason, ruling) if bad
                        else '%s (PM %s)' % (reason, ruling))
            if bad:
                lines.append('warning: `--ruling %s` is not citable (%s); recorded as unchecked'
                             % (ruling, bad))
        if blocker is not None:
            recorded += ' (unblocks %s)' % blocker
        for other in holding:
            try:
                if smod.scopes_collide(rec.all('scope'), other.all('scope')):
                    lines.append('warning: scope collides with `%s`, in flight on %s'
                                 % (other.id, other.get('pulled_by')))
            except smod.UndecidableScope as exc:
                lines.append('warning: scope cannot be compared with `%s` (in flight), so it is a '
                             'collision: %s' % (other.id, exc))
        displaced = ['%s rank %s priority %s' % (o.rec.id, o.rank, o.rec.get('priority'))
                     for o in offers] or ['-']
        changes += [('preempt', [recorded]), ('preempt_by', [branch]), ('preempt_at', [today]),
                    ('displaced', displaced)]
        if minus_one:
            changes += [('hold_ruling', [ruling]), ('priority', ['-']), ('owner_ask', [])]
            lines.append('  priority: -1 -> - (hold released; the question was: %s)'
                         % rec.get('owner_ask'))
        lines.append('  preempt:    %s' % recorded)
        lines += ['  displaced:  %s' % d for d in displaced]
        subject += ', pre-empting %d offered item%s' % (len(offers), '' if len(offers) == 1 else 's')
        if minus_one:
            subject += ', releasing the owner hold'
    pairs = rmod.apply_changes(rec.pairs(), changes)
    return Change({rid: _text(pairs, rec.body)}, subject, lines,
                  ruling=ruling if (minus_one or not offered) else None)


def branch_name_error(name, root):
    r = cmd.call(['check-ref-format', '--branch', name], root)
    if r.rc != 0 or cmd.text(r.out).strip() != name:
        return '`%s` is not a branch name' % name
    return None


def plan_release(snap, ctx, rid, reason):
    """Return a pull to `state: open`: the caller's own pull, or a dead one (no local branch, no
    remote-tracking branch, no worktree by its `pulled_by`). Pre-emption lines go with it, or the
    released record would keep its pre-emption sort."""
    refuse_machine_paths('--reason', reason)
    bad = reason_error(reason, '--reason')
    if bad:
        raise Usage(bad)
    rec = require(snap, rid)
    if rec.get('state') != 'pulled':
        raise Usage('`%s` is `state: %s`, not `pulled`: there is no pull to release'
                    % (rid, rec.get('state')))
    holder = rec.get('pulled_by')
    if not holder:
        raise Failure('`%s` is pulled with no `pulled_by`; `check` names it' % rid)
    mine = ctx.branch or refs.current_branch(ctx.root)[0]
    if holder != mine:
        bad = branch_name_error(holder, ctx.root)
        if bad:
            raise Refusal('%s, so what holds the pull of `%s` cannot be checked' % (bad, rid))
        names, why = omod.live_branches(ctx.root, ctx.remote)
        if names is None:
            raise Failure('cannot tell whether the pull of `%s` by `%s` is dead: %s' % (rid, holder, why))
        if holder in names:
            raise Refusal('the pull of `%s` by `%s` is live (a branch or worktree by that name '
                          'exists). Its holder releases it, from that branch' % (rid, holder))
    changes = [('state', ['open'])] + [(k, []) for k in rmod.PULL_KEYS + rmod.PREEMPT_KEYS]
    pairs = rmod.apply_changes(rec.pairs(), changes)
    text = ' '.join(reason.split())
    return Change({rid: _text(pairs, rec.body)},
                  'backlog: release %s from %s: %s' % (rid, holder, text),
                  ['RELEASED %s - state: pulled -> open' % rid,
                   '  was pulled_at: %s' % rec.get('pulled_at'),
                   '  was pulled_by: %s%s' % (holder, '' if holder == mine else ' (dead)')])


# --- close / retire -------------------------------------------------------------------------------

def dependent_rewrites(snap, rid):
    """`({stem: text}, lines)`: every record blocked on `item:<rid>`, without that member; one
    left with none takes `blocked_on: -` and, if blocked, `state: open`."""
    target = 'item:%s' % rid
    writes, lines = {}, []
    for other in snap.readable:
        members = other.all('blocked_on')
        if other.id == rid or target not in members:
            continue
        left = [m for m in members if m != target] or ['-']
        opened = left == ['-'] and other.get('state') == 'blocked'
        changes = [('blocked_on', left)] + ([('state', ['open'])] if opened else [])
        writes[other.stem] = _text(rmod.apply_changes(other.pairs(), changes), other.body)
        lines.append('UNBLOCKED %s - `%s` removed -> %s%s' % (other.id, target, ', '.join(left),
                                                              '; state: open' if opened else ''))
    return writes, lines


def _claim_subject(verb, rid, reason, flag):
    refuse_machine_paths(flag, reason)
    bad = reason_error(reason, flag)
    if bad:
        raise Usage(bad)
    return '%s %s: %s' % (verb, rid, ' '.join(reason.split()))


def plan_close(snap, ctx, rid, reason, ruling=None):
    """The work is done: delete the record. An owner-held record takes `--ruling` (deleting it
    ends his hold); its own live `hold_ruling` is accepted."""
    rec = require(snap, rid)
    subject = _claim_subject('closes', rid, reason, '--reason')
    held = rmod.is_owner_held(rec)
    if held and ruling is None:
        raise Refusal('`%s` is owner-held (%s), so closing it needs `--ruling R-nnnn`%s'
                      % (rid, rmod.owner_hold_label(rec),
                         '; its own `hold_ruling: %s` is live and accepted' % rec.get('hold_ruling')
                         if lmod.hold_live(rec) else ''))
    own = ruling is not None and ruling == rec.get('hold_ruling') and lmod.hold_live(rec)
    if ruling is not None:
        if not held and not own:
            raise Usage('`--ruling %s` was given, and `%s` is not owner-held and it is not the '
                        'record\'s own live `hold_ruling`' % (ruling, rid))
        if held:
            lmod.refuse_unnamed_release(rid, rec, ruling, 'close')
        if not own:
            lmod.check_ruling(ctx.ledger, rid, rec, ruling, 'close', hold_only=True)
    writes, lines = dependent_rewrites(snap, rid)
    writes[rid] = None
    lines.insert(0, 'CLOSED %s' % rid)
    if rec.get('state') != 'pulled':
        lines.append('warning: `%s` was `state: %s`, never pulled; it closes anyway'
                     % (rid, rec.get('state')))
    return Change(writes, subject, lines, ruling=ruling)


def plan_retire(snap, ctx, rid, why, ruling=None):
    """The work should not be done: delete the record. An owner-prioritised or owner-held record
    takes `--ruling`, an owner kind whose `Items:` line names it (never a delegated entry)."""
    rec = require(snap, rid)
    subject = _claim_subject('retires', rid, why, '--why')
    guarded = rmod.is_owner_prioritised(rec) or rmod.is_owner_held(rec)
    if guarded and ruling is None:
        raise Refusal('`%s` is %s, so retiring it needs `--ruling R-nnnn`'
                      % (rid, 'owner-held (%s)' % rmod.owner_hold_label(rec)
                         if rmod.is_owner_held(rec)
                         else 'owner-prioritised (`priority_by: %s`)' % rec.get('priority_by')))
    if ruling is not None:
        if not guarded:
            raise Usage('`--ruling %s` was given, and `%s` is neither owner-prioritised nor '
                        'owner-held: its retirement needs no ruling' % (ruling, rid))
        lmod.check_ruling(ctx.ledger, rid, rec, ruling, 'retirement')
    writes, lines = dependent_rewrites(snap, rid)
    writes[rid] = None
    lines.insert(0, 'RETIRED %s' % rid)
    return Change(writes, subject, lines, ruling=ruling)
