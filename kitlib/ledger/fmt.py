"""The rulings-ledger format: split, parse, render and validate (docs/ledger.md "Format").

An entry is a header `## <id> | YYYY-MM-DD HH:MMZ | <repo> | <prov>` then the fields Q, A,
Decides, Source and Supersedes, one `Key: value` line each (a continuation line starts with two
spaces). An older entry may end with an `Items: A, B` line: it is still parsed and validated, and
`render` writes none. A tombstone keeps its header and has one body line,
`Retired: <class> - <why> (see <id>|-)`. Ids are numbered from 1 in file order, one apart.

Two id forms (docs/ledger.md "Files"): `H-<repo>-nnnn` in a per-repo file `<ledger_dir>/<repo>.md`,
whose every entry has that repo column, and `R-nnnn` in the one-file ledger (`ledger_path`), frozen
once the per-repo files exist. validate's `repo` names the per-repo file being checked; None is
the one-file ledger.

Pure functions over text; kitlib.ledger.write reads and writes the file.
"""
import os
import re

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(KIT_ROOT, 'templates', 'common', 'LEDGER.md')

PROVS = ('typed', 'selected', 'relayed-from-terminal', 'delegated')
OWNER_PROVS = PROVS[:3]
# The repo column every install accepts: a ruling for every repository.
ALL_REPOS = 'all'
REPO_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
FIELDS = ('Q', 'A', 'Decides', 'Source', 'Supersedes')
OPTIONAL = ('Items',)
ITEM_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
WHEN_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z$')
# A ruling id: R-nnnn (the one-file ledger) or H-<repo>-nnnn (a per-repo file). No group.
ID = r'(?:R-\d{4,}|H-[a-z0-9][a-z0-9-]*-\d{4,})'
HEAD_RE = re.compile(r'^## (%s) \| (\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z) \| ([a-z0-9-]+) \| ([a-z-]+)$' % ID)
RETIRE_CLASSES = ('spent', 'superseded', 'duplicate')
TOMBSTONE_RE = re.compile(r'^Retired: (spent|superseded|duplicate) - (.+) \(see (%s|-)\)$' % ID)
RID_RE = re.compile(r'\b%s\b' % ID)
# A hub approval as a delegated entry's anchor, `under hub:A-nnn` (docs/delegations.md "Hub
# approvals"): the owner's signed approval on the hub is the anchor, so no owner entry is needed.
# The id exactly as the hub issues it (kitlib.guard.ownercommits.APPROVAL_RE). No group.
HUB_ANCHOR = r'hub:A-(?:(?!000)[0-9]{3}|[1-9][0-9]{3,})'
# `under <ruling id>`, or `under hub:A-nnn` followed by no further id character.
UNDER_RE = re.compile(r'\bunder (%s\b|%s(?![\w-]))' % (ID, HUB_ANCHOR))
# A Decides longer than this draws a warning, never a refusal: one line is the aim.
DECIDES_NUDGE = 400


def is_id(text):
    """True when `text` is one ruling id of either form."""
    return bool(re.fullmatch(ID, text or ''))


def num(rid):
    return int(rid.rsplit('-', 1)[1])


def repo_of(rid):
    """The repo of an H-<repo>-nnnn id; None for an R-nnnn id."""
    if rid.startswith('R-'):
        return None
    return rid[2:].rsplit('-', 1)[0]


def fmt_id(n, repo=None):
    """Id number `n`: H-<repo>-nnnn in `repo`'s file, R-nnnn in the one-file ledger (repo None)."""
    return 'R-%04d' % n if repo is None else 'H-%s-%04d' % (repo, n)


def template_text():
    """What a ledger holds before its first entry: templates/common/LEDGER.md, LF."""
    with open(TEMPLATE, encoding='utf-8', newline='') as f:
        return f.read().replace('\r\n', '\n')


def split_entries(text):
    """`(preamble, [(id or None, raw block)])`; a block keeps its exact text."""
    parts = re.split(r'(?m)^(?=## [RH]-)', text)
    out = []
    for b in parts[1:]:
        m = HEAD_RE.match(b.split('\n', 1)[0])
        out.append((m.group(1) if m else None, b))
    return parts[0], out


def repo_problem(repo, repos):
    """None when `repo` may be a repo column: `all`, else one of `repos` (None: any well-formed name)."""
    if not REPO_RE.match(repo or ''):
        return 'repo %r is not a lower-case name' % (repo,)
    if repos is not None and repo != ALL_REPOS and repo not in repos:
        return 'repo %r is not one of %s' % (repo, ', '.join(list(repos) + [ALL_REPOS]))
    return None


def parse_block(block, repos=None):
    """`(fields, errs)`. fields holds id, when, repo, prov and the Key: value fields; a tombstone has
    retired True, class, why and by instead. fields is None when the header is malformed."""
    lines = block.rstrip('\n').split('\n')
    m = HEAD_RE.match(lines[0])
    if not m:
        return None, ['malformed header: %r' % lines[0]]
    rid, when, repo, prov = m.groups()
    head = {'id': rid, 'when': when, 'repo': repo, 'prov': prov}
    errs = []
    bad_repo = repo_problem(repo, repos)
    if bad_repo:
        errs.append('%s: %s' % (rid, bad_repo))
    if prov not in PROVS:
        errs.append('%s: provenance %r not in %s' % (rid, prov, ', '.join(PROVS)))
    body = [ln for ln in lines[1:] if ln.strip()]
    if len(body) == 1:
        tm = TOMBSTONE_RE.match(body[0].strip())
        if tm:
            cls, why, by = tm.groups()
            return dict(head, retired=True, **{'class': cls, 'why': why, 'by': by}), errs
    fields, cur = {}, None
    for ln in lines[1:]:
        if not ln.strip():
            continue
        if ln.startswith('  ') and cur:
            fields[cur] += '\n' + ln[2:]
            continue
        k, sep, v = ln.partition(': ')
        if not sep or k not in FIELDS + OPTIONAL:
            errs.append('%s: unexpected line %r' % (rid, ln))
            cur = None
            continue
        if k in fields:
            errs.append('%s: duplicate field %s' % (rid, k))
        fields[k], cur = v, k
    for k in FIELDS:
        if not fields.get(k, '').strip():
            errs.append('%s: missing field %s' % (rid, k))
    if 'Items' in fields:
        items = [i.strip() for i in fields['Items'].split(',')]
        bad = [i for i in items if not ITEM_RE.match(i)]
        if bad or len(set(items)) != len(items):
            errs.append('%s: Items must be distinct record ids, comma-separated (bad: %r)'
                        % (rid, bad or items))
    return dict(head, **fields), errs


def fmt_value(v):
    return '\n  '.join(v.replace('\r\n', '\n').split('\n'))


def render(rid, when, repo, prov, q, a, decides, source, supersedes):
    return ('## %s | %s | %s | %s\n' % (rid, when, repo, prov)
            + 'Q: %s\nA: %s\nDecides: %s\nSource: %s\nSupersedes: %s\n'
            % tuple(fmt_value(x) for x in (q, a, decides, source, supersedes or '-'))
            + '\n')


def render_tombstone(header_line, cls, why, by):
    return '%s\nRetired: %s - %s (see %s)\n' % (header_line, cls, why, by or '-')


def next_id(blocks, repo=None):
    nums = [num(i) for i, _ in blocks if i]
    return fmt_id(max(nums) + 1 if nums else 1, repo)


def provs_of(texts):
    """{id: provenance} of every well-formed header in `texts` (the other ledger files), for the
    `under <id>` check of validate."""
    out = {}
    for text in texts:
        for rid, block in split_entries(text)[1]:
            m = HEAD_RE.match(block.split('\n', 1)[0])
            if rid and m:
                out.setdefault(rid, m.group(4))
    return out


def validate(text, repos=None, repo=None, others=()):
    """Every problem in the ledger text, as lines; [] when it is valid. `repos` None checks the repo
    column's shape only. `repo` set: the text is that repo's per-repo file (H-<repo>-nnnn ids, every
    repo column `repo`), and a delegated entry may be `under` an owner entry of `others` (the other
    files' texts) as well as an earlier one of this file; None: the one-file ledger (R-nnnn). A
    delegated entry `under hub:A-nnn` (HUB_ANCHOR) needs no owner entry. A per-repo entry may also
    supersede an `R-nnnn` entry found in `others` (the frozen ledger)."""
    errs = []
    _, blocks = split_entries(text)
    parsed = {}
    for n, (rid, block) in enumerate(blocks, 1):
        if rid != fmt_id(n, repo):
            errs.append('entry %d: id %s, expected %s (ids run in order from %s)'
                        % (n, rid, fmt_id(n, repo), fmt_id(1, repo)))
        e, more = parse_block(block, repos)
        errs += more
        if e:
            parsed[rid] = e
            if repo is not None and e['repo'] != repo:
                errs.append('%s: repo column %r in the file of %r (each file holds its own repo)'
                            % (rid, e['repo'], repo))
    known = set(parsed)
    elsewhere = provs_of(others) if repo is not None else {}
    for rid, e in parsed.items():
        if e.get('retired'):
            continue                    # a tombstone carries no Source or Supersedes to check
        if e['prov'] == 'delegated':
            m = UNDER_RE.search(e.get('Source', ''))
            if not m:
                errs.append("%s: a delegated entry's Source must say 'under <id>'" % rid)
            elif m.group(1).startswith('hub:'):
                pass                    # the hub approval is the anchor (HUB_ANCHOR)
            elif not _owner_before(m.group(1), rid, parsed, elsewhere):
                errs.append('%s: delegated under %s, which is not an earlier owner entry' % (rid, m.group(1)))
        if e.get('Supersedes', '-') != '-':
            for ref in RID_RE.findall(e['Supersedes']):
                if ref in known and num(ref) < num(rid):
                    continue
                if repo is not None and repo_of(ref) is None and ref in elsewhere:
                    continue            # an R-nnnn entry of the frozen ledger (read, never written)
                errs.append('%s: supersedes %s, which is not an earlier entry%s'
                            % (rid, ref, ' of this file or an entry of the frozen ledger'
                               if repo is not None else ''))
    return errs


def _owner_before(under, rid, parsed, elsewhere):
    """True when `under` is an owner entry: an earlier one of this file, or one of another file."""
    if under in parsed:
        return num(under) < num(rid) and parsed[under].get('prov') in OWNER_PROVS
    return elsewhere.get(under) in OWNER_PROVS


def check_append_only(old, new):
    """Problems where `new` is not `old` plus entries: every old entry must stay byte-identical in
    its place, except that a full entry may become its own tombstone (same header line). `old`
    None: nothing to compare."""
    if old is None:
        return []
    _, o = split_entries(old)
    _, n = split_entries(new)
    errs = []
    for i, (rid, block) in enumerate(o):
        if i >= len(n):
            errs.append('%s was removed' % rid)
            continue
        old_block, new_block = block.rstrip('\n'), n[i][1].rstrip('\n')
        if new_block == old_block:
            continue
        old_e, _ = parse_block(block)
        new_e, _ = parse_block(n[i][1])
        was_tomb = bool(old_e and old_e.get('retired'))
        becomes_tomb = bool(new_e and new_e.get('retired'))
        if not was_tomb and becomes_tomb and old_block.split('\n', 1)[0] == new_block.split('\n', 1)[0]:
            continue
        errs.append('%s was edited (entries are append-only; supersede it with a new entry)' % rid)
    return errs
