"""`check`: every named check with its own denominator.

A check that looked at nothing over a store that was read is vacuous (an answer); one with no
population at all is red (it never ran). No check reads the ledger: a ruling id on a record is
taken on its word.
"""
from kitlib.git import refs

from . import offer as omod
from . import record as rmod
from . import scope as smod
from .errors import Failure
from .store import dangling_items

PREEMPTIONS = 'pre-emptions'
PULL_RECORDS = 'pull records'


class CheckResult(object):
    def __init__(self, name, subject, findings, unit='records', warnings=(), proxy=None,
                 population=None, skipped=None, error=None):
        self.name = name
        self.subject = subject
        self.findings = list(findings)
        self.unit = unit
        self.warnings = list(warnings)
        self.proxy = proxy
        self.population = population
        self.skipped = skipped          # why the check did not run (not red)
        self.error = error              # why the check cannot run although it must (red)

    @property
    def failing_subjects(self):
        if self.unit in ('records', 'files read'):
            keys = set(f.path for f in self.findings)
        else:
            keys = set((f.path, f.line) for f in self.findings)
        return min(len(keys), self.subject)

    @property
    def vacuous(self):
        return self.subject == 0 and bool(self.population)

    @property
    def empty(self):
        return self.skipped is None and self.subject == 0 and not self.population

    @property
    def red(self):
        return self.error is not None or self.empty or bool(self.findings)

    def line(self):
        if self.skipped is not None:
            return '%-16s SKIPPED - %s' % (self.name, self.skipped)
        if self.error is not None:
            return '%-16s CONFIG ERROR - %s (%d %s not checked)' % (
                self.name, self.error, self.subject, self.unit)
        if self.empty:
            return '%-16s 0 ok, 0 FAILED, SUBJECT EMPTY - nothing was read' % self.name
        if self.vacuous:
            one = self.unit.rstrip('s')
            return ('%-16s 0 ok, 0 FAILED  (no record is %s %s; %d record%s read)'
                    % (self.name, 'an' if one[:1].lower() in 'aeiou' else 'a', one,
                       self.population, '' if self.population == 1 else 's'))
        failed = len(self.findings)
        line = ('%-16s %d ok, %d FAILED  (%d %s, %d finding%s)'
                % (self.name, self.subject - self.failing_subjects, self.failing_subjects,
                   self.subject, self.unit, failed, '' if failed == 1 else 's'))
        if self.warnings:
            line += ', %d WARN' % len(self.warnings)
        return line + ('\n%18s%s' % ('', self.proxy) if self.proxy else '')


def resting_unmapped(snap):
    """`blocked_on: unmapped` parses but must not rest: file the capability as an item."""
    out = []
    for rec in snap.readable:
        for n, key, value in rec.entries:
            if key == 'blocked_on' and value == 'unmapped':
                out.append(rmod.Finding(rmod.BLOCKED_ON, rec.path, n, '`blocked_on: unmapped` is '
                                        'a legal parse with an illegal resting value - file the '
                                        'blocker as an item and use `item:<ID>`'))
    return out


def check_preemptions(snap):
    subjects = [r for r in snap.readable if r.has('preempt')]
    found = []
    for rec in subjects:
        if not rec.all('displaced'):
            found.append(rmod.Finding(PREEMPTIONS, rec.path, rec.line_of('preempt'), '`preempt` '
                                      'with no `displaced:` line - an out-of-order pull records '
                                      'what it displaced'))
        for n, key, value in rec.entries:
            m = snap.rules.displaced_re.match(value) if key == 'displaced' else None
            if m and m.group(1) == rec.id:
                found.append(rmod.Finding(PREEMPTIONS, rec.path, n, '`displaced: %s` names the '
                                          'record itself' % value))
    return CheckResult(PREEMPTIONS, len(subjects), found, unit='pre-empting records',
                       proxy='reads the shape; it cannot tell a computed `displaced:` from a typed one')


def check_pull_records(snap):
    subjects = [r for r in snap.readable if r.get('state') == 'pulled']
    found, warned = [], []
    for rec in subjects:
        if not omod.is_ranked(rec) and not rec.has('preempt'):
            found.append(rmod.Finding(PULL_RECORDS, rec.path, rec.line_of('state'), '`state: '
                                      'pulled` on an unpriced item (`priority: %s`) with no '
                                      '`preempt` record' % rec.get('priority')))
    for i, a in enumerate(subjects):
        for b in subjects[i + 1:]:
            try:
                why = None
                if smod.scopes_collide(b.all('scope'), a.all('scope')):
                    hit = smod.colliding_pairs(b.all('scope'), a.all('scope'))
                    why = 'scope %s collides with %s' % hit[0]
            except smod.UndecidableScope as exc:
                why = 'the scope comparison could not be decided, so it is a collision: %s' % exc
            if why:
                warned.append(rmod.Finding(PULL_RECORDS, b.path, b.line_of('scope'), 'in flight '
                                           'alongside `%s`, and its %s' % (a.id, why)))
    return CheckResult(PULL_RECORDS, len(subjects), found, unit='pulled records', warnings=warned,
                       proxy='reads the shape; colliding in-flight scopes are a WARN')


HOLD_CITATIONS = 'hold citations'


def check_hold_citations(snap):
    """One WARN per `blocked_on: deliberate` or `owner` line on a blocked record whose hold cites
    no ruling, question or item (record.hold_citations; an uncited hold is void). Never
    red: the store holds such records already, and `block` refuses new ones."""
    subjects, warned = 0, []
    for rec in snap.readable:
        if rec.get('state') != 'blocked':
            continue
        for n, key, value in rec.entries:
            if key != 'blocked_on' or value not in rmod.UNCITED_KINDS:
                continue
            subjects += 1
            if not rmod.hold_citations(rec):
                warned.append(rmod.Finding(HOLD_CITATIONS, rec.path, n, '`blocked_on: %s` cites '
                                           'no R-nnnn, Q-nnn, T-nnn, A-nnn or item:<ID> it waits on, so '
                                           'the hold is void: unblock it, or cite the ruling and '
                                           'its end condition with `block %s --on %s --reason '
                                           '"R-nnnn: until ..."`' % (value, rec.id, value)))
    return CheckResult(HOLD_CITATIONS, subjects, (), unit='deliberate or owner holds',
                       warnings=warned, proxy='reads the ids in q, owner_ask, blocked_on and the '
                       '`Blocked on` body lines; hold_ruling and resume are not citations')


DEFAULT_STORE = 'default store'


def check_default_store(snap):
    """One WARN per record of the implicit default store (refs/backlog/main while .kit.json names
    no "backlog".store: store.default_store_refusal), which nothing writes, so a landing closes
    none of its records (at_landing._read_default): they are stranded there. Never red."""
    found = [rmod.Finding(DEFAULT_STORE, rec.path, 1, '%s is held by the default backlog store, '
                          'read only while .kit.json names no "backlog".store, so no landing '
                          'closes it: add it to the live backlog store, or, if this is the live '
                          'store, name it in .kit.json ("backlog": {"store": "ref"})' % rec.id)
             for rec in snap.readable]
    return CheckResult(DEFAULT_STORE, len(snap.readable), (), warnings=found,
                       proxy='a non-empty default store is a WARN, never red')


def load_for_check(store):
    """`(snap, note)`: `store.load()`, note None. One case reads the local ref instead: the
    implicit default store, whose remote has no refs/backlog/main while the local ref holds a
    store. The fetching read refuses that (a write would start a new store without those
    records), but nothing writes the implicit store, and those records are exactly what
    check_default_store reports; that fallback sets `backend.fetch = False` on the store it was
    given, so a later load of it reads the local ref too. Any other Failure, or a remote that
    cannot be asked, stands."""
    backend = store.backend
    try:
        return store.load(), None
    except Failure:
        if not (getattr(backend, 'implicit', False) and backend.remote and backend.fetch):
            raise
        sha, _ = refs.ls_remote(backend.remote, backend.remote_ref, backend.root)
        if sha != '':
            raise
    backend.fetch = False
    return store.load(), ('%s has no %s; read the local %s, which holds the default store'
                          % (backend.remote, backend.remote_ref, backend.local_ref))


USES_NAMES = 'uses names'


def check_uses(snap, resources):
    """One FAIL per `uses:` line naming a resource `.kit.json` does not declare. Such a record is
    never offered, and a live pull of one counts toward nothing (offer.next_offer)."""
    subjects, found = 0, []
    declared = ', '.join(sorted(resources)) or 'none'
    for rec in snap.readable:
        for n, key, value in rec.entries:
            if key != 'uses':
                continue
            subjects += 1
            if value not in resources and rmod._uses(value) is None:
                found.append(rmod.Finding(USES_NAMES, rec.path, n, '`uses: %s` is not in .kit.json '
                                          '"backlog".resources (declared: %s)' % (value, declared)))
    return CheckResult(USES_NAMES, subjects, found, unit='`uses:` lines')


def check_store(snap, implicit=False, resources=None):
    """Every check, each with its own denominator. `implicit`: `snap` is the implicit default
    store's, which adds check_default_store when it holds any file. `resources` ({name: capacity},
    the repository's; None: not read) adds check_uses when a record names one, so a store with no
    `uses:` line reads as it always did, and a write, which passes none, meets no config."""
    by_name = {}
    for f in snap.all_findings():
        by_name.setdefault(f.check, []).append(f)
    readable = snap.readable
    results = [
        CheckResult(rmod.SCHEMA, len(readable), by_name.get(rmod.SCHEMA, ())),
        CheckResult(rmod.UNREADABLE, snap.file_count, by_name.get(rmod.UNREADABLE, ()),
                    unit='files read'),
        CheckResult(rmod.BLOCKED_ON, sum(len(r.all('blocked_on')) for r in readable),
                    list(by_name.get(rmod.BLOCKED_ON, ())) + resting_unmapped(snap),
                    unit='blocked_on lines'),
        CheckResult(rmod.ITEM_TARGETS, sum(1 for r in readable for v in r.all('blocked_on')
                                           if v.startswith('item:')),
                    by_name.get(rmod.ITEM_TARGETS, ()), unit='`item:` members',
                    warnings=dangling_items(snap.by_id)),
        CheckResult(rmod.PIN_UNIQUENESS, sum(1 for r in readable if r.has('pin')),
                    by_name.get(rmod.PIN_UNIQUENESS, ()), unit='pinned records'),
        CheckResult(rmod.ID_FILENAME, len(readable), by_name.get(rmod.ID_FILENAME, ())),
        check_preemptions(snap),
        check_pull_records(snap),
        check_hold_citations(snap),
    ]
    if implicit and snap.file_count:
        results.append(check_default_store(snap))
    if resources is not None and any(r.has('uses') for r in readable):
        results.append(check_uses(snap, resources))
    for r in results:
        r.population = snap.file_count
    return results


def fail_findings(snap):
    """Every FAIL `check` reports for `snap`: a write may not add one (store.check_change), so
    `check` and writes share one meaning of red."""
    return [f for r in check_store(snap) for f in r.findings]
