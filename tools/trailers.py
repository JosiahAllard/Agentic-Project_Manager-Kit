"""trailers.py: fail a branch holding a commit with no agent Co-Authored-By trailer
(docs/repo-checks.md).

  trailers.py [-C DIR] [--base REV] [--merged]

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.trailerscheck, prints and
exits. The rule is kitlib.reviewpack.trailers. Exit codes: 0 no failing commit (warnings
included), 1 a failing commit, 2 USAGE, refused configuration, a refused --merged, or git failed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import trailerscheck as TC                              # noqa: E402
from kitlib.config import ConfigError, load as load_config          # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402


def say(line):
    print(line, flush=True)


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def build_parser():
    p = Parser(prog='trailers.py',
               description='Fail a commit with no agent Co-Authored-By trailer (docs/repo-checks.md).')
    p.add_argument('-C', dest='root', default='.', help='the checkout to check (default .)')
    p.add_argument('--base', default=None,
                   help='default: <remote>/<main>, else <main> (docs/gate.md "Default base")')
    p.add_argument('--merged', action='store_true',
                   help="a landing's merged-tree gate: the lander's own merges on main are exempt")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('trailers: NOT CHECKED: %s' % why)
        return 2
    try:
        out = TC.check(top, base=a.base or None, cfg=load_config(top), merged=a.merged)
    except ConfigError as e:
        say('trailers: refused: kit configuration: %s' % e)
        return 2
    for line in out.lines:
        say(line)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
