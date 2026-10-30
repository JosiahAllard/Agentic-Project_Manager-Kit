"""The branch gate budget (docs/gate.md "The branch gate budget"): a branch gets RUNS gate runs;
before booking one more, a branch gate is REFUSED until the branch's coordinator allows more
(`gate.py allow`).

A run counted is a history line of the machine lock (kitlib.lock.store history_records) with
`gate` true, a priority other than "land" (a landing books the branch's slug too), the gated
repository (case aside) and the branch (`of_branch`), that ended at or after the branch was made:
the first entry of its reflog (`since`), so a branch made again under an old name starts at 0;
a branch with no reflog file (git's reftable format, or the tests' fake git) has no such bound.
The branch is the line's `branch` (case aside: on Windows `Kit/A` and `kit/A` are one loose ref),
never its slug, which two branches can share and `gate.py --slug` sets; a null `branch` (a run
booked with none) never counts. A line with no `branch` key, written before history lines kept
it, counts by the branch's default slug (runner.slug_of), as every line did before. History
lines written before they kept `gate` have none and never count. A fully reused gate
books nothing and is never counted or refused (kitlib.gate.runner `gated` asks, for a branch
gate only, just before it waits for the gate cap and books); a landing, the daily full run and
redfirst never ask.

An allowance is `<dev_drive>/pm-kit/kit-gate-budget/<repo key>/<result.file_stem(branch)>.json`
(kitlib.gate.result.kit_folder and repo_key): {branch, runs (the new total), why, at, by (the
caller's role and session)}. The folder is outside every checkout, where agents' own file writes
do not reach. One written before the branch was last made is not read. With no dev_drive there
is no folder: `gate.py allow` writes, and this check reads, the .git copy
`<git common dir>/kit-gate-budget/<stem>.json` (legacy_path) instead; with a dev_drive the copy
is neither written nor read. It stays for load nodes set up without a dev_drive
(docs/load-nodes.md). The owner is never refused. Anything the check cannot read is one WARNING and the gate runs (fail
open): a wrong refusal would stop every agent's gate.
"""
import datetime
import json
import os
import re

from kitlib.gate import result
from kitlib.git import refs, repo
from kitlib.lock import store as lockstore

RUNS = 5
DIR_NAME = 'kit-gate-budget'
DOC = 'docs/gate.md "The branch gate budget"'


class BudgetError(Exception):
    """The budget's inputs cannot be read, or an allowance cannot be written."""


REFLOG_TIME = re.compile(r'> (\d+) [+-]\d{4}(?:\t|$)')


def since(root, branch):
    """When `branch` was made: the time of the first entry of its reflog file
    (`<git common dir>/logs/refs/heads/<branch>`, which git removes with the branch), a UTC
    datetime; None when it has none (no lower bound). BudgetError when it cannot be read."""
    common, why = repo.common_dir(root)
    if not common:
        raise BudgetError('no git common dir for %s: %s' % (root, why))
    path = os.path.join(common, 'logs', 'refs', 'heads', *branch.split('/'))
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            first = f.readline()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise BudgetError('the reflog of %s cannot be read: %s' % (branch, e))
    if not first.strip():
        return None
    m = REFLOG_TIME.search(first)
    if not m:
        raise BudgetError('the reflog %s does not start with a time' % path)
    return datetime.datetime.fromtimestamp(int(m.group(1)), datetime.timezone.utc)


def runs(store, repo_name, branch, after=None):
    """The counted history lines (above) of repo_name and branch that ended at or after `after`
    (None: the history the store keeps), oldest first."""
    want = str(repo_name).lower()
    return [d for d in store.history_records(since=after)
            if d.get('gate') is True and d.get('priority') != 'land'
            and str(d.get('repo') or '').lower() == want and of_branch(d, branch)]


def of_branch(d, branch):
    """Whether history line d is a run of `branch` (above): its `branch`, case aside; a line
    with no `branch` key, by its slug."""
    if 'branch' not in d:
        return d.get('slug') == slug_of(branch)
    got = d['branch']
    return isinstance(got, str) and got.lower() == branch.lower()


def _common(root):
    common, why = repo.common_dir(root)
    if not common:
        raise BudgetError('no git common dir for %s: %s' % (root, why))
    return common


class NoBudgetFolder(BudgetError):
    """There is no allowance folder: the machine config sets no dev_drive, or cannot be read."""


def allow_path(root, branch):
    """The allowance's file (the module docstring). BudgetError when there is no git common dir;
    NoBudgetFolder when there is no dev_drive."""
    common = _common(root)
    try:
        folder = result.kit_folder(DIR_NAME, 'gate budget folder')
    except result.ResultError as e:
        raise NoBudgetFolder(str(e))
    return os.path.join(folder, result.repo_key(common), '%s.json' % result.file_stem(branch))


def legacy_path(root, branch):
    """The allowance's .git copy: written for an older kit, and read when the folder has none
    (allowance)."""
    return os.path.join(_common(root), DIR_NAME, '%s.json' % result.file_stem(branch))


def _folder_path(root, branch):
    """allow_path, or None when there is no allowance folder (no dev_drive)."""
    try:
        return allow_path(root, branch)
    except NoBudgetFolder:
        return None


def allowance(root, branch, after=None):
    """The allowance record of `branch` (a dict), or None: none, or written before `after`. The
    folder's file; only when there is no folder (no dev_drive), the .git copy (legacy_path)."""
    folder = _folder_path(root, branch)
    # The .git copy is kept for load nodes set up without a dev_drive (docs/load-nodes.md).
    for path in ((folder,) if folder is not None else (legacy_path(root, branch),)):
        try:
            with open(path, encoding='utf-8') as f:
                rec = json.load(f)
            break
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            raise BudgetError('the allowance %s cannot be read: %s' % (path, e))
    else:
        return None
    n = rec.get('runs') if isinstance(rec, dict) else None
    if isinstance(n, bool) or not isinstance(n, int) or rec.get('branch') != branch:
        raise BudgetError('the allowance %s is not one this check wrote' % path)
    at = lockstore.epoch(rec.get('at'))
    if after is not None and (at is None or at < after.timestamp()):
        return None
    return rec


def limit(rec):
    return max(RUNS, rec['runs']) if rec else RUNS


def actor():
    """Who is asking (kitlib.lock.store.caller): read from this process's tree."""
    return lockstore.caller()


def is_owner(who):
    return isinstance(who, dict) and who.get('role') == 'owner'


def refusal(root, store, repo_name, branch, say, who=None):
    """None to book, or the REFUSED lines. Says `gate budget: run <k+1> of <n> on <branch>` when
    it books. None for a detached HEAD (no branch), and for the owner (the caller, read only once
    the runs reach the limit); None after one WARNING when an input cannot be read."""
    if not branch:
        return None
    try:
        after = since(root, branch)
        done = runs(store, repo_name, branch, after)
        n = limit(allowance(root, branch, after))
    except (BudgetError, OSError, ValueError) as e:
        say('WARNING: the branch gate budget is not applied: %s' % e)
        return None
    if len(done) < n:
        say('gate budget: run %d of %d on %s' % (len(done) + 1, n, branch))
        return None
    if is_owner(actor() if who is None else who):     # read only when it would refuse
        return None
    when = after.strftime('%Y-%m-%d %H:%M UTC') if after else 'the history the lock keeps'
    return ['REFUSED: the branch gate budget: %s has had %d gate runs since %s (%s), the %d a '
            'branch gets until its coordinator allows more (%s); nothing ran' % (
                branch, len(done), when, ', '.join(str(d.get('id')) for d in done), n, DOC),
            'Instead: stop and send your coordinator what each run found and why another is '
            'needed; it allows more with python <kit>/tools/gate.py allow %s --runs %d --why '
            '"<reason>" -C %s' % (branch, len(done) + 1, root)]


def allow(root, branch, total, why, store, who=None):
    """Writes the allowance of `branch`: `total` gate runs in all, replacing any earlier one, to
    the folder; with no folder (no dev_drive) to the .git copy (legacy_path) instead, which
    allowance() then reads. (path written, the folder's or else the copy's; runs used so far). ValueError when
    total is not a whole number above RUNS or why is empty; BudgetError when the branch, its
    repository or a file cannot be read or written."""
    if isinstance(total, bool) or not isinstance(total, int) or total <= RUNS:
        raise ValueError('--runs is the new total, a whole number above %d, not %r' % (RUNS, total))
    if not (why or '').strip():
        raise ValueError('--why is required: what the runs found and why more are needed')
    who = actor() if who is None else who
    sha, why_not = refs.ref_commit('refs/heads/%s' % branch, root)
    if sha is None:
        raise BudgetError('cannot tell whether %s is a branch: %s' % (branch, why_not))
    if not sha:
        raise BudgetError('%s is not a branch of %s' % (branch, root))
    after = since(root, branch)
    used = len(runs(store, repo_name_of(root), branch, after))
    path, copy = _folder_path(root, branch), legacy_path(root, branch)
    rec = {'branch': branch, 'runs': total, 'why': why.strip(), 'at': lockstore.iso(),
           'by': {'role': who.get('role'), 'session': who.get('session')}}
    if path is None:
        # Kept for load nodes set up without a dev_drive (docs/load-nodes.md).
        _write(copy, rec)
        return copy, used
    _write(path, rec)
    return path, used


def _write(path, rec):
    try:
        lockstore.write_json(path, rec)
    except OSError as e:
        raise BudgetError('the allowance %s cannot be written: %s' % (path, e))


def repo_name_of(root):
    """The repository name the branch gate books under (runner.repo_name)."""
    import kitlib.config as kconfig
    from kitlib.config.types import ConfigError
    from kitlib.gate import runner      # runner imports this module
    top, why = repo.toplevel(root)
    if not top:
        raise BudgetError(why)
    try:
        return runner.repo_name(kconfig.load(top), top)
    except ConfigError as e:
        raise BudgetError(str(e))


def slug_of(branch):
    from kitlib.gate import runner      # runner imports this module
    return runner.slug_of(branch)
