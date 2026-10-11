"""board.py: the coordinator's whole board in one call. Read-only unless --write-state.

  board.py [-C DIR] [--base REV] [--write-state]

Prints the base against <remote>/<base>, then a row per worktree: branch, tip, ahead/behind the
base, whether it contains the base tip, age of the last commit, dirty files, owner-only paths
touched; then the machine lock (the kit lock's own status), the stale-doc count and, when
`.kit.json` "preflight" sets engines or udp_ports, a `machine:` line. A reading that failed is `?`
or `unknown`, never a value (docs/preflight.md "board.py").

--write-state rewrites ONLY the block between `<!-- board:begin -->` and `<!-- board:end -->` in
the state file of the MAIN checkout: <git common dir>/kit/board-state.md, or "board".state_file when
that lies under the git dir (a path in the work tree is refused: it would block a landing). Exit 0; 2 on a usage error, a directory in no git work tree, or a `.kit.json` the
board cannot use.

A thin entry point (ARCHITECTURE.md, Layers): kitlib.board does the work.
"""
import argparse
import os
import sys
import time

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

from kitlib.board import report, state                              # noqa: E402
from kitlib.config import ConfigError, load as load_config        # noqa: E402
from kitlib.git import refs, repo                                   # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='board.py', description='The coordinator\'s board.')
    p.add_argument('-C', dest='dir', default='.', help='a checkout of the repository (default: here)')
    p.add_argument('--base', default=None,
                   help='the base revision (default: "repo".main_branch if set, else main if it '
                        'exists, else master; rows measured against <remote>/<base> when '
                        'the two differ)')
    p.add_argument('--write-state', action='store_true',
                   help='also rewrite the board block of the state file')
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
        lines = report.lines(top, cfg, KIT, base=a.base, **kw)
        rel = report.state_file(cfg)
    except ConfigError as e:
        print_safe('board: refused: kit configuration: %s' % e)
        return USAGE
    text = '\n'.join(lines)
    print_safe(text)
    if a.write_state:
        where, why = state.path(repo.main_checkout_toplevel(top) or top, rel)
        if where is None:
            print_safe('board: refused: %s' % why)
            return USAGE
        size = state.write(where, time.strftime('%Y-%m-%d %H:%M'), text)
        print_safe('state written: %s (%d bytes)' % (where, size))
    return 0


if __name__ == '__main__':
    sys.exit(main())
