"""kitlib.maintlint.worktrees: the repository's worktrees by kitlib.worktree.classify.classify's
class, read-only (no apply, no mark; docs/maint-lint.md "Worktree checks"). It reports what the
nightly sweep (kitlib.worktree.sweep) leaves, never what that sweep removes on its own:

  findings(top, classify)   `(findings, notes)`: one per tree the sweep leaves, by class: A only
                            when a pulled record claims its branch (the sweep keeps it; one
                            landed-still-pulled finding per claim goes with it), B and C only
                            when no stale mark holds on the branch (a mark that holds is the
                            sweep's to abandon; a void one is reported), E and O always. L (live
                            or kept) is never a finding.

The class is classify's own judgement; nothing here adds a rule of its own. A tree's idle time is
its age, context only, never a finding by itself.
"""
from kitlib import kitroot
from kitlib.worktree import classify as cmod
from kitlib.worktree import remove

from . import Finding

REPORTED = ('A', 'B', 'C', 'E', 'O')


def sweep_leaves(row):
    """True when the nightly sweep leaves the tree of this classify row: an A tree only when a
    pulled record claims its branch, a B or C tree only when no stale mark holds on it."""
    klass = row.get('class')
    if klass == 'A':
        claims = row.get('claims')
        return isinstance(claims, list) and bool(claims)
    if klass in ('B', 'C'):
        mark = row.get('mark')
        return not (mark and not mark.get('void'))
    return klass in REPORTED


def _age(row):
    idle = row.get('idle_s')
    return 'idle %dd' % (idle // 86400) if isinstance(idle, (int, float)) else None


def _handle(path):
    return path.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]


def fix_of(klass, row, tool, handle=lambda p: '"%s"' % p.replace('\\', '/')):
    slug = handle(row['path'])
    if klass == 'A':
        return '%s %s' % (tool, slug)
    if klass == 'O':
        return '%s --classify --apply   (from the main checkout; drops the registration)' % tool
    if klass == 'E':
        return '%s %s --abandon "evidence"' % (tool, slug)
    return '%s %s --abandon "<why>"   (after it is taken up: %s)' % (
        tool, slug, cmod.HEADINGS[klass][2])


def findings(top, classify=cmod.classify):
    res = classify(top)
    trees = (res.data or {}).get('trees')
    if trees is None:
        return [], ['worktrees NOT CHECKED: %s' % ((res.data or {}).get('error')
                                                    or ' '.join(res.lines[-1:]) or 'no data')]
    tool = kitroot.command('worktree_remove', top)
    backlog = kitroot.command('backlog')
    ctx, _refused = remove.open_ctx(top)
    handle = ((lambda p: remove.handle(ctx, p)) if ctx is not None
              else (lambda p: '"%s"' % p.replace('\\', '/')))
    out = []
    for row in trees:
        klass = row.get('class')
        if klass not in REPORTED or not sweep_leaves(row):
            continue
        name = cmod.HEADINGS[klass][0]
        age = _age(row)
        out.append(Finding('worktree-%s' % klass, row['path'],
                           '%s %s: %s (%s)' % (klass, name, _handle(row['path']),
                                               row.get('branch') or 'detached'),
                           fix_of(klass, row, tool, handle), age))
        if klass == 'A':
            for rid in row['claims']:
                out.append(Finding(
                    'landed-still-pulled', rid,
                    '%s is still pulled, and its branch %s is in the base (landed)'
                    % (rid, row.get('branch')),
                    '%s -C <worktree> release %s --reason "landed"   (or close it if the work is'
                    ' done)' % (backlog, rid), age))
    notes = []
    if trees and (res.data or {}).get('unreadable'):
        notes.append('worktrees: %s' % res.data['unreadable'])
    return out, notes
