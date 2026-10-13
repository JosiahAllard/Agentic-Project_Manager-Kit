"""render_content.py: write each host's agents and skills from content/ (docs/content.md).

  render_content.py [--check]   list every rendered file that differs, is missing or is stale; exit 1 if any
  render_content.py --write     write the differing and missing files, remove the stale ones
  --host NAME                   only that host (repeatable; default: every host in RENDERERS)

A thin entry point (ARCHITECTURE.md, Layers): it picks the host renderers, calls kitlib.content,
prints and exits. Exit codes: 0 agree / written, 1 drift, 2 USAGE or a content file of the wrong
shape.
"""
import argparse
import importlib
import os
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

from kitlib import content                                          # noqa: E402
from kitlib.host.content import ContentError                        # noqa: E402

USAGE = 2
# host -> the module whose renderer() is its ContentRenderer. Until hosts.json carries the
# adapter modules (docs/multi-host-plan.md step 2), the hosts with a renderer are listed here.
RENDERERS = {'claude': 'claude.adapter.content'}


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
    return [importlib.import_module(RENDERERS[h]).renderer() for h in sorted(hosts or RENDERERS)]


def main(argv=None, out=None):
    out = sys.stdout if out is None else out
    a = build_parser().parse_args(argv)
    root = os.path.abspath(a.root)
    try:
        rs = renderers(a.host)
        if a.write:
            paths = content.write(root, rs)
            for p in paths:
                out.write('wrote %s\n' % p)
            out.write('%d file(s) changed\n' % len(paths))
            return 0
        lines = content.drift(root, rs)
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
