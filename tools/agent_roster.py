"""agent_roster.py: docs/ROSTER.md's rendered tables against content/roles and every host's tier map.

  agent_roster.py [--check]     say whether docs/ROSTER.md is what the render makes; exit 1 if not (the default)
  agent_roster.py --write       rewrite only docs/ROSTER.md's rendered tables
  -C ROOT                       the kit checkout (default: this script's)

The role files are the roster (docs/ROSTER.md): a role's tier and effort are set in its file, and
render_content.py renders ROSTER.md with every host's files. This tool checks or writes ROSTER.md
alone. A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.content, prints and
exits. Exit codes: 0 agree / written, 1 differs, 2 USAGE or a content file of the wrong shape.
"""
import argparse
import os
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

import render_content                                               # noqa: E402
from kitlib import content                                          # noqa: E402
from kitlib.host.content import ContentError                        # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='agent_roster.py', description="docs/ROSTER.md's tables against content/roles and host tier maps.")
    g = p.add_mutually_exclusive_group()
    g.add_argument('--check', action='store_true', help='the default')
    g.add_argument('--write', action='store_true')
    p.add_argument('-C', dest='root', default=KIT, help="the kit checkout (default: this script's)")
    return p


def main(argv=None, out=None):
    out = sys.stdout if out is None else out
    a = build_parser().parse_args(argv)
    root = os.path.abspath(a.root)
    try:
        tiers = render_content.tier_maps()
        if a.write:
            paths = content.write(root, [], tiers=tiers)        # no host renderer: ROSTER.md alone
            for p in paths:
                out.write('wrote %s\n' % p)
            out.write('%d file(s) changed\n' % len(paths))
            return 0
        changed, _ = content.diff(root, [], tiers=tiers)
    except (ContentError, OSError) as e:
        out.write('agent_roster: %s\n' % e)
        return USAGE
    for p in sorted(changed):
        out.write('differs: %s; run python tools/render_content.py --write\n' % p)
    return 1 if changed else 0


if __name__ == '__main__':
    sys.exit(main())
