"""tool_deps.py: which tools test modules a change can break (docs/test-tools.md).

  tool_deps.py [-C DIR] PATH ...    the test modules the changed repo-relative paths select, or ALL
  tool_deps.py [-C DIR] --graph     every tools module and its direct edges
  tool_deps.py [-C DIR] --check     the audit of the "tool_tests" tables; exit 1 on a problem

DIR is the checkout (default here); its `.kit.json` "tool_tests" and "repo".tools_dir are read.
A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.tooldeps, prints and exits.
A {"piece": ...} or {"solution": true} pin needs a pieces table, which this entry point does not
pass: such a configuration is refused when the pin is read.
Exit codes: 0 listed / audit OK, 1 audit problems, 2 USAGE or a refused configuration or checkout.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.config import ConfigError                               # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.tooldeps import deps                                    # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='tool_deps.py',
               description='Which tools test modules a change can break (docs/test-tools.md).')
    p.add_argument('paths', nargs='*', metavar='PATH', help='changed repo-relative paths')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--graph', action='store_true', help='every module and its direct edges')
    g.add_argument('--check', action='store_true', help='audit the "tool_tests" tables')
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    return p


def main(argv=None, out=None):
    out = sys.stdout if out is None else out

    def say(line):
        out.write(line + '\n')
    parser = build_parser()
    a = parser.parse_args(argv)
    if (a.graph or a.check) and a.paths:
        parser.error('--graph and --check take no paths')
    if not (a.graph or a.check or a.paths):
        parser.error('give changed paths, --graph or --check')
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('tool_deps: refused: %s' % why)
        return USAGE
    try:
        cfg = deps.config(top)
        graph = deps.scan(None, cfg, top)
        if a.graph:
            for line in deps.graph_lines(graph):
                say(line)
            return 0
        if a.check:
            problems = deps.audit(top, graph, cfg)
            for p in problems:
                say(p)
            say('tool_deps: %s' % ('%d problem(s)' % len(problems) if problems else 'OK'))
            return 1 if problems else 0
        sel = deps.select(a.paths, graph, cfg=cfg)
    except ConfigError as e:
        say('tool_deps: refused: kit configuration: %s' % e)
        return USAGE
    say(sel if sel == deps.ALL else ' '.join(sel) or '(none)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
