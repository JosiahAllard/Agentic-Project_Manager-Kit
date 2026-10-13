"""agent_view.py: everything one role is given and held to, on one host (docs/content.md "Agent view").

  agent_view.py <role> [--host NAME] [-C REPO] [--brief]

Prints numbered sections: first what the agent is given, in the order it receives it (the host's
rendered role file, the repository's instructions file and what it imports, where the user's own
instructions file is, the kit header, what the start hook adds, the skills and the repo profile);
then what it is held to (model, tools and mode, the guard rules for its mode, the .kit.json
switches that turn them on, and its backlog rights). --host: default PM_KIT_HOST, else the
hosts.json default. -C: the repository checkout (default: here). --brief: paths only, no file
text, no start checks run.

A thin entry point (ARCHITECTURE.md, Layers): it picks the host's adapter, calls kitlib.agentview
with the adapter's ContentRenderer and start_text (the host contract), prints and exits. Exit
codes: 0 shown; 2 usage, an unknown role or host, or content/ that will not load.
"""
import argparse
import os
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

from kitlib import agentview                                        # noqa: E402
from kitlib.host import registry                                    # noqa: E402
from kitlib.host.content import ContentError                        # noqa: E402

import _host                                                        # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='agent_view.py', description='Everything one role is given and held to, on one host.')
    p.add_argument('role')
    p.add_argument('--host', help='default: PM_KIT_HOST, else the hosts.json default')
    p.add_argument('-C', dest='repo', default='.', help='the repository checkout (default: here)')
    p.add_argument('--brief', action='store_true', help='paths only: no file text, no start checks')
    return p


def main(argv=None, out=None, environ=None):
    out = sys.stdout if out is None else out
    environ = os.environ if environ is None else environ
    a = build_parser().parse_args(argv)
    try:
        host = registry.host(registry.choose(a.host, environ))
        adapter = _host.adapter_module(host.name, environ).ADAPTER
    except (registry.RegistryError, ImportError) as e:
        out.write('agent_view: %s\n' % e)
        return USAGE
    try:
        sections = agentview.view(KIT, os.path.abspath(a.repo), a.role, host, adapter.content().renderer(),
                                  start_text=getattr(adapter, 'start_text', None), environ=environ, brief=a.brief)
    except (agentview.UnknownRole, ContentError, OSError) as e:
        out.write('agent_view: %s\n' % e)
        return USAGE
    given = len(agentview.GIVEN)
    out.write('Role %s on host %s, in %s\n' % (a.role, host.name, os.path.abspath(a.repo)))
    out.write('Sections 1-%d: what it is given, in the order it gets it. %d-%d: what it is held to.%s\n'
              % (given, given + 1, len(sections), ' (--brief: paths only)' if a.brief else ''))
    for n, s in enumerate(sections, 1):
        if n == given + 1:
            out.write('\n%s\n' % ('-' * 72))
        out.write('\n== %d. %s ==\n' % (n, s.title))
        for line in s.lines:
            out.write(line.rstrip('\n') + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
