"""ruling_cites.py: fail a branch that ADDS a `PM R-nnnn` citation the PM's rulings ledger cannot
back; docs/repo-checks.md.

  ruling_cites.py [-C DIR] [--base REV]

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.rulingcites, prints and
exits. The ledger is the one the machine config names (kitlib.config.ledger_path); no argument or
environment variable overrides it. Exit codes: 0 no added bad citation (warnings included, or no
ledger to check against, said in a WARNING line), 1 an added one, 2 USAGE, refused configuration,
or git failed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import rulingcites as RC                                # noqa: E402
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
    p = Parser(prog='ruling_cites.py',
               description='Fail added citations the rulings ledger cannot back (docs/repo-checks.md).')
    p.add_argument('-C', dest='root', default='.', help='the checkout to check (default .)')
    p.add_argument('--base', default=None, help='default: <remote>/<main>, else <main> (docs/gate.md "Default base")')
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('RULING CITES NOT CHECKED: %s' % why)
        return 2
    try:
        out = RC.check(top, base=a.base, cfg=load_config(top))
    except ConfigError as e:
        say('ruling_cites: refused: kit configuration: %s' % e)
        return 2
    for line in out.lines:
        say(line)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
