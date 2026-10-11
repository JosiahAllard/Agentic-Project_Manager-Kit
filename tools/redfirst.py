"""redfirst.py: run named new tests against the pre-change commit in a throwaway worktree and confirm
they go red there - red-first evidence with no in-place mutant (docs/redfirst.md).

  redfirst.py [-C WORKTREE] --tests FILE[::FILTER] ... [--also FILE ...] [--base REV] [--head REV]
              [--log-dir DIR]

  --tests   a committed test file of --head, repo-relative, optionally ::<filter>; the runner that
            takes it is the first `.kit.json` "redfirst".runners entry whose glob matches (none
            written: tests/*test_*.py, then <tools_dir>/*test_*.py run with the tools folder as
            PYTHONPATH); may be repeated, the values accumulate
  --also    a further committed file of --head checked out with the tests (a new fixture); never run;
            may be repeated
  --base    the pre-change commit; default the merge-base of --head with <remote>/<main branch>
  --head    where the test files come from; default HEAD
  --log-dir where the logs go; default `<gate log dir>/redfirst/<stamp>-<pid>`

-C names the task worktree (default: here); a main checkout is refused. A thin entry point: it
parses, calls kitlib.redfirst, prints and exits. One line per selector: RED, GREEN, NONE (no test
matched) or ERROR. Exit: 0 every selector RED at the base; 1 at least one GREEN; 2 usage or a
refusal (`--help` too, since 0 means RED); 3 undecided; 4 the throwaway worktree was left.
"""
import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.redfirst import run as R                                # noqa: E402
from kitlib.redfirst import verdict as V                            # noqa: E402


def say(line):
    print(line, flush=True)


def split_root(argv):
    """`(root or None, rest, why)`: -C DIR or -CDIR, at most once."""
    root, rest, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == '-C' or (a.startswith('-C') and not a.startswith('--')):
            if root is not None:
                return None, None, '-C is given twice'
            value = a[2:] if a != '-C' else (argv[i + 1] if i + 1 < len(argv) else None)
            if not value:
                return None, None, '-C needs a directory'
            root = value
            i += 1 if a != '-C' else 2
            continue
        rest.append(a)
        i += 1
    return root, rest, None


def parse_args(argv):
    ap = argparse.ArgumentParser(prog='redfirst.py', description=__doc__.split('\n\n')[0])
    ap.add_argument('--tests', nargs='+', action='extend', required=True, metavar='FILE[::FILTER]')
    ap.add_argument('--also', nargs='+', action='extend', default=[], metavar='FILE')
    ap.add_argument('--base')
    ap.add_argument('--head', default='HEAD')
    ap.add_argument('--log-dir')
    return ap.parse_args(argv)


def main(argv=None, run=R.default_run, cleanup=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root, rest, why = split_root(argv)
    if why:
        say('usage: %s' % why)
        return V.EXIT_USAGE
    try:
        a = parse_args(rest)
    except SystemExit:                       # argparse's usage error, or --help: 0 would mean RED
        return V.EXIT_USAGE
    try:
        plan = R.prepare(os.path.abspath(root or os.getcwd()), a.tests, a.also, a.base, a.head,
                         a.log_dir)
    except V.Usage as e:
        say(str(e))
        return V.EXIT_USAGE
    except KeyboardInterrupt:
        say('stopped   interrupted before anything was made')
        say('REDFIRST UNDECIDED - nothing ran')
        return V.EXIT_UNDECIDED
    except Exception as e:  # noqa: BLE001  (a git read that failed: nothing is made yet)
        say('failed    %s: %s' % (type(e).__name__, e))
        say('REDFIRST UNDECIDED - nothing was made')
        return V.EXIT_UNDECIDED
    return R.execute(plan, say, run=run, cleanup=cleanup)


def child():
    for s in R.SIGNALS:
        signal.signal(s, signal.SIG_IGN)

    def write(line):                         # the parent may be gone; the cleanup still finishes
        try:
            os.write(1, (line + '\n').encode('utf-8', 'replace'))
        except OSError:
            pass
    return R.cleanup_child(sys.stdin.buffer.read(), write)


if __name__ == '__main__':
    if sys.argv[1:] == [R.CHILD_FLAG]:
        sys.exit(child())
    sys.exit(main())
