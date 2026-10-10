"""kitlib.ledger: reads the rulings ledger (the daily digest of delegated rulings). The writer is
kitlib.ledger.write over the format in kitlib.ledger.fmt, with git in kitlib.ledger.vcs
(docs/ledger.md).

An entry is `## R-nnnn | YYYY-MM-DD HH:MMZ | <repo> | <prov>` then `Key: value` lines. A tombstone
(a body that is one `Retired: ...` line) has no Decides and is never listed. Pure functions: the
text comes in, lines go out; tools/ledger.py reads the file named by the machine config.
"""
import re

from . import read

PROVS = ('typed', 'selected', 'relayed-from-terminal', 'delegated')
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
NO_DECIDES = '(no Decides)'


def entries(text):
    """Yield (id, date, prov, decides) per live entry; decides is None when it has none. Tombstones
    and headers not in the writer's form are skipped. A view of kitlib.ledger.read.parse."""
    for e in read.parse(text):
        if e.strict and not e.dead:
            yield e.id, e.date, e.kind, e.decides


def list_rulings(text, prov, since):
    """One `R-nnnn | date | Decides` line per entry of that provenance dated on or after `since` (YYYY-MM-DD), in ledger order."""
    if prov not in PROVS:
        raise ValueError('prov must be one of %s, not %r' % (', '.join(PROVS), prov))
    if not DATE.fullmatch(since or ''):
        raise ValueError('since must be YYYY-MM-DD, not %r' % (since,))
    return ['%s | %s | %s' % (rid, date, decides or NO_DECIDES)
            for rid, date, p, decides in entries(text) if p == prov and date >= since]
