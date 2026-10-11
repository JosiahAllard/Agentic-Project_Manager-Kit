"""`retarget OLD NEW`: rewrite a moved path across every record in one store commit
(docs/backlog.md "Retarget").

OLD and NEW are plain repo-relative paths: no glob, not `none`, no machine path, not the same
path, and neither under the other (a move into or out of its own folder would rewrite a member
twice over).

What is rewritten, per record, in header order:

- `scope:` lines. A member equal to OLD becomes NEW; a member under OLD (`OLD/...`, the
  trailing `/**` glob included) has that prefix replaced, so `OLD/**` becomes `NEW/**`. A
  rewritten member that the record already holds is dropped, not written twice. Left as they are
  and listed as not rewritten: a member that covers OLD from above (`docs/**` or the folder
  `docs` when OLD is `docs/guide`; narrowing it is a judgement, not a rename), and a member that
  matches OLD only when case is ignored (the store compares scopes casefolded, so it may well be
  the moved path, but a rename is never guessed).
- `ref:` and `summary:` lines. Every occurrence of OLD as a whole path in the text: not preceded
  by a path character, and followed by the end, a `/` (a path under OLD), or a character that
  cannot continue a path (a sentence's full stop counts as one). An OLD with no `/` (`tools`) is
  rewritten only where a `/` follows it, since the bare word is as likely English as a path; a
  line where it stands alone is listed as not rewritten.

Matching is case-sensitive and component-wise: OLD `docs/guide` never touches
`docs/guidebook.md`. Nothing else is rewritten (`area`, `files`, the body). A plan only decides:
it never touches a file, a ref or the screen.
"""
import re

from . import record as rmod
from . import scope as smod
from .errors import Usage
from .plan import refuse_machine_paths
from .store import Change

REWRITTEN_TEXT = ('ref', 'summary')
COVERS = 'covers'           # scope_member's note: a member that covers OLD from above
CASE = 'case'               # one that matches OLD only when case is ignored


def _path_error(p, flag):
    if p == smod.NONE:
        return '`%s none` is the empty-scope sentinel, not a path' % flag
    if any(ch in smod.WILDCARD_CHARS for ch in p or ''):
        return '`%s %s` is a glob; give the moved path itself (a folder retargets everything ' \
               'under it, `/**` members included)' % (flag, p)
    why = smod.path_shape_error(p, 'path')
    return None if why is None else '`%s %s`: %s' % (flag, p, why)


def _under(path, folder):
    return path.startswith(folder + '/')


def check_paths(old, new):
    """Usage when OLD or NEW is not a plain repo-relative path, carries a machine path, or the two
    are the same path or one lies under the other."""
    refuse_machine_paths('OLD', old)
    refuse_machine_paths('NEW', new)
    for p, flag in ((old, 'OLD'), (new, 'NEW')):
        why = _path_error(p, flag)
        if why:
            raise Usage(why)
    if old == new:
        raise Usage('OLD and NEW are the same path: there is nothing to retarget')
    for inner, outer in ((new, old), (old, new)):
        if _under(inner.casefold(), outer.casefold()):
            raise Usage('`%s` lies under `%s`: a move into or out of its own folder cannot be '
                        'retargeted in one step; retarget through a path outside both' % (inner, outer))


def _relation(member, old):
    """'same', 'under' (member below OLD) or 'covers' (member above OLD), else None."""
    if member == old:
        return 'same'
    if _under(member, old):
        return 'under'
    body = member[:-len(smod.GLOB_SUFFIX)] if member.endswith(smod.GLOB_SUFFIX) else member
    if _under(old, body):
        return 'covers'
    return None


def scope_member(member, old, new):
    """`(rewritten, note)`: the member with OLD replaced (None when it is not OLD or under it),
    and `note` COVERS when it covers OLD from above, CASE when it matches OLD only casefolded."""
    if member == smod.NONE:
        return None, None
    rel = _relation(member, old)
    if rel in ('same', 'under'):
        return new + member[len(old):], None
    if rel == 'covers':
        return None, COVERS
    if _relation(member.casefold(), old.casefold()):
        return None, CASE
    return None, None


def _text_re(old):
    return re.compile(r'(?<![\w./\\-])' + re.escape(old) + r'(?=/|$|[^\w.-]|\.(?!\w))')


def text_rewrite(value, old, new):
    """`(after, skipped)`: `value` with every whole-path occurrence of OLD replaced by NEW, and
    how many occurrences were left because OLD has no `/` and none follows it there."""
    skipped = [0]

    def one(m):
        if '/' in old or value[m.end():m.end() + 1] == '/':
            return new
        skipped[0] += 1
        return m.group(0)
    return _text_re(old).sub(one, value), skipped[0]


def text_value(value, old, new):
    """`value` with every whole-path occurrence of OLD replaced by NEW (text_rewrite's rules)."""
    return text_rewrite(value, old, new)[0]


class RecordEdit(object):
    """One record's rewrite: `lines` [(key, before, after)], `kept` [(note, key, value)] lines
    that name OLD and were left, `pairs` the header after the rewrite."""

    def __init__(self, rec, lines, kept, pairs):
        self.rec, self.lines, self.kept, self.pairs = rec, lines, kept, pairs


def edit_record(rec, old, new):
    """The RecordEdit for `rec` (its `lines` empty when nothing changes)."""
    lines, kept, out = [], [], []
    held = set(rec.all('scope'))
    for key, value in rec.pairs():
        if key == 'scope':
            moved, note = scope_member(value, old, new)
            if note:
                kept.append((note, key, value))
            if moved is None:
                out.append((key, value))
                continue
            if moved in held or (key, moved) in out:
                lines.append((key, value, None))
                continue
            lines.append((key, value, moved))
            out.append((key, moved))
            continue
        if key in REWRITTEN_TEXT:
            after, skipped = text_rewrite(value, old, new)
            if skipped:
                kept.append(('word', key, value))
            if after != value:
                lines.append((key, value, after))
                value = after
        out.append((key, value))
    return RecordEdit(rec, lines, kept, out)


_KEPT = {
    COVERS: 'covers %(old)s from above; narrow it with `set %(id)s --scope ...` if it should '
            'follow the move',
    CASE: 'matches %(old)s only when case is ignored; fix it with `set %(id)s --scope ...` if '
          'it is the moved path',
    'word': 'has `%(old)s` with no `/` after it, read as a word, not a path; edit it by hand if '
            'it means the path',
}


def plan_retarget(snap, old, new):
    """The Change rewriting OLD to NEW in every readable record: one write per changed record,
    one commit. Lines that name OLD but are left (scope_member's notes, a bare word) are listed."""
    check_paths(old, new)
    edits = [edit_record(r, old, new) for r in snap.readable]
    changed = [e for e in edits if e.lines]
    writes = {e.rec.stem: rmod.render(e.pairs, e.rec.body) for e in changed}
    out = ['RETARGET %s -> %s: %d record(s), %d line(s)'
           % (old, new, len(changed), sum(len(e.lines) for e in changed))]
    for e in changed:
        out.append('  %s' % e.rec.id)
        for key, before, after in e.lines:
            if after is None:
                out.append('    %-8s %s -> (dropped: the record already has the new path)'
                           % (key + ':', before))
            else:
                out.append('    %-8s %s -> %s' % (key + ':', before, after))
    for e in edits:
        for note, key, value in e.kept:
            out.append('  not rewritten: %s `%s: %s` %s' % (e.rec.id, key, value,
                                                          _KEPT[note] % {'old': old, 'id': e.rec.id}))
    subject = 'backlog: retarget %s -> %s in %d record(s)' % (old, new, len(changed))
    return Change(writes, subject, out)
