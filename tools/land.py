"""land.py: land a branch, or a batch, on the main branch: the one landing tool (docs/gate.md
"Landing").

  land.py BRANCH [BRANCH ...] --expect-main SHA --expect-tip SHA [--expect-tip SHA ...]
          [-C DIR] [--lane L] [--summary TEXT] [--full] [--fresh] [--wait SECONDS]
          [--log-dir D] [--keep-branch]
      from the main checkout: take a turn in the repository's landing queue and the landing lock,
      merge each branch with --no-ff (one --expect-tip per branch, in the same order), gate the
      merged tree, push, verify with ls-remote, release; undo on a red gate or a refused push.
      Landed: each branch's remote branch, worktree and local branch go, never forced (what main
      does not contain, or work, or a holder, keeps it); --keep-branch keeps all three. --fresh
      runs every step of the merged gate (no gate reuse, docs/gate.md "Gate reuse")
  land.py --queue [-C DIR]
      read-only: the landing lock's holder and the landing queue, in turn order
  land.py --sweep-landed [--apply] [-C DIR]
      the same cleanup for every branch main already contains (docs/gate.md "Branch cleanup");
      a dry run names what would go, --apply cleans up

--wait: seconds to wait for the turn and the lock (default "landing".wait_s of .kit.json; 0: BUSY
at once). Every landing writes a result record per branch (gate.py wait-result --kind land).

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.gate.land, prints and exits.
Exit codes (kitlib.gate): 0 OK, 1 FAILED, 2 USAGE, 3 STUCK, 4 REFUSED, 5 BUSY; a machine lock
refusal passes its own code through (6, 8, 10, 11, 12); 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import gate as G                                        # noqa: E402
from kitlib.gate import cleanup                                    # noqa: E402
from kitlib.gate import land as landmod                            # noqa: E402
from kitlib.gate import runner                                     # noqa: E402
from kitlib.lock.store import LockError, Store                     # noqa: E402


def say(line):
    print(line, flush=True)


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2: a usage error is never worded as a
    refusal."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(G.USAGE)


def parser():
    p = Parser(prog='land.py', description='Land a branch, or a batch, from the main checkout.')
    p.add_argument('branches', nargs='*', metavar='BRANCH')
    p.add_argument('--expect-main', metavar='SHA')
    p.add_argument('--expect-tip', action='append', default=[], metavar='SHA',
                   help='the tip of each BRANCH, in the same order')
    p.add_argument('-C', dest='root', default='.', help='the main checkout (default: here)')
    p.add_argument('--lane', default='full')
    p.add_argument('--summary', help='the merge title after "Merge <branch>: "')
    p.add_argument('--full', action='store_true', help='gate every non-manual step')
    p.add_argument('--fresh', action='store_true',
                   help='run every merged-gate step: reuse none from the branch gate')
    p.add_argument('--wait', type=int, default=None, metavar='SECONDS',
                   help='wait this long for the turn and the lock (default "landing".wait_s)')
    p.add_argument('--log-dir', help='where the merged gate\'s logs go')
    p.add_argument('--queue', action='store_true', help='print the landing lock and queue; land nothing')
    p.add_argument('--keep-branch', action='store_true',
                   help='after landing, keep the remote branch, the worktree and the local branch')
    p.add_argument('--sweep-landed', action='store_true',
                   help='clean up every branch main already contains (a dry run without --apply)')
    p.add_argument('--apply', action='store_true', help='--sweep-landed: clean up, not a dry run')
    return p


def emit(outcome):
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


def main(argv):
    p = parser()
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    if a.apply and not a.sweep_landed:
        p.error('--apply goes with --sweep-landed')
    if a.sweep_landed:
        if (a.branches or a.expect_main or a.expect_tip or a.queue or a.keep_branch or a.full
                or a.fresh or a.summary or a.wait is not None or a.log_dir or a.lane != 'full'):
            p.error('--sweep-landed takes only --apply and -C')
        try:
            return emit(cleanup.sweep(root, apply=a.apply))
        except KeyboardInterrupt:
            print('Ctrl+C: the sweep stopped; run it again to finish')
            return 130
    if a.queue:
        if a.branches or a.expect_main or a.expect_tip:
            p.error('--queue takes only -C')
        return emit(landmod.queue(root, Store(say=say)))
    if not a.branches:
        p.error('name the branch to land (or several: a batch)')
    if not a.expect_main or not a.expect_tip:
        p.error('--expect-main and one --expect-tip per branch are required')
    if a.wait is not None and a.wait < 0:
        p.error('--wait is 0 or more seconds')
    branch = a.branches[0] if len(a.branches) == 1 else a.branches
    tip = a.expect_tip[0] if len(a.expect_tip) == 1 else a.expect_tip
    try:
        out = landmod.land(root, branch, a.expect_main, tip, lane=a.lane, summary=a.summary,
                           full=a.full, wait_s=a.wait, say=say,
                           book_kw={'store': Store(say=say)}, log_dir=a.log_dir,
                           keep_branch=a.keep_branch, fresh=a.fresh)
    except SystemExit as e:     # the repo's code calling exit is a FAILED landing, never exit 0
        out = G.Outcome(G.FAILED, ['NOT LANDED: something the landing ran called exit '
                                   '(SystemExit, code %r) instead of returning' % (e.code,)])
    except LockError as e:
        return emit(runner.lock_refusal(e))
    except KeyboardInterrupt:
        print('Ctrl+C: the landing stopped')
        return 130
    return emit(out)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
