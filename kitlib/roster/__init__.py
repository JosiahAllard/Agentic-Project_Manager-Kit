"""kitlib.roster: docs/ROSTER.md against content/roles and each host's tier -> model map.

ROSTER.md is the one place the owner sets each role's tier and effort, and it shows each host's
tier -> model map. Two tables:
  | role | tier | effort | use for |      one row per role; `-` = no effort key
  | tier | <host> | ... |                 one row per tier, one column per host
Each role file's `tier:` and `effort:` front matter lines must equal its row, and each host
column must equal that host renderer's tier_models() (passed in: kitlib names no host). `check`
lists every mismatch; `write` rewrites only the differing `tier:` / `effort:` lines of role files
and leaves every other byte alone (line endings included); rendering the host files is the
caller's next step (kitlib.content). Pure file work: no printing, no exit.
"""
import os
import re

from kitlib.host.content import TIERS

ROSTER_REL = 'docs/ROSTER.md'
ROLES_REL = 'content/roles'
ROSTER = 'ROSTER.md'
NONE = '-'
_FM = re.compile(rb'\A---(\r?\n)(.*?)\r?\n---(?=\r?\n)', re.S)


class RosterError(Exception):
    """The roster or a role file cannot be read."""


def _tables(text):
    """[(header cells, [row cells, ...])] of every markdown table in `text`."""
    out, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith('|'):
            cur = None
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if cur is None:
            cur = (cells, [])
            out.append(cur)
        elif not all(set(c) <= set('-: ') for c in cells):
            cur[1].append(cells)
    return out


def parse_roster(text):
    """({role: (tier, effort)}, {host: {tier: model}}) from ROSTER.md's two tables."""
    rows, models = {}, {}
    for head, body in _tables(text):
        key = head[0].lower()
        if key == 'role':
            for cells in body:
                if len(cells) < 3:
                    continue
                if cells[0] in rows:
                    raise RosterError('%s: role %s has two rows' % (ROSTER, cells[0]))
                rows[cells[0]] = (cells[1], cells[2])
        elif key == 'tier':
            hosts = head[1:]
            for cells in body:
                for host, model in zip(hosts, cells[1:]):
                    models.setdefault(host, {})[cells[0]] = model
    if not rows:
        raise RosterError('%s: no | role | tier | effort | table' % ROSTER)
    return rows, models


def read_frontmatter(data):
    """{key: value} of the single-line keys in the front matter of bytes `data`, or None."""
    m = _FM.match(data)
    if not m:
        return None
    out = {}
    for line in m.group(2).decode('utf-8').splitlines():
        k, sep, v = line.partition(':')
        if sep and not line.startswith((' ', '\t')):
            out[k.strip()] = v.strip()
    return out


def role_files(root):
    folder = os.path.join(root, *ROLES_REL.split('/'))
    return sorted(n[:-3] for n in os.listdir(folder) if n.endswith('.md'))


def _role_path(root, name):
    return os.path.join(root, *ROLES_REL.split('/'), name + '.md')


def _load(root):
    path = os.path.join(root, *ROSTER_REL.split('/'))
    try:
        with open(path, encoding='utf-8') as f:
            rows, models = parse_roster(f.read())
    except OSError as e:
        raise RosterError('cannot read %s: %s' % (path, e))
    actual = {}
    try:
        names = role_files(root)
    except OSError as e:
        raise RosterError('cannot read %s: %s' % (ROLES_REL, e))
    for name in names:
        with open(_role_path(root, name), 'rb') as f:
            fm = read_frontmatter(f.read())
        if fm is None:
            raise RosterError('%s/%s.md has no front matter' % (ROLES_REL, name))
        actual[name] = (fm.get('tier', ''), fm.get('effort', NONE))
    return rows, models, actual


def check(root, tier_maps=None):
    """Every mismatch between ROSTER.md, the role files and `tier_maps` ({host: {tier: model}},
    each host renderer's tier_models()), as lines; [] when they agree."""
    rows, models, actual = _load(root)
    out = []
    for name in sorted(set(rows) | set(actual)):
        if name not in rows:
            out.append('%s: role file has no row in %s' % (name, ROSTER))
        elif name not in actual:
            out.append('%s: row in %s has no role file' % (name, ROSTER))
        elif rows[name] != actual[name]:
            for label, want, have in zip(('tier', 'effort'), rows[name], actual[name]):
                if want != have:
                    out.append('%s: %s is %s in %s, %s in the role file' % (name, label, want, ROSTER, have))
    for name, (tier, _) in sorted(rows.items()):
        if tier not in TIERS:
            out.append('%s: tier %s in %s is not one of %s' % (name, tier, ROSTER, ', '.join(TIERS)))
    for host in sorted(set(models) | set(tier_maps or {})):
        if host not in models:
            out.append('%s: no column in the %s tier table' % (host, ROSTER))
            continue
        if tier_maps is None:
            continue
        if host not in tier_maps:
            out.append('%s: a column in the %s tier table, but no such host renderer' % (host, ROSTER))
            continue
        for tier in sorted(set(models[host]) | set(tier_maps[host])):
            want, have = models[host].get(tier), tier_maps[host].get(tier)
            if want != have:
                out.append('%s: tier %s runs %s in %s, %s in the host renderer' % (host, tier, want, ROSTER, have))
    return out


def _set_line(data, key, value, after):
    """`data` with the front matter line `key:` set to `value` (inserted after the `after:` line
    when the file has none; removed for NONE); only that line's bytes change."""
    m = _FM.match(data)
    nl = m.group(1)
    body = m.group(2)
    pat = re.compile(rb'^' + key.encode() + rb':[^\r\n]*', re.M)
    new = ('%s: %s' % (key, value)).encode()
    if value == NONE:
        if not pat.search(body):
            return data
        new_body = re.sub(rb'(?:\r?\n)?' + pat.pattern, b'', body, count=1, flags=re.M)
    elif pat.search(body):
        new_body = pat.sub(lambda _: new, body, count=1)
    else:
        mm = re.search(rb'^' + after.encode() + rb':[^\r\n]*', body, re.M)
        at = mm.end() if mm else len(body)
        new_body = body[:at] + nl + new + body[at:]
    return data[:m.start(2)] + new_body + data[m.end(2):]


def write(root):
    """Bring the differing role files to their roster rows; the names changed."""
    rows, _, actual = _load(root)
    changed = []
    for name in sorted(set(rows) & set(actual)):
        if rows[name] == actual[name]:
            continue
        path = _role_path(root, name)
        with open(path, 'rb') as f:
            data = f.read()
        data = _set_line(data, 'tier', rows[name][0], 'description')
        data = _set_line(data, 'effort', rows[name][1], 'tier')
        with open(path, 'wb') as f:
            f.write(data)
        changed.append(name)
    return changed
