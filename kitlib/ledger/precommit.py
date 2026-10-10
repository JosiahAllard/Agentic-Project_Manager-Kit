"""The opt-in ledger pre-commit check (docs/ledger.md "Pre-commit check").

githooks/pre-commit (a shell front end) runs githooks/pre-commit.py, which calls `main`, only when
the repository's `.kit.json` text names `pre_commit_check`. A repository opts in with `.kit.json`
`ledger.pre_commit_check` set in `.kit.json` itself (the front end reads no section file);
otherwise nothing runs. Opted in, it runs `tools/ledger.py check --base HEAD` (the machine's ledger
file against its last commit: well formed, append-only, standing digest) and a failure blocks the
commit. A `.kit.json` the kit cannot load blocks the commit too when its text names
`pre_commit_check` (fails closed), and is a note and no check when it does not.

Nothing here prints or exits; `main` returns `(exit code, message or None)`.
"""
import os
import subprocess
import sys

from kitlib import config

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER_TOOL = os.path.join(KIT, 'tools', 'ledger.py')
WORD = 'pre_commit_check'


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
    return [sys.executable, LEDGER_TOOL, 'check', '--base', 'HEAD', '--root', root]


def main(cwd=None):
    """Git runs the hook at the work-tree root, so `cwd` (default the current folder) is the root."""
    root = os.path.abspath(cwd or os.getcwd())
    on, why = enabled(root)
    if why:
        if names_opt_in(root):
            return 1, ('pre-commit: .kit.json names %s but cannot be loaded: %s; commit refused'
                       % (WORD, why))
        return 0, 'pre-commit: ledger check skipped: .kit.json: %s' % why
    if not on:
        return 0, None
    rc = subprocess.call(command(root), cwd=root)
    if rc:
        return rc, 'pre-commit: tools/ledger.py check failed (exit %d); commit refused' % rc
    return 0, None
