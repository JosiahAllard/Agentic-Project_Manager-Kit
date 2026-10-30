"""preflight.py: the first command of a session - where it is, how its branch stands against the
base, what is dirty, the docs budget, strays in the main checkout, the machine and the lock, and
the kit commands to run next (docs/preflight.md).

  preflight.py [-C DIR] [--fetch] [--base REV] [--board [--write-state]]

-C names the checkout to report on (default: here). --fetch fetches the remote first. --base
names the base (any revision; default `.kit.json` "repo".main_branch when set, else main when it
exists, else master). --board adds the coordinator's board after the report: a row per worktree
against the base and the stale-doc line. --write-state (only with --board) rewrites the board
block of the coordinator's state file (docs/preflight.md "--board"). Exit 0 with the report; 2 on
a usage error, a directory in no git work tree, or a `.kit.json` preflight cannot use. A reading
that fails is a line saying so, never an exit code: the report is read, not gated on.

A thin entry point (ARCHITECTURE.md, Layers): kitlib.preflight does the work.
"""
import argparse
import os
import sys
import time

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

from kitlib.config import ConfigError, load as load_config        # noqa: E402
from kitlib.git import refs, repo                                   # noqa: E402
from kitlib.preflight import board, paths, report, state           # noqa: E402

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
    p.add_argument('--board', action='store_true',
                   help='also print the coordinator\'s board: a row per worktree, the stale-doc line')
    p.add_argument('--write-state', action='store_true',
                   help='with --board: also rewrite the board block of the state file')
    return p


def print_safe(text):
    """print(text), never raising UnicodeEncodeError for a path the console cannot encode."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(enc, errors='replace').decode(enc, errors='replace'), flush=True)


def main(argv=None, lock_reader=None, home=None):
    """`home` is the home folder the `config` line reads the machine config under (default the
    user's); tests pass a scratch one."""
    p = build_parser()
    a = p.parse_args(argv)
    if a.write_state and not a.board:
        p.error('--write-state needs --board')
    if a.base is not None:
        why = refs.bad_rev(a.base)
        if why:
            p.error('--base: %s' % why)
    fixed = paths.mangled_dir(a.dir)
    if fixed:
        p.error(paths.mangled_message(a.dir, fixed))
    top, why = repo.toplevel(os.path.abspath(a.dir))
    if top is None:
        p.error('-C: %s' % why)
    board_lines = rel = None
    try:
        cfg = load_config(top)
        kw = {'lock_reader': lock_reader} if lock_reader else {}
        lines = report.report(top, cfg, KIT, base=a.base, fetch=a.fetch, home=home, **kw)
        if a.board:
            board_lines = board.lines(top, cfg, KIT, base=a.base)
            rel = board.state_file(cfg)
    except ConfigError as e:
        print_safe(paths.forward('preflight: refused: kit configuration: %s' % e))
        return USAGE
    for line in lines + (board_lines or []):
        print_safe(paths.forward(line, top))
    if a.write_state:
        where, why = state.path(repo.main_checkout_toplevel(top) or top, rel)
        if where is None:
            print_safe('preflight: refused: %s' % why)
            return USAGE
        size = state.write(where, time.strftime('%Y-%m-%d %H:%M'), '\n'.join(board.state_lines(lines, board_lines)))
        print_safe(paths.forward('state written: %s (%d bytes)' % (where, size), where))
    return 0


if __name__ == '__main__':
    sys.exit(main())
