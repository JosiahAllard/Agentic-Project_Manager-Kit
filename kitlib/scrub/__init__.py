"""The built-in scrub: owner-specific text found by its shape, on any clone (docs/shipping.md,
"The built-in scrub").

tests/scrub/test_generic.py takes the install's own names from the machine config, so a clone
with no config checks no names. This package reads no config (a caller passes the configured
machine folders to classes()): each class in CLASSES is a shape
that owner-specific text takes whoever the owner is, and NAME_STORE holds salted digests of names
that are never written down (kitlib/forbidden's hashing; empty until the owner fills it).

Classes (CLASSES, each `(name, regex, in_tests)`):
- `gender`: a gendered pronoun. The kit names one person, the owner, and writes "the owner".
- `record-id`: a backlog record id (an uppercase prefix, then hyphenated uppercase or digit
  parts), or an owner question or ruling id (`Q` or `R`, a dash, digits), except the STOP_IDS acronyms and invented ids that start with MADE_UP_ID_PREFIX.
- `provenance`: a note that a file came over from the kit this one replaced, or which of its
  files it copies, which a reader of the public kit cannot follow. The kit's own name alone passes.
- `pinned-hash`: a commit hash, 7 to 40 hex digits with a digit and a letter, outside tests/
  (test fixtures need invented hashes); NOT_HASHES (git's empty tree, the hex alphabet) pass.
- `hardware`: one machine's parts (a GPU model, a CPU family or its chiplet, a total core or
  thread count of ten or more).
- `drive`: a drive path whose first folder is one of MACHINE_FOLDERS (a user profile), the
  machine folders tests/shipped.py BASE_FORBIDDEN also refuses, or one of the folders a caller
  passes to classes() (tests/scrub/test_builtin.py passes the machine config's
  `machine_path_folders`). Other drive paths (an invented source root) are the fixtures tests
  need; test_generic refuses every drive path outside tests/.
- `name`: a window of the normalised text hashes to one of NAME_STORE's digests.

Every class matches the whole text, so a phrase a markdown wrap splits over two lines is still
found; a hit is reported at the line it starts on, with its whitespace runs shown as one space.

The regexes are written so their own source lines do not match them (a character class around
one letter), so this file needs no allow entry for itself.

`scan` counts hits per (class, path); `ratchet` compares the counts with baselines, so a file's
existing hits can wait for the branch that removes them while a new hit anywhere fails.
"""
import bisect
import collections
import fnmatch
import re

from kitlib import forbidden

GENDER = re.compile(r'\bh(?:e|is|im|imself)\b', re.I)
RECORD_ID = re.compile(r'(?<![A-Za-z0-9_-])(?:[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+|[QR]-[0-9]+)'
                       r'(?![A-Za-z0-9_])')
# Words, acronyms, markers and date shapes that look like a record id and are not one.
STOP_IDS = frozenset((
    # dates, encodings, standards, licences, time zones
    'YYYY-MM-DD', 'YYYY-MM-DDTHH', 'YYYY-MM', 'MM-DD', 'UTF-8', 'UTF-16', 'UTF-16LE', 'SHA-1',
    'SHA-256', 'ISO-8601', 'JSON-RPC', 'ANSI-C', 'BSD-3', 'UTC-4',
    # a model name and an invalid one next to it; two threads per core; a span of rows
    'GPT-5', 'GPT-50', 'SMT-2', 'R4-R7',
    # standard hash and signature-scheme names (RFC 8017, FIPS 180-4)
    'SHA-224', 'SHA-384', 'SHA-512', 'EMSA-PKCS1', 'RSASSA-PKCS1',
    # hyphenated English and marker words written in capitals
    'OWNER-ONLY', 'SELF-TEST', 'ROW-ID', 'FOLLOW-UP', 'AUTO-GENERATED', 'UP-TO-DATE',
    'WORKING-RULES', 'CLAUDE-SPECIFIC', 'GATE-EXPECT-BEGIN', 'GATE-EXPECT-END', 'FIX-HERE'))
# Invented ids in fixtures and examples start with this, as ruling fixtures use R-9nnn.
MADE_UP_ID_PREFIX = 'EX-'
# "PM kit" alone is this kit's own name (AGENTS.md); what counts is the replaced one called
# old, earlier, previous or legacy, or one of its files named.
_PM_KIT = r'P[M](?:\s+|-)kit'
PROVENANCE = re.compile(
    r'\bport(?:ed)\s+from\b'
    r'|\bold\s+(?:P[M](?:\s+|-))?ki[t]\b'
    r'|\b(?:earlier|previous|legacy)\s+ki[t]\b'
    r'|\bearlier\s+(?:per-repository\s+)?tool[s]/'
    r"|\b" + _PM_KIT + r"'s\s+\S*(?:tool[s]/|\.p[y]\b)"
    r'|\b' + _PM_KIT + r'\s+\S+\.p[y]\b', re.I)
PINNED_HASH = re.compile(r'(?<![0-9A-Za-z_])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}'
                         r'(?![0-9A-Za-z_])')
# Hex strings that are not a commit: git's empty tree, and the hex digit alphabet.
NOT_HASHES = frozenset(('4b825dc642cb6eb9a060e54bf8d69288fbee4904', '0123456789abcdef'))
# A GPU model, a CPU family or its chiplet, and a machine's total core or thread count (ten or
# more: the lock's own slot arithmetic speaks of 2 or 4).
HARDWARE = re.compile(
    r'\bRT[X]\s*\d{4}\b|\bR[y]zen\b|\bCC[D]\b|\bThreadrippe[r]\b|\bEP[Y]C\b'
    r'|\bInte[l]\s+Core\s+i[3579]\b'
    r'|\b[1-9]\d+(?:\s+|-)(?:core[s]?|(?:hardware\s+)?thread[s]?)\b')
# The machine folders a drive path must not lead into (tests/shipped.py BASE_FORBIDDEN refuses
# the same ones in shipped text; tests/scrub/test_builtin.py checks the two agree): a user
# profile only. The kit codes no other folder name. This package reads no configuration: the
# machine config's `machine_path_folders` reach it only from a caller, through classes(folders).
MACHINE_FOLDERS = ('Users',)


def drive_pattern(folders=()):
    """The `drive` regex for MACHINE_FOLDERS plus `folders`."""
    names = MACHINE_FOLDERS + tuple(f for f in folders if f not in MACHINE_FOLDERS)
    return re.compile(r'(?<![A-Za-z0-9_?%$])[A-Za-z]:(?:\\|/)+(?:'
                      + '|'.join(re.escape(f) for f in names) + r')(?![A-Za-z0-9_])', re.I)


DRIVE = drive_pattern()

# (name, regex, scans tests/)
CLASSES = (
    ('gender', GENDER, True),
    ('record-id', RECORD_ID, True),
    ('provenance', PROVENANCE, True),
    ('pinned-hash', PINNED_HASH, False),
    ('hardware', HARDWARE, True),
    ('drive', DRIVE, True),
)


def classes(folders=()):
    """CLASSES, with the `drive` class also refusing a drive path into one of `folders` (the
    caller's machine config `machine_path_folders`); CLASSES itself when there are none."""
    if not folders:
        return CLASSES
    rx = drive_pattern(tuple(folders))
    return tuple((name, rx if name == 'drive' else r, t) for name, r, t in CLASSES)
NAME = 'name'
# Salted SHA-256 digests of names that must not appear (kitlib/forbidden's store shape, without
# its canary: a name class with no tokens finds nothing, and tests/scrub proves it bites).
# Whether to fill it is the owner's call. A record for a name is
# {'length': len(name), 'digest': forbidden.digest(bytes.fromhex(NAME_STORE['salt']), name.lower())}.
NAME_STORE = {'salt': b'kit-scrub'.hex(), 'tokens': []}

Hit = collections.namedtuple('Hit', 'path line cls text')
_WS = re.compile(r'\s+')


def _keep(cls, text):
    if cls == 'record-id':
        return text not in STOP_IDS and not text.startswith(MADE_UP_ID_PREFIX)
    if cls == 'pinned-hash':
        return text not in NOT_HASHES
    return True


def lines_of(text):
    """The file's lines, numbered as `hits` numbers them (split at '\\n' only)."""
    return text.split('\n')


def hits(rel, text, classes=CLASSES, store=NAME_STORE):
    """[Hit] for one file's text, sorted by line then class order. A class whose `in_tests` is
    false skips tests/."""
    starts = [0] + [m.end() for m in re.finditer('\n', text)]

    def line(off):
        return bisect.bisect_right(starts, off)

    found = []
    in_tests = rel.startswith('tests/')
    for order, (cls, rx, tests_too) in enumerate(classes):
        if in_tests and not tests_too:
            continue
        found.extend((line(m.start()), order, m.start(), Hit(rel, line(m.start()), cls,
                                                             _WS.sub(' ', m.group(0))))
                     for m in rx.finditer(text) if _keep(cls, m.group(0)))
    if store and store.get('tokens'):
        for off in forbidden.hits_in(text, store, False):
            found.append((line(off), len(classes), off, Hit(rel, line(off), NAME, '<name>')))
    return [h for _, _, _, h in sorted(found)]


def _allowed(hit, line, allowed, used):
    for i, (glob, cls, text, _why) in enumerate(allowed):
        if cls == hit.cls and fnmatch.fnmatchcase(hit.path, glob) and text in line:
            used.add(i)
            return True
    return False


def scan(files, read, allowed=(), classes=CLASSES, store=NAME_STORE):
    """`(hits, unused)`: every Hit in the files no allow entry covers, and the allow entries
    that covered nothing. read(rel) is the file's text or None (skipped). allowed holds
    `(path glob, class, text the hit's first line contains, why)`."""
    found, used = [], set()
    for rel in files:
        text = read(rel)
        if text is None:
            continue
        lines = lines_of(text)
        for h in hits(rel, text, classes, store):
            if not _allowed(h, lines[h.line - 1], allowed, used):
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
