"""pytests.py: run a checkout's tools test modules in parallel, one fresh process per unit, each in
its own job object (docs/test-tools.md).

  pytests.py [-C DIR] [--audit] [--jobs N] [--unit-timeout S] <test module> ...
  pytests.py [-C DIR] --all [--merged] [--audit] [--jobs N] [--unit-timeout S]
  pytests.py [-C DIR] --select [--base B] [--merged] [--changed PATH ...] [--plan] [--audit]
  add --pid-hold to any run: hold pids for it (opt-in, agent runs only; the gate refuses it)
  pytests.py --child <unit> -C ROOT [--shard I/N] [--path D] [--audit]      (internal)

A name is a test module of the checkout's tools folder, or `module.Class` / `module.Class.test`.
--select: the changed paths (`git diff --name-only --no-renames -z <merge-base B HEAD>` plus
untracked files, B = --base, default docs/gate.md "Default base": <remote>/<main branch> when it
resolves, else the main branch, a line saying when the local one differs; or the --changed
list) go through kitlib.tooldeps' selection (its `always_run` included); prints `pytests: <why>` and
`pytests: <names>`, then runs them (--plan: stops there). When git cannot say what changed it
REFUSES and no test runs. --merged (a landing's merged-tree gate, a step's merged_args) adds the
"tool_tests" whole_tree classes to a --select that a tools file changed; with --all it changes nothing. The log, units, kill and shield: kitlib/pytests/runner.py.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.pytests, prints and exits.
Exit codes: 0 every unit passed (or nothing selected, or --plan), 1 a unit failed or timed out,
2 USAGE, a refused configuration or checkout, or a change list git could not read. The last line
of a run is `OK` or `FAILED (units: ...)`: a gate step runs this with a `pass` pattern on it.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from kitlib.config import ConfigError, load as load_config          # noqa: E402
from kitlib.git import refs as gitrefs                              # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.pytests import child, runner                            # noqa: E402
from kitlib.tooldeps import deps as tool_deps                       # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='pytests.py', allow_abbrev=False,
               description='Run tools test modules in parallel (docs/test-tools.md).')
    p.add_argument('modules', nargs='*')
    p.add_argument('--all', action='store_true')
    p.add_argument('--select', action='store_true')
    p.add_argument('--base')
    p.add_argument('--merged', action='store_true')
    p.add_argument('--changed', nargs='*')
    p.add_argument('--plan', action='store_true')
    p.add_argument('--audit', action='store_true')
    p.add_argument('--jobs', type=int)
    p.add_argument('--unit-timeout', type=int)
    p.add_argument(runner.PID_HOLD_FLAG, dest='pid_hold', action='store_true',
                   help='hold pids for the run (opt-in; off by default; never in the gate)')
    p.add_argument('--child', help=argparse.SUPPRESS)
    p.add_argument('--shard', help=argparse.SUPPRESS)
    p.add_argument('--path', help=argparse.SUPPRESS)
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    return p


def run_child(a):
    """--child: the runner already resolved the checkout; `-C` is its root as given."""
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    root = os.path.abspath(a.root)
    tools = runner.tools_path(root)
    child.set_path(tools, a.path, drop=(HERE,))
    return child.run_child(a.child, a.shard, a.audit, root, tools, sys.stdout, a.path)


def main(argv=None, out=None):
    a = build_parser().parse_args(argv)
    if a.child:
        return run_child(a)
    out = sys.stdout if out is None else out

    def say(line):
        out.write(line + '\n')
    if a.select and (a.all or a.modules):
        say('USAGE: --select picks the modules itself; name none and no --all')
        return USAGE
    if not a.select and (a.plan or a.base or a.changed is not None):
        say('USAGE: --plan, --base and --changed go with --select')
        return USAGE
    # --merged only widens a selection; --all already runs every module, so the pair a landing's
    # merged-tree gate builds from a full_cmd and merged_args (kitlib.gate.steps.build) is accepted.
    if a.merged and not (a.select or a.all):
        say('USAGE: --merged goes with --select or --all')
        return USAGE
    if not a.select and a.all == bool(a.modules):
        say('USAGE: name test modules or --all, not both and not neither')
        return USAGE
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('pytests: refused: %s' % why)
        return USAGE
    try:
        kit = load_config(top)
        cfg = runner.settings(kit)
        tcfg = tool_deps.config(top)
        base, base_note = a.base, None
        if a.select and not base:
            base, base_note = gitrefs.default_base(top, kit.remote, kit.main_branch)
    except ConfigError as e:
        say('pytests: refused: kit configuration: %s' % e)
        return USAGE
    if not os.path.isdir(os.path.join(top, tcfg['tools_rel'])):
        say('pytests: refused: the checkout has no tools folder %s' % tcfg['tools_rel'])
        return USAGE
    graph_tests = None
    if a.select:
        if base_note:
            say('pytests: %s' % base_note)
        try:
            # Before the scan: an unreadable change list refuses before anything is selected.
            changed = set(a.changed) if a.changed is not None else runner.changed_paths(base, top)
        except runner.Refused as e:
            say(str(e))
            say('pytests FAILED | no test ran: git could not read the change list')
            return USAGE
        graph = tool_deps.scan(None, tcfg, top)
        graph_tests = sorted(graph.tests)
        try:
            names, why = tool_deps.selection(changed, graph, a.merged, cfg=tcfg)
        except ConfigError as e:
            say('pytests: refused: kit configuration: %s' % e)
            return USAGE
        say('pytests: %s' % why)
        say('pytests: %s' % ('ALL (%d test modules)' % len(graph_tests) if names == tool_deps.ALL
                             else ' '.join(names) or '(none)'))
        if a.plan:
            return 0
        if not names:
            say('pytests: no test module selected; nothing ran')
            return 0
        todo = graph_tests if names == tool_deps.ALL else names
    else:
        graph = tool_deps.scan(None, tcfg, top)
        graph_tests = sorted(graph.tests)
        unknown = [m for m in a.modules if m.split('.')[0] not in graph_tests]
        if unknown:
            say('USAGE: not a test module in %s: %s' % (graph.tools_rel, ', '.join(unknown)))
            return USAGE
        todo = graph_tests if a.all else sorted(set(a.modules))
    out.flush()
    return runner.run(todo, top, a.audit, a.jobs, a.unit_timeout, out=out, cfg=cfg,
                      tools_rel=tcfg['tools_rel'],
                      hold=runner.PidHoldRun() if a.pid_hold else None)


if __name__ == '__main__':
    sys.exit(main())
