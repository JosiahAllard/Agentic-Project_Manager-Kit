"""`check`: every named check with its own denominator (ported from an earlier per-repository
tools/backlog.py; decision cites, anchors, ruling orphans and the UNBUILT lines cut).

A check that looked at nothing over a store that was read is vacuous (an answer); one with no
population at all is red (it never ran). Ruling checks run only when the ledger is configured.
"""
from . import ledger as lmod
from . import offer as omod
from . import record as rmod
from . import scope as smod
from .store import dangling_items

PREEMPTIONS = 'pre-emptions'
PULL_RECORDS = 'pull records'
PRIORITY_RULINGS = 'priority rulings'
HOLD_RULINGS = 'hold rulings'


class CheckResult(object):
    def __init__(self, name, subject, findings, unit='records', warnings=(), proxy=None,
                 population=None, skipped=None):
        self.name = name
        self.subject = subject
        self.findings = list(findings)
        self.unit = unit
        self.warnings = list(warnings)
        self.proxy = proxy
        self.population = population
        self.skipped = skipped          # why the check did not run (not red)

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
        return self.empty or bool(self.findings)

    def line(self):
        if self.skipped is not None:
            return '%-16s SKIPPED - %s' % (self.name, self.skipped)
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


def _check_rulings(snap, ledger, name, key, kinds, skip=None):
    subjects = [r for r in snap.readable if r.has(key) and rmod._ruling(r.get(key)) is None
                and not (skip is not None and skip(r))]
    if not ledger.configured:
        return CheckResult(name, len(subjects), (), unit='ruled records',
                           skipped='no ledger configured (~/.claude-pm-kit/config.json ledger_path)')
    found, warned = [], []
    if subjects:
        entries, why = ledger.entries()
        for rec in subjects:
            rid = rec.get(key)
            if entries is None:
                warned.append(rmod.Finding(name, rec.path, rec.line_of(key), '`%s: %s` not '
                                           'checked - the ledger is unreadable (%s)' % (key, rid, why)))
                continue
            bad = lmod.ruling_refusal(ledger, entries, rid, kinds, allow_retired=key == 'hold_ruling'
                                      and not lmod.hold_live(rec))
            if bad is not None:
                found.append(rmod.Finding(name, rec.path, rec.line_of(key), '`%s`: %s' % (key, bad)))
    return CheckResult(name, len(subjects), found, unit='ruled records', warnings=warned,
                       proxy='checks the id is a ledger entry of the right kind, not what it says')


def check_store(snap, ledger):
    """Every check, each with its own denominator."""
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
        _check_rulings(snap, ledger, PRIORITY_RULINGS, 'priority_ruling', lmod.OWNER_KINDS,
                       skip=lambda r: r.get('priority_by') == 'pm'),
        _check_rulings(snap, ledger, HOLD_RULINGS, 'hold_ruling', lmod.HOLD_KINDS),
    ]
    for r in results:
        r.population = snap.file_count
    return results


def fail_findings(snap):
    """Every FAIL `check` reports for `snap` apart from the ruling checks (they need the ledger):
    a write may not add one (store.check_change), so `check` and writes share one meaning of red."""
    return [f for r in check_store(snap, lmod.Ledger(None, ())) for f in r.findings]
