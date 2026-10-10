"""landed.py: has a branch landed? One read-only look, never a wait (docs/landed.md).

  landed.py [-C DIR] <branch or commit>

-C names the checkout to look from (default: here). A thin entry point (ARCHITECTURE.md, Layers):
it parses, calls kitlib.landed.look, prints and exits. Exit codes: 0 LANDED; 1 NOT YET; 2 FAILED,
CANNOT ANSWER, the kit configuration refused, or usage; 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitlib.landed as landed                                      # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (a usage error is never worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='landed.py', description='One read-only look: has this branch landed?')
    p.add_argument('ref', help='the branch (or commit) to look for')
    p.add_argument('-C', dest='root', default='.', help='any work tree of the repository (default: here)')
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if not a.ref.strip() or a.ref.startswith('-'):
        p.error('a branch or commit is needed')
    top, why = gitrepo.toplevel(a.root)
    if top is None:
        p.error('-C: %s' % why)
    try:
        outcome = landed.look(top, a.ref)
    except KeyboardInterrupt:
        return 130
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
