"""Git around the ledger file: `add --commit/--push` and `check --base` (docs/ledger.md "Commit").

The ledger's repository is the git work tree its folder is in. `prepare` runs every refusal before
anything is written: no model for the trailer, no work tree, a broken `.kit.json` there, a
detached HEAD, a branch other than that repository's main branch (unless branch_ok), or uncommitted
changes to the ledger file (another edit would be swept in). `finish` then commits the ledger file
under its write lock, alone (`git commit -- <file>`, never -a: other staged files stay staged and out of the commit) as
`ledger: R-nnnn <title>` with the kit's Co-Authored-By trailer (kitlib.commit.coauthor), and with
push sends the current branch to that repository's `repo.remote` (`origin` by default) once the
lock is given up. A failure
after the write leaves the entry written and says what was not done. Nothing here prints or exits.

`prepare(..., noun=)` and `commit_one` serve any one file a kit tool writes and then commits alone
(tools/pending.py too); `noun` names it in the refusals.
"""
import collections
import os
import re
import time

from kitlib import config as kconfig
from kitlib import commit as kcommit
from kitlib.git import cmd, refs, repo

from . import write
from .errors import Refused, Usage

MODEL_ENV = kcommit.MODEL_ENV
TITLE_LIMIT = 60
PUSH_TIMEOUT = 120      # seconds
INDEX_LOCK_WAIT = 3.0   # seconds commit_one keeps retrying add+commit while git's index.lock is busy
INDEX_LOCK_PAUSE = 0.1  # seconds between those tries
DEFAULT_REMOTE = 'origin'   # the schema default of repo.remote, for a config that cannot be read

Plan = collections.namedtuple('Plan', 'root rel trailer env')


def locate(path, noun='the ledger'):
    """`(work tree root, the file's path relative to it with forward slashes)`; Refused outside
    git. The file's folder may not exist yet (the first add creates it): its nearest existing
    ancestor decides the work tree."""
    folder = os.path.dirname(os.path.abspath(path))
    while not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            raise Refused('no folder above %s exists' % path)
        folder = parent
    root, why = repo.toplevel(folder)
    if root is None:
        raise Refused('%s is in no git work tree: %s' % (noun, why))
    return root, os.path.relpath(os.path.abspath(path), root).replace('\\', '/')


def _settings(root, noun='the ledger'):
    try:
        return kcommit.settings(kconfig.load(root))
    except kconfig.ConfigError as e:
        raise Refused('%s repository\'s .kit.json: %s' % (noun, e))


def short_title(text, limit=TITLE_LIMIT):
    return re.sub(r'\s+', ' ', text).strip()[:limit].rstrip()


def prepare(path, branch_ok=False, model=None, env=None, noun='the ledger', provider=None):
    """A Plan for committing the file at `path` alone, or Usage/Refused. Call before writing."""
    name, _source, error = kcommit.resolve_model(False, model, env, provider)
    if error:
        raise Usage(error)
    if not (name or '').strip():
        raise Usage('--commit needs the model for the Co-Authored-By trailer: --model, else %s' % MODEL_ENV)
    root, rel = locate(path, noun)
    sets = _settings(root, noun)
    branch, why = refs.current_branch(root)
    if branch is None:
        raise Refused('cannot read the branch of %s: %s' % (root, why))
    if not branch:
        raise Refused('HEAD is detached in %s; check out a branch first (--branch-ok does not allow this)' % root)
    if branch != sets['main_branch'] and not branch_ok:
        raise Refused('the current branch of %s is %r, not %s (pass --branch-ok to commit here)'
                      % (root, branch, sets['main_branch']))
    out, why = cmd.run_text(['status', '--porcelain', '--', rel], root, env=env, strip=False)
    if out is None:
        raise Refused('git status failed: %s' % why)
    if out.strip():
        raise Refused('%s has uncommitted changes; refusing so another edit is not swept in' % rel)
    return Plan(root, rel, kcommit.coauthor(name, sets['coauthor_families'], provider), env)


def finish(plan, rid, title, decides, push=False):
    """Commit the ledger file alone under its write lock (kitlib.ledger.write.locked, docs/ledger.md
    "Write lock"), then, the lock given up, push when asked: a slow push never holds the other
    writers. Returns the lines to print; Refused names what was not done (the entry itself is
    written either way)."""
    sha = commit_entry(plan, rid, title, decides)
    lines = ['commit %s' % sha]
    if push:
        lines.append(push_branch(plan, 'the ledger entry %s' % rid, sha))
    return lines


def commit_entry(plan, rid, title, decides):
    """Commit the ledger file alone as `ledger: <rid> <title>` while holding its write lock (nested
    when the caller holds it already); returns the short sha."""
    with write.locked(os.path.join(plan.root, plan.rel)):
        return commit_file(plan, 'ledger: %s %s' % (rid, short_title(title or decides)),
                           'the ledger entry %s' % rid)


def commit_one(plan, subject, what, push=False):
    """Commit plan's file alone as `subject` with plan's trailer, then push the current branch
    when asked. Returns the lines to print; Refused names what was not done (`what` is written
    either way). A busy `index.lock` (and only that) is retried for INDEX_LOCK_WAIT seconds."""
    sha = commit_file(plan, subject, what)
    lines = ['commit %s' % sha]
    if push:
        lines.append(push_branch(plan, what, sha))
    return lines


def commit_file(plan, subject, what):
    """Commit plan's file alone as `subject` with plan's trailer; returns the short sha. A busy
    `index.lock` (and only that) is retried for INDEX_LOCK_WAIT seconds."""
    deadline = time.monotonic() + INDEX_LOCK_WAIT
    head, _ = cmd.run(['rev-parse', '-q', '--verify', 'HEAD'], plan.root, env=plan.env)
    while True:
        why = _add_commit(plan, subject)
        if why is None:
            break
        if not _index_busy(why) or time.monotonic() >= deadline:
            if _index_busy(why):
                why = 'index.lock busy for %s s: %s' % (INDEX_LOCK_WAIT, why)
            raise Refused('git failed: %s\n%s is written but NOT committed (left unstaged).' % (why, what))
        now, _ = cmd.run(['rev-parse', '-q', '--verify', 'HEAD'], plan.root, env=plan.env)
        if now != head:         # a commit landed after all: never make a second one
            raise Refused('git failed: %s\nHEAD moved during the commit; check `git log` before '
                          'retrying (%s is written).' % (why, what))
        time.sleep(INDEX_LOCK_PAUSE)
    sha, _ = cmd.run_text(['rev-parse', '--short', 'HEAD'], plan.root, env=plan.env)
    return sha


def push_branch(plan, what, sha):
    """Push the current branch of plan's repository to its `repo.remote`; returns the line to
    print, or Refused saying `what` is committed as `sha` but not pushed."""
    branch, why = refs.current_branch(plan.root)
    remote = DEFAULT_REMOTE
    if branch:
        try:
            remote = _remote(plan.root)
        except Refused as e:
            why = str(e)
            branch = None
    if branch:
        _, why = cmd.run(['push', '-q', remote, branch], plan.root, timeout=PUSH_TIMEOUT, env=plan.env)
    if not branch or why is not None:
        raise Refused('git push failed: %s\n%s is written and committed (%s) but NOT pushed.'
                      % (why or 'no branch', what, sha))
    return 'pushed %s/%s' % (remote, branch)


def _add_commit(plan, subject):
    """Stage and commit plan's file once; None on success, else why it failed (the file is left
    unstaged after a failed commit)."""
    _, why = cmd.run(['add', '--', plan.rel], plan.root, env=plan.env)
    if why is None:
        _, why = cmd.run(['commit', '-q', '-m', '%s\n\n%s\n' % (subject, plan.trailer), '--', plan.rel],
                         plan.root, env=plan.env)
        if why is not None:
            cmd.run(['reset', '-q', '--', plan.rel], plan.root, env=plan.env)
    return why


def _index_busy(why):
    """True when git's message says another process holds `index.lock` (and nothing else failed)."""
    return 'index.lock' in why and ('File exists' in why or 'Unable to create' in why)


def _remote(root):
    """`repo.remote` of the repository at root (`origin` unless its `.kit.json` says otherwise)."""
    try:
        return kconfig.load(root).remote
    except kconfig.ConfigError as e:
        raise Refused('the repository\'s .kit.json: %s' % e)


def default_base(path):
    """`<repo.remote>/<main branch>` of the ledger's repository (`origin/main` when that cannot be read)."""
    try:
        root, _ = locate(path)
        return '%s/%s' % (_remote(root), _settings(root)['main_branch'])
    except Refused:
        return DEFAULT_REMOTE + '/main'


def base_text(path, base, env=None):
    """`(the ledger's text at revision base, None)` or `(None, why)`."""
    try:
        root, rel = locate(path)
    except Refused as e:
        return None, str(e)
    out, why = cmd.run(['show', '%s:%s' % (base, rel)], root, env=env)
    if out is None:
        return None, why
    return cmd.text(out), None
