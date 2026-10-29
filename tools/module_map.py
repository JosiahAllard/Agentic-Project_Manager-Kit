"""module_map.py: the module boundary check (docs/module-map.md).

  module_map.py [-C DIR] --check                  every crossing allowed or under its ratchet's ceiling
  module_map.py [-C DIR] --baseline               print the ratchets and ceilings the current tree needs
  module_map.py [-C DIR] --page [--write]         print (or write) the architecture page from the map
  module_map.py [-C DIR] --scope [--base REV]     the changed modules and the modules they widen to
  module_map.py [-C DIR] --scope --record        ... and keep it as a scope record (a gate step)
  module_map.py [-C DIR] --isolate [--module M] [--plan] [--no-rerun]
                                                  run each module's test in a sparse clone of HEAD
  ... --map FILE [--ceilings FILE]                use these files instead of .kit.json "modules"
  --check --base REV                              hold the ceilings to REV's (default: the main branch)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.modulemap.check, prints and
exits. --check reads the map .kit.json "modules".map names and refuses unless "owner".paths locks
it; --map is a dry run that skips that rule. --baseline writes nothing; --scope writes only its
scope record, with --record (docs/module-map.md "Scope records and the weekly sum"); --page --write
writes only the page .kit.json "modules".page names. --isolate (docs/module-map.md "Isolation
runs") runs the tests the map names, so it needs the owner-locked map; --map goes with it only
under --plan, which prints each module's tree and command and runs nothing. It books the machine
lock for the daily_full kind (inside daily_full.py's run it nests).
Exit codes: 0 OK (a baseline, a page, a scope and an isolation plan always), 1 a refused crossing,
a ratchet over its ceiling, a raised ceiling, a stale page or a module that did not pass its
isolation run, 2 USAGE, refused config, an unusable map or ceilings file, a base git cannot read,
a refused page write, git could not list the tree, or the machine lock refused the isolation run.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.config import ConfigError, load as load_config          # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.modulemap import MapError                               # noqa: E402
from kitlib.modulemap import check as C                             # noqa: E402
from kitlib.modulemap import isolate as I                           # noqa: E402
from kitlib.modulemap.load import load_map                          # noqa: E402


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
    mode.add_argument('--page', action='store_true', help='print the architecture page the map renders')
    mode.add_argument('--scope', action='store_true',
                      help='print the changed modules and the modules they widen to')
    mode.add_argument('--isolate', action='store_true',
                      help="run each module's test in a sparse clone of HEAD holding only what "
                           'its map entry reaches')
    p.add_argument('--write', action='store_true',
                   help='--page: write it to .kit.json "modules".page instead of printing it')
    p.add_argument('--map', default=None, help='the map file (default: .kit.json "modules".map)')
    p.add_argument('--ceilings', default=None, help='the ceilings file (default: .kit.json "modules".ceilings)')
    p.add_argument('--base', default=None,
                   help='--check: the rev whose ceilings bound these; --scope: the rev the change '
                        'is measured from (default: the main branch)')
    p.add_argument('--record', action='store_true',
                   help='--scope: keep the report as a scope record for the weekly sum (a gate '
                        'step passes it)')
    p.add_argument('--module', default=None, help='--isolate: only this module')
    p.add_argument('--plan', action='store_true',
                   help='--isolate: print each tree and command, book nothing and run nothing')
    p.add_argument('--no-rerun', action='store_true',
                   help='--isolate: do not run a failing module again in a full clone')
    return p


def main(argv=None):
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.write and not a.page:
        parser.error('--write goes with --page')
    if a.base and not (a.check or a.scope):
        parser.error('--base goes with --check or --scope')
    if a.record and not a.scope:
        parser.error('--record goes with --scope')
    if (a.module or a.plan or a.no_rerun) and not a.isolate:
        parser.error('--module, --plan and --no-rerun go with --isolate')
    if a.isolate and (a.ceilings or (a.map and not a.plan)):
        parser.error('--isolate runs the owner-locked map: --map goes with it only under --plan, '
                     'and --ceilings not at all')
    mode = next(m for m in ('check', 'baseline', 'page', 'scope', 'isolate') if getattr(a, m))
    try:
        top, why = gitrepo.toplevel(os.path.abspath(a.root))
        if not top:
            raise MapError(why)
        cfg = load_config(top)
        if a.isolate:
            map_path, _, notes = C.map_paths(top, cfg, os.path.abspath(a.map) if a.map else None,
                                             require_owner=True)
            for line in notes:
                say('module_map: %s' % line)
            return I.run_tool(top, cfg, load_map(map_path), say, plan_only=a.plan,
                              only=a.module, rerun=not a.no_rerun)
        out = C.run(top, cfg, mode,
                    map_path=os.path.abspath(a.map) if a.map else None,
                    ceilings_path=os.path.abspath(a.ceilings) if a.ceilings else None,
                    base_rev=a.base, write=a.write, record=a.record)
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
