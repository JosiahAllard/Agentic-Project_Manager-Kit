"""review_pack.py: everything a reviewer or lander needs about a branch, in one read-only call
(docs/review-pack.md).

  review_pack.py [-C DIR] <branch> [--base BRANCH] [--diff PATH]

Prints base and merge-base, whether the branch contains the base tip, commits, files changed by
risk level, owner-only paths and their Owner-Approved trailers, the commits with no Co-Authored-By
trailer (.kit.json "review_pack".coauthor), and the doc-budget verdict. --diff PATH prints that
file's diff against the merge-base. --base defaults to docs/gate.md "Default base".

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.reviewpack, prints and exits.
Exit codes: 0 nothing to stop a landing, 1 an owner-only trailer problem, a commit the trailers
rule fails or a failing doc budget, 2 usage, no merge-base, a failed git read or the configuration.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import reviewpack                                       # noqa: E402


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(reviewpack.USAGE)


def build_parser():
    """allow_abbrev=False: a prefix of a long flag is a usage error, never taken as the flag."""
    p = Parser(prog='review_pack.py', allow_abbrev=False,
               description='Everything a reviewer or lander needs about a branch. Read-only.')
    p.add_argument('-C', dest='root', default='.', metavar='DIR',
                   help='a checkout of the repository (default: here)')
    p.add_argument('branch', help='the branch to review')
    p.add_argument('--base', help='the base branch (default: docs/gate.md "Default base")')
    p.add_argument('--diff', metavar='PATH', help='print only this file\'s merge-base diff')
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    if not a.root or not a.root.strip():
        p.error('-C needs a directory')
    if not a.branch.strip():
        p.error('name the branch to review')
    if a.base is not None and (not a.base.strip() or a.base.startswith('-')):
        p.error('--base needs a branch name')
    out = reviewpack.pack(a.root, a.branch, a.base, a.diff)
    for line in out.lines:
        print(line, flush=True)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
