"""stray_restore.py: discard a main-checkout stray only when a branch tip holds its exact bytes at
the same path (docs/stray-restore.md).

  stray_restore.py [-C DIR]
      the dry run: one line per stray in the main checkout of the repository at DIR (default:
      here; the main checkout or any linked worktree of it), DELETE / RESTORE / UNDELETE with the
      branch tip that proves it, or LEAVE with the reason. Writes nothing.
  stray_restore.py [-C DIR] --apply
      discards each proven stray (an untracked file is deleted, a work-tree change or delete is
      restored to HEAD), one log line each in <git common dir>/kit-stray-restore.jsonl, then
      lists what git status still shows. LEAVE paths are never touched.

Exit 0 when nothing is LEAVE (after --apply: nothing is left), 1 when something is or when an
error stopped --apply part way (the rest is LEAVE and the left lines still print), 2 on a usage
error, a refusal (the session's mark, a merge or rebase in progress) or a git failure before
anything was touched. Run by the owner, the PM, a coordinator or the lander only (the session
mark).

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.strayrestore, prints and
exits.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import sessionmark, strayrestore as sr                   # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='stray_restore.py',
               description='Discard main-checkout strays a branch tip holds byte for byte.')
    p.add_argument('-C', dest='path', default='.', metavar='DIR',
                   help='a checkout of the repository (default: here)')
    p.add_argument('--apply', action='store_true',
                   help='discard the proven strays (default: a dry run that writes nothing)')
    return p


def print_safe(text):
    """print(text), never raising UnicodeEncodeError for a path the console cannot encode."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(enc, errors='replace').decode(enc, errors='replace'), flush=True)


def line(item, applied):
    if item.verdict == sr.LEAVE:
        return 'LEAVE     %s: %s' % (item.path, item.reason)
    head = '%-9s %s' % (sr.ACTIONS[item.verdict] if applied else item.verdict, item.path)
    if item.match is None:
        return '%s = HEAD' % head
    return '%s = %s %s' % (head, item.match.ref, item.match.commit[:12])


def main(argv=None, environ=None):
    a = build_parser().parse_args(argv)
    mark = sessionmark.mark(environ)
    if not sr.allowed(mark):
        print_safe('REFUSED: stray_restore.py runs for the owner, the PM, a coordinator or the '
                   'lander; this session is marked %r' % mark)
        return USAGE
    run = sr.apply(a.path, environ) if a.apply else sr.plan(a.path)
    if run.refused:
        print_safe('REFUSED: %s; nothing was touched' % run.refused)
        return USAGE
    if run.top:
        print_safe('main checkout: %s' % run.top)
    for item in run.items:
        print_safe(line(item, a.apply))
    if run.error and not run.items:
        print_safe('ERROR: %s' % run.error)
        return USAGE
    if run.error:
        # An error part way through --apply: the items not reached are LEAVE, and what git
        # status still shows is listed, so nothing is reported as done that was not.
        print_safe('ERROR: %s' % run.error)
        for xy, p in run.left or ():
            print_safe('left: %s (git status %r)' % (p, xy))
        return 1
    if not run.items:
        print_safe('main checkout clean: no stray untracked or uncommitted files')
        return 0
    if a.apply:
        for xy, p in run.left or ():
            print_safe('left: %s (git status %r)' % (p, xy))
        return 1 if run.left else 0
    return 1 if any(i.verdict == sr.LEAVE for i in run.items) else 0


if __name__ == '__main__':
    sys.exit(main())
