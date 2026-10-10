"""pieces.py: answer from a repository's path-to-piece table (docs/repo-checks.md).

  pieces.py [-C DIR] --list                 id, group, kind, dir: one line per piece
  pieces.py [-C DIR] --dir ID               that piece's folder (or file), repo-relative
  pieces.py [-C DIR] --path ID REL          a path inside it
  pieces.py [-C DIR] --piece-of PATH        the piece holding a repo-relative path
  pieces.py [-C DIR] --gradle ID            a Gradle piece's project path (`:worker`)
  pieces.py [-C DIR] --output ID KEY        a build product's path (`outputs`), repo-relative
  pieces.py [-C DIR] --was ID               the folder before the move (empty when none)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.pieces, prints and exits.
The table is `.kit.json` "pieces" of the checkout -C names. Exit codes: 0 answered, 2 USAGE, no
or a malformed table, refused configuration, an unknown id or path.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import pieces as P                                      # noqa: E402
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
    p = Parser(prog='pieces.py', description='Answer from the pieces table (docs/repo-checks.md).')
    p.add_argument('-C', dest='root', default='.', help='the checkout whose table to read (default .)')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true', help='every piece')
    mode.add_argument('--dir', metavar='ID')
    mode.add_argument('--path', nargs=2, metavar=('ID', 'REL'))
    mode.add_argument('--piece-of', dest='piece_of', metavar='PATH')
    mode.add_argument('--gradle', metavar='ID')
    mode.add_argument('--output', nargs=2, metavar=('ID', 'KEY'))
    mode.add_argument('--was', metavar='ID')
    return p


def answer(t, a):
    """`(code, lines)` for the one question `a` asks of table `t`; KeyError for an unknown id."""
    if a.list:
        return 0, ['%-22s %-9s %-8s %s' % (p['id'], p['group'], p['kind'], p['dir']) for p in t.pieces]
    if a.dir is not None:
        return 0, [t.dir(a.dir)]
    if a.path is not None:
        return 0, [t.path(*a.path)]
    if a.gradle is not None:
        g = t.get(a.gradle).get('gradle')
        return (0, [g]) if g else (2, ['piece %r is not a Gradle module' % a.gradle])
    if a.output is not None:
        pid, key = a.output
        if key not in t.get(pid).get('outputs', {}):
            return 2, ['piece %r has no output %r' % (pid, key)]
        return 0, [t.output(pid, key)]
    if a.was is not None:
        return 0, [t.get(a.was).get('was', '')]
    pid = t.piece_of(a.piece_of)
    return (0, [pid]) if pid else (2, ['no piece holds %s' % a.piece_of])


def main(argv=None):
    a = build_parser().parse_args(argv)
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('pieces: refused: %s' % why)
        return 2
    try:
        t = P.load_repo(top, load_config(top))
        code, lines = answer(t, a)
    except (P.TableError, ConfigError) as e:
        say('pieces: refused: %s' % e)
        return 2
    except KeyError as e:
        say(e.args[0])
        return 2
    for line in lines:
        say(line)
    return code


if __name__ == '__main__':
    sys.exit(main())
