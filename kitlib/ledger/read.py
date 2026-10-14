"""The one ledger reader: every consumer of the rulings ledger's entries reads through here (the
digest feed `list` in kitlib.ledger, `check`'s standing-digest check in kitlib.ledger.standing, and
kitlib.backlog.ledger, which re-exports `Entry` and `read_entries`).

A header is read leniently, `## R-nnnn | <when> | <repo> | <kind>`; `strict` says it also has the
form the writer produces (kitlib.ledger.fmt, docs/ledger.md "Format"). Beneath a header only the
`Decides:` line and the last non-blank line are read. A last line `Items: A, B` names records; no
such line, or a malformed one, names nobody. A tombstone (`Retired: <class> - <why> (see R-n|-)` as
the whole body) has `retired` set to `(class, successor)`. Pure over text, except `read_file`,
which runs the ledger's path checks first (kitlib.ledger.safepath).
"""
import collections
import re

from . import safepath

HEADER_RE = re.compile(r'^## (R-[0-9]{4,}) \|([^|]*)\|([^|]*)\|(.*)$')
STRICT_RE = re.compile(r'^## (R-\d{4,}) \| (\d{4}-\d{2}-\d{2}) \d{2}:\d{2}Z \| ([^|]*) \| ([a-z-]+)\s*$')
ITEMS_LINE_RE = re.compile(r'^Items:(.*)$')
ITEM_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
TOMBSTONE_RE = re.compile(r'^Retired: (spent|superseded|duplicate) - (.+) \(see (R-[0-9]{4,}|-)\)$')

# id, date (the day when strict, else the raw when-field), repo and kind (stripped, lower-case),
# decides (None: no line), items (tuple or None), malformed (the bad Items line or None), retired
# ((class, successor) or None), strict, dead (any `Retired:` line under the header).
Entry = collections.namedtuple('Entry', 'id date repo kind decides items malformed retired strict dead')


def items_of(last):
    """`(items, malformed)` of a last line: `(tuple, None)`, `(None, None)` or `(None, the line)`."""
    m = ITEMS_LINE_RE.match(last or '')
    if not m:
        return None, None
    tokens = [t.strip() for t in m.group(1).split(',')]
    if any(not ITEM_ID_RE.match(t) for t in tokens):
        return None, last.strip()
    return tuple(tokens), None


def parse(text):
    """Every entry of the ledger text, in file order (a repeated id appears twice)."""
    out = []
    cur = None
    for line in text.split('\n') + ['## end']:
        line = line.rstrip('\r')
        if line.startswith('## '):
            if cur is not None:
                last, body = cur['last'], cur['body']
                tomb = TOMBSTONE_RE.match(last.strip()) if body == 1 else None
                items, bad = items_of(last)
                out.append(Entry(cur['id'], cur['date'], cur['repo'], cur['kind'], cur['decides'],
                                 items, bad, tomb.group(1, 3) if tomb else None, cur['strict'],
                                 cur['dead']))
            m = HEADER_RE.match(line)
            if m:
                s = STRICT_RE.match(line)
                cur = {'id': m.group(1), 'date': s.group(2) if s else m.group(2).strip(),
                       'repo': m.group(3).strip().lower(), 'kind': m.group(4).strip().lower(),
                       'strict': bool(s), 'decides': None, 'dead': False, 'last': None, 'body': 0}
            else:
                cur = None
        elif cur is not None and line.strip():
            cur['last'] = line
            cur['body'] += 1
            if line.startswith('Retired:'):
                cur['dead'] = True
            elif line.startswith('Decides:'):
                cur['decides'] = line[len('Decides:'):].strip()
    return out


def read_file(path):
    """`(text, None)` or `(None, why)`; a leading BOM is dropped. A path the ledger's path checks
    refuse (kitlib.ledger.safepath) is never opened."""
    why = safepath.refusal(path)
    if why is not None:
        return None, why
    try:
        with open(path, encoding='utf-8-sig') as fh:
            return fh.read(), None
    except OSError as exc:
        return None, 'cannot read %s: %s' % (path, exc)
    except UnicodeDecodeError as exc:
        return None, '%s is not UTF-8: %s' % (path, exc)


def read_entries(path):
    """`({id: Entry}, None)` or `(None, why)`: the file read, and holding at least one header."""
    text, why = read_file(path)
    if text is None:
        return None, why
    found = {e.id: e for e in parse(text)}
    if not found:
        return None, '%s holds no `## R-nnnn | <date> | <repo> | <kind>` header' % path
    return found, None
