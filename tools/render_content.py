"""render_content.py: write each host's agents, skills and plugin manifests from content/ and kit.json,
and docs/ROSTER.md's tables from content/roles and every host's tier map (docs/content.md).

  render_content.py [--check]   list every rendered file that differs, is missing or is stale; exit 1 if any
  render_content.py --write     write the differing and missing files, remove the stale ones
  --host NAME                   only that host's files (repeatable; default: every host in hosts.json);
                                docs/ROSTER.md is always checked or written, with every host's tiers

A thin entry point (ARCHITECTURE.md, Layers): it picks the host renderers, calls kitlib.content,
prints and exits. Exit codes: 0 agree / written, 1 drift, 2 USAGE, a content file of the wrong
shape, or --write into an installed plugin export (its .pm-kit-export marker; name -C <checkout>).
"""
import argparse
import os
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

from kitlib import content                                          # noqa: E402
from kitlib.host.content import ContentError                        # noqa: E402

import _host                                                        # noqa: E402
from kitlib.host import registry                                    # noqa: E402

USAGE = 2
# Every host hosts.json registers; its adapter module's ADAPTER.content().renderer() is its
# ContentRenderer (which also writes its plugin manifests from kit.json).
RENDERERS = registry.names()


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='render_content.py', description="Render content/ into each host's files.")
    g = p.add_mutually_exclusive_group()
    g.add_argument('--check', action='store_true', help='the default')
    g.add_argument('--write', action='store_true')
    p.add_argument('--host', action='append', choices=sorted(RENDERERS), help='default: every host')
    p.add_argument('-C', dest='root', default=KIT, help="the kit checkout (default: this script's)")
    return p


def renderers(hosts=None):
    """The ContentRenderer of each named host (default: all of RENDERERS), in name order."""
    return [_host.adapter_module(h).ADAPTER.content().renderer() for h in sorted(hosts or RENDERERS)]


def tier_maps():
    """{host: {tier: model}} of every host in RENDERERS: docs/ROSTER.md's tier table."""
    return {r.host: r.tier_models() for r in renderers()}


def main(argv=None, out=None):
    out = sys.stdout if out is None else out
    a = build_parser().parse_args(argv)
    root = os.path.abspath(a.root)
    try:
        rs = renderers(a.host)
        tiers = tier_maps()
        if a.write:
            paths = content.write(root, rs, tiers=tiers)
            for p in paths:
                out.write('wrote %s\n' % p)
            out.write('%d file(s) changed\n' % len(paths))
            return 0
        lines = content.drift(root, rs, tiers=tiers)
    except (ContentError, OSError) as e:
        out.write('render_content: %s\n' % e)
        return USAGE
    for line in lines:
        out.write(line + '\n')
    if lines:
        out.write('render_content: %d file(s) differ; run python tools/render_content.py --write\n' % len(lines))
    return 1 if lines else 0


if __name__ == '__main__':
    sys.exit(main())
