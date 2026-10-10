"""preflight.py: the first command of a session - where it is, how its branch stands against the
base, what is dirty, the docs budget, strays in the main checkout, the machine and the lock, and
the kit commands to run next (docs/preflight.md).

  preflight.py [-C DIR] [--fetch] [--base REV]

-C names the checkout to report on (default: here). --fetch fetches the remote first. --base
names the base (any revision; default `.kit.json` "repo".main_branch when set, else main when it
exists, else master). Exit 0 with the report; 2 on a usage error, a directory in no git work
tree, or a `.kit.json` preflight cannot use. A reading that fails is a line saying so, never an
exit code: the report is read, not gated on.

A thin entry point (ARCHITECTURE.md, Layers): kitlib.preflight does the work.
"""
import argparse
import os
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

from kitlib.config import ConfigError, load as load_config        # noqa: E402
from kitlib.git import refs, repo                                   # noqa: E402
from kitlib.preflight import report                                 # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='preflight.py', description='A session\'s first look at its checkout.')
    p.add_argument('-C', dest='dir', default='.', help='the checkout to report on (default: here)')
    p.add_argument('--fetch', action='store_true', help='git fetch the remote first')
    p.add_argument('--base', default=None,
                   help='the base revision (default: "repo".main_branch if set, else main if it '
                        'exists, else master; rows measured against <remote>/<base> when '
                        'the two differ)')
    return p


def print_safe(text):
    """print(text), never raising UnicodeEncodeError for a path the console cannot encode."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(enc, errors='replace').decode(enc, errors='replace'), flush=True)


def main(argv=None, lock_reader=None):
    p = build_parser()
    a = p.parse_args(argv)
    if a.base is not None:
        why = refs.bad_rev(a.base)
        if why:
            p.error('--base: %s' % why)
    top, why = repo.toplevel(os.path.abspath(a.dir))
    if top is None:
        p.error('-C: %s' % why)
    try:
        cfg = load_config(top)
        kw = {'lock_reader': lock_reader} if lock_reader else {}
        lines = report.report(top, cfg, KIT, base=a.base, fetch=a.fetch, **kw)
    except ConfigError as e:
        print_safe('preflight: refused: kit configuration: %s' % e)
        return USAGE
    for line in lines:
        print_safe(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
