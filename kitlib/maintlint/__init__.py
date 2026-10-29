"""kitlib.maintlint: the maintenance lint (docs/maint-lint.md). Every check flags on
evidence that something is spent or wrong (a duplicate, a path gone, a blocker that names no live
record, a pull with no branch, a landed tree), never on a record's or a tree's age alone.

  Finding(check, key, text, fix, age)
                                  one finding: its check, its target (a key unique within the
                                  check that names the thing itself, never a sha or a count), the
                                  line shown, the command (or step) that fixes it, printed and
                                  never run, and its age as context only (a date or an idle
                                  time, or None), which never decides a finding
  fingerprint(repo, finding)      a short hash of repo, check and key, never the text: the same
                                  target on the next run has the same one, however it is worded
  run(root, home, now, previous)  the whole lint of the repository at `root`, as a result dict
                                  (kitlib.maintlint.result writes it)

The result's `module_scope` is the weekly sum of the gates' scope records
(kitlib.modulemap.scopelog.summary): counts only, never a finding, and None when the repository
names no module map.

run() writes no record, ledger entry, worktree or git ref. maint_lint writes only the gate-time
record, in a plain run (kitlib.maintlint.gatetime.file, which tools/maint_lint.py calls).
"""
import collections
import datetime
import hashlib
import os
import time

from kitlib import kitroot
from kitlib.backlog import audit_check
from kitlib.backlog.errors import Failure, Refusal
from kitlib.backlog import pull_branch as pullmod
from kitlib.backlog.opener import open_store
from kitlib.modulemap import scopelog

Finding = collections.namedtuple('Finding', 'check key text fix age', defaults=(None,))

from . import backlog, gatetime, holds, ledger, result, verify, worktrees  # noqa: E402 (they use Finding)

VERSION = 1
CHECKS = ('keyword-hold', 'unpriced', 'dead-pull', 'dead-blocker', 'duplicate', 'audit',
          'body-path-gone', 'file-moved', 'scope-path-gone', 'worktree-A', 'worktree-B',
          'worktree-C', 'worktree-E', 'worktree-O', 'landed-still-pulled', 'closed-unlanded',
          'answered-question', 'ledger-stale-cites', 'ledger-over-budget', 'ledger-rule-conflicts',
          'gate-time')
# The store history closed-unlanded reads: the audit's window.
CLOSE_DAYS = backlog.AUDIT_DAYS


def fingerprint(repo, f):
    return hashlib.sha1(('%s|%s|%s' % (repo, f.check, f.key)).encode('utf-8')).hexdigest()[:16]


def repo_name(root):
    """The repository's name in the result: the checkout folder's name."""
    return os.path.basename(os.path.abspath(root).rstrip('\\/'))


def backlog_command(root):
    return kitroot.command('backlog', root)


def code_checkout(root):
    """The checkout a record's paths are read in: the code repository's main checkout when `root`
    is its PM data worktree (whose records name the code's files, not the data branch's), else
    `root` (kitlib.backlog.pull_branch.code_config)."""
    code = pullmod.code_config(root)
    return code.root if code is not None else root


def _backlog_checks(root, home, now, notes, questions):
    """Every check that reads the store; a store that cannot be opened is one note."""
    try:
        ctx = open_store(root, fetch=False)
        snap = ctx.store.load()
    except (Refusal, OSError) as exc:
        notes.append('backlog NOT CHECKED: %s' % ' '.join(str(exc).split()))
        return []
    cmd = backlog_command(ctx.root)
    if snap.unreadable:
        notes.append('backlog: %d record(s) could not be read' % len(snap.unreadable))
    code_root = code_checkout(ctx.root)
    out = holds.findings(snap, cmd)
    for step in (lambda: backlog.unpriced(snap, cmd),
                 lambda: backlog.dead_pulls(snap, ctx, cmd),
                 lambda: backlog.dead_blockers(snap, cmd),
                 lambda: backlog.duplicates(snap, cmd),
                 lambda: backlog.answered(snap, cmd, questions),
                 lambda: backlog.audit(ctx.root, cmd, home=home, now=now),
                 lambda: verify.paths(snap, code_root, cmd, ctx.date()),
                 lambda: verify.scopes(snap, code_root, ctx.code_main, cmd),
                 lambda: _landing(ctx, snap, code_root, cmd, now)):
        found, more = step()
        out += found
        notes += more
    return out


def _landing(ctx, snap, code_root, cmd, now):
    """landed-still-pulled and closed-unlanded against the code base (kitlib.maintlint.verify)."""
    base, why = verify.code_base(code_root, ctx.code_remote, ctx.code_main)
    if base is None:
        return [], ['landing checks NOT CHECKED: %s' % ' '.join(str(why).split())]
    out, notes = verify.landed_pulls(snap, code_root, ctx.code_remote, base, cmd)
    try:
        root, rev, rel = audit_check.store_place(ctx.store.backend, ctx.store.rules)
        changes = (audit_check.history(root, rev, rel, now.timestamp() - CLOSE_DAYS * 86400)
                   if rev else [])
    except (audit_check.NotChecked, Failure, Refusal, OSError) as exc:
        return out, notes + ['closed-unlanded NOT CHECKED: %s' % ' '.join(str(exc).split())]
    found, more = verify.closed_unlanded(changes, snap, code_root, base, cmd)
    return out + found, notes + more


def _first_of_each(findings):
    """One finding per (check, key): the first (landed-still-pulled comes from the store's pulls
    and again from an A worktree)."""
    seen, out = set(), []
    for f in findings:
        if (f.check, f.key) not in seen:
            seen.add((f.check, f.key))
            out.append(f)
    return out


def run(root, home=None, now=None, previous=None, questions=None):
    """The result dict for the repository at `root`. `previous`: the last run's result (or None),
    whose fingerprints mark each finding new or not. `questions`: a callable giving the hub's
    question documents (None: the hub on this machine, read with the agent token)."""
    started = time.monotonic()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    notes = []
    findings = _backlog_checks(root, home, now, notes, questions)
    found, more = worktrees.findings(root)
    findings += found
    notes += more
    found, more = gatetime.findings(code_checkout(root), now)
    findings += found
    notes += more
    module_scope, more = scopelog.summary(code_checkout(root), now)
    notes += more
    counts_ledger, more = ledger.counts(home)
    notes += more
    findings = _first_of_each(findings + ledger.findings(counts_ledger))
    seen = result.previous_fingerprints(previous)
    repo = repo_name(root)
    rows = []
    for f in findings:
        fp = fingerprint(repo, f)
        rows.append({'repo': repo, 'check': f.check, 'target': f.key, 'text': f.text,
                     'fix': f.fix, 'age': f.age, 'fingerprint': fp,
                     'new': previous is not None and fp not in seen})
    counts = collections.Counter(f.check for f in findings)
    return {'version': VERSION,
            'repo': repo,
            'started': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'elapsed_s': round(time.monotonic() - started, 2),
            'counts': {c: counts.get(c, 0) for c in CHECKS},
            'ledger': counts_ledger, 'module_scope': module_scope, 'notes': notes,
            'findings': rows}
