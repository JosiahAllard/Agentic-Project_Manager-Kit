"""kitlib.maintlint.worktrees: the repository's worktrees by kitlib.worktree.classify.classify's
class, read-only (no apply, no mark; docs/maint-lint.md "Worktree checks").

  findings(top, classify)   `(findings, notes)`: one per A, B, C, E and O tree (L, live or kept,
                            is never a finding), and one per A tree whose branch still holds a
                            pulled record (landed, but the record was never released)

The class is classify's own judgement; nothing here adds a rule of its own, and a tree's idle
time is never a finding by itself.
"""
from kitlib import kitroot
from kitlib.worktree import classify as cmod
from kitlib.worktree import remove

from . import Finding

REPORTED = ('A', 'B', 'C', 'E', 'O')


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
        if klass not in REPORTED:
            continue
        name = cmod.HEADINGS[klass][0]
        out.append(Finding('worktree-%s' % klass, row['path'],
                           '%s %s: %s (%s)' % (klass, name, _handle(row['path']),
                                               row.get('branch') or 'detached'),
                           fix_of(klass, row, tool, handle)))
        claims = row.get('claims')
        if klass == 'A' and isinstance(claims, list):
            for rid in claims:
                out.append(Finding(
                    'landed-still-pulled', rid,
                    '%s is still pulled, and its branch %s is in the base (landed)'
                    % (rid, row.get('branch')),
                    '%s -C <worktree> release %s --reason "landed"   (or close it if the work is'
                    ' done)' % (backlog, rid)))
    notes = []
    if trees and (res.data or {}).get('unreadable'):
        notes.append('worktrees: %s' % res.data['unreadable'])
    return out, notes
