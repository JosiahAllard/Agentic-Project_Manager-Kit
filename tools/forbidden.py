"""forbidden.py: refuse a forbidden token anywhere in the tree, without writing it down
(docs/repo-checks.md).

  forbidden.py [-C DIR] [--base REV] [--worktree]   what this branch changed (a gate step)
  forbidden.py [-C DIR] --all                       every tracked file and path
  forbidden.py [-C DIR] --paths P [P ...]           these paths, as given
  forbidden.py ... --paranoid                       also the space-removed form
  forbidden.py [-C DIR] --add                       read one token on stdin, store its digest

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.forbidden, prints and exits.
The token is read from stdin, never an argument, so it lands in no shell history or process list;
no output line ever holds it. Exit codes: 0 clean (or added), 1 a hit, 2 USAGE, refused config,
or a store that is missing, disarmed or unreadable, nothing read, or git failed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import forbidden as F                                   # noqa: E402
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
    p = Parser(prog='forbidden.py', description='Refuse a forbidden token (docs/repo-checks.md).')
    p.add_argument('-C', dest='root', default='.', help='the checkout to check (default .)')
    p.add_argument('--base', default=None, help='default: .kit.json "repo".main_branch, else main, else master, as '
                   '<remote>/<that> when it resolves (docs/gate.md "Default base")')
    p.add_argument('--worktree', action='store_true', help='compare the working tree, untracked files included')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--all', dest='sweep_all', action='store_true', help='every tracked path')
    mode.add_argument('--paths', nargs='+', metavar='PATH', help='these paths only')
    mode.add_argument('--add', action='store_true', help='read one token on stdin and store its digest')
    p.add_argument('--paranoid', action='store_true', help='also the space-removed form')
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        top, why = gitrepo.toplevel(os.path.abspath(a.root))
        if not top:
            raise F.ForbiddenError(why)
        cfg = load_config(top)
        if a.add:
            path = F.store_path(top, cfg)
            added, count, why = F.add_token(path, sys.stdin.readline())
            if not added:
                say('already present; nothing written')
            else:
                say('stored: %d token(s); self-test %s'
                    % (count, 'passes' if why is None else 'FAILS - %s' % why))
            return 0 if why is None else 2
        paths = None
        if a.paths:
            paths = [os.path.relpath(os.path.abspath(p), top).replace(os.sep, '/') for p in a.paths]
        out = F.check(top, base=a.base, worktree=a.worktree, sweep_all=a.sweep_all, paths=paths,
                      paranoid=a.paranoid, cfg=cfg)
    except ConfigError as e:
        say('forbidden: refused: kit configuration: %s' % e)
        return 2
    except F.ForbiddenError as e:
        say('forbidden: NOT CHECKED: %s' % e)
        return 2
    for line in out.lines:
        say(line)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
