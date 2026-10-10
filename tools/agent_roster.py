"""agent_roster.py: docs/ROSTER.md against the agent files' model and effort frontmatter.

  agent_roster.py [--check]     list every mismatch; exit 1 if any (the default)
  agent_roster.py --write       rewrite only the differing `model:` / `effort:` lines of agent files

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.roster, prints and exits.
Exit codes: 0 agree / written, 1 mismatch, 2 USAGE or an unreadable roster.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import roster                                           # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='agent_roster.py', description='docs/ROSTER.md against agent frontmatter.')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--check', action='store_true', help='the default')
    g.add_argument('--write', action='store_true')
    p.add_argument('--dir', default=None, help='the agents folder (default: this kit\'s claude/agents/)')
    return p


def main(argv=None, out=None):
    out = sys.stdout if out is None else out
    a = build_parser().parse_args(argv)
    agents = a.dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'claude', 'agents')
    try:
        if a.write:
            names = roster.write(agents)
            for n in names:
                out.write('wrote %s\n' % n)
            out.write('%d agent file(s) changed\n' % len(names))
            return 0
        lines = roster.check(agents)
    except roster.RosterError as e:
        out.write('agent_roster: %s\n' % e)
        return USAGE
    for line in lines:
        out.write(line + '\n')
    return 1 if lines else 0


if __name__ == '__main__':
    sys.exit(main())
