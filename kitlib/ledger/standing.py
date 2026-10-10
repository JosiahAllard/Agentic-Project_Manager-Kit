"""The standing-rulings digest: STANDING.md beside the ledger file (docs/ledger.md "Standing digest").

The digest lists the rules still in force, one `- R-nnnn: <rule>` line each under `##` (and
deeper) headings for repository and topic; the `#` title is not a heading here. Written by hand
(the guard's ledger rule does not cover it); the ledger tool only reads it:

- `problems` (for `check`): every ruling id the digest cites must be a live ledger entry, present
  and not a tombstone. No digest is no finding.
- `topic` (for `standing WORD`): the rule lines for a topic word.

Entries are read through kitlib.ledger.read. Pure over text, except `path_beside` and `read`.
"""
import os
import re

from . import read
from .fmt import RID_RE

NAME = 'STANDING.md'
HEADING_RE = re.compile(r'^(#{2,})\s')


def path_beside(ledger_path):
    """The digest's path: STANDING.md in the ledger file's folder."""
    return os.path.join(os.path.dirname(os.path.abspath(ledger_path)), NAME)


def read_digest(path):
    """`(text, None)`, `(None, None)` when there is no digest, or `(None, why)`."""
    if not os.path.exists(path):
        return None, None
    return read.read_file(path)


def problems(ledger_text, digest_text):
    """One line per digest citation of an id the ledger lacks or has retired; [] for no digest."""
    if digest_text is None:
        return []
    live, retired = set(), set()
    for e in read.parse(ledger_text):
        (retired if e.dead else live).add(e.id)
    errs = []
    for n, line in enumerate(digest_text.replace('\r\n', '\n').split('\n'), 1):
        for rid in RID_RE.findall(line):
            if rid in retired:
                errs.append('%s:%d cites %s, which is retired (cite the live ruling instead)'
                            % (NAME, n, rid))
            elif rid not in live:
                errs.append('%s:%d cites %s, which is not in the ledger' % (NAME, n, rid))
    return errs


def topic(digest_text, word):
    """The digest's rule lines (`- ...`) for `word`, case-insensitive: a line matches when the word
    is in it or in a heading it sits under. Each heading comes once, before its first match."""
    word = word.lower()
    heads, printed, out = {}, set(), []          # heads: {level: (line number, heading)}
    for n, line in enumerate(digest_text.replace('\r\n', '\n').split('\n')):
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            heads = {k: v for k, v in heads.items() if k < level}
            heads[level] = (n, line)
            continue
        if not line.startswith('- '):
            continue
        if word not in line.lower() and not any(word in h.lower() for _, h in heads.values()):
            continue
        for level in sorted(heads):
            if heads[level] not in printed:
                printed.add(heads[level])
                out.append(heads[level][1])
        out.append(line)
    return out
