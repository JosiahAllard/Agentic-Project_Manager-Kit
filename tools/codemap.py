"""codemap.py: where code lives (kitlib.docs.codemap; docs/doc-tools.md "codemap").

  codemap.py [-C DIR] [--dir DIR | --find NAME | --file PATH | --symbol NAME [PATH]]

With no option, one row per source folder (files, lines, largest files). --dir lists a folder's
files with their types, --find the types (substring) and members (exact) named NAME, --file one
file's types and members with line numbers, --symbol one Python function or class (`Class.method`
too) as numbered source (not found or ambiguous: exit 1; PATH narrows it). Tracked and untracked-not-ignored files count.
Exit 0, or 2 on a usage error or when git cannot list the files.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.docs import codemap                                  # noqa: E402
from kitlib.git import repo                                       # noqa: E402


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def main(argv=None):
    ap = Parser(prog='codemap.py', description='Show where code lives.')
    ap.add_argument('-C', dest='root', default='.', help='the checkout to map (default: .)')
    ap.add_argument('--dir', help='list this folder\'s files and their types')
    ap.add_argument('--find', help='find types (substring) and members (exact) by name')
    ap.add_argument('--file', help='list one file\'s types and members')
    ap.add_argument('--symbol', help='print one Python function or class (Class.method too) with line numbers')
    ap.add_argument('path', nargs='?', help='with --symbol: a file or folder to look in')
    a = ap.parse_args(argv)
    if a.path and not a.symbol:
        ap.error('a PATH needs --symbol')
    root, why = repo.toplevel(a.root)
    if root is None:
        print('codemap: refused: -C %s' % why)
        return 2
    return codemap.run(root, lambda line: print(line, flush=True), dir=a.dir, find=a.find, file=a.file,
                       symbol=a.symbol, path=a.path)


if __name__ == '__main__':
    sys.exit(main())
