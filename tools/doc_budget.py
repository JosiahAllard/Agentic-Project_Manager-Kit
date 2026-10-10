"""doc_budget.py: the doc budget check (kitlib.docs.budget; docs/doc-tools.md "doc_budget").

  doc_budget.py [-C DIR] [--base REF] [--head REF] [--worktree] [--report]

Measures every tracked .md against its cap in .kit.json "doc_budget" (at HEAD, or with --worktree
the working tree), retired paths and stale budget keys; owner-only paths and the doc index are
commit.py's, kitlib.guard.ownercommits' and doc_index.py's. Last line `DOC BUDGET OK (...)`, exit 0; `DOC BUDGET FAILED: ...`, exit 1; a usage, config or
git error exits 2. Other tools run it as `[sys.executable, tools/doc_budget.py, -C, <repo>]` and
judge it with kitlib.verdict.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.docs import budget                                   # noqa: E402
from kitlib.git import repo                                       # noqa: E402


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def main(argv=None):
    ap = Parser(prog='doc_budget.py', description='Check doc sizes, retired paths and stale budget keys.')
    ap.add_argument('-C', dest='dir', default='.', help='the checkout to check (default: .)')
    ap.add_argument('--base', default=None, help='the branch it lands on (default: docs/gate.md "Default base")')
    ap.add_argument('--head', default='HEAD', help='the commit to measure (default: HEAD)')
    ap.add_argument('--worktree', action='store_true', help='measure the working tree, uncommitted files included')
    ap.add_argument('--report', action='store_true', help='also list every .md with its size and cap')
    a = ap.parse_args(argv)
    root, why = repo.toplevel(a.dir)
    if root is None:
        print('doc_budget: refused: -C %s' % why)
        return 2
    return budget.run(root, lambda line: print(line, flush=True), base=a.base, head=a.head,
                      worktree=a.worktree, report=a.report)


if __name__ == '__main__':
    sys.exit(main())
