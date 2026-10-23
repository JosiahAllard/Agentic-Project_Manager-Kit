"""The rulings-ledger writer: add, retire, show, grep and check (docs/ledger.md).

The file is one repo's `<ledger_dir>/<repo>.md` (`split`), or the one-file `ledger_path` while no
`ledger_dir` is set (kitlib.ledger.files routes; tools/ledger.py reads the machine config; this
module takes the path). A missing file starts from the template (kitlib.ledger.fmt.template_text). Every write
validates the whole ledger before and after, and replaces the file atomically, LF only. A path the
ledger's path checks refuse (kitlib.ledger.safepath) is never read or written. Nothing
here prints or exits: Usage and Refused (kitlib.ledger.errors) carry the refusal.

One writer at a time (docs/ledger.md "Write lock"): add and retire hold `locked(path)` from their
read to their write, so two writers never take the same next id and neither replace loses the
other's entry. The lock is kitlib.fs.writelock's: an OS byte-range lock on a file in the ledger
repository's git common dir (lock_path; beside the file only outside any git checkout), one per
ledger file, so writers of two repos never wait on each other and no checkout gets an untracked
file: the system drops it when its holder exits, so a dead holder never blocks. A busy lock
is waited on for LOCK_WAIT seconds, then refused with nothing written. It nests within one thread
(tools/ledger.py holds it over prepare, add and commit) and excludes every other thread. The
replace retries a PermissionError (a reader holding the file open) for kitlib.fs.replace.WAIT s.

A rule change (`add(..., rule=RuleChange)`, docs/ledger.md "Rulebooks") writes a rulebook too,
under the same lock: the log entry first, then the rulebook. A crash between the two leaves an
entry whose rule is missing, which kitlib.ledger.rulebook `applied` reports.
"""
import collections
import contextlib
import datetime
import os
import re
import tempfile

from kitlib.fs import replace as fsreplace
from kitlib.fs import writelock
from kitlib.git import repo as gitrepo     # noqa: F401  (tests patch write.gitrepo.classify)

from . import fmt as E
from . import rulebook, safepath
from .errors import Refused, Usage

Added = collections.namedtuple('Added', 'id warnings')

LOCK_WAIT = 30.0     # seconds a writer waits for another writer's lock before it is refused
LOCK_PAUSE = 0.05    # seconds between its tries
LOCK_PREFIX = 'kit-ledger'    # the lock file's name in the git common dir: kit-ledger-<name>-<hash>.lock


def lock_path(path):
    """The write lock's file for the ledger at `path` (kitlib.fs.writelock.lock_path): in a git
    checkout `kit-ledger-<name>-<hash>.lock` in the repository's git common dir, outside any
    checkout `.<name>.lock` beside the file. Refused when git cannot tell."""
    try:
        return writelock.lock_path(path, LOCK_PREFIX)
    except writelock.NoPlace as e:
        raise Refused('cannot tell whether the ledger %s is in a git checkout, so where its write '
                      'lock is: %s; nothing was written' % (path, e.detail))


def held(path):
    """True when this thread holds the write lock of the ledger at `path`."""
    return writelock.held(path, LOCK_PREFIX)


@contextlib.contextmanager
def locked(path, wait=None):
    """Hold the ledger's write lock for the block. Taken already by this thread: nests. Busy: tried
    every LOCK_PAUSE for `wait` seconds (default LOCK_WAIT), then Refused naming the holder."""
    refuse_unsafe(path)
    wait = LOCK_WAIT if wait is None else wait
    try:
        with writelock.locked(path, LOCK_PREFIX, wait, what='ledger write', pause=LOCK_PAUSE):
            yield
    except writelock.NoPlace as e:
        raise Refused('cannot tell whether the ledger %s is in a git checkout, so where its write '
                      'lock is: %s; nothing was written' % (path, e.detail))
    except writelock.CannotMake as e:
        raise Refused('cannot make the ledger write lock %s: %s; nothing was written' % (e.lock, e.detail))
    except writelock.Busy as e:
        raise Refused('the ledger %s is being written by another process: its write lock %s is '
                      '%s; waited %s s, nothing was written. Try again.' % (path, e.lock, e.holder, e.wait))
    except writelock.CannotTake as e:
        raise Refused('cannot take the ledger write lock: %s; nothing was written' % e.detail)


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
        fsreplace.replace(tmp, path)
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
        others=(), rule=None):
    """Append one entry and return Added(id, warnings). `repos` is the configured repo columns
    (None: any well-formed name; `all` is always accepted). `when` 'YYYY-MM-DD HH:MMZ' UTC,
    default now. `split`: `path` is `repo`'s per-repo file, numbered H-<repo>-nnnn; `repo` must be
    one of `repos` and never `all`, and `others` (the other files' texts) are where a delegated
    entry's `under <id>` may also point. `rule` (a RuleChange): the entry also changes the
    rulebook of `repo` (rulebook.target), its Decides ending with the change's tag; under the one
    write lock both are checked, then the log is written, then the rulebook. A `set` refuses a
    missing or stale `checked` hash, and its rule text defaults to `decides`."""
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
    warnings = []
    with locked(path):
        text = read_text(path)
        _valid_or_refuse(text, repos, 'the ledger is already invalid', mine, others)
        _, blocks = E.split_entries(text)
        rid = E.next_id(blocks, mine)
        logged, book = decides, None
        if rule is not None:
            book, logged = _rule_change(rule, repo, rid, decides, warnings)
        block = E.render(rid, when or now_utc(), repo, prov, q, a, logged, source, supersedes)
        new = text.rstrip('\n') + '\n\n' + block
        _valid_or_refuse(new, repos, 'the new entry is invalid', mine, others)
        if book is not None:
            _books_or_refuse(rule.folder, book, [new] + list(others))
        write_atomic(path, new)
        if book is not None:        # the second write, still under the one lock
            write_atomic(os.path.join(rule.folder, rulebook.file_name(book.name)),
                         rulebook.render(book))
    if len(decides) > E.DECIDES_NUDGE:
        warnings.insert(0, "%s's Decides is %d characters; keep it to one line under %d"
                        % (rid, len(decides), E.DECIDES_NUDGE))
    return Added(rid, warnings)


# A rulebook change paired with a log entry (docs/ledger.md "Rulebooks"). folder: the rules folder
# (kitlib.ledger.files `Files.rules`). op: 'set' (key, topic, text, checked: the hash `conflicts`
# printed), 'dropped' (key) or 'header' (repos, budget, title; the book is made when absent).
RuleChange = collections.namedtuple('RuleChange', 'folder op key topic text checked repos budget title',
                                    defaults=(None,) * 7)


def load_books(folder):
    """`{name: Book}` of the rulebooks in `folder`; Refused when one is invalid (fix it first)."""
    books, errs = rulebook.load(folder)
    if errs:
        raise Refused('the rulebooks are already invalid (fix them first; nothing was written):\n  %s'
                      % '\n  '.join(errs))
    return books


def rule_target(folder, repo):
    """`(book name, path)` of the rulebook a rule change for log repo `repo` writes."""
    name = rulebook.target(load_books(folder), repo)
    return name, os.path.join(folder, rulebook.file_name(name))


def _rule_change(rule, repo, rid, decides, warnings):
    """`(the new Book, the Decides to log, its tag at the end)` for `rule`, or Usage/Refused."""
    books = load_books(rule.folder)
    name = rulebook.target(books, repo)
    book = books.get(name)
    fix = ('run `ledger.py conflicts --repo %s --topic ... --key %s --text ...`, read what it lists, '
           'then pass the checked hash it prints' % (repo, rule.key))
    if rule.op == 'set':
        text = rule.text if (rule.text or '').strip() else decides
        if not (rule.checked or '').strip():
            raise Refused('--rule needs --checked: %s; nothing was written' % fix)
        now = rulebook.digest(books, name, rule.topic, rule.key, text)
        if rule.checked.strip() != now:
            raise Refused('--checked %s is stale: %s or the rule changed since it was printed; %s; '
                          'nothing was written' % (rule.checked.strip(), ' or '.join(
                              rulebook.file_name(n) for n in rulebook.scope(books, name)), fix))
        try:
            book, _ = rulebook.set_rule(book or rulebook.new_book(name, (repo,)), rule.key,
                                        rule.topic, text, (rid,))
        except ValueError as e:
            raise Usage(str(e))
        if len(book.rules) > book.budget:
            warnings.append('%s has %d rules, over its budget of %d: drop or merge some'
                            % (rulebook.file_name(name), len(book.rules), book.budget))
    elif rule.op == 'dropped':
        try:
            book, _ = rulebook.drop_rule(book or rulebook.new_book(name, (repo,)), rule.key)
        except KeyError:
            raise Refused('%s has no rule %s; nothing was written' % (rulebook.file_name(name), rule.key))
    elif rule.op == 'header':
        if book is None and not rule.repos:
            raise Usage('%s does not exist yet: give it --repos' % rulebook.file_name(name))
        book = rulebook.set_header(book or rulebook.new_book(name, rule.repos), rule.repos,
                                   rule.budget, rule.title)
    else:
        raise Usage('unknown rule change %r' % (rule.op,))
    return book, '%s %s' % (decides.rstrip(), rulebook.tag(rule.op, name, rule.key))


def _books_or_refuse(folder, book, texts):
    """Refused when writing `book` would leave the rulebooks with an error they did not have."""
    _, errs = rulebook.parse(rulebook.render(book), book.name)
    if errs:
        raise Refused('the rulebook change is invalid; nothing was written:\n  %s' % '\n  '.join(errs))
    old = load_books(folder)
    ids = {rid for text in texts for rid, _ in E.split_entries(text)[1] if rid}

    def plain(lines):
        return {re.sub(r':\d+:', ':', e) for e in lines}
    before = plain(rulebook.check(old, ids).errors)
    new = dict(old, **{book.name: book})
    errs = [e for e in rulebook.check(new, ids).errors if re.sub(r':\d+:', ':', e) not in before]
    if errs:
        raise Refused('the rulebook change is refused; nothing was written:\n  %s' % '\n  '.join(errs))


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
