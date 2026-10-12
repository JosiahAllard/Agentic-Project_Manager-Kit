"""What a record's own ruling fields allow.

No backlog write and no `check` reads a ledger file: the rulings ledger is the PM's private
record. A write that names a ruling is judged against the record alone (plan.py
`_check_ruling`), with the helpers here: a `hold_ruling` that is still live, one that is spent
because the record was held again, and the release of a `priority: -1` beside a live hold.
`Entry` and `read_entries` are kitlib.ledger.read's, re-exported; `successor` follows a
tombstone's `see` chain over the entries that reader returns.
"""
from kitlib.ledger import read

from . import record as rmod
from .errors import Refusal

# The entry type and the reader are kitlib.ledger.read's: one reader for every consumer.
Entry = read.Entry
read_entries = read.read_entries


def successor(entries, rid):
    """The live id a tombstone's `see` chain ends at, or None."""
    seen = set()
    while rid in entries and entries[rid].retired:
        if rid in seen:
            return None
        seen.add(rid)
        rid = entries[rid].retired[1]
        if rid == '-':
            return None
    return rid


def spent_hold_rulings(rec):
    """The record's own `hold_ruling` while it is blocked on the owner: a hold set again after
    that ruling released one, so the id never releases a hold here again."""
    own = rec.get('hold_ruling')
    return (own,) if own and 'owner' in rec.all('blocked_on') else ()


def hold_live(rec):
    """A `hold_ruling` that is not spent: `close` accepts it as the ruling it needs."""
    return rec.has('hold_ruling') and rec.get('hold_ruling') not in spent_hold_rulings(rec)


def refuse_unnamed_release(rid, rec, ruling, verb):
    """A `priority: -1` beside a live `hold_ruling` R was released by R: its release takes R."""
    own = rec.get('hold_ruling')
    if rec.get('priority') == rmod.OWNER_PRIORITY and hold_live(rec) and ruling != own:
        raise Refusal('`%s`\'s `priority: -1` was released by `hold_ruling: %s`, still live, so this '
                      '%s needs `--ruling %s`, not `%s`' % (rid, own, verb, own, ruling))
