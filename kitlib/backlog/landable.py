"""`landable`: may a branch land without leaving a dead pull? (ported from an earlier
tools/backlog.py `cmd_landable`).

Landing deletes the branch, so a record still `state: pulled` with `pulled_by: <branch>` would
name a branch that no longer exists. With a ref or branch store the records are read from the
store snapshot; with a main store, from the branch's own tip. Reads git only, writes nothing.
"""
import collections

from kitlib.git import refs

from .plan import branch_name_error
from .store import MainBackend, records_in_tree

LANDABLE = 'LANDABLE'
NOT_LANDABLE = 'NOT LANDABLE'
CANNOT_TELL = 'CANNOT TELL'

Verdict = collections.namedtuple('Verdict', 'branch verdict held detail')


def check(backend, snap, root, branches):
    """One Verdict per branch."""
    out = []
    for branch in branches:
        bad = branch_name_error(branch, root)
        if bad:
            out.append(Verdict(branch, CANNOT_TELL, (), bad))
            continue
        # known=True: a git ended from outside is no answer, never "no such branch"
        # (KIT-GIT-EXIT1-ANSWERS-2; `rev-parse --verify --quiet` said both as exit 1).
        tip, why = refs.resolve('refs/heads/%s' % branch, root, known=True)
        if tip is None:
            out.append(Verdict(branch, CANNOT_TELL, (), 'cannot read refs/heads/%s: %s'
                               % (branch, why)))
            continue
        if not tip:
            out.append(Verdict(branch, CANNOT_TELL, (), 'no local branch of that name'))
            continue
        if isinstance(backend, MainBackend):
            records, _, why = records_in_tree(root, tip, backend.rel)
            if records == []:
                why = 'no record under %s/ at its tip' % backend.rel
            if not records:
                out.append(Verdict(branch, CANNOT_TELL, (), why))
                continue
        else:
            records = snap.records
        unread = [x.stem for x in records if not x.readable]
        if unread:
            out.append(Verdict(branch, CANNOT_TELL, (), 'unreadable record(s): %s'
                               % ', '.join(unread)))
            continue
        held = [x.id for x in records if x.get('state') == 'pulled' and x.get('pulled_by') == branch]
        out.append(Verdict(branch, NOT_LANDABLE if held else LANDABLE, tuple(held),
                           '%d records read' % len(records)))
    return out
