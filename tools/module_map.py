"""module_map.py: the module boundary check (docs/module-map.md).

  module_map.py [-C DIR] --check                  every crossing allowed or under its ratchet's ceiling
  module_map.py [-C DIR] --baseline               print the ratchets and ceilings the current tree needs
  ... --map FILE [--ceilings FILE]                use these files instead of .kit.json "modules"

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.modulemap.check, prints and
exits. --check reads the map .kit.json "modules".map names and refuses unless "owner".paths locks
it; --map is a dry run that skips that rule. --baseline writes nothing.
Exit codes: 0 OK (a baseline always), 1 a refused crossing or a ratchet over its ceiling, 2 USAGE,
refused config, an unusable map or ceilings file, or git could not list the tree.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.config import ConfigError, load as load_config          # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.modulemap import MapError                               # noqa: E402
from kitlib.modulemap import check as C                             # noqa: E402


def say(line):
    print(line, flush=True)


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def build_parser():
    p = Parser(prog='module_map.py', description='The module boundary check (docs/module-map.md).')
    p.add_argument('-C', dest='root', default='.', help='the checkout to check (default .)')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='refuse a crossing the map does not allow')
    mode.add_argument('--baseline', action='store_true', help='print the ratchets the tree needs')
    p.add_argument('--map', default=None, help='the map file (default: .kit.json "modules".map)')
    p.add_argument('--ceilings', default=None, help='the ceilings file (default: .kit.json "modules".ceilings)')
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        top, why = gitrepo.toplevel(os.path.abspath(a.root))
        if not top:
            raise MapError(why)
        cfg = load_config(top)
        out = C.run(top, cfg, 'check' if a.check else 'baseline',
                    map_path=os.path.abspath(a.map) if a.map else None,
                    ceilings_path=os.path.abspath(a.ceilings) if a.ceilings else None)
    except ConfigError as e:
        say('module_map: refused: kit configuration: %s' % e)
        return 2
    except MapError as e:
        say('module_map: NOT CHECKED: %s' % e)
        return 2
    for line in out.lines:
        say(line)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
