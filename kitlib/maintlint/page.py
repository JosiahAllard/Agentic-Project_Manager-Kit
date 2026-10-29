"""kitlib.maintlint.page: a run's result as one static HTML page that kitlib.hubsite.pagecheck.check
passes (docs/maint-lint.md "Output"): no script, no link, no url(); every value escaped.

  render(result)   the whole document, as a string

Record text is the repository's, not the kit's, so beyond html.escape the two words pagecheck
refuses anywhere in the text (`javascript:` and `http-equiv`) are written with a character
reference, which a reader sees unchanged. A page that would pass pagecheck's size cap shows the
first findings of each check, and the first notes, and says how many it left out.
"""
import html
import re

from kitlib.hubsite import pagecheck

STYLE = ('body{font:14px/1.45 system-ui,sans-serif;margin:16px;max-width:1100px;color:#1d1d1f;'
         'background:#fff}h1{font-size:20px}h2{font-size:16px;margin-top:24px}'
         'table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:4px 6px;'
         'text-align:left;vertical-align:top}code{font:12px/1.4 Consolas,monospace;'
         'white-space:pre-wrap;word-break:break-all}.new{font-weight:bold;color:#a40}'
         '@media (prefers-color-scheme:dark){body{color:#e8e8e8;background:#1b1b1d}'
         'td,th{border-color:#555}.new{color:#f96}}')
_DEFANG = [(re.compile('javascript:', re.I), lambda m: m.group(0)[:-1] + '&#58;'),
           (re.compile('http-equiv', re.I), lambda m: m.group(0)[:4] + '&#45;' + m.group(0)[5:])]
ROW_STEPS = (None, 200, 50, 10)


_SURROGATE = re.compile('[\ud800-\udfff]')


def esc(value):
    """The value escaped for the page. A lone surrogate (a path read with errors='surrogateescape',
    say) is dropped: it cannot be written as UTF-8."""
    out = html.escape(_SURROGATE.sub('', '' if value is None else str(value)), quote=True)
    for rx, sub in _DEFANG:
        out = rx.sub(sub, out)
    return out


def _section(check, rows, limit):
    shown = rows if limit is None else rows[:limit]
    out = ['<h2>%s (%d)</h2>' % (esc(check), len(rows)),
           '<table><tr><th>Finding</th><th>Fix</th></tr>']
    for r in shown:
        mark = '<span class="new">new</span> ' if r.get('new') else ''
        out.append('<tr><td>%s%s</td><td><code>%s</code></td></tr>'
                   % (mark, esc(r.get('text')), esc(r.get('fix'))))
    out.append('</table>')
    if len(shown) < len(rows):
        out.append('<p>%d more not shown: result.json has them all.</p>' % (len(rows) - len(shown)))
    return out


def _module_scope(s, limit):
    """The "Module scope" section of the gates' scope sum (kitlib.modulemap.scopelog.summary):
    nothing when the repository names no module map (None). Module names and edge reasons only,
    never a path."""
    if not isinstance(s, dict):
        return []
    runs, widened = s.get('runs', 0), s.get('widened', 0)
    out = ['<h2>Module scope</h2>',
           '<p>%d gate run%s in %d days kept a scope record (%d head%s); %d widened beyond the '
           'changed modules%s.</p>' % (
               runs, '' if runs == 1 else 's', s.get('days', 0), s.get('heads', 0),
               '' if s.get('heads') == 1 else 's', widened,
               ' (%d%%)' % round(100.0 * widened / runs) if runs else '')]
    routes = s.get('routes') or []
    if not routes:
        return out
    shown = routes if limit is None else routes[:limit]
    out.append('<table><tr><th>Changed module</th><th>Widened to</th><th>Why</th><th>Runs</th></tr>')
    out += ['<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>'
            % (esc(r.get('from')), esc(', '.join(r.get('modules') or ())), esc(r.get('why')),
               r.get('runs', 0)) for r in shown]
    out.append('</table>')
    if len(shown) < len(routes):
        out.append('<p>%d more not shown: result.json has them all.</p>' % (len(routes) - len(shown)))
    return out


def _document(result, limit):
    counts = result.get('counts') or {}
    rows = result.get('findings') or []
    total = len(rows)
    body = ['<h1>Maintenance lint: %s</h1>' % esc(result.get('repo')),
            '<p>%s, %s s. %d finding%s. Every fix is printed, never run.</p>'
            % (esc(result.get('started')), esc(result.get('elapsed_s')), total,
               '' if total == 1 else 's'),
            '<h2>Counts</h2><table><tr><th>Check</th><th>Findings</th></tr>']
    body += ['<tr><td>%s</td><td>%d</td></tr>' % (esc(c), n) for c, n in counts.items()]
    body.append('</table>')
    ledger = result.get('ledger')
    body.append('<h2>Decision ledger</h2>')
    if ledger is None:
        body.append('<p>Not checked.</p>')
    else:
        files = ledger.get('rules') or {}
        over = ledger.get('over_budget') or []
        body.append('<p>%d stale citation%s in the STANDING digest. Rulebooks: %s. Over budget: %s. '
                    '%d possible conflicting pair%s; %d override%s.</p>' % (
                        ledger.get('stale_cites', 0),
                        '' if ledger.get('stale_cites') == 1 else 's',
                        esc(', '.join('%d rules in %s' % (n, f) for f, n in files.items())
                            or 'none'),
                        esc(', '.join(over) or 'none'),
                        ledger.get('conflict_pairs', 0),
                        '' if ledger.get('conflict_pairs') == 1 else 's',
                        ledger.get('overrides', 0),
                        '' if ledger.get('overrides') == 1 else 's'))
    body += _module_scope(result.get('module_scope'), limit)
    by_check = {}
    for r in rows:
        by_check.setdefault(r.get('check'), []).append(r)
    for check, group in by_check.items():
        body += _section(check, group, limit)
    notes = result.get('notes') or []
    if notes:
        shown = notes if limit is None else notes[:limit]
        body.append('<h2>Notes (%d)</h2><ul>%s</ul>'
                    % (len(notes), ''.join('<li>%s</li>' % esc(n) for n in shown)))
        if len(shown) < len(notes):
            body.append('<p>%d more not shown: result.json has them all.</p>'
                        % (len(notes) - len(shown)))
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body>%s</body></html>\n'
            % ('Maintenance lint ' + esc(result.get('repo')), STYLE, '\n'.join(body)))


def render(result):
    for limit in ROW_STEPS:
        doc = _document(result, limit)
        if len(doc.encode('utf-8')) <= pagecheck.MAX_BYTES:
            return doc
    return doc
