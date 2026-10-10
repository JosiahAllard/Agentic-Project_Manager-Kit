"""gate.py: the universal gate, a landing, and the kinds-table proposal (docs/gate.md).

  gate.py [-C DIR] [--base REV] [--full | --only STEP ...] [--plan] [--slug S] [--log-dir D]
      gate the checkout under the machine lock; on a pass of a clean tree, the gate-OK record.
      Through a stub run from outside its checkout, refused (4) when the caller is in another
      checkout, unless an absolute -C names the tree (kitlib.gate.caller)
  gate.py land BRANCH --expect-main SHA --expect-tip SHA [-C DIR] [--lane L] [--summary TEXT]
                      [--full] [--fresh] [--wait SECONDS] [--log-dir D] [--keep-branch]
      land BRANCH from the main checkout: merge, gate the merged tree, push, verify, then delete
      the landed branch (remote and local) and remove its worktree, never forced, unless
      --keep-branch (docs/gate.md "Branch cleanup"); --fresh runs every step of the merged
      gate (no gate reuse, docs/gate.md "Gate reuse")
  gate.py --measure --out PATH [--days N]
      write a proposed kinds table from the lock's run history (never the live table)
  gate.py wait-result BRANCH --tip SHA [--kind gate|land] [--timeout S] [-C DIR]
      read-only: wait for BRANCH's result record at SHA, print it; 0 pass, 1 any other verdict,
      7 no record at that tip within S seconds

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.gate, prints and exits.
Exit codes (kitlib.gate): 0 OK, 1 FAILED, 2 USAGE, 3 STUCK, 4 REFUSED, 5 BUSY; a machine lock
refusal passes its own code through (6, 8, 10, 11, 12); 130 Ctrl+C.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import gate as G                                        # noqa: E402
from kitlib.config import policy                                   # noqa: E402
from kitlib.gate import land as landmod                            # noqa: E402
from kitlib.gate import caller, measure, runner                    # noqa: E402
from kitlib.gate import result as resultmod                       # noqa: E402
from kitlib.lock import layout as lay                              # noqa: E402
from kitlib.lock.store import LockError, Store                     # noqa: E402


def say(line):
    print(line, flush=True)


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (refusal-log P10: a usage error is never
    worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(G.USAGE)


def gate_parser():
    p = Parser(prog='gate.py', description='Gate this checkout under the machine lock.')
    p.add_argument('-C', dest='root', help='the checkout (default: here; through a stub run from '
                   'outside its checkout, refused when the caller is in another checkout unless '
                   '-C is an absolute path)')
    p.add_argument('--base', help='compare against this rev (default <remote>/<main>)')
    sel = p.add_mutually_exclusive_group()
    sel.add_argument('--full', action='store_true', help='every step that is not manual')
    sel.add_argument('--only', nargs='+', metavar='STEP', help='exactly these steps (no record)')
    p.add_argument('--plan', action='store_true', help='print what would run; run nothing')
    p.add_argument('--slug', help='the machine-lock slug (default: the branch)')
    p.add_argument('--log-dir', help='where this run\'s logs go')
    p.add_argument('--measure', action='store_true', help='write a proposed kinds table')
    p.add_argument('--out', help='--measure: the proposal file (never the live kinds table)')
    p.add_argument('--days', type=int, default=30, help='--measure: history days (default 30)')
    return p


def land_parser():
    p = Parser(prog='gate.py land', description='Land one branch from the main checkout.')
    p.add_argument('branch')
    p.add_argument('--expect-main', required=True, metavar='SHA')
    p.add_argument('--expect-tip', required=True, metavar='SHA')
    p.add_argument('-C', dest='root', default='.', help='the main checkout (default: here)')
    p.add_argument('--lane', default='full')
    p.add_argument('--summary', help='the merge title after "Merge <branch>: "')
    p.add_argument('--full', action='store_true', help='gate every non-manual step')
    p.add_argument('--fresh', action='store_true',
                   help='run every merged-gate step: reuse none from the branch gate')
    p.add_argument('--wait', type=int, default=0, metavar='SECONDS',
                   help='wait this long for another landing to finish (default 0: BUSY at once)')
    p.add_argument('--log-dir', help='where the merged gate\'s logs go')
    p.add_argument('--keep-branch', action='store_true',
                   help='after landing, keep the remote branch, the worktree and the local branch')
    return p


def wait_parser():
    p = Parser(prog='gate.py wait-result',
               description='Wait (read-only) for the result record of BRANCH at SHA.')
    p.add_argument('branch')
    p.add_argument('--tip', required=True, metavar='SHA', help='the tip gated (7+ hex digits)')
    p.add_argument('--kind', choices=resultmod.KINDS, default='gate',
                   help='gate (a branch gate, default) or land (a landing)')
    p.add_argument('--timeout', type=float, metavar='S',
                   help='give up after S seconds, exit 7 (default: wait forever)')
    p.add_argument('--poll', type=float, default=resultmod.POLL_S, metavar='S', help=argparse.SUPPRESS)
    p.add_argument('-C', dest='root', default='.', help='any checkout of the repository')
    return p


def do_wait(argv):
    a = wait_parser().parse_args(argv)
    if not re.fullmatch(r'[0-9a-fA-F]{7,40}', a.tip):
        print('USAGE: --tip is 7 to 40 hex digits, not %r' % a.tip, file=sys.stderr)
        return G.USAGE
    if not resultmod.valid_timeout(a.timeout):
        print('USAGE: --timeout is a finite number of seconds, 0 or more, not %s' % a.timeout,
              file=sys.stderr)
        return G.USAGE
    root = os.path.abspath(a.root)
    try:
        rec, _ = resultmod.wait(root, a.branch, a.tip, kind=a.kind, timeout_s=a.timeout,
                                poll_s=max(0.05, a.poll))
    except resultmod.ResultError as e:
        print('USAGE: -C %s: %s' % (root, e), file=sys.stderr)
        return G.USAGE
    if rec is None:
        print('TIMEOUT: no final %s result record for %s at %s within %s s' % (
            a.kind, a.branch, a.tip, a.timeout))
        return resultmod.TIMEOUT
    print('RESULT %s %s %s at %s: %s (%s)' % (rec.get('verdict'), a.kind, a.branch,
                                             str(rec.get('tip'))[:10], rec.get('summary'),
                                             rec.get('log')))
    print(json.dumps(rec, sort_keys=True))
    return G.OK if rec.get('verdict') == 'pass' else G.FAILED


def emit(outcome):
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


def guarded(fn, *args, **kw):
    """fn(...)'s Outcome; a SystemExit that escapes the library (the repo's code calling exit) is a
    FAILED gate with a verdict line, never a silent exit 0."""
    try:
        return fn(*args, **kw)
    except SystemExit as e:
        return G.Outcome(G.FAILED, ['GATE FAILED: something the gate ran called exit (SystemExit, '
                                    'code %r) instead of returning; no gate-OK record' % (e.code,)])


def do_measure(a):
    if not a.out:
        print('USAGE: --measure needs --out PATH (a proposal file for PM review)', file=sys.stderr)
        return G.USAGE
    if a.days < 1:
        print('USAGE: --days must be at least 1', file=sys.stderr)
        return G.USAGE
    out = os.path.abspath(a.out)
    live = os.path.abspath(policy.KINDS_FILE)
    try:                            # a link or another spelling of the live table is the table
        same = os.path.normcase(out) == os.path.normcase(live) or (
            os.path.exists(out) and os.path.samefile(out, live))
    except OSError as e:
        print('REFUSED: cannot tell whether %s is the live kinds table: %s' % (out, e))
        return G.REFUSED
    if same:
        print('REFUSED: %s is the live kinds table; sizes change only by a PM-reviewed commit'
              % live)
        return G.REFUSED
    try:
        with open(live, encoding='utf-8') as f:
            table = json.load(f)['kinds']
        store = Store(say=say)
        layout = lay.in_force(store)['layout']
        history = store.history_records(a.days)
    except (OSError, ValueError, KeyError) as e:
        print('REFUSED: cannot read the kinds table or the lock history: %s' % e)
        return G.REFUSED
    kinds, notes = measure.propose(table, history, layout['medium_cores'],
                                   store.settings.history_min_runs, agent_cap=layout['agent_cap'])
    doc = measure.document(kinds, 'PROPOSED by gate.py --measure from %d day(s) of history; '
                                  'review, then commit to kitlib/lock/kinds.json.' % a.days)
    try:
        with open(out, 'w', encoding='utf-8') as f:
            f.write(json.dumps(doc, indent=1) + '\n')
    except OSError as e:
        print('REFUSED: cannot write the proposal %s: %s' % (out, e))
        return G.REFUSED
    for n in notes:
        print(n)
    print('proposal written: %s' % out)
    return G.OK


def main(argv):
    book_kw = None
    here, why = caller.take()       # every subcommand: no child inherits the stub's variable
    try:
        if argv[:1] == ['wait-result']:
            return do_wait(argv[1:])
        if argv[:1] == ['land']:
            a = land_parser().parse_args(argv[1:])
            book_kw = {'store': Store(say=say)}
            return emit(guarded(landmod.land, os.path.abspath(a.root), a.branch, a.expect_main,
                                a.expect_tip, lane=a.lane, summary=a.summary, full=a.full,
                                wait_s=a.wait, say=say, book_kw=book_kw, log_dir=a.log_dir,
                                keep_branch=a.keep_branch, fresh=a.fresh))
        a = gate_parser().parse_args(argv)
        if a.measure:
            if a.only or a.full or a.plan or a.base:
                print('USAGE: --measure takes only --out and --days', file=sys.stderr)
                return G.USAGE
            return do_measure(a)
        if a.out:
            print('USAGE: --out goes with --measure', file=sys.stderr)
            return G.USAGE
        if why:
            print('REFUSED: %s; nothing ran' % why)
            return G.REFUSED
        root = os.path.abspath(a.root or '.')
        # Only an absolute -C names the tree: through a stub that moved the caller, a relative
        # one resolves against the stub's checkout, not where the caller typed it.
        if here is not None and not (a.root and os.path.isabs(a.root)):
            refused = caller.refusal(root, here)
            if refused is not None:
                return emit(refused)
        book_kw = {'store': Store(say=say)}
        return emit(guarded(runner.branch_gate, root, base=a.base, full=a.full,
                            only=a.only, plan=a.plan, slug=a.slug, log_dir=a.log_dir,
                            say=say, book_kw=book_kw))
    except LockError as e:
        return emit(runner.lock_refusal(e))
    except KeyboardInterrupt:
        print('Ctrl+C: the gate stopped')
        return 130


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
