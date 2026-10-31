"""The partition, the rank order and the offer.

Every record lands in exactly one bucket, and the banner refuses to print a partition that does
not add up. The offer is a greedy fill down the rank order: scopes of live pulls are occupied
first, and a comparison that cannot be decided is a collision, never a clearance.
"""
import re

from kitlib.git import cmd, refs

from . import record as rmod
from . import scope as smod

PULLABLE = 'pullable'
HELD = 'held'
PARKED = 'parked'
IN_FLIGHT = 'in flight'
UNPRIORITISED_OPEN = 'unprioritised+open'
REVIEW = 'to review'
UNCLASSIFIED = 'unclassifiable'
NOT_READ = 'unreadable'
BUCKETS = (PULLABLE, HELD, PARKED, IN_FLIGHT, UNPRIORITISED_OPEN, REVIEW, UNCLASSIFIED, NOT_READ)
EFFORT_ORDER = {'s': 0, 'm': 1, 'l': 2, '-': 3}
PROBE_TIMEOUT = 60
DEFAULT_CAP = 5


class PartitionError(Exception):
    """The buckets did not account for every file."""


def classify(rec):
    """`(bucket, None)`, or `(UNCLASSIFIED, why)`. Nothing is inferred."""
    if not rec.readable:
        return NOT_READ, None
    state = rec.get('state')
    if state is None or rmod._state(state) is not None:
        return UNCLASSIFIED, 'no readable `state`'
    if state == 'blocked':
        return HELD, None
    if state == 'parked':
        return PARKED, None
    if state == 'pulled':
        return IN_FLIGHT, None
    if state != 'open':
        return UNCLASSIFIED, '`state: %s` parses but has no bucket' % state
    if rec.has('review'):
        # Marked by the Action Hub's add: never offered, priced or not, until `clear-review`.
        return REVIEW, None
    priority = rec.get('priority')
    if priority is None or rmod._priority(priority) is not None:
        return UNCLASSIFIED, 'no readable `priority`'
    if priority == '-':
        return UNPRIORITISED_OPEN, None
    if rmod.is_band(priority):
        return PULLABLE, None
    return UNCLASSIFIED, '`priority: %s` parses but has no bucket' % priority


class Banner(object):
    """The line every read prints first, and the partition behind it."""

    def __init__(self, snap):
        self.snap = snap
        self.buckets = {k: [] for k in BUCKETS}
        self.unclassifiable_why = []
        for rec in snap.records:
            bucket, why = classify(rec)
            self.buckets[bucket].append(rec)
            if why:
                self.unclassifiable_why.append((rec, why))
        total = sum(len(v) for v in self.buckets.values())
        if total != snap.file_count:
            raise PartitionError('%d records in buckets, %d files read' % (total, snap.file_count))

    def count(self, bucket):
        return len(self.buckets[bucket])

    def held_groups(self):
        """`([(group, n)], multi)`: each held record counted once, under its first group."""
        counts = dict.fromkeys(rmod.HELD_ORDER, 0)
        multi = unaccounted = 0
        for rec in self.buckets[HELD]:
            kinds = [k for k in (rmod.blocker_kind(v) for v in rec.all('blocked_on')) if k]
            if len(kinds) > 1:
                multi += 1
            for group in rmod.HELD_ORDER:
                if group in kinds:
                    counts[group] += 1
                    break
            else:
                unaccounted += 1
        groups = [(g, counts[g]) for g in rmod.HELD_ORDER if counts[g]]
        if unaccounted:
            groups.append(('unaccounted (blocked with no nameable blocker)', unaccounted))
        if sum(n for _, n in groups) != self.count(HELD):
            raise PartitionError('the held breakdown does not sum to the held count')
        return groups, multi

    def lines(self):
        parts = ['BACKLOG %s' % self.snap.head_label(),
                 '%d records' % self.snap.file_count,
                 '%d pullable' % self.count(PULLABLE),
                 '%d held' % self.count(HELD),
                 '%d unprioritised+open' % self.count(UNPRIORITISED_OPEN)]
        if self.count(REVIEW):
            parts.append('%d %s' % (self.count(REVIEW), REVIEW))
        for bucket in (PARKED, IN_FLIGHT, NOT_READ, UNCLASSIFIED):
            if self.count(bucket):
                parts.append('%d %s' % (self.count(bucket), bucket))
        out = [' | '.join(parts)]
        groups, multi = self.held_groups()
        if self.count(HELD):
            line = 'held %d: %s' % (self.count(HELD), ', '.join('%d %s' % (n, g) for g, n in groups))
            if multi:
                line += '  (%d held on more than one thing, counted under the first)' % multi
            out.append(line)
        return out

    def queue_warning(self):
        """A line under 10 pullable, or under 3 pullable at priority 1-2; never an exit code."""
        pullable = self.buckets[PULLABLE]
        high = [r for r in pullable if r.get('priority') in ('1', '2')]
        if len(pullable) >= 10 and len(high) >= 3:
            return None
        groups, _ = self.held_groups()
        largest = max(groups, key=lambda gn: gn[1]) if groups else None
        why = ('%d pullable' % len(pullable) if len(pullable) < 10
               else '%d pullable at priority 1-2' % len(high))
        return 'QUEUE DEPTH: %s. Largest held group: %s.' % (
            why, '%s (%d)' % largest if largest else 'none - nothing is held')


# --- rank -----------------------------------------------------------------------------------------

def is_ranked(rec):
    return rec.readable and rmod.is_band(rec.get('priority'))


def rank_key(rec):
    """Preempt, pin, then `resume` (an unparked record: above priority, never a pin),
    priority, effort, id."""
    pin = rec.get('pin')
    pinned = pin is not None and rmod._pin(pin) is None
    resume = rec.get('resume')
    resumed = resume is not None and rmod._ruling(resume) is None
    effort = rec.get('effort')
    if effort not in EFFORT_ORDER:
        effort = '-'
    return (0 if rec.has('preempt') else 1, 0 if pinned else 1, int(pin) if pinned else 0,
            0 if resumed else 1, int(rec.get('priority')), EFFORT_ORDER[effort], rec.id)


def ranked(snap):
    """`{id: rank}` over every banded record, 1-based. Blocked and pulled records rank too."""
    order = sorted((r for r in snap.readable if is_ranked(r)), key=rank_key)
    return {r.id: i for i, r in enumerate(order, 1)}


def rank_label(rec, ranks):
    if rec.has('preempt'):
        return '*'
    return str(ranks.get(rec.id, '-')) if is_ranked(rec) else '-'


# --- in flight ------------------------------------------------------------------------------------

def live_branches(root, remote):
    """`(names, None)`: every local branch, `<remote>/<name>` and worktree branch; `(None, why)`
    when git fails (half an answer is no answer)."""
    out, why = cmd.run_text(['for-each-ref', '--format=%(refname)', 'refs/heads',
                             'refs/remotes/%s' % remote], root, timeout=PROBE_TIMEOUT)
    if out is None:
        return None, why
    trees, why = cmd.run_text(['worktree', 'list', '--porcelain'], root, timeout=PROBE_TIMEOUT)
    if trees is None:
        return None, why
    names = set()
    for ref in out.split('\n'):
        for prefix in ('refs/heads/', 'refs/remotes/%s/' % remote):
            if ref.startswith(prefix):
                names.add(ref[len(prefix):])
    for line in trees.split('\n'):
        if line.startswith('branch refs/heads/'):
            names.add(line[len('branch refs/heads/'):].strip())
    return names, None


def in_flight_split(snap, root, remote):
    """`(holding, dead, why)` over the pulled records. A pull is dead only when git answered and
    found no branch by its `pulled_by`; a failed probe holds every pull (fail closed)."""
    pulled = [r for r in snap.records if classify(r)[0] == IN_FLIGHT]
    if not pulled:
        return [], [], None
    names, why = live_branches(root, remote)
    if names is None:
        return pulled, [], why
    holding = [r for r in pulled if r.get('pulled_by') in names]
    return holding, [r for r in pulled if r not in holding], None


# --- requires -------------------------------------------------------------------------------------

def unmet_requires(rec, snap, history, root, main_branch):
    """`[(member, why)]` for every gated `requires:` line not met. `item:X` is met when X has no
    live record and the store history closed it; `commit:H` when H is on `main_branch`. Any
    other `requires:` line is a note and gates nothing. A git that cannot answer is unmet."""
    out = []
    for member in rec.all('requires'):
        kind, _, target = member.partition(':')
        if kind == 'item' and target and snap.rules.id_re.match(target):
            live = snap.get(target)
            if live is not None:
                out.append((member, 'it is still `state: %s`, not closed' % live.get('state')))
                continue
            last = history(target)
            if last is None or not re.match(r'^\S+ closes ', last):
                out.append((member, 'retired or deleted, so it will never land' if last else
                            'no record has that id and none was closed'))
        elif kind == 'commit' and target:
            ok, why = refs.is_ancestor(target, 'refs/heads/%s' % main_branch, root)
            if not ok:
                out.append((member, 'it is not on %s' % main_branch if ok is False
                            else 'git cannot tell (%s)' % why))
    return out


# --- the offer ------------------------------------------------------------------------------------

class Offer(object):
    def __init__(self, rec, rank, seeded=False):
        self.rec, self.rank, self.seeded = rec, rank, seeded


class PassedOver(object):
    def __init__(self, rec, rank, why, blocker=None):
        # `blocker`: `(who, record id)` of the live pull or other session's record whose scope or
        # resource this one collides with; None for any other reason (`blocker_summary` counts
        # these).
        self.rec, self.rank, self.why, self.blocker = rec, rank, why, blocker


# --- resources ------------------------------------------------------------------------------------

def uses(rec):
    """The record's `uses:` names, each once."""
    return list(dict.fromkeys(rec.all('uses')))


def live_label(rec):
    return '%s (in flight on %s)' % (rec.id, rec.get('pulled_by') or '?')


def resource_load(holding, capacity):
    """`{name: [the live pulls of `holding` using it]}` for every name `capacity` declares. A name
    it does not declare counts nothing (`check` flags it). One git-free pass, so `show` shares it
    with the offer without computing one."""
    load = {name: [] for name in capacity}
    for rec in holding:
        for name in uses(rec):
            if name in load:
                load[name].append(rec)
    return load


def capacity_why(name, limit, takers):
    return 'resource %s at capacity %d, taken by %s' % (name, limit, ', '.join(takers))


def undeclared_why(names):
    return 'uses %s, which .kit.json does not declare' % ', '.join(names)


def uses_line(rec, holding, capacity):
    """`show`'s line: `uses dev-clone (1/1: X on B), gpu (0/1)`."""
    load = resource_load(holding, capacity)
    parts = []
    for name in uses(rec):
        if name not in capacity:
            parts.append('%s (not declared in .kit.json)' % name)
            continue
        taken = ['%s on %s' % (r.id, r.get('pulled_by') or '?') for r in load[name]]
        parts.append('%s (%d/%d%s)' % (name, len(taken), capacity[name],
                                       ': %s' % ', '.join(taken) if taken else ''))
    return 'uses %s' % ', '.join(parts)


def next_offer(snap, holding, cap=DEFAULT_CAP, seeds=(), session=None, unmet=None,
               folded=None, capacity=None):
    """`((offers, passed), refusals, ranks)`. `holding` are the live pulls whose scopes are taken
    first. `cap` None offers every startable record (`next --all`), with the same pass-overs. `unmet(rec)` -> `[(member, why)]` gates `requires:`. Only records whose `session:`
    equals `session` are offered; another session's record takes its scope silently. A record
    passed over on a collision names the holder: a live pull by id and branch, another session's
    record by id and session, a record already in this offer as `(offered above)`. Scopes are each record's current `scope:` lines, so a pulled record
    narrowed with `set --scope` frees what it gave up. A seed (`next --with`) goes first and is
    not passed over for the rest of the offer, but one that collides with a live pull is passed
    over like any record: only `pull --reason`, by a role that may take the `pull-collision` row
    (kitlib/backlog/roles.py), takes scope a live pull holds.
    `folded(rec)` -> a reason or None: a record of `session` it gives a reason for (its scope
    folds into another live pull's branch, or git cannot tell) is passed over with that reason
    and takes no scope; it is asked only of a record that would otherwise be offered.
    `capacity` ({resource: limit}; None: none declared) counts `uses:` the way scope is counted:
    the live pulls of `holding` first, then, in rank order, the records offered above and other
    sessions' records. A record with no room in any one of its resources is passed over, a live
    pull named as its blocker; each record taking a place takes one unit of each. A seed is
    measured against the live pulls alone. A candidate naming a resource `capacity` does not
    declare is passed over, never offered and never a crash."""
    capacity = capacity or {}
    ranks = ranked(snap)
    occupied = []
    for rec in holding:
        occupied.extend(rec.all('scope'))
    bad = ['an in-flight record has `scope: %s` - %s' % (m, smod.scope_member_error(m, True))
           for m in occupied if smod.scope_member_error(m, True) is not None]
    if bad:
        return None, bad, ranks
    live = list(occupied)

    def own(r):
        return r.get('session') == session

    candidates = sorted((r for r in snap.readable if classify(r)[0] == PULLABLE), key=rank_key)
    mine_by_id = {r.id: r for r in candidates if own(r)}
    ordered = []
    for sid in seeds:
        if sid in mine_by_id and mine_by_id[sid] not in ordered:
            ordered.append(mine_by_id[sid])
    seeded = set(id(r) for r in ordered)
    ordered.extend(r for r in candidates if r not in ordered)

    offers, passed, reached, holders = [], [], set(), {}
    for rec in holding:
        for m in rec.all('scope'):
            holders.setdefault(m, ('%s (in flight on %s)' % (rec.id, rec.get('pulled_by') or '?'),
                                   (rec.get('pulled_by') or '?', rec.id)))
    live_takers = {name: [(live_label(r), (r.get('pulled_by') or '?', r.id)) for r in recs]
                   for name, recs in resource_load(holding, capacity).items()}
    takers = {name: list(held) for name, held in live_takers.items()}
    for rec in ordered:
        reached.add(id(rec))
        rid = rec.id
        scopes = rec.all('scope')
        errs = [(m, smod.scope_member_error(m, True)) for m in scopes]
        errs = [(m, why) for m, why in errs if why is not None]
        if not scopes:
            errs = [('(no scope line)', 'a record with no `scope:` is never offered')]
        if errs:
            if own(rec):
                passed.append(PassedOver(rec, ranks.get(rid, '-'), 'scope undecidable, so this '
                                         'is a collision: `%s` - %s' % errs[0]))
            continue
        # A seed goes first over the rest of this offer, never over a live pull's scope.
        against = live if id(rec) in seeded else occupied
        try:
            hit = smod.colliding_pairs(scopes, against) if against else []
        except smod.UndecidableScope as exc:
            if own(rec):
                passed.append(PassedOver(rec, ranks.get(rid, '-'),
                                         'scope undecidable, so this is a collision: %s' % exc))
            continue
        if hit:
            if own(rec):
                holder, who = holders.get(hit[0][1]) or (None, None)
                passed.append(PassedOver(rec, ranks.get(rid, '-'), 'scope %s collides with %s '
                                         'already taken%s' % (hit[0][0], hit[0][1], ' by %s'
                                         % holder if holder else ''), who))
            continue
        names = uses(rec)
        unknown = [n for n in names if n not in capacity]
        if unknown:
            if own(rec):
                passed.append(PassedOver(rec, ranks.get(rid, '-'), undeclared_why(unknown)))
            continue
        # As for scope: a seed goes first over the rest of this offer, never over a live pull.
        room = live_takers if id(rec) in seeded else takers
        full = [n for n in names if len(room[n]) >= capacity[n]]
        if full:
            if own(rec):
                held = room[full[0]]
                passed.append(PassedOver(rec, ranks.get(rid, '-'),
                                         capacity_why(full[0], capacity[full[0]],
                                                      [label for label, _ in held]),
                                         next((who for _, who in held if who), None)))
            continue
        if own(rec) and unmet is not None:
            missing = unmet(rec)
            if missing:
                passed.append(PassedOver(rec, ranks.get(rid, '-'), 'requires %s, which is unmet: '
                                         '%s' % missing[0]))
                continue
        if own(rec) and folded is not None:
            why = folded(rec)
            if why:
                passed.append(PassedOver(rec, ranks.get(rid, '-'), why))
                continue
        occupied.extend(scopes)
        taker = (('%s (offered above)' % rid, None) if own(rec) else
                 ('%s (session %s)' % (rid, rec.get('session') or 'none'),
                  ('session %s' % (rec.get('session') or 'none'), rid)))
        for m in scopes:
            holders.setdefault(m, taker)
        for name in names:
            takers[name].append(taker)
        if not own(rec):
            continue
        offers.append(Offer(rec, ranks.get(rid, '-'), id(rec) in seeded))
        if cap is not None and len(offers) >= cap:
            break
    for rec in ordered:
        if id(rec) not in reached and own(rec):
            passed.append(PassedOver(rec, ranks.get(rec.id, '-'),
                                     'not reached - the cap was already full'))
    return (offers, passed), [], ranks


def blocker_summary(passed, top=5):
    """`['blocks N: <branch or session> (<record id>)', ...]`: the live pulls and other sessions'
    records that hold the scope of the most passed-over records, most first (ties by name), at
    most `top` lines. Empty when nothing was passed over on such a collision."""
    counts = {}
    for p in passed:
        if p.blocker is not None:
            counts[p.blocker] = counts.get(p.blocker, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return ['blocks %d: %s (%s)' % (n, who, rid) for (who, rid), n in ordered]


def not_offered_why(rec, passed, session=None):
    """Why `rec` is not in the offer, named."""
    bucket, why = classify(rec)
    if bucket == HELD:
        return 'it is held on %s' % ', '.join(rec.all('blocked_on'))
    if bucket == PARKED:
        return 'it is parked'
    if bucket == REVIEW:
        return ('it is marked for review (review: %s): `clear-review %s --reason T` first'
                % (rec.get('review'), rec.id))
    if bucket == UNPRIORITISED_OPEN:
        return 'it is unprioritised, so it has no rank and is never offered'
    if bucket == UNCLASSIFIED:
        return 'it cannot be classified: %s' % why
    if rec.get('session') != session:
        if rec.get('session'):
            return ('it belongs to session `%s`, so only `next --session %s` offers it'
                    % (rec.get('session'), rec.get('session')))
        return 'it has no `session:`, and `--session %s` offers only that session\'s records' % session
    for p in passed:
        if p.rec is rec:
            return 'it was passed over at rank %s: %s' % (p.rank, p.why)
    return 'it is not in the offer'
