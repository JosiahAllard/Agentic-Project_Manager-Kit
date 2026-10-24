"""The ledger split's one move (docs/ledger.md "A new ledger"): the standing digest STANDING.md and
the rulebooks' `rules` folder go from beside the frozen `ledger_path` file into `ledger_dir`.

Run after the owner's config edit, when both places are in the machine config: the source is what
kitlib.ledger.files.of_config gives without `ledger_dir` (beside `ledger_path`), the destination
what it gives with it. Two items, each moved whole by one os.rename (atomic on one volume; on
Windows it never replaces an existing destination), rules first, then the digest. Nothing is
copied, merged or rewritten.

Each item is `move` (source there, destination not), `done` (destination there, source not: a
repeated or interrupted run, so a rerun resumes), `none` (neither) or `clash` (both: refused, the
owner keeps one). Every check is made for both items before anything moves; any refusal moves
nothing. A rename that fails part-way stops there and says what moved.

apply() holds the ledger write lock (kitlib.ledger.write.locked) of each rulebook, old and new
path, and of both digest paths, taken in one sorted order and given up when done, so a rule
writer of either layout waits for the move. A lock is taken only where its file sits in a git
checkout's common dir: outside any checkout writelock puts `.<name>.lock` beside the file, which
would sit inside the folder being renamed (and hold it open) or make the destination folder.

Nothing here prints or exits: Refused (kitlib.ledger.errors) carries the refusal.
"""
import collections
import contextlib
import errno
import os

from kitlib.git import cmd as gitcmd
from kitlib.git import repo as gitrepo

from . import files, rulebook, safepath, standing, write
from .errors import Refused

MOVE, DONE, NONE, CLASH = 'move', 'done', 'none', 'clash'

# name: 'rules' or 'STANDING.md'; src, dst: absolute paths; state: one of the four; count: the
# files under src for a folder being moved (else 0).
Item = collections.namedtuple('Item', 'name src dst state count')
# items: [Item], rules first; same: ledger_dir is the ledger_path folder (nothing to move);
# old, new: the frozen file's folder and ledger_dir.
Plan = collections.namedtuple('Plan', 'items same old new')

NEEDS = ('split-move runs after the owner adds ledger_dir and ledger_repos to the machine config '
         'and keeps ledger_path as the frozen file (docs/ledger.md "A new ledger")')


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _within(path, folder):
    """True when `path` is `folder` or lies under it (case-blind, as Windows paths are)."""
    a, b = (os.path.normcase(os.path.abspath(x)) for x in (path, folder))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:                    # different drives
        return False


def _safe(path):
    why = safepath.refusal(path)
    if why is not None:
        raise Refused('%s; nothing was moved' % why)


def _files_under(folder):
    out = []
    for top, _, names in os.walk(folder):
        out.extend(os.path.join(top, n) for n in names)
    return out


def plan(cfg):
    """The Plan of the move for a machine config dict; Refused when it cannot be made."""
    folder, frozen = cfg.get('ledger_dir'), cfg.get('ledger_path')
    if not folder:
        raise Refused('no ledger_dir in the machine config, so there is nowhere to move to: ' + NEEDS)
    if not frozen:
        raise Refused('no ledger_path in the machine config, so there is no folder to move from: ' + NEEDS)
    old = os.path.dirname(os.path.abspath(frozen))
    new = os.path.abspath(folder)
    if _same(old, new):
        return Plan([], True, old, new)
    old_rules = os.path.join(old, rulebook.FOLDER)
    if _within(new, old_rules):
        raise Refused('ledger_dir %s lies inside %s, the rules folder it would move into it; set '
                      'ledger_dir outside it; nothing was moved' % (new, old_rules))
    if os.path.lexists(new) and not os.path.isdir(new):
        raise Refused('ledger_dir %s is not a folder; nothing was moved' % new)
    _safe(new)
    found = files.of_config(cfg)
    pairs = ((rulebook.FOLDER, os.path.join(old, rulebook.FOLDER), found.rules, True),
             (standing.NAME, standing.path_beside(frozen), found.digest, False))
    items, clashes = [], []
    for name, src, dst, is_dir in pairs:
        src, dst = os.path.abspath(src), os.path.abspath(dst)
        here, there = os.path.lexists(src), os.path.lexists(dst)
        for p in [x for x, e in ((src, here), (dst, there)) if e]:
            if os.path.isdir(p) != is_dir:
                raise Refused('%s is %s, not %s; nothing was moved' % (
                    p, 'a folder' if os.path.isdir(p) else 'a file', 'a folder' if is_dir else 'a file'))
            _safe(p)
        count = 0
        if here and is_dir:
            inside = _files_under(src)
            for p in inside:
                _safe(p)
            count = len(inside)
        state = (CLASH if here and there else MOVE if here else DONE if there else NONE)
        if state == CLASH:
            clashes.append('%s exists, and %s is still there' % (dst, src))
        items.append(Item(name, src, dst, state, count))
    if clashes:
        raise Refused('%s: keep one of each pair (compare them, then move or delete the other by '
                      'hand), then run split-move again; nothing was moved' % '; '.join(clashes))
    return Plan(items, False, old, new)


def lock_paths(p):
    """The ledger write locks apply() holds for Plan `p`, sorted: each rulebook's old and new path
    and both digest paths, only those whose folder is in a git checkout (the module docstring)."""
    out, kinds = {}, {}
    for item in p.items:
        if item.name == rulebook.FOLDER:
            names = set()
            for folder in (item.src, item.dst):
                if os.path.isdir(folder):
                    names.update(os.path.basename(x) for x in rulebook.paths(folder).values())
            pairs = [(folder, n) for n in names for folder in (item.src, item.dst)]
        else:
            pairs = [(os.path.dirname(x), os.path.basename(x)) for x in (item.src, item.dst)]
        for folder, name in pairs:
            key = os.path.normcase(folder)
            if key not in kinds:
                kinds[key] = gitrepo.classify(folder).kind
            if kinds[key] != gitrepo.NONE:
                path = os.path.join(folder, name)
                out[os.path.normcase(path)] = path
    return [out[k] for k in sorted(out)]


def _cross_volume(e):
    return getattr(e, 'winerror', None) == 17 or e.errno == errno.EXDEV


def apply(cfg, wait=None, rename=os.rename):
    """Move every `move` item of plan(cfg) under the locks (lock_paths), checking again once they
    are held. Returns `(Plan, [moved Item])`. Refused, naming what moved, when a rename fails."""
    first = plan(cfg)
    if first.same or not any(i.state == MOVE for i in first.items):
        return first, []
    with contextlib.ExitStack() as stack:
        for path in lock_paths(first):
            stack.enter_context(write.locked(path, wait))
        now = plan(cfg)
        todo = [i for i in now.items if i.state == MOVE]
        if todo:
            try:
                os.makedirs(now.new, exist_ok=True)
            except OSError as e:
                raise Refused('cannot make ledger_dir %s: %s; nothing was moved' % (now.new, e))
        moved = []
        for item in todo:
            try:
                rename(item.src, item.dst)
            except OSError as e:
                done = ('moved %s; ' % ', '.join('%s -> %s' % (m.src, m.dst) for m in moved)
                        if moved else 'nothing was moved; ')
                if _cross_volume(e):
                    fix = ('it is on another volume than ledger_dir: the owner moves it by hand '
                           "(the guard refuses an agent's move into ledger_dir), then split-move "
                           'runs again')
                elif isinstance(e, FileExistsError):
                    fix = '%s appeared meanwhile: keep one, then run split-move again' % item.dst
                else:
                    fix = ('close what holds it open (an editor, a reader), then run split-move '
                           '--apply again: it resumes')
                raise Refused('cannot move %s -> %s: %s; %s%s' % (item.src, item.dst, e, done, fix))
            moved.append(item)
    return now, moved


def _in_index(top, rel):
    """True when git's index at work tree `top` lists something at or under `rel`, or when git
    cannot say (the path is then kept, and git's own pathspec error is the answer)."""
    out, _ = gitcmd.run(['ls-files', '-z', '--', rel], top)
    return out is None or bool(out.strip(b'\0'))


def git_steps(p):
    """`[(work tree, [paths relative to it, forward slashes])]` to stage after the move: the new
    path of every item moved or already done, and its old path while git's index still lists
    something there (one gone from disk and from the index, never committed or its removal
    already committed, would fail `git add`'s pathspec and stage nothing), grouped by checkout;
    paths outside any checkout are left out (nothing to stage there)."""
    tops = collections.OrderedDict()
    for item in p.items:
        if item.state not in (MOVE, DONE):
            continue
        for path in (item.src, item.dst):
            place = gitrepo.classify(os.path.dirname(path))
            if place.kind not in (gitrepo.MAIN, gitrepo.LINKED) or not place.top:
                continue
            rel = os.path.relpath(path, place.top).replace(os.sep, '/')
            if path == item.src and not os.path.lexists(path) and not _in_index(place.top, rel):
                continue
            tops.setdefault(place.top, []).append(rel)
    return list(tops.items())
