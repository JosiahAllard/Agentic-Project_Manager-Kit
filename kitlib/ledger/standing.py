"""The standing-rulings digest: STANDING.md beside the ledger file, or in `ledger_dir` with the
per-repo files (docs/ledger.md "Standing digest").

The digest lists the rules still in force, one `- <id>: <rule>` line each under `##` (and
deeper) headings for repository and topic; the `#` title is not a heading here. The owner edits
it by hand (the guard's ledger rule refuses an agent's write to it); the ledger tool only reads it:

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
    """One line per digest citation of an id the ledger lacks or has retired; [] for no digest.
    `ledger_text` is one ledger text or a list of them (every per-repo file and the frozen one)."""
    if digest_text is None:
        return []
    texts = [ledger_text] if isinstance(ledger_text, str) else list(ledger_text)
    live, retired = set(), set()
    for e in (e for text in texts for e in read.parse(text)):
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


def section(digest_text, repo):
    """The digest's `## <repo>` section (any case), its deeper headings included, up to the next
    `##` heading; '' when it has none."""
    out, inside = [], False
    for line in digest_text.replace('\r\n', '\n').split('\n'):
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 2:
            inside = line[3:].strip().lower() == repo.lower()
        if inside:
            out.append(line)
    return '\n'.join(out)


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
