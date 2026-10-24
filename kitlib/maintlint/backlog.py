"""kitlib.maintlint.backlog: findings read from a repository's backlog store, each on a fact
(docs/maint-lint.md "Backlog checks"). No check here flags a record for its age; a finding's age
(the day it was pulled or parked) is context only.

  unpriced(snap, cmd)          records next waits to see priced (`backlog.py needs-priority`)
  dead_pulls(snap, ctx, cmd, split)   pulls with no branch by their pulled_by (offer.in_flight_split)
  dead_blockers(snap, cmd)     `item:X` blockers naming no live record (store.dangling_items)
  duplicates(snap, cmd)        live records with the same summary, word for word
  answered(snap, cmd, questions)      parked records whose hub question the owner has answered
  audit(root, cmd, home, now, run)    kitlib.backlog.audit_check.run, days=7, fetch=False: one
                               count finding per kind, never the lines, and its NOTE lines as
                               notes

Each returns `(findings, notes)`. Nothing here writes: the fix is printed, never run.
"""
import re

from kitlib import kitroot
from kitlib.backlog import audit_check
from kitlib.backlog import offer as omod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.config.types import ConfigError

from . import Finding

AUDIT_DAYS = 7                  # the audit log window read; the findings are store changes no
                                # log line covers, never an age
AUDIT_KINDS = {'UNAUDITED': 'store change(s) in %d days with no audit line',
               'UNKNOWN': 'store change(s) in %d days whose subject names no backlog verb',
               'UNMAPPED': 'audit line(s) in %d days whose role the role map does not list'}
# a hub question's status once the owner has answered it; `done`: the PM marked it handled with
# no ruling (hub.py questions done); `filed`: the hub wrote the answer into the bound record
# itself, so the PM has no log step left before the unpark
ANSWERED = ('answered', 'filed', 'logged', 'done')


def age_of(rec):
    """A record's age as context: the day it was pulled or parked, else None."""
    for key in ('pulled_at', 'parked_at'):
        if rec is not None and rec.get(key) not in (None, '', '-'):
            return '%s %s' % (key, rec.get(key))
    return None


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
                if not owner else 'the PM asks the owner for the price of %s' % rec.stem,
                age_of(rec)))
    return out, []


def dead_pulls(snap, ctx, cmd, split=omod.in_flight_split):
    holding, dead, why = split(snap, ctx.root, ctx.remote)
    notes = ['dead pulls NOT CHECKED: %s' % why] if why else []
    out = [Finding('dead-pull', r.stem,
                   '%s is pulled by %s, and no branch of that name exists' % (
                       r.stem, r.get('pulled_by')),
                   '%s release %s --reason "<why>"' % (cmd, r.stem), age_of(r)) for r in dead]
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
                           '%s unblock %s --on %s --reason "<why>"' % (cmd, stem, member),
                           age_of(snap.by_id.get(stem))))
    return out, []


def _words(text):
    return ' '.join(re.findall(r'[a-z0-9]+', (text or '').lower()))


def duplicates(snap, cmd):
    """Live records whose summaries are the same words in the same order (case and punctuation
    aside). A near match is not a finding: whether two records are one is judgement. The target
    is the sorted ids, whatever order the store lists them in."""
    seen = {}
    for rec in snap.readable:
        key = _words(rec.get('summary'))
        if key:
            seen.setdefault(key, []).append(rec.stem)
    out = []
    for stems in seen.values():
        if len(stems) > 1:
            stems = sorted(stems)
            keep, rest = stems[0], stems[1:]
            out.append(Finding('duplicate', ' '.join(stems),
                               '%s have the same summary' % ', '.join(stems),
                               '; '.join('%s retire %s --why "duplicate of %s"' % (cmd, r, keep)
                                         for r in rest)))
    return out, []


def hub_questions():
    """Every question document on the hub of this machine, read with the agent token
    (kitlib.hubsite.client, as tools/hub.py reads them). Raises what the client raises
    (serve.ServeError and its kinds), and ConfigError for a hub config that cannot be read."""
    from kitlib.hubsite import client, settings
    return client.Client(settings.load().port).listing('questions', status='all',
                                                         role=client.AGENT)


def answered(snap, cmd, questions=None):
    """A parked record whose question (`q`) the owner has answered on the hub: its wait is over.
    `questions`: a callable giving the question documents (None: hub_questions). A hub that
    cannot be asked is a NOT CHECKED note, never a failure; with no parked record it is not
    asked. The text names the question and its status, never the ruling."""
    parked = [r for r in snap.readable if r.get('state') == 'parked' and r.get('q')]
    if not parked:
        return [], []
    from kitlib.hubsite import serve
    try:
        docs = (questions or hub_questions)()
    except (serve.ServeError, ConfigError, OSError, ValueError) as exc:
        return [], ['answered questions NOT CHECKED: %s' % ' '.join(str(exc).split())]
    status = {str(d.get('id')): d.get('status') for d in docs or () if isinstance(d, dict)}
    out = []
    for rec in parked:
        q = rec.get('q')
        got = status.get(q)
        if got not in ANSWERED:
            continue
        if got == 'done':
            fix = ('%s unpark %s; the question was marked done with no ruling, so there is none '
                   'to cite' % (cmd, rec.stem))
        else:
            first = ('' if got in ('logged', 'filed') else
                     'the PM logs the answer (hub.py questions log %s <ruling> --if-version V), '
                     'then ' % q)
            fix = '%s%s unpark %s, giving the ruling it was logged as' % (first, cmd, rec.stem)
        out.append(Finding('answered-question', rec.stem,
                           '%s is parked on %s, which the owner has answered (%s)'
                           % (rec.stem, q, got), fix, age_of(rec)))
    return out, []


def audit(root, cmd, home=None, now=None, run=audit_check.run):
    """One count finding per kind of audit_check line, never the lines themselves: they carry
    commit subjects, whose ids and reasons do not belong on the page. audit_check's NOTE lines
    become notes."""
    code, lines = run(root, days=AUDIT_DAYS, fetch=False, home=home, now=now)
    if code == audit_check.NOT_CHECKED:
        return [], ['audit %s' % (lines[0] if lines else 'NOT CHECKED')]
    counts = dict.fromkeys(AUDIT_KINDS, 0)
    notes = []
    for line in lines:
        if line.startswith('NOTE:'):
            notes.append('audit: %s' % line[len('NOTE:'):].strip())
            continue
        kind = line.split(' ', 1)[0]
        if kind in counts:
            counts[kind] += 1
    fix = ('%s --days %d --no-fetch --all   (read the changes; the PM logs or explains each)'
           % (kitroot.command('backlog_audit', root), AUDIT_DAYS))
    out = [Finding('audit', kind, '%d %s' % (n, AUDIT_KINDS[kind] % AUDIT_DAYS), fix)
           for kind, n in counts.items() if n]
    return out, notes
