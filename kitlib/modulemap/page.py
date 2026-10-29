"""The architecture page generated from the module map (docs/module-map.md "The architecture
page").

The page lists the modules, the messaging modules, the allowed edges and the ratchets, each with
the owner's reason, so planning starts from the map. It holds nothing but the map (no counts, no
ceilings), so it changes only when the map does. `.kit.json` "modules".page names the file;
`--page --write` writes it and `--check` fails when it does not match the map.
"""
from .load import ROOT

TITLE = '# Module map'


def _cell(text):
    """`text` safe inside a Markdown table cell: one line, `|` escaped; '-' when empty."""
    text = ' '.join(str(text or '').split())
    return text.replace('|', '\\|') or '-'


def _code(items):
    return ', '.join('`%s`' % i.replace('`', "'") for i in items)


def _table(head, rows):
    out = ['| %s |' % ' | '.join(head), '|%s' % ('---|' * len(head))]
    out.extend('| %s |' % ' | '.join(_cell(c) for c in r) for r in rows)
    return out


def render(mm, map_rel):
    """The page's text (LF, ending in one newline) for the ModuleMap `mm` read from `map_rel`."""
    lines = [TITLE, '',
             'Generated from `%s` by `module_map.py --page --write` (the PM kit\'s '
             'docs/module-map.md). Do not edit it by hand: change the map, then write the page '
             'again; `module_map.py --check` fails while the two differ.' % map_rel, '',
             'A crossing the map does not allow (an edge of its kind, a messaging module or a '
             'ratchet) is refused. A path no module holds is in `%s`.' % ROOT, '',
             '## Modules', '']
    lines += _table(['Module', 'Folders', 'What it is'],
                    [(n, _code(mm.paths_as_written[n]), mm.why.get(n))
                     for n in sorted(mm.modules)])
    lines += ['', '## Messaging', '']
    if mm.messaging:
        lines.append('Every module may use %s.' % ', '.join(mm.messaging))
    else:
        lines.append('None: no module is open to every other.')
    lines += ['', '## Allowed edges', '']
    if mm.edges:
        lines += _table(['From', 'To', 'Kind', 'Why'],
                        [(e['from'], e['to'], e['kind'], e['why']) for e in mm.edges])
    else:
        lines.append('None.')
    lines += ['', '## Ratchets', '']
    if mm.ratchets:
        lines.append('Crossings counted, not refused; each count is driven down to its goal.')
        lines.append('')
        lines += _table(['Ratchet', 'From', 'To', 'Counts', 'Goal', 'Record'],
                        [(r['id'], r['from'], r['to'], _counts(r), _goal(r), r.get('record'))
                         for r in mm.ratchets])
    else:
        lines.append('None.')
    return '\n'.join(lines) + '\n'


def _counts(r):
    if r.get('imports'):
        return 'java-imports of %s in %s' % (', '.join(r['imports']), ', '.join(r['files']))
    what = ', '.join(r.get('kinds') or ['every kind'])
    return '%s (%s only)' % (what, r['detector']) if r.get('detector') else what


def _goal(r):
    return '%d (%s)' % (r['goal'], r['goal_reason']) if r.get('goal_reason') else str(r['goal'])


def stale(current, expected):
    """True when the page text on disk (`current`, None when missing) differs from `expected`.
    Line endings do not count: a checkout may hold the page with CRLF."""
    if current is None:
        return True
    return current.replace('\r\n', '\n') != expected
