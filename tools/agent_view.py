"""agent_view.py: everything one role is given and held to, on one host (docs/content.md "Agent view").

  agent_view.py <role> [--host NAME] [-C REPO] [--brief]

Prints, in the order the agent receives them, the host's rendered role file, the repository's
instructions file and what it imports, where the user's own instructions file is, the kit header,
what the start hook adds, the skills and the repo profile; then the enforced side: model, mode,
writes, capabilities as the host's tools, backlog rights, the .kit.json switches and the guard
rules for its mode. --host: default PM_KIT_HOST, else the hosts.json default. -C: the repository
checkout (default: here). --brief: paths only, no file text, no start checks run.

A thin entry point (ARCHITECTURE.md, Layers): it picks the host's adapter, calls kitlib.agentview,
prints and exits. Exit codes: 0 shown; 2 usage, an unknown role or host, or content/ that will not
load.
"""
import argparse
import importlib
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


def host_start(adapter_name):
    """The adapter's start.text(event, kit, payload, environ) when it has one (what its start hook
    adds to the context), else None: kitlib.agentview then runs the kit's own start checks."""
    try:
        module = importlib.import_module(adapter_name + '.start')
    except ImportError:
        return None
    return getattr(module, 'text', None)


def main(argv=None, out=None, environ=None):
    out = sys.stdout if out is None else out
    environ = os.environ if environ is None else environ
    a = build_parser().parse_args(argv)
    try:
        host = registry.host(registry.choose(a.host, environ))
        module = _host.adapter_module(host.name, environ).ADAPTER.content()
    except (registry.RegistryError, ImportError) as e:
        out.write('agent_view: %s\n' % e)
        return USAGE
    values = module.tokens() if hasattr(module, 'tokens') else None
    try:
        sections = agentview.view(KIT, os.path.abspath(a.repo), a.role, host, module.renderer(), values=values,
                                  tools=getattr(module, 'TOOLS', None), host_start=host_start(host.adapter),
                                  environ=environ, brief=a.brief)
    except agentview.UnknownRole as e:
        out.write('agent_view: %s\n' % e)
        return USAGE
    except (ContentError, OSError) as e:
        out.write('agent_view: %s\n' % e)
        return USAGE
    out.write('Role %s on host %s, in %s\n' % (a.role, host.name, os.path.abspath(a.repo)))
    for s in sections:
        out.write('\n== %s ==\n' % s.title)
        for line in s.lines:
            out.write(line.rstrip('\n') + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
