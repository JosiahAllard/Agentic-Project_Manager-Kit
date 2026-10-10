"""worktree_paths.py: which path a write from a worktree may go to (docs/worktree-paths.md).

  worktree_paths.py [-C WORKTREE] PATH...

Prints each PATH resolved against the work tree WORKTREE is in (default: here), or the refusal: the main checkout,
another worktree, a relative `..` climbing out, a path git cannot place, and every path when
WORKTREE is not a linked worktree root (kitlib.git.repo.check_write). Scratch scripts use
kitlib.git.checkout.read_text / write_text with the same rule.

A thin entry point (ARCHITECTURE.md, Layers). Exit codes: 0 every path allowed, 2 a path refused
or a usage error.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.git import repo                                         # noqa: E402


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def main(argv):
    p = Parser(prog='worktree_paths.py', description='Resolve paths a worktree may write.')
    p.add_argument('-C', dest='root', default='.', help='the linked worktree (default: here)')
    p.add_argument('paths', nargs='+', metavar='PATH')
    a = p.parse_args(argv)
    root, why = repo.toplevel(os.path.abspath(a.root))
    if root is None:
        print('REFUSED: %s. Nothing was written.' % why)
        return 2
    code = 0
    for path in a.paths:
        try:
            print(repo.check_write(path, root))
        except repo.WriteRefused as e:
            print('REFUSED: %s. Nothing was written.' % e)
            code = 2
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
