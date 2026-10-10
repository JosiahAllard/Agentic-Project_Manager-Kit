"""Git around the ledger file: `add --commit/--push` and `check --base` (docs/ledger.md "Commit").

The ledger's repository is the git work tree its folder is in. `prepare` runs every refusal before
anything is written: no model for the trailer, no work tree, a broken `.kit.json` there, a
detached HEAD, a branch other than that repository's main branch (unless branch_ok), or uncommitted
changes to the ledger file (another edit would be swept in). `finish` then commits the ledger file
alone (`git commit -- <file>`, never -a: other staged files stay staged and out of the commit) as
`ledger: R-nnnn <title>` with the kit's Co-Authored-By trailer (kitlib.commit.coauthor), and with
push sends the current branch to origin. A failure after the write leaves the entry written and
says what was not done. Nothing here prints or exits.

`prepare(..., noun=)` and `commit_one` serve any one file a kit tool writes and then commits alone
(tools/pending.py too); `noun` names it in the refusals.
"""
import collections
import os
import re

from kitlib import config as kconfig
from kitlib import commit as kcommit
from kitlib.git import cmd, refs, repo

from .errors import Refused, Usage

MODEL_ENV = kcommit.MODEL_ENV
TITLE_LIMIT = 60
PUSH_TIMEOUT = 120      # seconds

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


def prepare(path, branch_ok=False, model=None, env=None, noun='the ledger'):
    """A Plan for committing the file at `path` alone, or Usage/Refused. Call before writing."""
    name = model if model is not None else (os.environ if env is None else env).get(MODEL_ENV, '')
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
    return Plan(root, rel, kcommit.coauthor(name, sets['coauthor_families']), env)


def finish(plan, rid, title, decides, push=False):
    """Commit the ledger file alone, then push when asked. Returns the lines to print; Refused
    names what was not done (the entry itself is written either way)."""
    subject = 'ledger: %s %s' % (rid, short_title(title or decides))
    return commit_one(plan, subject, 'the ledger entry %s' % rid, push)


def commit_one(plan, subject, what, push=False):
    """Commit plan's file alone as `subject` with plan's trailer, then push the current branch
    when asked. Returns the lines to print; Refused names what was not done (`what` is written
    either way)."""
    _, why = cmd.run(['add', '--', plan.rel], plan.root, env=plan.env)
    if why is None:
        _, why = cmd.run(['commit', '-q', '-m', '%s\n\n%s\n' % (subject, plan.trailer), '--', plan.rel],
                         plan.root, env=plan.env)
        if why is not None:
            cmd.run(['reset', '-q', '--', plan.rel], plan.root, env=plan.env)
    if why is not None:
        raise Refused('git failed: %s\n%s is written but NOT committed (left unstaged).' % (why, what))
    sha, _ = cmd.run_text(['rev-parse', '--short', 'HEAD'], plan.root, env=plan.env)
    lines = ['commit %s' % sha]
    if push:
        branch, why = refs.current_branch(plan.root)
        if branch:
            _, why = cmd.run(['push', '-q', 'origin', branch], plan.root, timeout=PUSH_TIMEOUT, env=plan.env)
        if not branch or why is not None:
            raise Refused('git push failed: %s\n%s is written and committed (%s) but NOT pushed.'
                          % (why or 'no branch', what, sha))
        lines.append('pushed origin/%s' % branch)
    return lines


def default_base(path):
    """`origin/<main branch>` of the ledger's repository (`origin/main` when that cannot be read)."""
    try:
        root, _ = locate(path)
        return 'origin/%s' % _settings(root)['main_branch']
    except Refused:
        return 'origin/main'


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
