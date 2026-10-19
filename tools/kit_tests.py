"""kit_tests.py: run the kit's own test suite, whole or only the test packages a branch's change can
reach (docs/test-tools.md "kit_tests").

  kit_tests.py [-C DIR] --select [--smoke] [--base B] [--changed PATH ...] [--plan] [--jobs N]
  kit_tests.py [-C DIR] --full [--base B] [--plan] [--jobs N]
  kit_tests.py [-C DIR] --scrub [--plan] [--jobs N]: only tests/scrub, one pass (the docs-only
  gate step)
  --smoke adds tests/selection.json's "smoke" modules to the selection unless every changed
  path is prose (kitlib.tooldeps.selftree.smoke; the gate's smoke_steps, docs/gate.md); a
  smoke entry naming no test module refuses
  kit_tests.py [-C DIR] --list: every test id sorted, then a summary and the decorator skips
  (kitlib.tooldeps.selflist; nothing runs; runtime skips are not counted)
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
is set; --real-git gives them 0, so the unit tier also runs on real git. On the fake, --select
and --full also run the test modules the change adds or changes (tiers.changed_test_modules: the
--select change list, or for --full the same diff against --base or the default base, said in a
line) as a second pass on real git, with no flag (the gate's cmd and full_cmd); when there is
none, one pass and a line `kit_tests: real-git pass: no added or changed test module`.
--both-tiers runs the selection on KIT_FAKE_GIT 1, then tiers.REAL_GIT_MODULES of it and the
added or changed test modules on 0. Two passes print `kit_tests: OK` only when both passed; each
pass's own lines read `kit_tests [fake git]: ` / `kit_tests [real git]: `. The fake pass gives cpu
slots back as it drains, down to what the real pass can use at once (tiers.keep_for_next); one
drainer serves both passes.
The units also get the home fence's marker (docs/test-tools.md "Home fence"): KIT_HOME_FENCE, the
home folder this process resolves, unless the environment already carries one. The tools a test
starts inherit it and refuse that home.
No test child gets the session's kit binding: PM_KIT_ROOT, PM_KIT_RELEASE_ID, KIT_LIVE_ROOT and
KIT_PINNED, PM_KIT_ROLE, PM_KIT_HOST and PM_KIT_SESSION_NAME (KIT_SESSION_ENV), each registered
host's session_env variable and its PM_KIT_<HOST>_ variables (hosts.json; session_env_names()) are
dropped from its environment, so a session's env cannot steer the suite.
A thin entry point (ARCHITECTURE.md, Layers): it parses, calls the library, prints, runs, exits.
Exit codes: the unittest run's (0 passed or nothing selected, 1 failed); 2 USAGE or a refusal
(git could not list the change - for --full too, unless --real-git - or --select with a
malformed table): no test ran; 130 Ctrl+C.
--full never refuses a malformed table: it warns and runs every module, none serial.
"""
import argparse
import os
import subprocess          # noqa: F401  (tests/tooldeps fake the one-process run through it)
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import config as kconfig                                # noqa: E402
from kitlib.config import user as kuser                             # noqa: E402
from kitlib.git import refs as gitrefs                              # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.host import registry as hostregistry                    # noqa: E402
from kitlib import sessionmark                                      # noqa: E402
from kitlib.lock import giveback                                    # noqa: E402
from kitlib.proc import spawn                                       # noqa: E402
from kitlib.pytests import runner as unit_runner                    # noqa: E402
from kitlib.tooldeps import selflist, selfrun, selftree, tiers              # noqa: E402

USAGE = 2
FULL = ['-m', 'unittest', 'discover', '-s', 'tests', '-t', '.']
# The session's identity and kit binding, never passed to a test child (child_env): boot/kitboot.py
# reads PM_KIT_ROOT and PM_KIT_RELEASE_ID (the kit a host entry binds, kitlib.kitrelease's pin),
# kitlib.gate.pin KIT_PINNED (run live, never pin) and kitlib.pluginsource.live_root
# KIT_LIVE_ROOT (the checkout a printed command names); PM_KIT_ROLE and PM_KIT_HOST are the
# session's role and host, PM_KIT_SESSION_NAME its name (kitlib.sessionmark). Inherited, they would
# steer the code under test toward the session's kit; a test that wants one sets it itself. Also
# dropped, read from hosts.json (session_env_names): each host's session_env variable and every
# PM_KIT_<HOST>_ variable (a host adapter's own state folder and binary).
KIT_SESSION_ENV = ('PM_KIT_ROOT', 'PM_KIT_RELEASE_ID', 'KIT_LIVE_ROOT', 'KIT_PINNED',
                   'PM_KIT_ROLE', 'PM_KIT_HOST', sessionmark.SESSION_NAME_ENV)


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
    g.add_argument('--list', dest='list_tests', action='store_true',
                   help='print every test id, sorted, and the decorator skips; run nothing')
    g.add_argument('--scrub', action='store_true',
                   help='only the scrub test package (the docs-only gate step)')
    p.add_argument('--smoke', action='store_true',
                   help='--select: add the table\'s "smoke" modules unless every changed path '
                   'is prose')
    p.add_argument('--base', help='--select and --full: diff against the merge-base with this '
                   '(default: origin/main when it resolves, else main); --select picks from the '
                   'change, --full takes only the added or changed test modules for its real-git '
                   'pass (no diff under --real-git or with --changed)')
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
    if (a.full or a.scrub) and a.changed:
        parser.error('--changed goes with --select')
    if a.smoke and not a.select:
        parser.error('--smoke goes with --select (--full already runs every module)')
    if a.scrub:
        for flag, used in (('--both-tiers', a.both_tiers), ('--base', a.base is not None)):
            if used:
                parser.error('%s does not go with --scrub (one pass, no change list)' % flag)
    if a.jobs is not None and a.jobs < 1:
        parser.error('--jobs must be at least 1')
    if a.both_tiers and a.real_git:
        parser.error('--both-tiers already runs the real-git tier; drop --real-git')
    if a.both_tiers and a.pid_hold:
        parser.error('--both-tiers takes no --pid-hold (one hold cannot span two passes)')
    if a.list_tests:
        for flag, used in (('--jobs', a.jobs is not None), ('--plan', a.plan),
                           ('--real-git', a.real_git), ('--both-tiers', a.both_tiers),
                           (unit_runner.PID_HOLD_FLAG, a.pid_hold), ('--changed', a.changed),
                           ('--base', a.base is not None)):
            if used:
                parser.error('%s does not go with --list (it runs nothing)' % flag)
    top, why = gitrepo.toplevel(os.path.abspath(a.root))
    if not top:
        say('kit_tests: refused: %s' % why)
        return USAGE
    if a.list_tests:
        modules = selftree.all_test_modules(top)
        env = child_env()
        env.setdefault('KIT_FAKE_GIT', '1')
        entries = selflist.collect(top, modules, environ=env)
        if entries is None:
            say('kit_tests: refused: the child that loads the tests failed - nothing listed')
            return USAGE
        for line in selflist.render(entries, modules):
            say(line)
        return 0
    try:
        cfg = selftree.config(top)
    except selftree.ConfigError as e:
        if not (a.full or a.scrub):
            say('kit_tests: refused: %s - no test ran' % e)
            return USAGE
        # --full is the escape hatch: it needs no table, so a broken one cannot stop it; nor
        # --scrub, which selects nothing from it.
        say('kit_tests: WARNING: %s; %s runs %s, none serial'
            % (e, '--full' if a.full else '--scrub', 'every module' if a.full else 'the scrub'))
        cfg = {'serial': [], 'skip_fails': []}
    stale = selftree.stale_skip_fails(cfg.get('skip_fails', ()), selftree.all_test_modules(top))
    for m in stale:
        say('kit_tests: WARNING: skip_fails names %s, which is not a test module; its entry guards '
            'nothing (fix tests/selection.json)' % m)
    # The tier the units get. On the fake git, the test modules the change adds or changes also run
    # on real git, as a second pass (kitlib.tooldeps.tiers): a test that only ever ran on the fake
    # cannot reach main unseen by real git.
    tier = '0' if a.real_git else os.environ.get('KIT_FAKE_GIT', '1')
    if a.scrub:
        # The docs-only gate step: no test a docs change cannot reach, but every tracked file
        # still scanned. One pass, with the same isolation as any other run.
        changed = []
        args = selftree.test_modules(top, [selftree.SCRUB])
        if not args:
            say('kit_tests: refused: tests/%s holds no test module - no test ran' % selftree.SCRUB)
            return USAGE
        say('kit_tests: --scrub: tests/%s only, %d module(s)' % (selftree.SCRUB, len(args)))
    elif a.full:
        say('kit_tests: full suite (--full)')
        args = FULL
        changed = []
        if tier == '1':
            base = a.base
            if base is None:
                base, note = default_base(top)
                if note:
                    say('kit_tests: %s' % note)
            changed, why = selftree.changed_paths(base, top)
            if changed is None:
                say('kit_tests: refused: %s - no test ran; the real-git pass of the added or '
                    'changed test modules needs the change list: fix git, or run --real-git'
                    % why)
                return USAGE
            say('kit_tests: --full: the real-git pass\'s change list against the merge-base '
                'with %s: %d changed path(s)' % (base, len(changed)))
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
        if a.smoke:
            if args is FULL:
                say('kit_tests: --smoke: the full suite holds every smoke module')
            else:
                try:
                    extra, why = selftree.smoke(changed, cfg, args,
                                                selftree.all_test_modules(top))
                except selftree.ConfigError as e:
                    say('kit_tests: refused: %s - no test ran' % e)
                    return USAGE
                say('kit_tests: --smoke: %s%s' % (why, ': ' + ', '.join(extra) if extra else ''))
                if extra:
                    args = (args or []) + extra
    again = []
    if tier == '1':
        again = tiers.changed_test_modules(changed, selftree.all_test_modules(top))
    if again:
        say('kit_tests: real-git pass: %d added or changed test module(s): %s'
            % (len(again), ', '.join(again)))
    if a.plan or args is None:
        return 0
    if tier == '1' and not again and not a.both_tiers and not a.scrub:
        # Said before the pass, and never a verdict: the run's verdict is the pass's own.
        say('kit_tests: real-git pass: no added or changed test module')
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
    if a.both_tiers or again:
        # The fake pass, then the real-git pass (kitlib.tooldeps.tiers); only the fake pass
        # records unit times (the landing tier's). The real-git pass runs only some modules:
        # under --both-tiers tiers.REAL_GIT_MODULES of the selection and the added or changed test
        # modules, otherwise only those.
        if a.both_tiers:
            every = selftree.all_test_modules(top) if args is FULL else args
            real = tiers.real_git_modules(every, also=again)
            label = '--both-tiers'
        else:
            real = again
            label = '%s, then its added or changed test modules on real git' % (
                '--full' if a.full else '--select')
            if hold is not None:
                say('kit_tests: the pid hold covers the fake-git pass; the real-git pass runs '
                    'without the pid hold')
        # One drainer for both passes, its lines going to the pass that runs. A second one would
        # read the threads per slot afresh from a record the first pass already shrank, against
        # the cpu mask this process started with, and so keep too few slots for the real pass.
        stream = {'out': out}
        give = None
        if jobs != 1:
            give = drainer(lambda line: (stream['out'].write(line + '\n'), stream['out'].flush()))
        counted = None
        first_give = give
        if give is not None and real:
            # Giving back is one-way, so the fake pass keeps what the real pass can use at once.
            # The count child gets no offer, as the pool's own count child gets none.
            env0 = {k: v for k, v in units_env('0').items() if k != giveback.GIVE_ENV}
            try:
                counted = selfrun.count_units(top, real, environ=env0)
            except KeyboardInterrupt:
                say('kit_tests: INTERRUPTED, exit %d before the first pass' % tiers.INTERRUPTED)
                return tiers.INTERRUPTED
            bound = tiers.unit_bound(real, counted[1])
            need = min(jobs, bound)
            first_give = tiers.keep_for_next(give, need)
            say('kit_tests: give back: the fake-git pass keeps %d thread(s) for the real-git pass '
                '(%d module(s), up to %d unit(s))' % (need, len(real), bound))

        def one_pass(fake, tier_out, last):
            stream['out'] = tier_out
            use = args if fake == '1' else real
            if not use:
                tier_out.write('kit_tests: no real-git module in the selection\n')
                return 0
            if fake == '1':
                return run_pass(use, top, cfg, jobs, fake, tier_out, hold, give_back=first_give)
            # Counted before the fake pass, so the units it plans are the ones the fake pass kept
            # threads for.
            return run_pass(use, top, cfg, jobs, fake, tier_out, None, give_back=give,
                            record_times=False, counted=counted)
        return tiers.run_tiers(one_pass, out, label=label)
    # The units' environment carries the tier: a pool worker imports tests/__init__.py once, so
    # its default cannot reach each unit (kitlib/tooldeps/pool_worker.py). This process keeps
    # its own git real.
    if a.real_git:
        say('kit_tests: --real-git: every test on real git')
    return run_pass(args, top, cfg, jobs, tier, out, hold,
                    give_back=drainer(say) if jobs != 1 else None)


def drainer(say):
    """The pool's give back (kitlib.lock.giveback.drainer), its lines said through `say`; None,
    and a line that says so, when the lock run offers none."""
    give = giveback.drainer(lambda line: say('kit_tests: ' + line))
    if give is None:
        say('kit_tests: give back: no offer (only a run of this kit\'s lock wrapper offers one); '
            'the run keeps its cpu slots to its end')
    return give


def session_env_names():
    """`(names, prefixes)`, upper-cased, of the variables a test child never gets: KIT_SESSION_ENV
    and every registered host's session_env variable (hosts.json), and the prefix PM_KIT_<HOST>_
    of each registered host; only KIT_SESSION_ENV when hosts.json cannot be read."""
    try:
        hosts = hostregistry.union('session_env')
        prefixes = tuple('PM_KIT_%s_' % n.upper() for n in hostregistry.load())
    except hostregistry.RegistryError:
        hosts, prefixes = (), ()
    return {n.upper() for n in KIT_SESSION_ENV + hosts}, prefixes


def child_env():
    """The environment every test child starts from (the units, the count child, --list's loader):
    this process's without session_env_names(), with the home fence's marker (kitlib.config.user.
    fence_env: a pool worker (kitlib/tooldeps/pool_worker.py) replaces its environment for each
    unit, so tests/__init__.py's marker would not reach it)."""
    names, prefixes = session_env_names()
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in names and not k.upper().startswith(prefixes)}
    return kuser.fence_env(env)


def units_env(fake):
    """The environment the units of a pass get: child_env() with KIT_FAKE_GIT `fake`. selfrun
    drops the give back's offer from what the units inherit."""
    env = child_env()
    env['KIT_FAKE_GIT'] = fake
    return env


def run_pass(args, top, cfg, jobs, fake, out, hold, give_back=None, record_times=True,
             counted=None):
    """One run of `args` (FULL or test modules) with the units' KIT_FAKE_GIT set to `fake`
    (units_env), writing to `out`; its exit status. `give_back`: the pool's (drainer, or
    tiers.keep_for_next of it), None for none. `counted`: selfrun.count_units' answer for `args`,
    already asked (the pool then asks no count child)."""
    def say(line):
        out.write(line + '\n')
        out.flush()
    env = units_env(fake)
    label = 'full' if args is FULL else 'selected'
    if jobs == 1:
        start = time.time()
        cmd = args if args is FULL else ['-m', 'unittest'] + args
        out.flush()
        rc = spawn.call([sys.executable] + cmd, cwd=top, env=env)
        if hold is not None:
            say('kit_tests: %s' % hold.end())
        say('kit_tests: %s in %.0fs (%s, one process)' % (
            'OK' if rc == 0 else 'FAILED, exit %d' % rc, time.time() - start, label))
        return 0 if rc == 0 else 1
    modules = selftree.all_test_modules(top) if args is FULL else args
    common, _why = gitrepo.common_dir(top)
    times = os.path.join(common, selfrun.TIMES_FILE) if common else None
    counts, classes = counted if counted is not None else (None, None)
    rc, _summary = selfrun.run(modules, top, jobs, serial=cfg['serial'], out=out, label=label,
                               times_path=times, hint=cfg.get('long', ()), environ=env,
                               give_back=give_back, hold=hold, record_times=record_times,
                               skip_fails=cfg.get('skip_fails', ()), counts=counts,
                               classes=classes)
    return rc


if __name__ == '__main__':
    sys.exit(main())
