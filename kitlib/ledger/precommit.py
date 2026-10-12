"""The opt-in ledger pre-commit check (docs/ledger.md "Pre-commit check").

githooks/pre-commit (a shell front end) runs githooks/pre-commit.py only when the repository's
`.kit.json` text names `pre_commit_check`; that calls kitlib.githooks.precommit.main, which runs
`check` here. A repository opts in with `.kit.json` `ledger.pre_commit_check` set in `.kit.json`
itself (the front end reads no section file); otherwise nothing runs.

Opted in, the check judges only a commit that changes the ledger file the machine config's
`ledger_path` names, or the standing digest beside it (kitlib.ledger.standing.path_beside): any
other commit passes after one `git diff --cached`. Such a commit is refused when either file has
unstaged changes, whichever of them was staged (`tools/ledger.py check` reads the working files of
both, so both must be what the commit holds); otherwise it runs
`tools/ledger.py check --base HEAD` (well formed, append-only, standing digest), and a failure
refuses the commit.

Fails closed, opted in: a `.kit.json` the kit cannot load whose text names `pre_commit_check`, a
machine config that cannot be read or sets no `ledger_path`, and git or the check not running all
refuse. A `.kit.json` that cannot be loaded and does not name `pre_commit_check` is a note and no
check.

Nothing here prints or exits; `check` returns `(exit code, message or None)` and may raise, which
the dispatch turns into a refusal.
"""
import os
import subprocess
import sys

from kitlib import config
from kitlib.config import ConfigError
from kitlib.git import cmd

from . import standing

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER_TOOL = os.path.join(KIT, 'tools', 'ledger.py')
WORD = 'pre_commit_check'
REFUSED = '; commit refused'


def enabled(root):
    """`(True or False, None)`, or `(False, why)` when `.kit.json` cannot be loaded."""
    try:
        return bool(config.load(root).section('ledger')[WORD]), None
    except Exception as e:                       # a ConfigError, or any other load failure
        return False, str(e) or type(e).__name__


def names_opt_in(root):
    """True when `.kit.json`'s raw text names `pre_commit_check` (an unreadable file counts)."""
    try:
        with open(config.config_path(root), encoding='utf-8', errors='replace') as f:
            return WORD in f.read()
    except FileNotFoundError:
        return False
    except OSError:
        return True


def command(root):
    return [sys.executable, LEDGER_TOOL, 'check', '--base', 'HEAD']


def targets(home=None):
    """`(ledger path, standing digest path)` from the machine config. Raises ConfigError when the
    config cannot be read or sets no `ledger_path`."""
    path = config.ledger_path(home)
    if not path:
        raise ConfigError('no ledger_path in %s' % config.user.display(home))
    return path, standing.path_beside(path)


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _real(path):
    return os.path.normcase(os.path.realpath(path))


def staged_targets(root, staged, paths):
    """The entries of `staged` (paths relative to `root`) that are one of `paths`. The targets are
    resolved once; a staged path is compared by its absolute path, and resolved (realpath) only
    when that misses and its file name is a target's."""
    keys = {_key(p) for p in paths}
    reals = {_real(p) for p in paths}
    names = {os.path.normcase(os.path.basename(p)) for p in paths}
    out = []
    for rel in staged:
        full = os.path.join(root, rel)
        if _key(full) in keys or (os.path.normcase(os.path.basename(rel)) in names
                                  and _real(full) in reals):
            out.append(rel)
    return out


def under_root(root, paths):
    """`paths` that lie in the work tree at `root`, relative to it with `/` (git pathspecs); each
    is tried as given, then resolved (realpath) against the resolved root."""
    out = []
    for p in paths:
        for full, base in ((os.path.abspath(p), os.path.abspath(root)),
                           (os.path.realpath(p), os.path.realpath(root))):
            try:
                rel = os.path.relpath(full, base)
            except ValueError:                       # another drive
                continue
            if rel != os.curdir and rel != os.pardir and not rel.startswith(os.pardir + os.sep):
                out.append(rel.replace(os.sep, '/'))
                break
    return out


def unstaged(root, rels):
    """The entries of `rels` whose working file differs from the index. Raises OSError when git
    cannot answer."""
    out, why = cmd.run(['diff', '--name-only', '--no-renames', '-z', '--'] + list(rels), root)
    if out is None:
        raise OSError('cannot compare the ledger with the index: %s' % why)
    return [p for p in cmd.text(out).split('\0') if p]


def check(commit, home=None):
    """The ledger step of kitlib.githooks.precommit: `commit` has `root` and `staged()`."""
    root = commit.root
    on, why = enabled(root)
    if why:
        if names_opt_in(root):
            return 1, 'pre-commit: .kit.json names %s but cannot be loaded: %s%s' % (WORD, why, REFUSED)
        return 0, 'pre-commit: ledger check skipped: .kit.json: %s' % why
    if not on:
        return 0, None
    try:
        paths = targets(home)
    except ConfigError as e:
        return 1, 'pre-commit: ledger check cannot run: %s%s' % (e, REFUSED)
    hit = staged_targets(root, commit.staged(), paths)
    if not hit:
        return 0, None
    # The check reads the working ledger AND digest, so neither may differ from the index.
    dirty = unstaged(root, under_root(root, paths) or hit)
    if dirty:
        return 1, ('pre-commit: %s has unstaged changes and the ledger check reads the working file: '
                   'stage them (git add) or set them aside, then commit again%s'
                   % (', '.join(dirty), REFUSED))
    rc = subprocess.call(command(root), cwd=root)
    if rc:
        return rc, 'pre-commit: tools/ledger.py check failed (exit %d)%s' % (rc, REFUSED)
    return 0, None
