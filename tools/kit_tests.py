"""kit_tests.py: run the kit's own test suite, whole or only the test packages a branch's change can
reach (docs/test-tools.md "kit_tests").

  kit_tests.py [-C DIR] --select [--base B] [--changed PATH ...] [--plan] [--jobs N]
  kit_tests.py [-C DIR] --full [--plan] [--jobs N]
  add --real-git to either: every test on real git, not the fake
  add --both-tiers to either: the fake-git pass, then the real-git pass (kitlib.tooldeps.tiers)
  add --pid-hold to either: hold pids for the run (opt-in, agent runs only; the gate refuses it;
  docs/test-tools.md "The pid shield")

--select diffs the checkout against its merge-base with B (default: origin/main when it resolves,
else main - docs/gate.md "Default base" - and a line says when the local main differs), work
tree and untracked files included (or takes the --changed list), maps the paths through
kitlib.tooldeps.selftree and the table tests/selection.json, prints each selected package with the reasons it was picked, and
runs `python -m unittest <its test modules>`. A shared-core, test-helper, selector or unmapped
change runs the full suite instead, and says why. --full runs `python -m unittest discover -s
tests -t .`'s modules; the daily full run and go-live use it (a landing gates with --select). --plan prints and runs nothing.
The modules run in parallel worker processes (kitlib.tooldeps.selfrun), --jobs at a time - by
default the logical processors the machine lock run holds - longest first by the times recorded
in the git common dir (kit-tests-times.json), long modules split by class - then the table's
`serial` modules one at a time. As the pool drains, the cpu slots its last units do not use go
back to the lock (kitlib.lock.giveback.drainer) when the lock run offers it. --jobs 1 runs the
one `python -m unittest` process the tool always ran.
Tests run on the fake git (docs/test-tools.md "Fake git"): the units get KIT_FAKE_GIT=1 unless it
is set; --real-git gives them 0, so the unit tier also runs on real git. --both-tiers runs the
selection twice, KIT_FAKE_GIT 1 then 0, and prints `kit_tests: OK` only when both passed; each
pass's own lines read `kit_tests [fake git]: ` / `kit_tests [real git]: `.
A thin entry point (ARCHITECTURE.md, Layers): it parses, calls the library, prints, runs, exits.
Exit codes: the unittest run's (0 passed or nothing selected, 1 failed); 2 USAGE or a refusal
(git could not list the change, or --select with a malformed table): no test ran; 130 Ctrl+C.
--full never refuses a malformed table: it warns and runs every module, none serial.
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import config as kconfig                                # noqa: E402
from kitlib.git import refs as gitrefs                              # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.lock import giveback                                    # noqa: E402
from kitlib.pytests import runner as unit_runner                    # noqa: E402
from kitlib.tooldeps import selfrun, selftree, tiers                # noqa: E402

USAGE = 2
FULL = ['-m', 'unittest', 'discover', '-s', 'tests', '-t', '.']


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='kit_tests.py', allow_abbrev=False,
               description="Run the kit's own tests, whole or selected (docs/test-tools.md).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--select', action='store_true', help='only what the change can reach')
    g.add_argument('--full', action='store_true', help='the whole suite')
    p.add_argument('--base', help='--select: diff against the merge-base with this (default: '
                   'origin/main when it resolves, else main)')
    p.add_argument('--changed', nargs='+', metavar='PATH', help='--select: these paths, not git')
    p.add_argument('--plan', action='store_true', help='print the selection, run nothing')
    p.add_argument('--jobs', type=int, metavar='N',
                   help='modules run at once (default: the cores the lock run holds; 1: one process)')
    p.add_argument('--real-git', action='store_true',
                   help='every test on real git, not the fake (KIT_FAKE_GIT=0)')
    p.add_argument('--both-tiers', action='store_true',
                   help='run twice: on the fake git, then on real git; OK only when both pass '
                   '(the daily run\'s daily_cmd)')
    p.add_argument(unit_runner.PID_HOLD_FLAG, dest='pid_hold', action='store_true',
                   help='hold pids for the run (opt-in for agent runs; off by default; the gate '
                   'refuses it)')
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    return p


def default_base(top):
    """`(rev, note)`: the shared default base (kitlib.git.refs.default_base) for the checkout's
    .kit.json "repo" remote and main branch; origin and main when .kit.json cannot be read."""
    try:
        disk = kconfig.load(top)
        remote, main = disk.remote, disk.main_branch
    except kconfig.ConfigError:
        remote, main = 'origin', 'main'
    return gitrefs.default_base(top, remote, main)


def plan(root, changed, cfg, say):
    """The test modules to run (FULL: all; None: nothing), after printing the selection."""
    tree = selftree.scan(root)
    picked, why, notes = selftree.selection(changed, tree, cfg)
    for f in notes:
        say('  (no test)  %s: prose no test names' % f)
    if picked == selftree.ALL:
        say('kit_tests: %s' % why[0])
        return FULL
    if not picked:
        say('kit_tests: OK, nothing selected (%d changed path(s))' % len(changed))
        return None
    width = max(len(p) for p in picked) + 6
    for p in picked:
        say('  %s %s' % (('tests/' + p).ljust(width), why[p][0]))
        for more in why[p][1:]:
            say('  %s %s' % (' ' * width, more))
    modules = selftree.test_modules(root, picked)
    say('kit_tests: selected %d of %d test package(s), %d module(s), from %d changed path(s)'
        % (len(picked), len(tree.tests), len(modules), len(changed)))
    return modules


def main(argv=None, out=None):
    out = sys.stdout if out is None else out

    def say(line):
        out.write(line + '\n')
        out.flush()
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.full and a.changed:
        parser.error('--changed goes with --select')
    if a.jobs is not None and a.jobs < 1:
        parser.error('--jobs must be at least 1')
    if a.both_tiers and a.real_git:
        parser.error('--both-tiers already runs the real-git tier; drop --real-git')
    if a.both_tiers and a.pid_hold:
        parser.error('--both-tiers takes no --pid-hold (one hold cannot span two passes)')
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('kit_tests: refused: %s' % why)
        return USAGE
    try:
        cfg = selftree.config(top)
    except selftree.ConfigError as e:
        if not a.full:
            say('kit_tests: refused: %s - no test ran' % e)
            return USAGE
        # --full is the escape hatch: it needs no table, so a broken one cannot stop it.
        say('kit_tests: WARNING: %s; --full runs every module, none serial' % e)
        cfg = {'serial': []}
    if a.full:
        say('kit_tests: full suite (--full)')
        args = FULL
    else:
        changed = a.changed
        if changed is None:
            base = a.base
            if base is None:
                base, note = default_base(top)
                if note:
                    say('kit_tests: %s' % note)
            changed, why = selftree.changed_paths(base, top)
            if changed is None:
                say('kit_tests: refused: %s - no test ran; fix git, or run --full' % why)
                return USAGE
            say('kit_tests: --select against the merge-base with %s: %d changed path(s)'
                % (base, len(changed)))
        args = plan(top, changed, cfg, say)
    if a.plan or args is None:
        return 0
    hold = None
    if a.pid_hold:
        # Opt-in only (never in the gate): started before the units' environment is copied below,
        # so its published beacon reaches them.
        hold = unit_runner.PidHoldRun()
        say('kit_tests: %s' % hold.start())
    if a.jobs is None:
        jobs, why = selfrun.default_jobs()
        say('kit_tests: jobs %d: %s' % (jobs, why))
    else:
        jobs = a.jobs
    if a.both_tiers:
        # The fake pass, then the real-git pass (kitlib.tooldeps.tiers): only the last may give
        # cpu slots back, and only the fake pass records unit times (the landing tier's).
        return tiers.run_tiers(
            lambda fake, stream, last: run_pass(args, top, cfg, jobs, fake, stream, None,
                                                give_back=last, record_times=fake == '1'), out)
    # The units' environment carries the tier: a pool worker imports tests/__init__.py once, so
    # its default cannot reach each unit (kitlib/tooldeps/pool_worker.py). This process keeps
    # its own git real.
    if a.real_git:
        fake = '0'
        say('kit_tests: --real-git: every test on real git')
    else:
        fake = os.environ.get('KIT_FAKE_GIT', '1')
    return run_pass(args, top, cfg, jobs, fake, out, hold)


def run_pass(args, top, cfg, jobs, fake, out, hold, give_back=True, record_times=True):
    """One run of `args` (FULL or test modules) with the units' KIT_FAKE_GIT set to `fake`,
    writing to `out`; its exit status."""
    def say(line):
        out.write(line + '\n')
        out.flush()
    env = dict(os.environ)
    env['KIT_FAKE_GIT'] = fake
    label = 'full' if args is FULL else 'selected'
    if jobs == 1:
        start = time.time()
        cmd = args if args is FULL else ['-m', 'unittest'] + args
        out.flush()
        rc = subprocess.call([sys.executable] + cmd, cwd=top, env=env)
        if hold is not None:
            say('kit_tests: %s' % hold.end())
        say('kit_tests: %s in %.0fs (%s, one process)' % (
            'OK' if rc == 0 else 'FAILED, exit %d' % rc, time.time() - start, label))
        return 0 if rc == 0 else 1
    modules = selftree.all_test_modules(top) if args is FULL else args
    common, _why = gitrepo.common_dir(top)
    times = os.path.join(common, selfrun.TIMES_FILE) if common else None
    give = None
    if give_back:
        give = giveback.drainer(lambda line: say('kit_tests: ' + line))
        if give is None:
            say('kit_tests: give back: no offer (only a run of this kit\'s lock wrapper offers '
                'one); the run keeps its cpu slots to its end')
    else:
        say('kit_tests: give back: off for this pass (a later tier runs on the same slots)')
    rc, _summary = selfrun.run(modules, top, jobs, serial=cfg['serial'], out=out, label=label,
                               times_path=times, hint=cfg.get('long', ()), environ=env,
                               give_back=give, hold=hold, record_times=record_times)
    return rc


if __name__ == '__main__':
    sys.exit(main())
