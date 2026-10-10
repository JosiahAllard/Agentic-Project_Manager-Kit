"""doc_index.py: find docs through the doc index (kitlib.docs.index; docs/doc-tools.md "doc_index").

  doc_index.py [-C DIR] [--find WORD... | --section HEADING [--doc PATH] | --tools | --stale | --role ROLE]
               [--head REF] [--days N] [--config-ref REF]

With no mode it checks the index (.kit.json "doc_index".index, default docs/INDEX.md) against the
tracked docs. --find prints the index entries for a topic, --section one doc section by heading,
--tools the index's Tools section, --stale the docs not touched for --days, --role every section a
role reads. Exit 0 pass, 1 a miss, 2 a usage, config or read error.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.docs import index                                    # noqa: E402
from kitlib.git import repo                                       # noqa: E402


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def main(argv=None):
    ap = Parser(prog='doc_index.py', description='Find docs through the doc index, or check it.')
    ap.add_argument('-C', dest='dir', default='.', help='the checkout to read (default: .)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--section', help='print one section, found by heading text, from any tracked .md')
    mode.add_argument('--find', nargs='+', metavar='WORD', help='print the index entries matching a topic')
    mode.add_argument('--tools', action='store_true', help='print the Tools section of the index')
    mode.add_argument('--stale', action='store_true', help='list docs not touched for --days')
    mode.add_argument('--role', help='print every doc section this agent role reads')
    ap.add_argument('--doc', help='with --section: restrict to this file')
    ap.add_argument('--head', default='HEAD', help='the commit whose tracked docs count (default: HEAD)')
    ap.add_argument('--days', type=int, help='with --stale: the age in days')
    ap.add_argument('--config-ref', metavar='REF',
                    help='read .kit.json as this commit holds it, not the working tree '
                         '(review_pack passes the base, so a branch cannot switch its own check off)')
    a = ap.parse_args(argv)
    root, why = repo.toplevel(a.dir)
    if root is None:
        print('doc_index: refused: -C %s' % why)
        return 2
    return index.run(root, lambda line: print(line, flush=True), section=a.section,
                     find=' '.join(a.find) if a.find else None, tools=a.tools, stale=a.stale,
                     role=a.role, doc=a.doc, head=a.head, days=a.days, config_ref=a.config_ref)


if __name__ == '__main__':
    sys.exit(main())
