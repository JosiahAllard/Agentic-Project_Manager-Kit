"""worktree_remove.py: remove a workstream's worktree only when its work has landed and nothing
holds it (docs/worktree-remove.md).

  worktree_remove.py [-C DIR] <path or slug>                     check, remove, then `git branch -d`
  worktree_remove.py [-C DIR] <path or slug> --keep-branch       the same; the branch stays
  worktree_remove.py [-C DIR] <path or slug> --abandon "<why>" [--keep-branch]
                                                                 UNLANDED work: tag archive/<slug>,
                                                                 then remove; LANDED but with
                                                                 uncommitted work: save that work
                                                                 under archive/<slug>, then remove
  worktree_remove.py [-C DIR] <branch> --branch                  LANDED local branch with NO worktree:
                                                                 `git branch -d` it
  worktree_remove.py [-C DIR] <branch> --branch --abandon "<why>"
                                                                 UNLANDED local branch with NO worktree:
                                                                 tag archive/<name>, then delete it
  ... --abandon "<why>" --tag archive/<name>                     either abandon, under the tag a
                                                                 ruling names: made when absent,
                                                                 reused on the tip, else refused
  worktree_remove.py [-C DIR] <path> --scratch                   a DETACHED tree under a temp folder
                                                                 whose dirt git already holds: restore,
                                                                 then remove
  worktree_remove.py [-C DIR] --check                           leftovers; exit 1 if any
  worktree_remove.py [-C DIR] --sweep-merged                     DRY RUN of the nightly sweep
  worktree_remove.py [-C DIR] --sweep-merged --apply             the nightly sweep
  worktree_remove.py [-C DIR] --classify [--json]               read-only: every worktree's class
                                                                 (A landed, B finished, C half-done,
                                                                 E evidence, L live or kept, O orphan)
  worktree_remove.py [-C DIR] --classify [--json] --apply       the same, then remove each A and E
                                                                 tree by its own remove path and
                                                                 drop each O tree's stale
                                                                 registration (its branch kept); B,
                                                                 C and L are never touched

-C names the repository by any of its work trees (default: here); the worktrees acted on are always
the MAIN checkout's, and the sweep takes only the main checkout. A relative path argument means
the caller's directory. A registered DETACHED worktree outside the worktree folders is named by
its path, never its slug. A thin entry point (ARCHITECTURE.md, Layers): it parses, calls
kitlib.worktree.remove, .scratch, .sweep or .classify, prints and exits. Exit codes: 0 done (or
nothing to do); 1 refused, a leftover, git failed, the sweep deferred, --classify found a tree of
class A, B, C, E or O, or --classify --apply left a B or C tree or did not remove an A, E or O
one; 2
usage, or the kit configuration was refused; 130 Ctrl+C.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.worktree import classify, remove, scratch, sweep        # noqa: E402

USAGE = 2


def usage(message):
    sys.stderr.write(__doc__)
    sys.stderr.write('USAGE: %s\n' % message)
    return USAGE


def split_root(argv):
    """`(root, rest)`, or `(None, why)` when -C is repeated or has no value."""
    root, rest, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == '-C' or a.startswith('-C') and len(a) > 2 and not a.startswith('--'):
            if root is not None:
                return None, '-C is given twice'
            value = a[2:] if a != '-C' else (argv[i + 1] if i + 1 < len(argv) else None)
            if not value:
                return None, '-C needs a directory'
            root = value
            i += 1 if a != '-C' else 2
            continue
        rest.append(a)
        i += 1
    return (root or '.'), rest


def split_tag(args):
    """`(tag or None, rest)`, or `(False, why)` when --tag is repeated or has no name."""
    tag, rest, i = None, [], 0
    while i < len(args):
        a = args[i]
        if a == '--tag' or a.startswith('--tag='):
            if tag is not None:
                return False, '--tag is given twice'
            value = a[len('--tag='):] if a != '--tag' else (
                args[i + 1] if i + 1 < len(args) else None)
            if not value or value.startswith('-'):
                return False, '--tag needs a tag name, archive/<name>'
            tag = value
            i += 1 if a != '--tag' else 2
            continue
        rest.append(a)
        i += 1
    return tag, rest


def parse(args):
    """`(verb, target, why, keep, apply)` or a usage message (str). Verbs: sweep, check, abandon,
    abandon-branch, remove-branch, remove, scratch, classify and classify-apply (`apply` then
    means --json). A why
    that starts with `--` is a flag typed where the why belongs, never a reason."""
    if '--classify' in args or '--json' in args:
        if sorted(args) not in (['--classify'], ['--classify', '--json'], ['--apply', '--classify'],
                                ['--apply', '--classify', '--json']):
            return '--classify takes only --json and --apply (and -C)'
        return ('classify-apply' if '--apply' in args else 'classify'), None, None, False, \
            '--json' in args
    if '--sweep-merged' in args or '--apply' in args:
        if sorted(args) not in (['--sweep-merged'], ['--apply', '--sweep-merged']):
            return '--sweep-merged takes only --apply (and -C)'
        return 'sweep', None, None, False, '--apply' in args
    if '--scratch' in args:
        rest = [a for a in args if a != '--scratch']
        if args.count('--scratch') != 1 or len(rest) != 1 or rest[0].startswith('-'):
            return '--scratch takes exactly one <path> and no other option'
        return 'scratch', rest[0], None, False, False
    keep = args.count('--keep-branch')
    by_branch = args.count('--branch')
    rest, whys, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == '--abandon':
            value = args[i + 1] if i + 1 < len(args) else None
            if value is not None and value.startswith('--'):
                value = None
            else:
                i += 1
            whys.append(value)
        elif a.startswith('--abandon='):
            value = a[len('--abandon='):]
            whys.append(None if value.startswith('--') else value)
        elif a not in ('--keep-branch', '--branch'):
            rest.append(a)
        i += 1
    if by_branch and (by_branch > 1 or keep):
        return '--branch takes at most one --abandon "<why>" and not --keep-branch'
    if len(rest) != 1 or keep > 1 or len(whys) > 1 or (
            rest[0] == '--check' and (keep or whys)) or (
            rest[0].startswith('-') and rest[0] != '--check'):
        return 'one <path or slug>, or --check, with at most one --keep-branch and one --abandon'
    why = whys[0].strip() if whys and whys[0] is not None else None
    if whys and not why:
        return '--abandon needs a reason: say why this work is dropped (it becomes the tag message)'
    if by_branch and rest[0] == '--check':
        return '--branch names a branch, not --check'
    if rest[0] == '--check':
        return 'check', None, None, False, False
    if by_branch:
        return ('abandon-branch' if why is not None else 'remove-branch'), rest[0], why, False, False
    return ('abandon' if why is not None else 'remove'), rest[0], why, bool(keep), False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root, rest = split_root(argv)
    if root is None:
        return usage(rest)
    tag, rest = split_tag(rest)
    if tag is False:
        return usage(rest)
    got = parse(rest)
    if isinstance(got, str):
        return usage(got)
    verb, target, why, keep, apply = got
    if tag is not None and verb not in ('abandon', 'abandon-branch'):
        return usage('--tag goes only with --abandon "<why>" (the tag the abandon keeps the work'
                     ' under)')
    top, err = gitrepo.toplevel(root)
    if top is None:
        return usage('-C: %s' % err)
    try:
        if verb in ('classify', 'classify-apply'):
            outcome = classify.classify(top, apply=verb == 'classify-apply')
            if apply:
                print(json.dumps(outcome.data, indent=2, sort_keys=True), flush=True)
                return outcome.code
        elif verb == 'sweep':
            outcome = sweep.sweep(top, apply=apply)
        elif verb == 'check':
            outcome = remove.check(top)
        elif verb == 'scratch':
            outcome = scratch.remove_scratch(top, target)
        elif verb == 'abandon-branch':
            outcome = remove.abandon_branch(top, target, why, tag=tag)
        elif verb == 'remove-branch':
            outcome = remove.remove_branch(top, target)
        elif verb == 'abandon':
            outcome = remove.abandon(top, target, why, keep_branch=keep, tag=tag)
        else:
            outcome = remove.remove(top, target, keep_branch=keep)
    except KeyboardInterrupt:
        return 130
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
