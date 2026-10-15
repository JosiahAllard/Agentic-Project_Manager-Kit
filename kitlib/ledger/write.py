"""The rulings-ledger writer: add, retire, show, grep and check (docs/ledger.md).

The file is one repo's `<ledger_dir>/<repo>.md` (`split`), or the one-file `ledger_path` while no
`ledger_dir` is set (kitlib.ledger.files routes; tools/ledger.py reads the machine config; this
module takes the path). A missing file starts from the template (kitlib.ledger.fmt.template_text). Every write
validates the whole ledger before and after, and replaces the file atomically, LF only. A path the
ledger's path checks refuse (kitlib.ledger.safepath) is never read or written. Nothing
here prints or exits: Usage and Refused (kitlib.ledger.errors) carry the refusal.

One writer at a time (docs/ledger.md "Write lock"): add and retire hold `locked(path)` from their
read to their write, so two writers never take the same next id and neither replace loses the
other's entry. The lock is kitlib.dailyfull.treelock's OS byte-range lock on a file in the ledger
repository's git common dir (lock_path; beside the file only outside any git checkout), one per
ledger file, so writers of two repos never wait on each other and no checkout gets an untracked
file: the system drops it when its holder exits, so a dead holder never blocks. A busy lock
is waited on for LOCK_WAIT seconds, then refused with nothing written. It nests within one thread
(tools/ledger.py holds it over prepare, add and commit) and excludes every other thread.
"""
import collections
import contextlib
import datetime
import hashlib
import os
import re
import tempfile
import threading
import time

from kitlib.dailyfull.treelock import TreeBusy, TreeLock, TreeLockError
from kitlib.git import repo as gitrepo

from . import fmt as E
from . import safepath
from .errors import Refused, Usage

Added = collections.namedtuple('Added', 'id warnings')

LOCK_WAIT = 30.0     # seconds a writer waits for another writer's lock before it is refused
LOCK_PAUSE = 0.05    # seconds between its tries

_held = threading.local()    # .locks: {lock file identity: [TreeLock, depth]} of this thread


def lock_path(path):
    """The write lock's file for the ledger at `path`. In a git checkout (kitlib.git.repo.classify
    of its folder, MAIN or LINKED): `kit-ledger-<name>-<hash>.lock` in the repository's git common
    dir, never in the work tree, so a write leaves no untracked file in the checkout; the hash is
    of the file's real, case-folded path, so every spelling of one file names one lock. Outside any
    checkout (NONE): `.<name>.lock` beside the file. Refused when git cannot tell (UNKNOWN). Never
    removed (a remove and a re-create would give two files, each with a holder)."""
    folder, name = os.path.split(os.path.abspath(path))
    place = gitrepo.classify(folder)
    if place.kind == gitrepo.NONE:
        return os.path.join(folder, '.%s.lock' % name)
    if place.kind not in (gitrepo.MAIN, gitrepo.LINKED) or not place.common:
        raise Refused('cannot tell whether the ledger %s is in a git checkout, so where its write '
                      'lock is: %s; nothing was written' % (path, place.detail))
    name = os.path.normcase(name)
    key = os.path.join(gitrepo.real(folder), name)
    tag = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name) or 'ledger'
    digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]
    return os.path.join(place.common, 'kit-ledger-%s-%s.lock' % (tag, digest))


def _identity(lock):
    """The lock file's identity (volume, file index), so every spelling of one path is one key;
    None when the file does not exist."""
    try:
        st = os.stat(lock)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _locks():
    if not hasattr(_held, 'locks'):
        _held.locks = {}
    return _held.locks


def held(path):
    """True when this thread holds the write lock of the ledger at `path`."""
    try:
        key = _identity(lock_path(path))
    except Refused:             # git cannot say where the lock is: nobody could have taken it
        return False
    return key is not None and key in _locks()


@contextlib.contextmanager
def locked(path, wait=None):
    """Hold the ledger's write lock for the block. Taken already by this thread: nests. Busy: tried
    every LOCK_PAUSE for `wait` seconds (default LOCK_WAIT), then Refused naming the holder."""
    refuse_unsafe(path)
    lock = lock_path(path)
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, 'ab'):          # exists before its identity is read; a holder's record stays
            pass
    except OSError as e:
        raise Refused('cannot make the ledger write lock %s: %s; nothing was written' % (lock, e))
    key = _identity(lock)
    mine = _locks()
    if key in mine:
        mine[key][1] += 1
        try:
            yield
        finally:
            mine[key][1] -= 1
        return
    wait = LOCK_WAIT if wait is None else wait
    deadline = time.monotonic() + wait
    tl = TreeLock(lock, what='ledger write')
    while True:
        try:
            tl.acquire()
            break
        except TreeBusy as e:
            if time.monotonic() >= deadline:
                raise Refused('the ledger %s is being written by another process: its write lock %s is '
                              '%s; waited %s s, nothing was written. Try again.' % (path, lock, e, wait))
        except TreeLockError as e:
            raise Refused('cannot take the ledger write lock: %s; nothing was written' % e)
        time.sleep(LOCK_PAUSE)
    mine[key] = [tl, 1]
    try:
        yield
    finally:
        del mine[key]
        tl.release()


def refuse_unsafe(path):
    """Refused when the ledger's path checks (kitlib.ledger.safepath) refuse `path`."""
    why = safepath.refusal(path)
    if why is not None:
        raise Refused(why)


def read_text(path):
    """The ledger text, LF; the template when the file does not exist yet."""
    refuse_unsafe(path)
    if not os.path.exists(path):
        return E.template_text()
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            return f.read().replace('\r\n', '\n')
    except (OSError, UnicodeDecodeError) as e:
        raise Refused('cannot read %s: %s' % (path, e))


def write_atomic(path, text):
    refuse_unsafe(path)
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


def _valid_or_refuse(text, repos, what, repo=None, others=()):
    errs = E.validate(text, repos, repo, others)
    if errs:
        raise Refused('%s:\n  %s' % (what, '\n  '.join(errs)))


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%MZ')


def file_repo(path):
    """The repo a per-repo ledger file holds: its name without `.md`."""
    name = os.path.basename(path)
    return name[:-3] if name.lower().endswith('.md') else name


def split_repo_problem(path, repo, repos):
    """None when `path` may take an entry for `repo` in the per-repo ledger (docs/ledger.md
    "Files"): `repo` is a configured name and `path` is its file."""
    if repo == E.ALL_REPOS:
        return ('`%s` is not a ledger repo once ledger_dir is set: use pm for a ruling for every '
                'repository' % E.ALL_REPOS)
    if repos is None or repo not in repos:
        return 'repo %r is not one of ledger_repos (%s)' % (repo, ', '.join(repos or ()))
    if file_repo(path) != repo:
        return 'repo %r is not the repo of %s, whose file holds %r' % (repo, path, file_repo(path))
    return None


def add(path, repo, prov, q, a, decides, source, supersedes='-', when=None, repos=None, split=False,
        others=()):
    """Append one entry and return Added(id, warnings). `repos` is the configured repo columns
    (None: any well-formed name; `all` is always accepted). `when` 'YYYY-MM-DD HH:MMZ' UTC,
    default now. `split`: `path` is `repo`'s per-repo file, numbered H-<repo>-nnnn; `repo` must be
    one of `repos` and never `all`, and `others` (the other files' texts) are where a delegated
    entry's `under <id>` may also point."""
    for name, value in (('q', q), ('a', a), ('decides', decides), ('source', source)):
        if not (value or '').strip():
            raise Usage('--%s is empty' % name)
    bad = split_repo_problem(path, repo, repos) if split else E.repo_problem(repo, repos)
    if bad:
        raise Usage(bad)
    if prov not in E.PROVS:
        raise Usage('provenance %r is not one of %s' % (prov, ', '.join(E.PROVS)))
    if when is not None and not E.WHEN_RE.match(when):
        raise Usage("--when must be UTC 'YYYY-MM-DD HH:MMZ', not %r" % when)
    mine = repo if split else None
    with locked(path):
        text = read_text(path)
        _valid_or_refuse(text, repos, 'the ledger is already invalid', mine, others)
        _, blocks = E.split_entries(text)
        rid = E.next_id(blocks, mine)
        block = E.render(rid, when or now_utc(), repo, prov, q, a, decides, source, supersedes)
        new = text.rstrip('\n') + '\n\n' + block
        _valid_or_refuse(new, repos, 'the new entry is invalid', mine, others)
        write_atomic(path, new)
    warnings = []
    if len(decides) > E.DECIDES_NUDGE:
        warnings.append("%s's Decides is %d characters; keep it to one line under %d"
                        % (rid, len(decides), E.DECIDES_NUDGE))
    return Added(rid, warnings)


def retire(path, rid, cls, why, by=None, ruling=None, repos=None, split=False, others=()):
    """Turn entry `rid` into its tombstone in place and return the id. `ruling` names the live
    entry whose Decides orders this retirement; its mention of `rid` is the order, so it does not
    count as a citation. Any other live entry whose Decides or Items names `rid` refuses. `split`:
    `path` is a per-repo file and `rid` one of its H-<repo>-nnnn ids; `by` may name any file's
    entry, `ruling` one of this file, and a live entry of `others` (the other files' texts) that
    cites `rid` refuses too."""
    if cls not in E.RETIRE_CLASSES:
        raise Usage('--class must be one of %s, not %r' % (', '.join(E.RETIRE_CLASSES), cls))
    if not (why or '').strip():
        raise Usage('--why is empty')
    shape = E.ID if split else r'R-\d{4,}'
    for name, value in (('--by', by), ('--ruling', ruling)):
        if value is not None and not re.fullmatch(shape, value):
            raise Usage('%s must be a ruling id %s, not %r'
                        % (name, 'H-<repo>-nnnn or R-nnnn' if split else 'R-nnnn', value))
    mine = file_repo(path) if split else None
    if split and E.repo_of(rid) != mine:
        raise Refused('%s is not an entry of %s (the file of %r): %s' % (
            rid, path, mine, 'the R-nnnn ledger is frozen' if E.repo_of(rid) is None
            else 'retire it in its own file'))
    with locked(path):
        return _retire(path, rid, cls, why, by, ruling, repos, mine, others)


def _retire(path, rid, cls, why, by, ruling, repos, mine=None, others=()):
    text = read_text(path)
    _valid_or_refuse(text, repos, 'the ledger is already invalid', mine, others)
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
    elsewhere = [b for text in others for b in E.split_entries(text)[1]]
    for r, block in list(blocks) + elsewhere:
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
    _valid_or_refuse(new, repos, 'retiring %s would make the ledger invalid' % rid, mine, others)
    write_atomic(path, new)
    return rid


def show(text, rid):
    """The entry's block without its trailing blank lines, or None."""
    for r, block in E.split_entries(text)[1]:
        if r == rid:
            return block.rstrip('\n')
    return None


def _repo_column(header):
    parts = header.split('|')
    return parts[2].strip().lower() if len(parts) > 2 else None


def grep(text, needle, repo=None):
    """The header (without `## `) of every entry whose block holds `needle`, case-insensitive.
    `repo`: only the entries whose repo column is `repo` or `all`."""
    low = needle.lower()
    heads = [b.split('\n', 1)[0][3:] for _, b in E.split_entries(text)[1] if low in b.lower()]
    if repo is None:
        return heads
    return [h for h in heads if _repo_column(h) in (repo, E.ALL_REPOS)]


def check(text, base, repos=None, repo=None, others=()):
    """validate plus check_append_only against `base` (the ledger at the base revision, or None).
    `repo` and `others`: the per-repo file of `repo`, as validate takes them."""
    return E.validate(text, repos, repo, others) + E.check_append_only(base, text)


def count(text):
    return len(E.split_entries(text)[1])
