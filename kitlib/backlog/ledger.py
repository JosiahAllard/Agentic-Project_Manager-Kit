"""The rulings ledger's entries, for kitlib.backlog's readers.

No backlog write and no `check` reads a ledger file: the rulings ledger is the PM's private
record, and an owner gate on a write is a role (kitlib/backlog/roles.py), not a cited id.
`Entry` and `read_entries` are kitlib.ledger.read's, re-exported; `successor` follows a
tombstone's `see` chain over the entries that reader returns.
"""
from kitlib.ledger import read

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
