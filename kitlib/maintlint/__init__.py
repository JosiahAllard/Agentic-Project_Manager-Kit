"""kitlib.maintlint: the maintenance lint, read-only (docs/maint-lint.md). Every check flags on
evidence that something is spent or wrong (a duplicate, a path gone, a blocker that names no live
record, a pull with no branch, a landed tree), never on a record's or a tree's age alone.

  Finding(check, key, text, fix)  one finding: its check, a key unique within the check, the line
                                  shown, and the command (or step) that fixes it, printed and
                                  never run
  fingerprint(finding)            a short hash of check, key and text: the same finding on the next
                                  run has the same one
  run(root, home, now, previous)  the whole lint of the repository at `root`, as a result dict
                                  (kitlib.maintlint.result writes it)

Nothing here writes a record, the ledger, a worktree or git.
"""
import collections
import datetime
import hashlib
import os
import time

from kitlib import kitroot
from kitlib.backlog.errors import Refusal
from kitlib.backlog import pull_branch as pullmod
from kitlib.backlog.opener import open_store

Finding = collections.namedtuple('Finding', 'check key text fix')

from . import backlog, holds, ledger, result, verify, worktrees     # noqa: E402 (they use Finding)

VERSION = 1
CHECKS = ('keyword-hold', 'unpriced', 'dead-pull', 'dead-blocker', 'duplicate', 'audit',
          'body-path-gone', 'file-moved', 'scope-path-gone', 'worktree-A', 'worktree-B',
          'worktree-C', 'worktree-E', 'worktree-O', 'landed-still-pulled')


def fingerprint(f):
    return hashlib.sha1(('%s|%s|%s' % (f.check, f.key, f.text)).encode('utf-8')).hexdigest()[:16]


def backlog_command(root):
    return kitroot.command('backlog', root)


def code_checkout(root):
    """The checkout a record's paths are read in: the code repository's main checkout when `root`
    is its PM data worktree (whose records name the code's files, not the data branch's), else
    `root` (kitlib.backlog.pull_branch.code_config)."""
    code = pullmod.code_config(root)
    return code.root if code is not None else root


def _backlog_checks(root, home, now, notes):
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
                 lambda: backlog.audit(ctx.root, cmd, home=home, now=now),
                 lambda: verify.paths(snap, code_root, cmd, ctx.date()),
                 lambda: verify.scopes(snap, code_root, ctx.code_main, cmd)):
        found, more = step()
        out += found
        notes += more
    return out


def run(root, home=None, now=None, previous=None):
    """The result dict for the repository at `root`. `previous`: the last run's result (or None),
    whose fingerprints mark each finding new or not."""
    started = time.monotonic()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    notes = []
    findings = _backlog_checks(root, home, now, notes)
    found, more = worktrees.findings(root)
    findings += found
    notes += more
    counts_ledger, more = ledger.counts(home)
    notes += more
    seen = result.previous_fingerprints(previous)
    rows = []
    for f in findings:
        fp = fingerprint(f)
        rows.append({'check': f.check, 'key': f.key, 'text': f.text, 'fix': f.fix,
                     'fingerprint': fp, 'new': previous is not None and fp not in seen})
    counts = collections.Counter(f.check for f in findings)
    return {'version': VERSION,
            'repo': os.path.basename(os.path.abspath(root).rstrip('\\/')),
            'started': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'elapsed_s': round(time.monotonic() - started, 2),
            'counts': {c: counts.get(c, 0) for c in CHECKS},
            'ledger': counts_ledger, 'notes': notes, 'findings': rows}
