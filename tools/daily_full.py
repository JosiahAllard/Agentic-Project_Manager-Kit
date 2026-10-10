"""daily_full.py: the opt-in daily full run (docs/daily-full.md).

  daily_full.py [-C DIR] [--out DIR] [--stamp S] [--force]
                                                  run every non-manual gate step on the ref,
                                                  unless it passed green at this commit already
  daily_full.py [-C DIR] --plan                    print what would run; run nothing
  daily_full.py [-C DIR] --summary [--out DIR]     print the status lines from the recorded runs

A thin entry point: it parses, calls kitlib.dailyfull, prints and exits. It refuses unless
`.kit.json` "daily_full".enabled is true. The kit never schedules it.
Exit: 0 green, or skipped by the machine lock (recorded); 1 a step red; 2 usage, or off;
4 the worktree or its gate could not be readied (recorded red); 5 another run holds the tree
(nothing touched or recorded); a lock usage code passes through.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import dailyfull as D                                   # noqa: E402
from kitlib import gate as G                                        # noqa: E402
from kitlib.config import load as load_config                             # noqa: E402
from kitlib.config.types import ConfigError                         # noqa: E402
from kitlib.gate import steps as S                                  # noqa: E402
from kitlib.worktree import remove                                  # noqa: E402


def say(line):
    print(line, flush=True)


def parse_args(argv):
    p = argparse.ArgumentParser(prog='daily_full.py', description=__doc__.split('\n\n')[0])
    p.add_argument('-C', dest='root', default='.', help='any checkout of the repository')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true', help='print the steps; run nothing')
    mode.add_argument('--summary', action='store_true', help='print the recorded runs\' status')
    p.add_argument('--out', help='the results folder (default <gate log dir>/daily)')
    p.add_argument('--force', action='store_true',
                   help='run even when the ref is the commit the last run passed green at')
    p.add_argument('--stamp', help=argparse.SUPPRESS)
    return p.parse_args(argv)


def load(root):
    """`(main, cfg)` of the repository holding `root`; ConfigError or GitReadError."""
    main = remove.main_checkout(os.path.abspath(root))
    return main, load_config(main)


def summary_lines(root=None):
    """The report-hook form (`.kit.json` "preflight".report_hooks: daily_full:summary_lines): the
    status lines of the repository at `root` (default: here)."""
    main, cfg = load(root or os.getcwd())
    s = D.settings(cfg, main)
    return D.summary_lines(s['out'], s['stale_hours'])


def main(argv=None, book_kw=None):
    try:
        a = parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:                       # a usage error, or --help: 0 would mean green
        return G.USAGE
    try:
        top, cfg = load(a.root)
    except (ConfigError, remove.GitReadError) as e:
        say('REFUSED: %s' % e)
        return G.USAGE
    s = D.settings(cfg, top)
    if a.summary:
        for line in D.summary_lines(a.out or s['out'], s['stale_hours']):
            say(line)
        return G.OK
    if not s['enabled']:
        say(D.OFF)
        return G.USAGE
    if a.plan:
        say('daily: %s in %s, kind %s, waits up to %d s for its slot' % (
            s['ref'], s['worktree'], s['kind'], s['max_wait']))
        try:
            gate_cfg = load_config(s['worktree']) if os.path.isdir(s['worktree']) else cfg
            for step in D.plan(gate_cfg, s['worktree'], 'HEAD'):
                say('RUN   %s' % step.name)
        except (ConfigError, S.GateConfigError) as e:
            say('REFUSED: %s' % e)
            return G.REFUSED
        return G.OK
    out = D.run(top, cfg, out=a.out, stamp=a.stamp, say=say, book_kw=book_kw,
                force=a.force)
    for line in out.lines:
        say(line)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
