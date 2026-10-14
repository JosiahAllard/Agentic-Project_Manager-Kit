"""testlist.py: the test-name list of a tree - print it, save it, diff it - so a move or rename
that loses a test shows (docs/test-tools.md).

  testlist.py [-C DIR] --names [--status] [--dir D]
  testlist.py [-C DIR] --save FILE [--status] [--dir D]
  testlist.py [-C DIR] --diff FILE [--status] [--dir D]
  testlist.py --diff OLD --against NEW

Every test name in the result files under D (default: the checkout at DIR, default here), sorted;
the result globs are the checkout's `.kit.json` "testlist". --save writes them before the change;
--diff after it lists the names removed (`  - `) and added (`  + `). --against diffs two saved
lists, with no freshness check. --status makes each line `<name><TAB>passed|failed|skipped`.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.testlist, prints and exits.
Exit codes: 0 no difference / listed / saved, 1 names differ, 2 USAGE, nothing found, no result
newer than the saved list, or a refused configuration or checkout.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import kitroot                                         # noqa: E402
from kitlib.config import ConfigError, load as load_config          # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.testlist import results                                 # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='testlist.py', description='The test-name list of a tree (docs/test-tools.md).')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--names', action='store_true')
    g.add_argument('--save', metavar='FILE')
    g.add_argument('--diff', metavar='FILE')
    p.add_argument('--against', metavar='FILE', help='--diff: a second saved list, not the tree')
    p.add_argument('--status', action='store_true')
    p.add_argument('--dir', default=None, help='where the result files are (default: the checkout)')
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    return p


def main(argv=None, out=None):
    out = sys.stdout if out is None else out

    def say(line):
        out.write(line + '\n')
    a = build_parser().parse_args(argv)
    if a.against and not a.diff:
        say('USAGE: --against goes with --diff')
        return USAGE
    if a.against:
        lines, code = results.diff(results.read_list(a.diff), results.read_list(a.against))
        for line in lines:
            say(line)
        return code
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('testlist: refused: %s' % why)
        return USAGE
    try:
        cfg = load_config(top)
        globs = results.settings(cfg)
    except ConfigError as e:
        say('testlist: refused: kit configuration: %s' % e)
        return USAGE
    where = os.path.abspath(a.dir or top)
    statuses = {} if a.status else None
    tests, newest = results.load(where, statuses, globs)
    if a.status:
        tests = ['%s\t%s' % (n, statuses[n]) for n in tests]
    if not tests:
        say('no test results under %s (run the gate first: %s)'
            % (a.dir or where, kitroot.command('gate', top)))
        return USAGE
    if a.names:
        out.write('\n'.join(tests) + '\n')
        return 0
    if a.save:
        results.write_list(a.save, tests)
        say('saved %d test names to %s' % (len(tests), a.save))
        return 0
    if newest <= os.path.getmtime(a.diff):
        say('refused: no test result is newer than %s - no test has run since --save. '
            'Run the gate, then --diff.' % a.diff)
        return USAGE
    lines, code = results.diff(results.read_list(a.diff), tests)
    for line in lines:
        say(line)
    return code


if __name__ == '__main__':
    sys.exit(main())
