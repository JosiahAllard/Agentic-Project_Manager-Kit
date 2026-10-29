"""land.py: land a branch, or a batch, on the main branch: the one landing tool (docs/gate.md
"Landing").

  land.py BRANCH [BRANCH ...] --expect-tip SHA [--expect-tip SHA ...] [--expect-main SHA]
          [-C DIR] [--summary TEXT] [--full] [--fresh] [--wait SECONDS]
          [--log-dir D] [--keep-branch] [--reviewed-by REVIEWER_ID ... --submit]
      from the main checkout: take the landing's turn in the machine lock's queue (one booking,
      whose slot the merged gate runs in) and the landing lock, fetch, merge each branch onto
      <remote>/<main> as it is then (main moved on since --expect-main: said, never refused) with
      --no-ff (one --expect-tip per branch, in the same order) in the kit's landing tree (never
      the main checkout), gate the merged tree there, push, verify with ls-remote, fast-forward
      the main checkout's main to it when that is safe, release; on a red gate, a conflict
      (CONFLICT) or a refused push nothing is pushed and the merge is dropped.
      Landed: each branch's remote branch, worktree and local branch go, never forced (what main
      does not contain, or work, or a holder, keeps it); --keep-branch keeps all three. --fresh
      runs every step of the merged gate (no gate reuse, docs/gate.md "Gate reuse").
      Every branch lands only with a verdict queued for its tip (tools/verdict.py add, or carried
      to it over main merges by tools/verdict.py carry and checked again here) by a reviewer who
      is not its author (docs/gate.md "Landing" step 4). A branch that changes a strict path
      (kitlib/gate/strict_defaults.py, read from main) lands alone, its reviewer strong-tier, and
      its merged gate runs --full --fresh whatever was passed (docs/gate.md "Strict paths").
      --submit: the same landing, started detached and windowless; prints a REVIEW line for each
      branch with a verdict queued at its tip, then the last line, SUBMITTED ... wait: <the
      gate.py wait-result command that waits for its result record>, and exits 0. It needs one
      --reviewed-by per branch, before --submit, in --expect-tip's order: the reviewer id of its
      verdict queued at its tip. Missing or miscounted: USAGE (2) with an `Instead:` line, the
      command with the queued ids; an id that is not the reviewer, or no verdict queued at the
      tip: REFUSED (4); nothing written (docs/gate.md "Submitting a landing")
  land.py --queue [-C DIR]
      read-only: the landing lock's holder and the landings in the machine lock's queue
  land.py BRANCH [BRANCH ...] --check-strict [-C DIR]
      read-only, before a reviewer is picked: per branch, `STRICT <branch>: <paths>: ...` (its
      reviewer must be strong-tier, and it lands alone) or `NOT STRICT <branch>: ...`, by the
      matcher a landing refuses by, against <remote>/<main> as last fetched; no lock, no verdict
  land.py --sweep-landed [--apply] [-C DIR]
      the same cleanup for every branch main already contains (docs/gate.md "Branch cleanup");
      a dry run names what would go, --apply cleans up

--wait: seconds to wait for the turn and the lock (default "landing".wait_s of .kit.json; 0: BUSY
at once). Every landing writes a result record per branch (gate.py wait-result --kind land); a
submitted landing writes them running before it starts, so every outcome, a refusal included,
ends in a final record.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.gate.land, prints and exits.
Exit codes (kitlib.gate): 0 OK (or SUBMITTED), 1 FAILED (CONFLICT included), 2 USAGE, 3 STUCK,
4 REFUSED, 5 BUSY; 9 LANDED_UNGATED is retired (no landing gives it); a machine lock refusal
passes its own code through (6, 8, 10, 11, 12); 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def safe_stdio():
    """tools/gate.py's safe_stdio: a character the console's encoding has no byte for (cp1252 on
    a redirected Windows stream: a branch name, a merged gate's step reason or a console window's
    title) is written as a \\u escape, never raised: a landing never ends `error` over printing."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors='backslashreplace')
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == '__main__':      # from the kit's main checkout: run from pushed main (kitlib.gate.pin)
    safe_stdio()
    from kitlib.gate import pin                                     # noqa: E402
    _code, _said = pin.run(__file__, sys.argv[1:])
    if _said:
        sys.stderr.write(_said + '\n')
    if _code is not None:
        sys.exit(_code)

from kitlib import gate as G                                       # noqa: E402
from kitlib.gate import cleanup                                    # noqa: E402
from kitlib.gate import land as landmod                            # noqa: E402
from kitlib.gate import runner                                     # noqa: E402
from kitlib.gate import submit as submitmod                        # noqa: E402
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
    p.add_argument('--expect-main', metavar='SHA',
                   help='the main the landing was planned on (optional): main moved on is said, '
                        'never refused')
    p.add_argument('--expect-tip', action='append', default=[], metavar='SHA',
                   help='the tip of each BRANCH, in the same order')
    p.add_argument('-C', dest='root', default='.', help='the main checkout (default: here)')
    p.add_argument('--summary', help='the merge title after "Merge <branch>: "')
    p.add_argument('--full', action='store_true', help='gate every non-manual step')
    p.add_argument('--fresh', action='store_true',
                   help='run every merged-gate step: reuse none from the branch gate')
    p.add_argument('--wait', type=int, default=None, metavar='SECONDS',
                   help='wait this long for the turn and the lock (default "landing".wait_s)')
    p.add_argument('--log-dir', help='where the merged gate\'s logs go')
    p.add_argument('--queue', action='store_true', help='print the landing lock and queue; land nothing')
    p.add_argument('--check-strict', action='store_true',
                   help='say whether each BRANCH changes a strict path; land nothing')
    p.add_argument('--keep-branch', action='store_true',
                   help='after landing, keep the remote branch, the worktree and the local branch')
    p.add_argument('--sweep-landed', action='store_true',
                   help='clean up every branch main already contains (a dry run without --apply)')
    p.add_argument('--apply', action='store_true', help='--sweep-landed: clean up, not a dry run')
    p.add_argument(submitmod.REVIEWED_BY, dest='reviewed_by', action='append', default=[],
                   metavar='REVIEWER_ID',
                   help='--submit: the reviewer id of each BRANCH\'s verdict queued at its tip, '
                        'in the same order (required)')
    p.add_argument('--submit', action='store_true',
                   help='start the landing detached; print the wait-result command and exit 0')
    p.add_argument(submitmod.FLAG, dest='submitted', metavar='PAYLOAD', help=argparse.SUPPRESS)
    return p


def again_args(a, root):
    """This submission's arguments in the land permission rule's order (docs/allow-rules.md):
    -C (absolute, forward slashes), the branches, --expect-tip, then the rest; for the Instead:
    command of a submission that did not name its reviewers (submit.again_line)."""
    args = ['-C', root.replace('\\', '/')] + list(a.branches)
    for tip in a.expect_tip:
        args += ['--expect-tip', tip]
    if a.expect_main:
        args += ['--expect-main', a.expect_main]
    if a.summary:
        args += ['--summary', a.summary]
    if a.wait is not None:
        args += ['--wait', str(a.wait)]
    if a.log_dir:
        args += ['--log-dir', a.log_dir]
    return args + [flag for flag, on in (('--full', a.full), ('--fresh', a.fresh),
                                         ('--keep-branch', a.keep_branch)) if on]


def child_args(a, root):
    """The arguments of the detached landing a --submit starts: this landing's, -C and --log-dir
    absolute."""
    args = ['-C', root]
    if a.expect_main:
        args += ['--expect-main', a.expect_main]
    for tip in a.expect_tip:
        args += ['--expect-tip', tip]
    if a.summary:
        args += ['--summary', a.summary]
    if a.wait is not None:
        args += ['--wait', str(a.wait)]
    if a.log_dir:
        args += ['--log-dir', os.path.abspath(a.log_dir)]
    args += [flag for flag, on in (('--full', a.full), ('--fresh', a.fresh),
                                   ('--keep-branch', a.keep_branch)) if on]
    return args + list(a.branches)


def restore(root, a):
    """The detached landing's `submitted`, its output now going to the submission's log; None
    when the payload is not the one a submission wrote for this landing (submit.Untrusted: that
    is said on stderr and, at the submission's own path, in its log; no record is written) or,
    read with the submission's token, cannot be read (the submission's records are
    then finished refused, submit.abandon, and that is said, on the hidden console)."""
    payload = a.submitted
    try:
        sub = submitmod.restore(root, payload, a.branches, a.expect_tip,
                                expect_main=a.expect_main)
    except submitmod.Untrusted as e:    # also in the submission's log: stderr may be hidden
        sys.stderr.write(submitmod.untrusted_refusal(payload, e) + '\n')
        return None
    except submitmod.SubmitError as e:
        emit(submitmod.abandon(root, payload, str(e)))
        return None
    if sub.get('output'):
        try:
            submitmod.redirect_output(sub['output'])
        except OSError as e:
            print('WARNING: the output stays on the hidden console: %s' % e, flush=True)
    return sub


def emit(outcome):
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


def main(argv):
    safe_stdio()
    p = parser()
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    if a.apply and not a.sweep_landed:
        p.error('--apply goes with --sweep-landed')
    if a.reviewed_by and not a.submit:
        p.error('%s goes with --submit' % submitmod.REVIEWED_BY)
    if a.sweep_landed:
        if (a.branches or a.expect_main or a.expect_tip or a.queue or a.keep_branch or a.full
                or a.fresh or a.summary or a.wait is not None or a.log_dir
                or a.submit or a.submitted or a.check_strict):
            p.error('--sweep-landed takes only --apply and -C')
        try:
            return emit(cleanup.sweep(root, apply=a.apply))
        except KeyboardInterrupt:
            print('Ctrl+C: the sweep stopped; run it again to finish')
            return 130
    if a.check_strict:
        if (a.expect_main or a.expect_tip or a.queue or a.keep_branch or a.full or a.fresh
                or a.summary or a.wait is not None or a.log_dir or a.submit or a.submitted):
            p.error('--check-strict takes only BRANCH ... and -C')
        if not a.branches:
            p.error('name the branch to check (or several)')
        return emit(landmod.check_strict(root, a.branches))
    if a.queue:
        if a.branches or a.expect_main or a.expect_tip or a.submit or a.submitted:
            p.error('--queue takes only -C')
        return emit(landmod.queue(root, Store(say=say)))
    if not a.branches:
        p.error('name the branch to land (or several: a batch)')
    if not a.expect_tip:
        p.error('one --expect-tip per branch is required')
    if a.wait is not None and a.wait < 0:
        p.error('--wait is 0 or more seconds')
    if a.submit and a.submitted:
        p.error('--submit starts the landing that --submitted runs; not both')
    if a.submit:
        return emit(submitmod.submit(
            root, __file__, child_args(a, root), a.branches, a.expect_tip,
            expect_main=a.expect_main, reviewed_by=a.reviewed_by,
            again=lambda ids: submitmod.again_line('land.py', again_args(a, root), ids)))
    submitted = None
    if a.submitted:
        submitted = restore(root, a)
        if submitted is None:
            return G.REFUSED
    branch = a.branches[0] if len(a.branches) == 1 else a.branches
    tip = a.expect_tip[0] if len(a.expect_tip) == 1 else a.expect_tip
    try:
        out = landmod.land(root, branch, a.expect_main, tip, summary=a.summary,
                           full=a.full, wait_s=a.wait, say=say,
                           book_kw={'store': Store(say=say)}, log_dir=a.log_dir,
                           keep_branch=a.keep_branch, fresh=a.fresh, submitted=submitted)
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
