"""`ledger.py check`'s ruling-orphan scan (docs/ledger.md "check"), a WARN and never a failure.

A live ledger entry for this repository (its repo column is one of `repos` or `all`) whose
`Items:` last line names a record that is open nowhere: not in the store now, and the store's
history shows no commit that closed it. Such an id was mistyped, or named before its record
existed; the ruling is not defective for it, so nothing here fails. A tombstone, a missing Items
line and a malformed one name nobody and are not read. Pure: the entries (kitlib.ledger.read), the
open ids and a `closed(id)` test come in; kitlib.backlog.ledger.record_view supplies the last two.
"""
import collections

Scan = collections.namedtuple('Scan', 'subjects warnings')


def scan(entries, repos, open_ids, closed):
    """`Scan(subjects, warnings)`: the number of entries read (live, this repository's, with a
    well-formed Items line) and one line per entry naming an orphan, in ledger order."""
    here = set(repos) | {'all'}
    subjects = [e for e in entries.values() if e.repo in here and e.items is not None and e.retired is None]
    known = {}

    def gone(rid):
        if rid not in known:
            known[rid] = rid not in open_ids and not closed(rid)
        return known[rid]

    warnings = []
    for e in subjects:
        orphans = [i for i in e.items if gone(i)]
        if orphans:
            warnings.append('%s: Items: %s - %s no record open now and none closed in the store history'
                            % (e.id, ', '.join(e.items), ('`%s` is' % orphans[0]) if len(orphans) == 1
                               else '%s are' % ', '.join('`%s`' % o for o in orphans)))
    return Scan(len(subjects), warnings)
