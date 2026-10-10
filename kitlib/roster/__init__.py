"""kitlib.roster: docs/ROSTER.md against the agent files' frontmatter.

ROSTER.md is the one table where the owner adjusts which agent runs at which model and effort.
Each agent file's `model:` and `effort:` frontmatter lines must equal its row (`-` = no effort
key). `check` lists every mismatch; `write` rewrites only the differing `model:` / `effort:` lines
and leaves every other byte alone (line endings included). Pure file work: no printing, no exit.
"""
import os
import re

ROSTER = 'ROSTER.md'
NONE = '-'
_FM = re.compile(rb'\A---(\r?\n)(.*?)\r?\n---(?=\r?\n)', re.S)


class RosterError(Exception):
    """The roster table or an agent file cannot be read."""


def parse_roster(text):
    """{agent: (model, effort)} from the table rows of ROSTER.md."""
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if not line.strip().startswith('|') or len(cells) < 3:
            continue
        name = cells[0]
        if name.lower() == 'agent' or set(name) <= set('-: '):
            continue
        if name in rows:
            raise RosterError('%s: agent %s has two rows' % (ROSTER, name))
        rows[name] = (cells[1], cells[2])
    return rows


def read_frontmatter(data):
    """{key: value} of the single-line keys in the frontmatter of bytes `data`, or None."""
    m = _FM.match(data)
    if not m:
        return None
    out = {}
    for line in m.group(2).decode('utf-8').splitlines():
        k, sep, v = line.partition(':')
        if sep and not line.startswith((' ', '\t')):
            out[k.strip()] = v.strip()
    return out


def roster_path(agents_dir):
    """docs/ROSTER.md beside the agents folder or one level up (the kit keeps agents in
    claude/agents and the roster in docs/, out of the plugin loader's sight); the first that
    exists, else the one beside."""
    parent = os.path.dirname(os.path.abspath(agents_dir))
    near = os.path.join(parent, 'docs', ROSTER)
    far = os.path.join(os.path.dirname(parent), 'docs', ROSTER)
    return far if not os.path.exists(near) and os.path.exists(far) else near


def agent_files(agents_dir):
    return sorted(n[:-3] for n in os.listdir(agents_dir) if n.endswith('.md'))


def _load(agents_dir):
    path = roster_path(agents_dir)
    try:
        with open(path, encoding='utf-8') as f:
            rows = parse_roster(f.read())
    except OSError as e:
        raise RosterError('cannot read %s: %s' % (path, e))
    actual = {}
    for name in agent_files(agents_dir):
        with open(os.path.join(agents_dir, name + '.md'), 'rb') as f:
            fm = read_frontmatter(f.read())
        if fm is None:
            raise RosterError('%s.md has no frontmatter' % name)
        actual[name] = (fm.get('model', ''), fm.get('effort', NONE))
    return rows, actual


def check(agents_dir):
    """Every mismatch between the roster and the agent files, as lines; [] when they agree."""
    rows, actual = _load(agents_dir)
    out = []
    for name in sorted(set(rows) | set(actual)):
        if name not in rows:
            out.append('%s: agent file has no row in %s' % (name, ROSTER))
        elif name not in actual:
            out.append('%s: row in %s has no agent file' % (name, ROSTER))
        elif rows[name] != actual[name]:
            for label, want, have in zip(('model', 'effort'), rows[name], actual[name]):
                if want != have:
                    out.append('%s: %s is %s in %s, %s in the file' % (name, label, want, ROSTER, have))
    return out


def _set_line(data, key, value):
    """`data` with the frontmatter line `key:` set to `value` (inserted after `model:` when the
    file has none); only that line's bytes change."""
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
        mm = re.search(rb'^model:[^\r\n]*', body, re.M)
        at = mm.end() if mm else len(body)
        new_body = body[:at] + nl + new + body[at:]
    return data[:m.start(2)] + new_body + data[m.end(2):]


def write(agents_dir):
    """Bring the differing agent files to their roster rows; the names changed."""
    rows, actual = _load(agents_dir)
    changed = []
    for name in sorted(set(rows) & set(actual)):
        if rows[name] == actual[name]:
            continue
        path = os.path.join(agents_dir, name + '.md')
        with open(path, 'rb') as f:
            data = f.read()
        data = _set_line(data, 'model', rows[name][0])
        data = _set_line(data, 'effort', rows[name][1])
        with open(path, 'wb') as f:
            f.write(data)
        changed.append(name)
    return changed
