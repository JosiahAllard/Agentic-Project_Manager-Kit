"""The kit's git pre-commit dispatch (docs/ledger.md "Pre-commit check").

githooks/pre-commit.py calls `main` and nothing else. `main` runs `CHECKS` in order against one
`Commit` and stops at the first that refuses. Today `CHECKS` holds only the ledger check
(kitlib.ledger.precommit.check). Another step, a repository's own hook for example, is one more
entry in `CHECKS`; nothing here looks for one.

A check is `check(commit) -> (exit code, message or None)`. A check that raises refuses the
commit (fails closed): a hook that cannot judge must not wave a commit through.

Nothing here prints or exits; `main` returns `(exit code, message or None)`.
"""
import os

from kitlib.git import cmd
from kitlib.ledger import precommit as ledger_precommit

CHECKS = (ledger_precommit.check,)


class StagedError(Exception):
    """git could not list the files the commit changes."""


class Commit:
    """The commit being made. Git runs the hook at the work-tree root with GIT_INDEX_FILE naming
    the index the commit is built from (a temporary one for `commit -a` or `commit -- <paths>`);
    kitlib.git.cmd keeps GIT_INDEX_FILE, so `staged` sees that index."""

    def __init__(self, root):
        self.root = root
        self._staged = None

    def staged(self):
        """The paths, relative to the root with `/`, that the commit adds, changes or deletes,
        read once. Raises StagedError when git cannot answer."""
        if self._staged is None:
            out, why = cmd.run(['diff', '--cached', '--name-only', '--no-renames', '-z'], self.root)
            if out is None:
                raise StagedError('cannot list the staged files: %s' % why)
            self._staged = tuple(p for p in cmd.text(out).split('\0') if p)
        return self._staged


def _name(check):
    return '%s.%s' % (getattr(check, '__module__', '?'), getattr(check, '__name__', repr(check)))


def main(cwd=None, checks=None):
    """Run each check in order; the first non-zero code refuses the commit. `cwd` (default the
    current folder) is the work-tree root."""
    commit = Commit(os.path.abspath(cwd or os.getcwd()))
    notes = []
    for check in CHECKS if checks is None else checks:
        try:
            code, message = check(commit)
        except Exception as e:                   # any failure to judge refuses (fail closed)
            notes.append('pre-commit: %s could not run: %s; commit refused'
                         % (_name(check), str(e) or type(e).__name__))
            return 1, '\n'.join(notes)
        if message:
            notes.append(message)
        if code:
            return code, '\n'.join(notes)
    return 0, '\n'.join(notes) or None
