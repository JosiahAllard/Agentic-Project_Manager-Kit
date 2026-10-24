"""machine_paths.py: refuse a hard-coded machine path anywhere in the tree (docs/commit.md).

  machine_paths.py [-C DIR]
      search every file git tracks (never an untracked one) in the work tree DIR is in
      (default: here) for a drive-rooted machine folder (the machine config's
      machine_path_folders; unset: none, and a NOTE says so) or user profile, against the
      .kit.json "machine_paths" allow-list

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.machinepaths, prints and
exits. Exit codes: 0 OK (WARNING lines included), 1 FAILED (a hit off the allow-list),
2 NOT CHECKED (git could not list the tree, nothing was read, or the config was refused) or USAGE.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import machinepaths as M                                # noqa: E402
from kitlib.git import repo                                         # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='machine_paths.py', allow_abbrev=False,
               description='Refuse a hard-coded machine path anywhere in the tree.')
    p.add_argument('-C', '--root', dest='root', default='.',
                   help='a path in the work tree to search (default: here)')
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    root, why = repo.toplevel(os.path.abspath(a.root))
    if root is None:
        print('MACHINE PATHS NOT CHECKED: %s' % why, flush=True)
        return M.NOT_CHECKED
    code, lines = M.check(root)
    for line in lines:
        print(line, flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
