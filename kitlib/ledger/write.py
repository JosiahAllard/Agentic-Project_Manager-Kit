"""The rulings-ledger writer: add, retire, show, grep and check (docs/ledger.md).

The file is the machine config's `ledger_path` (tools/ledger.py reads it; this module takes the
path). A missing file starts from the template (kitlib.ledger.fmt.template_text). Every write
validates the whole ledger before and after, and replaces the file atomically, LF only. Nothing
here prints or exits: Usage and Refused (kitlib.ledger.errors) carry the refusal.
"""
import collections
import datetime
import os
import re
import tempfile

from . import fmt as E
from .errors import Refused, Usage

Added = collections.namedtuple('Added', 'id warnings')


def read_text(path):
    """The ledger text, LF; the template when the file does not exist yet."""
    if not os.path.exists(path):
        return E.template_text()
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            return f.read().replace('\r\n', '\n')
    except (OSError, UnicodeDecodeError) as e:
        raise Refused('cannot read %s: %s' % (path, e))


def write_atomic(path, text):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.ledger-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _valid_or_refuse(text, repos, what):
    errs = E.validate(text, repos)
    if errs:
        raise Refused('%s:\n  %s' % (what, '\n  '.join(errs)))


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%MZ')


def add(path, repo, prov, q, a, decides, source, supersedes='-', items=None, when=None, repos=None):
    """Append one entry and return Added(id, warnings). `repos` is the configured repo columns
    (None: any well-formed name; `all` is always accepted). `items` a list of record ids or None.
    `when` 'YYYY-MM-DD HH:MMZ' UTC, default now."""
    for name, value in (('q', q), ('a', a), ('decides', decides), ('source', source)):
        if not (value or '').strip():
            raise Usage('--%s is empty' % name)
    bad = E.repo_problem(repo, repos)
    if bad:
        raise Usage(bad)
    if prov not in E.PROVS:
        raise Usage('provenance %r is not one of %s' % (prov, ', '.join(E.PROVS)))
    if when is not None and not E.WHEN_RE.match(when):
        raise Usage("--when must be UTC 'YYYY-MM-DD HH:MMZ', not %r" % when)
    text = read_text(path)
    _valid_or_refuse(text, repos, 'the ledger is already invalid')
    _, blocks = E.split_entries(text)
    rid = E.next_id(blocks)
    items = [i.strip() for i in (items or []) if i.strip()] or None
    block = E.render(rid, when or now_utc(), repo, prov, q, a, decides, source, supersedes, items)
    new = text.rstrip('\n') + '\n\n' + block
    _valid_or_refuse(new, repos, 'the new entry is invalid')
    write_atomic(path, new)
    warnings = []
    if len(decides) > E.DECIDES_NUDGE:
        warnings.append("%s's Decides is %d characters; keep it to one line under %d"
                        % (rid, len(decides), E.DECIDES_NUDGE))
    return Added(rid, warnings)


def retire(path, rid, cls, why, by=None, ruling=None, repos=None):
    """Turn entry `rid` into its tombstone in place and return the id. `ruling` names the live
    entry whose Decides orders this retirement; its mention of `rid` is the order, so it does not
    count as a citation. Any other live entry whose Decides or Items names `rid` refuses."""
    if cls not in E.RETIRE_CLASSES:
        raise Usage('--class must be one of %s, not %r' % (', '.join(E.RETIRE_CLASSES), cls))
    if not (why or '').strip():
        raise Usage('--why is empty')
    for name, value in (('--by', by), ('--ruling', ruling)):
        if value is not None and not re.fullmatch(r'R-\d{4,}', value):
            raise Usage('%s must be a ruling id R-nnnn, not %r' % (name, value))
    text = read_text(path)
    _valid_or_refuse(text, repos, 'the ledger is already invalid')
    pre, blocks = E.split_entries(text)
    idx = next((i for i, (r, _) in enumerate(blocks) if r == rid), None)
    if idx is None:
        raise Refused('unknown id %s' % rid)
    target, _ = E.parse_block(blocks[idx][1])
    if target.get('retired'):
        raise Refused('%s is already retired' % rid)
    if ruling:
        rul = next((b for r, b in blocks if r == ruling), None)
        rul_e = E.parse_block(rul)[0] if rul else None
        decides = (rul_e or {}).get('Decides', '')
        if (not rul_e or rul_e.get('retired') or rid not in E.RID_RE.findall(decides)
                or not re.search(r'\bretir', decides, re.I)):
            raise Refused('--ruling %s must be a live entry whose Decides orders %s retired' % (ruling, rid))
    citing = []
    for r, block in blocks:
        if r in (rid, ruling):
            continue
        e, _ = E.parse_block(block)
        if not e or e.get('retired'):
            continue
        if rid in E.RID_RE.findall(e.get('Decides', '') + ' ' + e.get('Items', '')):
            citing.append(block.rstrip('\n'))
    if citing:
        raise Refused('%s is still cited (outside Supersedes) by:\n%s' % (rid, '\n'.join(citing)))
    old = blocks[idx][1]
    tail = old[len(old.rstrip('\n')):]
    tomb = E.render_tombstone(old.split('\n', 1)[0], cls, why.strip(), by).rstrip('\n') + tail
    new_blocks = list(blocks)
    new_blocks[idx] = (rid, tomb)
    new = pre + ''.join(b for _, b in new_blocks)
    _valid_or_refuse(new, repos, 'retiring %s would make the ledger invalid' % rid)
    write_atomic(path, new)
    return rid


def show(text, rid):
    """The entry's block without its trailing blank lines, or None."""
    for r, block in E.split_entries(text)[1]:
        if r == rid:
            return block.rstrip('\n')
    return None


def grep(text, needle):
    """The header (without `## `) of every entry whose block holds `needle`, case-insensitive."""
    low = needle.lower()
    return [b.split('\n', 1)[0][3:] for _, b in E.split_entries(text)[1] if low in b.lower()]


def check(text, base, repos=None):
    """validate plus check_append_only against `base` (the ledger at the base revision, or None)."""
    return E.validate(text, repos) + E.check_append_only(base, text)


def count(text):
    return len(E.split_entries(text)[1])
