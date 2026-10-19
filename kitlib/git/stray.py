"""Stray changes in a repository's MAIN checkout (from the PM kit's
checkout_guard.main_checkout_changes and main_stray_check; its stray-writer attribution was dropped:
a guess at which worktree wrote a stray is not worth its cost).

The main checkout is lander-only. A write meant for a linked worktree that lands in the main
checkout instead (a relative path resolved against the wrong directory, an Edit with a wrong
absolute path) shows there as an untracked or uncommitted change. `changes(path)` lists them from
the main checkout itself or from any linked worktree of it; `check(path)` turns them into the
`[FAIL]` lines tools/checkout_guard.py `--main-stray` prints (exit 1).

"Could not tell" is never "clean": a git failure is a WARNING (code 0) or, under `allow_tracked`
(a repo's landing gate step, run in the `_landing` linked worktree), a `[FAIL]` (code 1). The
flag changes nothing else: tracked and untracked strays count the same with or without it.
Nothing here prints or exits.
"""
import os

from . import refs, repo

STRAY_FAIL = ('[FAIL] stray in the main checkout: %s (%s) - a write went to the main checkout; '
              'remove it (owner if a delete is refused)')
CLEAN = 'main checkout clean: no stray untracked or uncommitted files'


def changes(path):
    """`([(xy, path)], None)`: the MAIN checkout's changes a branch author could have caused, found
    from `path` (the main checkout or any linked worktree of it); or `([], why)`.

    Counted: every untracked, non-ignored file (`--untracked-files=all`, so a stray in a new folder
    is named as the file, not the folder) and every modified, added, deleted or renamed tracked
    path, a merge in progress in the main checkout included. The lander merges in the `_landing`
    linked worktree, never in the main checkout, so no tracked edit there is the lander's own."""
    place = repo.classify(path)
    top = repo.main_checkout_toplevel(path, place)
    if top is None:
        return [], 'could not find the main checkout of the repository at %s (%s%s)' % (
            os.path.abspath(path), place.kind, ': %s' % place.detail if place.detail else '')
    entries, why = refs.status(top, untracked='all')
    if entries is None:
        return [], 'git status failed in the main checkout (%s): %s' % (top, why)
    return entries, None


def check(path, allow_tracked=False):
    """`(code, [line])`: one STRAY_FAIL line per change changes() finds, and code 1; or the CLEAN
    line and 0. A lookup failure is a WARNING and 0 ("could not tell" is not a stray, and a hung
    git must not fail every gate), but under `allow_tracked` it is a `[FAIL]` and 1: a landing
    gate must not pass unchecked. `allow_tracked` changes nothing else (the name is kept for the
    `merged_args` that repos already pass)."""
    entries, error = changes(path)
    if error:
        if allow_tracked:
            return 1, ['[FAIL] could not check the main checkout for stray files: %s' % error]
        return 0, ['WARNING: could not check the main checkout for stray files: %s' % error]
    if not entries:
        return 0, [CLEAN]
    return 1, [STRAY_FAIL % (p, 'untracked' if xy == '??'
                             else 'uncommitted, git status %r' % xy.strip())
               for xy, p in entries]
