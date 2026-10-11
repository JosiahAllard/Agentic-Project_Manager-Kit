"""`retarget OLD NEW`: rewrite a moved path across every record in one store commit
(docs/backlog.md "Retarget").

What is rewritten, per record, in header order:

- `scope:` lines. A member equal to OLD becomes NEW; a member under OLD (`OLD/...`, the
  trailing `/**` glob included) has that prefix replaced, so `OLD/**` becomes `NEW/**`. A
  member that only covers OLD from above (`docs/**` when OLD is `docs/guide`) is left as it is
  and listed as not rewritten: narrowing it is a judgement, not a rename. A rewritten member that
  the record already holds is dropped, not written twice.
- `ref:` and `summary:` lines. Every occurrence of OLD as a whole path in the text: not preceded
  by a path character, and followed by the end, a `/` (a path under OLD), or a character that
  cannot continue a path (a sentence's full stop counts as one).

Matching is case-sensitive and component-wise: OLD `docs/guide` never touches
`docs/guidebook.md`. Nothing else is rewritten (`area`, `files`, the body). A plan only decides:
it never touches a file, a ref or the screen.
"""
import re

from . import record as rmod
from . import scope as smod
from .errors import Usage
from .store import Change

REWRITTEN_TEXT = ('ref', 'summary')


def _path_error(p, flag):
    if any(ch in smod.WILDCARD_CHARS for ch in p or ''):
        return '`%s %s` is a glob; give the moved path itself (a folder retargets everything ' \
               'under it, `/**` members included)' % (flag, p)
    why = smod.path_shape_error(p, 'path')
    return None if why is None else '`%s %s`: %s' % (flag, p, why)


def check_paths(old, new):
    """Usage when OLD or NEW is not a plain repo-relative path, or they are the same."""
    for p, flag in ((old, 'OLD'), (new, 'NEW')):
        why = _path_error(p, flag)
        if why:
            raise Usage(why)
    if old == new:
        raise Usage('OLD and NEW are the same path: there is nothing to retarget')


def scope_member(member, old, new):
    """`(rewritten, covers)`: the member with OLD replaced (None when it is not OLD or under
    it), and True when the member covers OLD from above without naming it."""
    if member == smod.NONE:
        return None, False
    if member == old:
        return new, False
    if member.startswith(old + '/'):
        return new + member[len(old):], False
    if member.endswith(smod.GLOB_SUFFIX):
        prefix = member[:-len(smod.GLOB_SUFFIX)]
        if old.startswith(prefix + '/'):
            return None, True
    return None, False


def _text_re(old):
    return re.compile(r'(?<![\w./\\-])' + re.escape(old) + r'(?=/|$|[^\w.-]|\.(?!\w))')


def text_value(value, old, new):
    """`value` with every whole-path occurrence of OLD replaced by NEW."""
    return _text_re(old).sub(lambda m: new, value)


class RecordEdit(object):
    """One record's rewrite: `lines` [(key, before, after)], `kept` scope members that cover OLD
    and were left, `pairs` the header after the rewrite."""

    def __init__(self, rec, lines, kept, pairs):
        self.rec, self.lines, self.kept, self.pairs = rec, lines, kept, pairs


def edit_record(rec, old, new):
    """The RecordEdit for `rec` (its `lines` empty when nothing changes)."""
    lines, kept, out = [], [], []
    held = set(rec.all('scope'))
    for key, value in rec.pairs():
        if key == 'scope':
            moved, covers = scope_member(value, old, new)
            if covers:
                kept.append(value)
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
            after = text_value(value, old, new)
            if after != value:
                lines.append((key, value, after))
                value = after
        out.append((key, value))
    return RecordEdit(rec, lines, kept, out)


def plan_retarget(snap, old, new):
    """The Change rewriting OLD to NEW in every readable record: one write per changed record,
    one commit. Records holding a scope that only covers OLD are listed, not changed."""
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
        for member in e.kept:
            out.append('  not rewritten: %s `scope: %s` covers %s from above; narrow it with '
                       '`set %s --scope ...` if it should follow the move' % (e.rec.id, member,
                                                                            old, e.rec.id))
    subject = 'backlog: retarget %s -> %s in %d record(s)' % (old, new, len(changed))
    return Change(writes, subject, out)
