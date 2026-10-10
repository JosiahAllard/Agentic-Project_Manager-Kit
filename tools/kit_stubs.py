"""kit_stubs.py: write or check a repository's stubs for the kit's tools (docs/install.md).

  kit_stubs.py write [-C DIR] [--tools a,b,...] [--replace] [--dry-run]
  kit_stubs.py check [-C DIR] [--tools a,b,...]

-C names the checkout (default: here). A thin entry point (ARCHITECTURE.md, Layers): it parses,
calls kitlib.stubs.run, prints and exits. Exit codes: 0 done or all current; 1 a problem named;
2 usage, or the kit configuration was refused; 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import stubs                                            # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (a usage error is never worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='kit_stubs.py', description="Write or check a repository's kit tool stubs.")
    p.add_argument('verb', choices=('write', 'check'))
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    p.add_argument('--tools', help='comma list; default .kit.json "stubs".tools, else the shared tools')
    p.add_argument('--replace', action='store_true',
                   help='write: replace files that are not stubs (a repository\'s own copies)')
    p.add_argument('--dry-run', action='store_true', help='write: say what would be written, write nothing')
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if a.verb == 'check' and (a.replace or a.dry_run):
        p.error('--replace and --dry-run are write options')
    tools = None
    if a.tools is not None:
        tools = [t.strip() for t in a.tools.split(',')]
        if not all(tools):
            p.error('--tools needs comma-separated tool names')
    top, why = gitrepo.toplevel(a.root)
    if top is None:
        p.error('-C: %s' % why)
    try:
        outcome = stubs.run(top, a.verb, tools=tools, replace=a.replace, dry_run=a.dry_run)
    except KeyboardInterrupt:
        return 130
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
