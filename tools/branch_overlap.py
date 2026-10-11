"""branch_overlap.py: which unmerged branches change files in a set (docs/branch-overlap.md).

  branch_overlap.py [-C DIR] (--files PATH ... | --files-from FILE | --branch BRANCH) [--base REV]

Read-only: it never fetches, writes or moves a ref. A thin entry point (ARCHITECTURE.md, Layers):
it parses, calls kitlib.git.overlap.look, prints and exits. Exit codes: 0 no overlap; 1 overlap;
2 usage, or git gave no answer (CANNOT ANSWER); 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.git import overlap                                      # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='branch_overlap.py', allow_abbrev=False,
               description='List the files each unmerged branch changed that are in a set.')
    p.add_argument('-C', dest='root', default='.', help='any work tree of the repository (default: here)')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--files', nargs='+', metavar='PATH',
                   help='repo-relative paths; a trailing /** is a prefix')
    g.add_argument('--files-from', metavar='FILE',
                   help='a file of paths, one per line (# comments and blank lines skipped)')
    g.add_argument('--branch', metavar='BRANCH',
                   help='the set is the files BRANCH changed since its merge base with the base')
    p.add_argument('--base', metavar='REV',
                   help='default: refs/remotes/<remote>/<main branch> from .kit.json when it '
                        'resolves, else <main branch> (docs/gate.md "Default base")')
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    top, fail, why = overlap.work_tree(a.root)
    if fail == 'usage':
        p.error(why)
    if fail:
        print('CANNOT ANSWER: %s' % why, flush=True)
        return overlap.CANNOT
    files = a.files
    if a.files_from is not None:
        try:
            with open(a.files_from, encoding='utf-8-sig') as fh:
                text = fh.read()
        except OSError as exc:
            p.error('--files-from: %s' % exc)
        try:
            files = overlap.parse_list(text)
        except overlap.Usage as exc:
            p.error('--files-from: %s' % exc)
    try:
        outcome = overlap.look(top, files=files, branch=a.branch, base=a.base)
    except overlap.Usage as exc:
        p.error(str(exc))
    except KeyboardInterrupt:
        return 130
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
