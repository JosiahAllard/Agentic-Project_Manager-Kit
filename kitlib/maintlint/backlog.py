"""kitlib.maintlint.backlog: findings read from a repository's backlog store, each on a fact
(docs/maint-lint.md "Backlog checks"). No check here flags a record for its age.

  unpriced(snap, cmd)          records next waits to see priced (`backlog.py needs-priority`)
  dead_pulls(snap, ctx, cmd, split)   pulls with no branch by their pulled_by (offer.in_flight_split)
  dead_blockers(snap, cmd)     `item:X` blockers naming no live record (store.dangling_items)
  duplicates(snap, cmd)        live records with the same summary, word for word
  audit(root, cmd, home, now, run)    kitlib.backlog.audit_check.run, days=7, fetch=False

Each returns `(findings, notes)`. Nothing here writes: the fix is printed, never run.
"""
import re

from kitlib import kitroot
from kitlib.backlog import audit_check
from kitlib.backlog import offer as omod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod

from . import Finding

AUDIT_DAYS = 7                  # the audit log window read; the findings are store changes no
                                # log line covers, never an age
AUDIT_KINDS = ('UNAUDITED', 'UNKNOWN', 'UNMAPPED')


def needs_priority(rec):
    """`backlog.py needs-priority`'s filter (tools/backlog.py do_list)."""
    bucket = omod.classify(rec)[0]
    return (bucket in (omod.UNPRIORITISED_OPEN, omod.OWNER_PRIORITISED)
            or (bucket == omod.REVIEW and rec.get('priority') in ('-', rmod.OWNER_PRIORITY)))


def unpriced(snap, cmd):
    out = []
    for rec in snap.readable:
        if needs_priority(rec):
            owner = rec.get('priority') == rmod.OWNER_PRIORITY
            out.append(Finding(
                'unpriced', rec.stem,
                '%s is open and %s' % (rec.stem, 'waits on the owner\'s price' if owner
                                       else 'has no price'),
                '%s set %s --priority <band> --priority-by <who>' % (cmd, rec.stem)
                if not owner else 'the PM asks the owner for the price of %s' % rec.stem))
    return out, []


def dead_pulls(snap, ctx, cmd, split=omod.in_flight_split):
    holding, dead, why = split(snap, ctx.root, ctx.remote)
    notes = ['dead pulls NOT CHECKED: %s' % why] if why else []
    out = [Finding('dead-pull', r.stem,
                   '%s is pulled by %s, and no branch of that name exists' % (
                       r.stem, r.get('pulled_by')),
                   '%s release %s --reason "<why>"' % (cmd, r.stem)) for r in dead]
    return out, notes


_DANGLING = re.compile(r'`(item:[^`]+)` names no live record')


def dead_blockers(snap, cmd):
    out = []
    for w in smod.dangling_items(snap.by_id):
        stem = w.path.rsplit('/', 1)[-1][:-3] if w.path.endswith('.md') else w.path
        m = _DANGLING.search(w.message)
        member = m.group(1) if m else 'item:<ID>'
        out.append(Finding('dead-blocker', '%s %s' % (stem, member),
                           '%s is blocked on %s, which names no live record (closed or retired)'
                           % (stem, member),
                           '%s unblock %s --on %s --reason "<why>"' % (cmd, stem, member)))
    return out, []


def _words(text):
    return ' '.join(re.findall(r'[a-z0-9]+', (text or '').lower()))


def duplicates(snap, cmd):
    """Live records whose summaries are the same words in the same order (case and punctuation
    aside). A near match is not a finding: whether two records are one is judgement."""
    seen = {}
    for rec in snap.readable:
        key = _words(rec.get('summary'))
        if key:
            seen.setdefault(key, []).append(rec.stem)
    out = []
    for stems in seen.values():
        if len(stems) > 1:
            keep, rest = stems[0], stems[1:]
            out.append(Finding('duplicate', ' '.join(stems),
                               '%s have the same summary' % ', '.join(stems),
                               '; '.join('%s retire %s --why "duplicate of %s"' % (cmd, r, keep)
                                         for r in rest)))
    return out, []


def audit(root, cmd, home=None, now=None, run=audit_check.run):
    code, lines = run(root, days=AUDIT_DAYS, fetch=False, home=home, now=now)
    if code == audit_check.NOT_CHECKED:
        return [], ['audit %s' % (lines[0] if lines else 'NOT CHECKED')]
    out = []
    for line in lines:
        kind = line.split(' ', 1)[0]
        if kind in AUDIT_KINDS:
            out.append(Finding('audit', line, line,
                               '%s --days %d --no-fetch --all   (read the change; the PM logs or'
                               ' explains it)' % (kitroot.command('backlog_audit', root),
                                                  AUDIT_DAYS)))
    return out, []
