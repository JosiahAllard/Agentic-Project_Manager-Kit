"""The built-in scrub: owner-specific text found by its shape, on any clone (docs/shipping.md,
"The built-in scrub").

tests/scrub/test_generic.py takes the install's own names from the machine config, so a clone
with no config checks no names. This package needs no config: each class in CLASSES is a shape
that owner-specific text takes whoever the owner is, and NAME_STORE holds salted digests of names
that are never written down (kitlib/forbidden's hashing; empty until the owner fills it).

Classes (CLASSES, each `(name, regex, in_tests)`):
- `gender`: a gendered pronoun. The kit names one person, the owner, and writes "the owner".
- `record-id`: a backlog record id (an uppercase prefix, then hyphenated uppercase or digit
  parts), except the STOP_IDS acronyms and invented ids that start with MADE_UP_ID_PREFIX.
- `provenance`: a note that a file came over from the earlier kit, or which of its files it
  copies, which a reader of the public kit cannot follow.
- `pinned-hash`: a commit hash, 7 to 40 hex digits with a digit and a letter, outside tests/
  (test fixtures need invented hashes); NOT_HASHES (git's empty tree, the hex alphabet) pass.
- `hardware`: one machine's parts (a GPU model, a CPU family or its core and thread counts).
- `name`: a window of the normalised line hashes to one of NAME_STORE's digests.

The regexes are written so their own source lines do not match them (a character class around
one letter), so this file needs no allow entry for itself.

`scan` counts hits per (class, path); `ratchet` compares the counts with baselines, so a file's
existing hits can wait for the branch that removes them while a new hit anywhere fails.
"""
import collections
import fnmatch
import re

from kitlib import forbidden

GENDER = re.compile(r'\bh(?:e|is|im|imself)\b', re.I)
RECORD_ID = re.compile(r'(?<![A-Za-z0-9_-])[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+(?![A-Za-z0-9_])')
# Words, acronyms, markers and date shapes that look like a record id and are not one.
STOP_IDS = frozenset((
    # dates, encodings, standards, licences, time zones
    'YYYY-MM-DD', 'YYYY-MM-DDTHH', 'YYYY-MM', 'MM-DD', 'UTF-8', 'UTF-16', 'UTF-16LE', 'SHA-1',
    'SHA-256', 'ISO-8601', 'JSON-RPC', 'ANSI-C', 'BSD-3', 'UTC-4',
    # a model name and an invalid one next to it; two threads per core; a span of rows
    'GPT-5', 'GPT-50', 'SMT-2', 'R4-R7',
    # hyphenated English and marker words written in capitals
    'OWNER-ONLY', 'SELF-TEST', 'ROW-ID', 'FOLLOW-UP', 'AUTO-GENERATED', 'UP-TO-DATE',
    'WORKING-RULES', 'CLAUDE-SPECIFIC', 'GATE-EXPECT-BEGIN', 'GATE-EXPECT-END'))
# Invented ids in fixtures and examples start with this, as ruling fixtures use R-9nnn.
MADE_UP_ID_PREFIX = 'EX-'
# "PM kit" alone is this kit's own name (CLAUDE.md), so only a pointer at the earlier kit's files
# counts, not the name.
PROVENANCE = re.compile(r'\bport(?:ed)\s+from\b|\bold\s+ki[t]\b|\bearlier\s+(?:per-repository\s+)?'
                        r'tool[s]/|\bP[M][ -]kit\'s\s+\W?tool[s]/', re.I)
PINNED_HASH = re.compile(r'(?<![0-9A-Za-z_])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}'
                         r'(?![0-9A-Za-z_])')
# Hex strings that are not a commit: git's empty tree, and the hex digit alphabet.
NOT_HASHES = frozenset(('4b825dc642cb6eb9a060e54bf8d69288fbee4904', '0123456789abcdef'))
# A GPU model, a CPU family or its chiplet, and a machine's total core or thread count (two
# digits or more: the lock's own slot arithmetic speaks of 2 or 4).
HARDWARE = re.compile(r'\bRT[X]\s?\d{4}\b|\bR[y]zen\b|\bCC[D]\b'
                      r'|\b[1-9]\d+\s(?:core[s]|thread[s])\b')

# (name, regex, scans tests/)
CLASSES = (
    ('gender', GENDER, True),
    ('record-id', RECORD_ID, True),
    ('provenance', PROVENANCE, True),
    ('pinned-hash', PINNED_HASH, False),
    ('hardware', HARDWARE, True),
)
NAME = 'name'
# Salted SHA-256 digests of names that must not appear (kitlib/forbidden's store shape, without
# its canary: a name class with no tokens finds nothing, and tests/scrub proves it bites).
# Whether to fill it is the owner's call; add a record with forbidden.digest(salt, name.lower()).
NAME_STORE = {'salt': b'kit-scrub'.hex(), 'tokens': []}

Hit = collections.namedtuple('Hit', 'path line cls text')


def _keep(cls, text):
    if cls == 'record-id':
        return text not in STOP_IDS and not text.startswith(MADE_UP_ID_PREFIX)
    if cls == 'pinned-hash':
        return text not in NOT_HASHES
    return True


def hits(rel, text, classes=CLASSES, store=NAME_STORE):
    """[Hit] for one file's text, in line order. A class whose `in_tests` is false skips tests/."""
    found = []
    in_tests = rel.startswith('tests/')
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        for cls, rx, tests_too in classes:
            if in_tests and not tests_too:
                continue
            found.extend(Hit(rel, n, cls, m.group(0)) for m in rx.finditer(line)
                         if _keep(cls, m.group(0)))
    if store and store.get('tokens'):
        for off in forbidden.hits_in(text, store, False):
            found.append(Hit(rel, forbidden.line_of(text, off), NAME, '<name>'))
    return found


def _allowed(hit, line, allowed, used):
    for i, (glob, cls, text, _why) in enumerate(allowed):
        if cls == hit.cls and fnmatch.fnmatchcase(hit.path, glob) and text in line:
            used.add(i)
            return True
    return False


def scan(files, read, allowed=(), classes=CLASSES, store=NAME_STORE):
    """`(hits, unused)`: every Hit in the files no allow entry covers, and the allow entries
    that covered nothing. read(rel) is the file's text or None (skipped). allowed holds
    `(path glob, class, text the line contains, why)`."""
    found, used = [], set()
    for rel in files:
        text = read(rel)
        if text is None:
            continue
        lines = text.splitlines()
        for h in hits(rel, text, classes, store):
            if not _allowed(h, lines[h.line - 1] if h.line <= len(lines) else '', allowed, used):
                found.append(h)
    return found, [a for i, a in enumerate(allowed) if i not in used]


def counts(found):
    """{(class, path): number of hits}."""
    out = collections.Counter()
    for h in found:
        out[(h.cls, h.path)] += 1
    return dict(out)


def ratchet(found, baselines):
    """`(over, under)` against baselines {(class, path): count} (several dicts merge; a key in
    two of them adds up). over: [(class, path, count, baseline)] where a file has more hits of a
    class than its baseline, a file with no baseline included (baseline 0). under: the same where
    it has fewer, a baseline with no hits left included: the baseline should come down."""
    base = collections.Counter()
    for b in baselines:
        base.update(b)
    got = counts(found)
    over = sorted((c, p, n, base.get((c, p), 0)) for (c, p), n in got.items()
                  if n > base.get((c, p), 0))
    under = sorted((c, p, got.get((c, p), 0), n) for (c, p), n in base.items()
                   if got.get((c, p), 0) < n)
    return over, under
