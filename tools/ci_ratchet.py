"""ci_ratchet.py: run the whole suite in one child and hold its failing tests to a baseline
(docs/test-tools.md "Platform markers and the ratchet").

  ci_ratchet.py [-C ROOT] [--baseline PATH]

The suite is `unittest discover -s tests -t .` under ROOT (default here). The baseline is one test
id per line, sorted, none twice, '#' comments; the default is tests/linux-baseline.txt on Linux,
tests/windows-baseline.txt on Windows (no file there: an empty baseline). It fails on a failing
test the baseline does not list (an error, an unexpected success or a module that does not import
counts) and on a listed test that no longer fails (it passes or is gone: take it out). Until slice 2
of docs/linux-support-plan.md every Linux run is a broken run: the tests package does not import.
On Windows the gate (`gate.py --full`) runs the suite; this tool is for the platforms it does not.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.ci.ratchet, prints, exits.
It loads no kitlib.proc beyond kitlib/__init__'s console hook, and no kitlib.config or kitlib.git,
so it runs where those cannot import.
Exit codes: 0 green (the failing tests are exactly the baseline), 1 a difference, 2 USAGE, a
malformed or missing baseline, or a broken run (the tests package did not import, the child died,
timed out or printed no result, no test ran, or this tool met an unexpected error).
"""
import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.ci import ratchet                                       # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='ci_ratchet.py',
               description='The suite\'s failing tests against a baseline (docs/test-tools.md).')
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    p.add_argument('--baseline', metavar='PATH',
                   help='the baseline (default: tests/linux-baseline.txt on Linux, '
                        'tests/windows-baseline.txt on Windows)')
    return p


def main(argv=None, out=None):
    out = sys.stdout if out is None else out

    def say(line):
        out.write(line + '\n')
        out.flush()
    a = build_parser().parse_args(argv)
    try:
        return _run(a, say)
    except Exception:
        say('ci_ratchet: BROKEN RUN: an unexpected error')
        say(traceback.format_exc().rstrip())
        return USAGE


def _run(a, say):
    root =os.path.abspath(a.root)
    if not os.path.isdir(os.path.join(root, 'tests')):
        say('ci_ratchet: refused: no tests folder in %s' % root)
        return USAGE
    path = a.baseline or ratchet.default_baseline(root)
    if path is None:
        say('ci_ratchet: refused: no default baseline on %s: give --baseline' % sys.platform)
        return USAGE
    try:
        baseline = ratchet.read_baseline(path, default=a.baseline is None)
    except ratchet.Refused as e:
        say('ci_ratchet: refused: %s' % e)
        return USAGE
    say('ci_ratchet: the suite in one child under %s; baseline %s (%d id(s))'
        % (root, path, len(baseline)))
    run = ratchet.run_suite(root)
    for kind, reason, n in run.markers:
        say('ci_ratchet: marker: %s_only x%d: %s' % (kind, n, reason))
    if run.broken:
        say('ci_ratchet: BROKEN RUN: %s' % run.broken)
        if run.output:
            say(run.output)
        return USAGE
    new, stale = ratchet.compare(run.failing, baseline)
    for test in new:
        say('ci_ratchet: NEW FAILURE: %s' % test)
    for test in stale:
        say('ci_ratchet: STALE: %s (it passes or is gone: take it out of the baseline)' % test)
    if new or stale:
        say('ci_ratchet: DIFFERENT: %d new failure(s), %d stale entr(ies); %d test(s) ran'
            % (len(new), len(stale), run.ran))
        return 1
    say('ci_ratchet: GREEN: %d test(s) ran, %d failing, each in the baseline'
        % (run.ran, len(run.failing)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
