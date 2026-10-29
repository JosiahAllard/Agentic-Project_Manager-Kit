"""The scope report (docs/module-map.md "The scope report").

For a change (the merge base of the base rev and HEAD against the work tree, plus untracked files
git does not ignore), the report names each changed path's module, then the modules the change
widens to: those that reach a changed module by an allowed edge or a ratchet, and every module
when a messaging module changed. It refuses nothing; a gate step prints it so a widening shows.
"""
import collections

from kitlib.git import cmd as gitcmd
from kitlib.git import refs as gitrefs

from .load import MapError

DIFF_TIMEOUT = 120
# path -> module; changed: the changed modules, sorted; widen: [(module, changed module, why)].
Scope = collections.namedtuple('Scope', 'paths changed widen')


def _names(r, what):
    if r.rc != 0:
        raise MapError('git %s failed: %s' % (what, r.err or 'exit %s' % r.rc))
    return set(n for n in gitcmd.text(r.out).split('\0') if n)


def changed_paths(root, rev):
    """`(sorted repo-relative paths, merge base sha)` changed since the merge base of `rev` and
    HEAD. MapError when git cannot answer."""
    mb, why = gitrefs.merge_base(rev, 'HEAD', root)
    if mb is None:
        raise MapError('cannot read the base %s: %s' % (rev, why))
    if not mb:
        raise MapError('the base %s shares no commit with HEAD' % rev)
    paths = _names(gitcmd.call(['diff', '--no-renames', '--name-only', '-z', mb, '--'], root,
                               timeout=DIFF_TIMEOUT), 'diff')
    paths |= _names(gitcmd.call(['ls-files', '--others', '--exclude-standard', '-z'], root,
                                timeout=DIFF_TIMEOUT), 'ls-files')
    return sorted(paths), mb


def scope_of(mm, paths):
    """The Scope of a change to `paths` under the ModuleMap `mm`."""
    owner = collections.OrderedDict((p, mm.module_of(p)[0]) for p in paths)
    changed = sorted(set(owner.values()))
    widen, seen = [], set(changed)
    for m in changed:
        if m in mm.messaging:
            for other in sorted(mm.modules):
                if other not in seen:
                    seen.add(other)
                    widen.append((other, m, 'via messaging'))
            continue
        for e in mm.edges:
            if e['to'] == m and e['from'] not in seen:
                seen.add(e['from'])
                widen.append((e['from'], m, 'via edge %s: %s' % (e['kind'], e['why'])))
        for r in mm.ratchets:
            if r['to'] == m and r['from'] not in seen:
                seen.add(r['from'])
                widen.append((r['from'], m, 'via ratchet %s' % r['id']))
    return Scope(owner, changed, widen)


def report(s, base):
    """The report's lines for the Scope `s`; `base` names the rev compared with."""
    lines = ['module_map scope: base %s, %d changed path(s) in %d module(s)'
             % (base, len(s.paths), len(s.changed))]
    lines.extend('  %s -> %s' % (p, m) for p, m in s.paths.items())
    lines.extend('widen: %s <- %s  %s' % (m, src, why) for m, src, why in s.widen)
    lines.append('module_map scope: changed %s; widened by %d%s'
                 % (', '.join(s.changed) or 'none', len(s.widen),
                    ': ' + ', '.join(m for m, _, _ in s.widen) if s.widen else ''))
    return lines
