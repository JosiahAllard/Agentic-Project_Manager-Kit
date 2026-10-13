"""agent_roster.py: docs/ROSTER.md against content/roles and each host's tier -> model map.

  agent_roster.py [--check]     list every mismatch; exit 1 if any (the default)
  agent_roster.py --write       rewrite only the differing `tier:` / `effort:` lines of role files,
                                then render every host's files from content/ (render_content.py)
  -C ROOT                       the kit checkout (default: this script's)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.roster and kitlib.content,
prints and exits. Exit codes: 0 agree / written, 1 mismatch, 2 USAGE or an unreadable roster.
"""
import argparse
import os
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

import render_content                                               # noqa: E402
from kitlib import content, roster                                  # noqa: E402
from kitlib.host.content import ContentError                        # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='agent_roster.py', description='docs/ROSTER.md against content/roles and host tier maps.')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--check', action='store_true', help='the default')
    g.add_argument('--write', action='store_true')
    p.add_argument('-C', dest='root', default=KIT, help="the kit checkout (default: this script's)")
    return p


def main(argv=None, out=None):
    out = sys.stdout if out is None else out
    a = build_parser().parse_args(argv)
    root = os.path.abspath(a.root)
    rs = render_content.renderers()
    try:
        if a.write:
            names = roster.write(root)
            for n in names:
                out.write('wrote content/roles/%s.md\n' % n)
            rendered = content.write(root, rs)
            for p in rendered:
                out.write('rendered %s\n' % p)
            out.write('%d role file(s) changed, %d host file(s) rendered\n' % (len(names), len(rendered)))
            return 0
        lines = roster.check(root, {r.host: r.tier_models() for r in rs})
    except (roster.RosterError, ContentError, OSError) as e:
        out.write('agent_roster: %s\n' % e)
        return USAGE
    for line in lines:
        out.write(line + '\n')
    return 1 if lines else 0


if __name__ == '__main__':
    sys.exit(main())
