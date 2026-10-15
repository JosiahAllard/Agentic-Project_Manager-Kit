"""The owner's pending-steps file: a flat numbered list the PM session edits (docs/pending.md).

The file is the machine config's `pending_path`. Items are `N. text`, continuation lines indented,
no sections; numbers stay stable and are never reused. A done item stays in place as
`N. DONE MM-DD (note). Was: <text>`; an item whose text begins DONE or CLOSED is done. Lines
outside the item touched keep their exact bytes (a BOM, CRLF line ends).

Pure over text (`parse`, `add`, `done`, `edit`, `open_items`) plus `read_raw`, `write_atomic` and
`locked`; nothing here prints or exits. A refusal raises kitlib.ledger.errors Usage or Refused.

One writer at a time: a change holds `locked(path)` from its read to its write (tools/pending.py
holds it over prepare, read, write and commit), so two writers never number two items alike and
neither replace drops the other's change. The lock is kitlib.fs.writelock's, named LOCK_PREFIX; a
busy lock is waited on for LOCK_WAIT seconds, then refused with nothing written. write_atomic's
replace retries a PermissionError (a reader holding the file open) for kitlib.fs.replace.WAIT s.
"""
import collections
import contextlib
import os
import re
import tempfile

from kitlib.fs import replace as fsreplace
from kitlib.fs import writelock
from kitlib.ledger.errors import Refused, Usage

LOCK_WAIT = 30.0             # seconds a writer waits for another writer's lock before it is refused
LOCK_PREFIX = 'kit-pending'  # the lock file's name in the git common dir: kit-pending-<name>-<hash>.lock

ITEM_RE = re.compile(r'^(\d+)\. (.*)$')
DONE_RE = re.compile(r'^(DONE|CLOSED)\b')
WHEN_RE = re.compile(r'^\d{2}-\d{2}$')
BOM = '\ufeff'
INDENT = '    '

# number, first and last line index (the item's own line and its last continuation line)
Item = collections.namedtuple('Item', 'n first last')
Parsed = collections.namedtuple('Parsed', 'eol lines items')


def read_raw(path):
    try:
        with open(path, encoding='utf-8', newline='') as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise Refused('cannot read %s: %s' % (path, e))


def write_atomic(path, text):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        fsreplace.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


@contextlib.contextmanager
def locked(path, wait=None):
    """Hold the pending file's write lock for the block (kitlib.fs.writelock: nests in one thread,
    excludes every other). Busy for `wait` seconds (default LOCK_WAIT), or not to be had: Refused,
    and the block never runs."""
    wait = LOCK_WAIT if wait is None else wait
    try:
        with writelock.locked(path, LOCK_PREFIX, wait, what='pending write'):
            yield
    except writelock.Busy as e:
        raise Refused('%s is being written by another process: its write lock %s is %s; waited %s s, '
                      'nothing was written. Try again.' % (path, e.lock, e.holder, e.wait))
    except writelock.WriteLockError as e:
        raise Refused('cannot take the write lock of %s: %s; nothing was written' % (path, e))


def parse(raw):
    """Parsed(eol, lines without terminators, [Item]); CRLF when the file holds any."""
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.split(eol)
    items = []
    for i, line in enumerate(lines):
        m = ITEM_RE.match(line.lstrip(BOM))
        if m:
            items.append([int(m.group(1)), i, i])
        elif items and line[:1] in (' ', '\t') and line.strip():
            items[-1][2] = i
    return Parsed(eol, lines, [Item(*x) for x in items])


def text_of(lines, item):
    return ITEM_RE.match(lines[item.first].lstrip(BOM)).group(2)


def is_done(lines, item):
    return bool(DONE_RE.match(text_of(lines, item)))


def open_items(raw):
    """`N. text` (first line only) for every item not done, in file order."""
    p = parse(raw)
    return ['%d. %s' % (it.n, text_of(p.lines, it)) for it in p.items if not is_done(p.lines, it)]


def _block(text):
    """Item text as file lines: the first as given, further lines indented four spaces."""
    if not (text or '').strip():
        raise Usage('--text is empty')
    parts = text.replace('\r\n', '\n').strip('\n').split('\n')
    return [parts[0]] + [INDENT + x.lstrip() if x.strip() else '' for x in parts[1:]]


def _find(items, n):
    it = next((x for x in items if x.n == n), None)
    if it is None:
        raise Refused('no item %d' % n)
    return it


def _bom(line):
    return BOM if line.startswith(BOM) else ''


def add(raw, text):
    """`(new raw, n)`: the item appended after the last item, numbered one past the highest."""
    p = parse(raw)
    new = _block(text)
    n = max([x.n for x in p.items] or [0]) + 1
    new[0] = '%d. %s' % (n, new[0])
    # after the last item; with none, before the final newline's empty element (or at the end)
    at = p.items[-1].last + 1 if p.items else (len(p.lines) - 1 if p.lines[-1] == '' else len(p.lines))
    return p.eol.join(p.lines[:at] + new + p.lines[at:]), n


def done(raw, n, when, note=None):
    """The new raw with item `n` marked `DONE <when>[ (note)]. Was: <text>`; `when` is MM-DD."""
    if not WHEN_RE.match(when or ''):
        raise Usage('--when must be MM-DD, not %r' % (when,))
    p = parse(raw)
    it = _find(p.items, n)
    if is_done(p.lines, it):
        raise Refused('item %d is already done' % n)
    note = (note or '').strip()
    head = 'DONE %s%s. Was: ' % (when, ' (%s)' % note if note else '')
    lines = list(p.lines)
    lines[it.first] = '%s%d. %s%s' % (_bom(lines[it.first]), n, head, text_of(p.lines, it))
    return p.eol.join(lines)


def edit(raw, n, text):
    """The new raw with item `n` (its continuation lines too) replaced by `text`."""
    new = _block(text)
    p = parse(raw)
    it = _find(p.items, n)
    new[0] = '%s%d. %s' % (_bom(p.lines[it.first]), n, new[0])
    return p.eol.join(p.lines[:it.first] + new + p.lines[it.last + 1:])
