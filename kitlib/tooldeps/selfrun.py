"""Run the kit's own test modules in parallel worker processes (tools/kit_tests.py; docs/test-tools.md
"kit_tests"). selftree picks the modules; this runs them.

Units. One unit is one dotted test module (`tests.<pkg>.test_x`), or one test class of a split
module (below), run as `python -m unittest <unit>` would run it, with cwd the checkout root. Units
run in a pool of long-lived interpreters, one per job (kitlib.tooldeps.pool, pool_worker), each
put back between units (cwd, os.environ, sys.path, sys.argv, the modules the unit loaded, the
globals it changed); a worker whose unit timed out, crashed, was interrupted or left a process or
thread running is replaced. Each worker imports the units' top-level packages once
(unit_packages) before its first unit. A module whose source has a `KIT_TESTS_OWN_PROCESS = '<why>'` line
(OWN_PROCESS) runs as its own `python -m unittest <unit>` process instead, and the log says why.
Each unit gets its own temp folder: TEMP and TMP point at
`<run folder>/<n>`, so `tempfile` scratch, scratch lock stores (whose mutex names derive from their
path) and fixture repositories never meet another unit's. The run folder is made in the kit's temp folder
for the checkout (work_parent: kitlib.config.user.work_temp, on the checkout's own drive and out
of every session's TEMP), else, with a note, in the kit's default temp folder or this process's
temp folder, and removed at the end. An unfenced run that made its folder in the kit's temp folder
first removes the run folders there a killed run left over a day ago (prune_stale), and says how
many.

The process. Each pool worker, and each own-process unit (kitlib.pytests.runner.run_unit), is
created suspended, put in its own kill-on-close job, shielded from kills by pid, then resumed; a
unit still running after `timeout` seconds is killed with everything it started (for a pooled
unit, its worker too) and reported TIMEOUT, and a unit that ran no test is a FAIL. End the runner,
never a unit or a worker, by its pid. The log's `interpreters` line counts the workers started,
those replaced and why, the units run in their own process, and the process starts of the pooled
units (kitlib.tooldeps.pool `starts`: conhosts and nested jobs included), naming the HEAVIEST
units by them; a pooled unit's own line says `, N process starts` after its test count, before
its module name, when it started any (an own-process unit's are not counted). With kit_tests' opt-in
`--pid-hold` (never in the gate), the caller starts this process's pid hold before the units'
environment is copied (kitlib.pytests.runner.PidHoldRun: warmed, published), so no process a test
starts can be given the pid of a shell a stale taskkill is still aimed at; `run` writes whether
the hold lasted to the end.

Empty modules. Before the run one child process asks unittest's loader how many tests each module
holds (count_tests); a module with none (a helper discover's `test*.py` pattern catches, such as
tests/testutil.py) is listed and not run, since its unit would FAIL for running no test.

Order and splitting (plan_units). Up to `jobs` units run at once, longest first by the seconds
each unit took when last run (`times_path`: kit_tests keeps them in the repository's git common
dir, TIMES_FILE, shared by its work trees and never in a work tree); units with no record start
first, the selection table's `long` hint list in its order, then the largest source. A module
expected to take more than half the run's ideal wall time (expected seconds summed, over jobs;
at least SPLIT_FLOOR) runs one unit per test class (`python -m unittest <module>.<Class>`), so it
cannot floor the wall time alone; with no record, the `long` list names the modules to split.
Only a module the count child can split is split (no load_tests hook; every test a TestCase the
module holds by name). The modules in the selection table's `serial` list (tests/selection.json;
each entry carries why) are never split and run afterwards, one at a time, with nothing else of
this run beside them.

Ctrl+C. No new unit starts; the units already running end (the console's Ctrl+C reaches their
interpreters too) and are waited for, every worker is ended, the run folder is removed, the log and
summary are written, and the run exits INTERRUPTED (130).

Jobs. `default_jobs`: the logical processors the machine lock run holds (its MACHINE_LOCK_CPUMASK,
which the lock sets on the command it runs), else FALLBACK_JOBS with a note. The lock's affinity
already keeps every unit on those processors.

Giving back (`give_back`, kitlib.lock.giveback.drainer). Once every pooled unit has started, each
unit that ends leaves a worker with nothing more to run; run then calls give_back(the units still
running), so the lock run hands back the cpu slots past what they use and a queued run starts on
them at once. Not while a `serial` module is still to run (what it needs is not known). The units
never get the offer (GIVE_ENV is dropped from their environment), so no test gives away the run's
slots.

The log (`run`): a line per unit as it ends, the full output of every unit that did not pass, a
table per test package (result, tests, modules, summed unit seconds), then one summary line.
A unit that skipped tests says `N tests, K skipped` (unittest's `skipped=K`, `skipped_of`; the
package row and the summary end carry the sum), and nothing extra when none were. A module in the
selection table's `skip_fails` list FAILS when it skipped any test.
"""
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from kitlib.config import user as kuser
from kitlib.fs.rmtree import rmtree_force
from kitlib.proc import spawn
from kitlib.pytests import runner as unit_runner
from kitlib.tooldeps import pool as worker_pool

# The variable kitlib.lock.run (CPUMASK_ENV) sets on the command a lock run starts: a hex mask of
# the logical processors the run holds. Named here, not imported, so this module does not load the
# lock; tests/tooldeps checks the two names agree.
CPUMASK_ENV = 'MACHINE_LOCK_CPUMASK'
# The variable kitlib.lock.giveback (GIVE_ENV) offers a give back with; units never inherit it.
GIVE_ENV = 'MACHINE_LOCK_GIVE_BACK'
FALLBACK_JOBS = 2
UNIT_TIMEOUT = 300
COUNT_TIMEOUT = 600
INTERRUPTED = 130           # the exit status of a run stopped by Ctrl+C
POLL = 0.5                  # seconds between checks for Ctrl+C while units run
# The child count_tests starts: unittest's own loader, so a module whose TestCases come from an
# import (`from x import *`), a base class or load_tests counts as they will run. A module that
# fails to import loads as a failing test, so it is run and its unit reports why.
# The same child also names each module's test classes, in load order, when the module can be run
# class by class: no load_tests hook, and every test is a TestCase of a class the module holds
# under its own name (so `python -m unittest <module>.<Class>` loads exactly those tests); else
# null.
_COUNT_MARK = 'kit-tests-count: '
_CLASSES_MARK = 'kit-tests-classes: '
_COUNT = '\n'.join([
    'import json, sys, unittest',
    'def walk(s):',
    '    for t in s:',
    '        if isinstance(t, unittest.TestSuite):',
    '            yield from walk(t)',
    '        else:',
    '            yield t',
    'out, cls = {}, {}',
    'for m in sys.argv[1:]:',
    '    try:',
    '        s = unittest.defaultTestLoader.loadTestsFromName(m)',
    '        out[m] = s.countTestCases()',
    '    except Exception:',
    '        out[m] = None',
    '        continue',
    '    try:',
    '        mod, names = sys.modules[m], []',
    '        ok = not hasattr(mod, "load_tests")',
    '        for t in walk(s):',
    '            c = type(t)',
    '            if not isinstance(t, unittest.TestCase) or getattr(mod, c.__name__, None) is not c:',
    '                ok = False',
    '                break',
    '            if c.__name__ not in names:',
    '                names.append(c.__name__)',
    '        cls[m] = names if ok else None',
    '    except Exception:',
    '        cls[m] = None',
    'print(%r + json.dumps(out))' % _COUNT_MARK,
    'print(%r + json.dumps(cls))' % _CLASSES_MARK,
])
# Splitting and ordering (plan_units). A module whose expected seconds pass SPLIT_SHARE of the
# run's ideal wall time (expected seconds summed, over jobs), and at least SPLIT_FLOOR, runs one
# unit per test class, so no single unit floors the wall time; units start longest first.
SPLIT_FLOOR = 60.0
SPLIT_SHARE = 0.5
TIMES_FILE = 'kit-tests-times.json'
# A test module that must not share an interpreter with other units says so, and why, in its own
# source: `KIT_TESTS_OWN_PROCESS = '<why>'`; its units run as `python -m unittest <unit>` alone.
# unittest's summary line ends `OK (skipped=N)` or `FAILED (failures=1, skipped=N)`.
SKIPPED = re.compile(r'^(?:OK|FAILED) \(.*?\bskipped=(\d+)', re.M)
OWN_PROCESS = re.compile(r'''^KIT_TESTS_OWN_PROCESS\s*=\s*(['"])(.+?)\1''', re.M)


def default_jobs(environ=None):
    """(jobs, where the number came from)."""
    env = os.environ if environ is None else environ
    raw = env.get(CPUMASK_ENV)
    if raw:
        try:
            n = bin(int(raw, 16)).count('1')
        except ValueError:
            n = 0
        if n:
            return n, 'the %d logical processor(s) the lock run holds (%s)' % (n, CPUMASK_ENV)
        return FALLBACK_JOBS, '%s=%r is not a processor mask; %d' % (CPUMASK_ENV, raw, FALLBACK_JOBS)
    return FALLBACK_JOBS, 'not under a machine lock run (no %s); %d' % (CPUMASK_ENV, FALLBACK_JOBS)


def skipped_of(text):
    """The tests a unit skipped, from unittest's own summary in its output (`OK (skipped=2)`,
    `FAILED (failures=1, skipped=2)`), runtime `skipTest()` included; 0 when it has none."""
    return sum(int(x) for x in SKIPPED.findall(text or ''))


def package_of(module):
    """'tests.lock.test_api' -> 'lock'; a top-level 'tests.test_x' -> '(tests)'."""
    parts = module.split('.')
    return parts[1] if len(parts) > 2 else '(tests)'


def unit_packages(modules):
    """The top-level packages `modules` are in ('tests.lock.test_api' -> 'tests'), sorted: what a
    pool worker imports before its first unit (pool_worker.py)."""
    return sorted({m.split('.')[0] for m in modules if '.' in m})


def unit_env(base_env, folder):
    """The unit's environment: `base_env` with TEMP and TMP set to its own folder."""
    env = dict(base_env)
    env['TEMP'] = env['TMP'] = folder
    return env


def git_direct_env(base_env):
    """(environment, note): `base_env` with Git for Windows' own git.exe first on PATH when its
    `git` is the `<git>\\cmd\\git.exe` launcher, which starts that git.exe as a second process for
    every call; else `base_env` unchanged and note None. Git Bash already puts it first."""
    key = next((k for k in base_env if k.upper() == 'PATH'), None)
    if key is None or os.name != 'nt':
        return base_env, None
    found = shutil.which('git', path=base_env[key])
    if not found:
        return base_env, None
    here = os.path.dirname(os.path.abspath(found))
    if os.path.basename(here).lower() != 'cmd':
        return base_env, None
    direct = os.path.join(os.path.dirname(here), 'mingw64', 'bin')
    if not os.path.isfile(os.path.join(direct, 'git.exe')):
        return base_env, None
    env = dict(base_env)
    env[key] = direct + os.pathsep + base_env[key]
    return env, 'git: %s first on PATH, not the %s launcher' % (direct, found)


def work_parent(root):
    """The folder a run's folder is made in: the kit's temp folder for the checkout `root`
    (kitlib.config.user.work_temp, the rule the gate logs follow too), so a unit's temp files,
    fixture repositories and copies stay on the checkout's own drive."""
    return kuser.work_temp(root)


# A run folder's name, and a direct test run's fixture folder's (tests/gitfixture.py
# fixture_parent): the two prune_stale removes once STALE_AFTER seconds old.
RUN_PREFIX = 'kit-tests-'
DIRECT_PREFIX = 'kit-direct-'
STALE_AFTER = 24 * 3600
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def make_work(root):
    """(the run folder, note): a new RUN_PREFIX folder in work_parent(root), made if missing;
    else, with a note saying why, one in the kit's default temp folder or this process's
    (kuser.make_temp)."""
    work, note = kuser.make_temp(root, RUN_PREFIX)
    return work, note and 'the run folder %s' % note


def _plain_dir(path):
    """Whether `path` is a folder itself: not a link, junction or other reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or getattr(st, 'st_file_attributes', 0) & _REPARSE:
        return None
    return st


def prune_stale(parent, now=None, after=STALE_AFTER):
    """(removed, kept): remove the RUN_PREFIX and DIRECT_PREFIX folders directly in `parent`
    whose own modification time is at least `after` seconds before `now`, left by a run that was
    killed or crashed: nothing else sweeps the kit's temp folder. Each is first claimed by
    renaming it to `<name>.stale-<pid>` (one whose removal stopped part way keeps its first
    name); Windows refuses that while any process has a file open in it, and such a folder is
    counted kept and left whole. Links, junctions and
    other reparse points are never followed or removed, whether they are the entry or inside it
    (shutil.rmtree removes a link inside, not what it points at). A `parent` that cannot be
    listed is (0, 0)."""
    now = time.time() if now is None else now
    removed = kept = 0
    try:
        names = sorted(e.name for e in os.scandir(parent))
    except OSError:
        return 0, 0
    for name in names:
        if not name.startswith((RUN_PREFIX, DIRECT_PREFIX)):
            continue
        path = os.path.join(parent, name)
        st = _plain_dir(path)
        if st is None or now - st.st_mtime < after:
            continue
        # A claimed folder whose removal stopped part way is claimed again under its first name.
        claimed = os.path.join(parent, '%s.stale-%d' % (name.split('.stale-')[0], os.getpid()))
        if os.path.normcase(claimed) == os.path.normcase(path):
            claimed += '-again'
        try:
            os.rename(path, claimed)
        except OSError:
            kept += 1
            continue
        if _plain_dir(claimed) is None:
            kept += 1
            continue
        rmtree_force(claimed, ignore_errors=True)
        removed += 1
    return removed, kept


def _source(root, module):
    return os.path.join(root, *module.split('.')) + '.py'


def _weight(root, module):
    try:
        return os.path.getsize(_source(root, module))
    except OSError:
        return 0


def own_process(root, module):
    """Why `module` must run in its own process, not a pool worker: its OWN_PROCESS line
    (`KIT_TESTS_OWN_PROCESS = '<why>'` at the start of a line of its source); else None."""
    try:
        with open(_source(root, module), encoding='utf-8', errors='replace') as fh:
            m = OWN_PROCESS.search(fh.read())
    except OSError:
        return None
    return m.group(2) if m else None


def count_tests(root, modules, environ=None, timeout=COUNT_TIMEOUT):
    """{module: the tests unittest's loader finds in it}, loaded in one child process with cwd
    `root` as each unit will load it, so this process imports none of them. A module missing
    from the answer (it could not be counted, or the child failed) is unknown: run it."""
    return count_units(root, modules, environ, timeout)[0]


def count_units(root, modules, environ=None, timeout=COUNT_TIMEOUT):
    """(count_tests' answer, {module: [its test classes, in load order]}) from one child; a
    module missing from the second cannot be split by class."""
    if not modules:
        return {}, {}
    env = dict(os.environ if environ is None else environ)
    try:
        r = spawn.run([sys.executable, '-c', _COUNT] + list(modules), cwd=root, env=env,
                      capture_output=True, text=True, encoding='utf-8', errors='replace',
                      timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    counts = classes = None
    for line in reversed(r.stdout.splitlines()):
        for mark in (_COUNT_MARK, _CLASSES_MARK):
            if line.startswith(mark):
                try:
                    answer = json.loads(line[len(mark):])
                except ValueError:
                    answer = {}
                if mark == _COUNT_MARK and counts is None:
                    counts = {m: n for m, n in answer.items() if isinstance(n, int)}
                elif mark == _CLASSES_MARK and classes is None:
                    classes = {m: list(c) for m, c in answer.items()
                               if isinstance(c, list) and all(isinstance(x, str) for x in c)}
    return counts or {}, classes or {}


def module_of(unit):
    """The test module a unit belongs to: the unit itself, or `<module>` of `<module>.<Class>`
    (a module's last part starts `test`, discover's pattern; a class's does not)."""
    head, _, last = unit.rpartition('.')
    return unit if last.startswith('test') or not head else head


def load_times(path):
    """{unit: seconds it took when last run} from `path`; {} when missing or unreadable."""
    if not path:
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def save_times(path, results, split):
    """Merge this run's unit seconds into `path` (re-read first, so a concurrent run's other
    modules survive; written whole through a temp file). A split module's own entry becomes the
    sum of its classes when all of them ran. Never fails the run."""
    if not path:
        return
    times = load_times(path)
    took = {}
    for (unit, _, _), status, _ran, secs, _ in results:
        if status in ('OK', 'FAILED'):
            took[unit] = round(secs, 1)
    times.update(took)
    for m, classes in split.items():
        parts = [took.get('%s.%s' % (m, c)) for c in classes]
        if parts and all(p is not None for p in parts):
            times[m] = round(sum(parts), 1)
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(times, fh, indent=0, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def plan_units(modules, root, jobs, classes=None, times=None, hint=()):
    """(units in start order, {module: [classes]} for each split module, how the order was
    picked). A module is split by class (when it has two or more) if its expected seconds pass
    max(SPLIT_FLOOR, SPLIT_SHARE * expected total / jobs), or, with no record of it, when the
    `hint` list names it. Expected seconds: the recorded `times` of the unit, else of its module
    (shared among its classes). Units with no record start first (hinted ones, then the largest
    source), then the rest longest first."""
    classes = classes or {}
    times = times or {}
    hint = list(hint)

    def expected(m):
        if m in times:
            return times[m]
        parts = [times['%s.%s' % (m, c)] for c in classes.get(m) or ()
                 if '%s.%s' % (m, c) in times]
        return sum(parts) if parts else None
    est = {m: expected(m) for m in modules}
    known = [e for e in est.values() if e is not None]
    limit = max(SPLIT_FLOOR, SPLIT_SHARE * sum(known) / max(1, jobs)) if known else None
    units, split, unit_est = [], {}, {}
    for m in modules:
        cs = classes.get(m) or []
        e = est[m]
        long_ = (e > limit) if (e is not None and limit is not None) else (e is None and m in hint)
        if len(cs) > 1 and long_:
            split[m] = cs
            for c in cs:
                u = '%s.%s' % (m, c)
                units.append(u)
                unit_est[u] = times.get(u, e / len(cs) if e is not None else None)
        else:
            units.append(m)
            unit_est[m] = e

    def key(u):
        e = unit_est[u]
        if e is None:
            m = module_of(u)
            return (0, hint.index(m) if m in hint else len(hint), -_weight(root, m), u)
        return (1, 0, -e, u)
    units.sort(key=key)
    how = ('longest first by recorded times' if known else
           'no recorded times: the hinted long modules, then the largest source, first')
    return units, split, how


def run(modules, root, jobs, serial=(), out=None, timeout=None, run_unit=None, environ=None,
        label='selected', counts=None, classes=None, times_path=None, hint=(), give_back=None,
        hold=None, record_times=True, skip_fails=()):
    """Run `modules` (checkout `root`), up to `jobs` at once, then the `serial` ones ({module:
    why}) one at a time; write the log to `out`; return (exit status 0, 1 or INTERRUPTED, the
    summary line). `counts` {module: tests} skips the count_tests child (tests use it), and
    `classes` {module: [classes]} goes with it. `times_path`: the recorded unit seconds plan_units
    orders and splits by, updated after the run; `hint`: the modules to split and start first
    while unrecorded. `give_back(units still running)`: called as the pool drains (Giving back).
    `hold`: the run's opted-in pid hold (kitlib.pytests.runner.PidHoldRun), already started by the
    caller before it copied `environ`, so its beacon reaches the units; its end line is written
    before the summary. None: no hold. `record_times` False: the times at `times_path` still
    order and split the run, but this run's are not written back (kit_tests --both-tiers'
    real-git pass, so the file keeps the landing tier's times). `skip_fails` ([module, why] pairs,
    the selection table's list): a listed module whose unit skipped any test is FAILED, since a
    skip voids the proof it exists for."""
    proof = dict(skip_fails)
    out = out or sys.stdout
    injected = run_unit
    timeout = timeout or UNIT_TIMEOUT
    base_env, git_note = git_direct_env(os.environ if environ is None else environ)
    base_env = {k: v for k, v in base_env.items() if k != GIVE_ENV}
    serial = dict(serial)
    say_lock = threading.Lock()
    done = []
    total = [len(modules)]

    def say(line):
        with say_lock:
            out.write(line + '\n')
            out.flush()

    if git_note:
        say('kit_tests: %s' % git_note)

    work, work_note = make_work(root)
    if work_note:
        say('kit_tests: %s' % work_note)
    elif not kuser.fenced():
        removed, kept = prune_stale(os.path.dirname(work))
        if removed or kept:
            say('kit_tests: removed %d stale run folder(s) from %s (%d older one(s) left: in use)'
                % (removed, os.path.dirname(work), kept))
    counter = itertools.count(1)
    counter_lock = threading.Lock()
    workers = None if injected else worker_pool.WorkerPool(root, work, unit_env(base_env, work),
                                                           preload=unit_packages(modules))
    own = {}                # module -> why it runs in its own process

    def one(module):
        with counter_lock:
            n = next(counter)
        folder = os.path.join(work, str(n))
        if injected:
            run_unit = injected
        elif module_of(module) in own:
            run_unit = unit_runner.run_unit
        else:
            run_unit = workers.run_unit
        try:
            os.makedirs(folder)
            r = run_unit((module, 1, 1), False, timeout, root=root,
                         cmd=[sys.executable, '-m', 'unittest', module],
                         env=unit_env(base_env, folder))
        except Exception as e:      # noqa: BLE001 - a runner bug FAILs its unit, named
            r = ((module, 1, 1), 'FAILED', 0, 0.0, 'kit_tests: the runner failed on this unit: %r\n'
                 % (e,))
        finally:
            rmtree_force(folder, ignore_errors=True)
        unit_id, status, ran, secs, text = r
        starts = workers.starts(module) if workers is not None else None    # pooled units only
        skipped = skipped_of(text)
        why = proof.get(module_of(module))
        if skipped and why and status == 'OK':
            status = 'FAILED'
            r = (unit_id, status, ran, secs,
                 text + '\nkit_tests: %s skipped %d test(s); it is a proof module (tests/'
                 'selection.json skip_fails), so a skip FAILS: %s\n' % (module, skipped, why))
        with say_lock:
            done.append(r)
            out.write('  [%d/%d] %-7s %6.0fs %4d tests%s%s  %s%s\n' % (
                len(done), total[0], status, secs, ran,
                ', %d skipped' % skipped if skipped else '',
                ', %d process starts' % starts if starts else '', module,
                ' (serial)' if module in serial else ''))
            out.flush()
        return r

    start = time.time()
    interrupted = False
    alone = []
    split = {}
    jobs = max(1, jobs)
    pool = None
    try:
        if counts is None:
            counts, classes = count_units(root, modules, unit_env(base_env, work))
        empty = [m for m in modules if counts.get(m) == 0]
        modules = [m for m in modules if m not in empty]
        pooled, split, how = plan_units([m for m in modules if m not in serial], root, jobs,
                                        classes, load_times(times_path), hint)
        alone = [m for m in modules if m in serial]
        jobs = max(1, min(jobs, len(pooled) or 1))
        total[0] = len(pooled) + len(alone)
        for m in empty:
            say('kit_tests: (no tests) %s defines no test; not run' % m)
        if workers is not None:
            for m in modules:
                why = own_process(root, m)
                if why:
                    own[m] = why
                    say('kit_tests: own process: %s: %s' % (m, why))
        if split:
            say('kit_tests: %d module(s) as %d unit(s), %d at a time (%s), then %d serial' % (
                len(modules), total[0], jobs, how, len(alone)))
            for m in sorted(split):
                say('kit_tests: split by class: %s (%d classes)' % (m, len(split[m])))
        else:
            say('kit_tests: %d module(s), %d at a time, then %d serial' % (
                total[0], jobs, len(alone)))
        if pooled:
            pool = ThreadPoolExecutor(max_workers=jobs)
            pending = {pool.submit(one, m) for m in pooled}
            asked = None
            while pending:      # a timed wait, so Ctrl+C is seen within POLL seconds
                finished, pending = wait(pending, timeout=POLL, return_when=FIRST_COMPLETED)
                for f in finished:
                    f.result()
                # pending holds the queued units too: at `jobs` or under, every one left runs.
                if (give_back is not None and not alone and pending and len(pending) <= jobs
                        and len(pending) != asked):
                    asked = len(pending)
                    give_back(asked)
            pool.shutdown()
        for m in alone:
            say('kit_tests: serial %s: %s' % (m, serial[m]))
            one(m)
    except KeyboardInterrupt:
        interrupted = True
        say('kit_tests: interrupted (Ctrl+C): no new unit starts; waiting for the running ones')
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)        # running units end through their own processes
        if workers is not None:
            workers.close()
        rmtree_force(work, ignore_errors=True)
    if workers is not None:
        say(interpreters_line(workers.stats(), done, own, workers.starts()))
        for name, why in workers.why_replaced():
            say('kit_tests: pool worker replaced after %s: %s' % (name, why))
    if hold is not None:
        say('kit_tests: %s' % hold.end())
    wall = time.time() - start
    if record_times:
        save_times(times_path, done, split)
    return report(done, wall, jobs, len(alone), out, label,
                  not_run=(total[0] - len(done)) if interrupted else None)


HEAVIEST = 3        # the units interpreters_line names by their process starts


def interpreters_line(stats, done, own, starts=None):
    """The log line saying how many interpreters ran the units: the pool workers started, the
    ones ended early and why (WorkerPool.stats), and the units that ran in their own process.
    `starts` {unit: process starts} (WorkerPool.starts): their sum, counted in the pooled units'
    jobs (conhosts and nested jobs included; the pool workers themselves not), and the HEAVIEST
    units by it."""
    started, ended = stats
    alone = sum(1 for r in done if module_of(r[0][0]) in own)
    why = ', '.join('%d %s' % (n, w) for w, n in sorted(ended.items()))
    line = ('kit_tests: interpreters: %d pool worker(s) ran %d unit(s)%s; %d unit(s) in their own '
            'process' % (started, len(done) - alone,
                         ' (%d replaced: %s)' % (sum(ended.values()), why) if ended else '', alone))
    if starts:
        top = sorted(((n, u) for u, n in starts.items() if n), key=lambda x: (-x[0], x[1]))
        line += '; %d process start(s) in %d pooled unit(s)%s' % (
            sum(starts.values()), len(starts),
            ' (most: %s)' % ', '.join('%s %d' % (u, n) for n, u in top[:HEAVIEST]) if top else '')
    return line


def report(results, wall, jobs, n_serial, out, label='selected', not_run=None):
    """Write the failures, the per-package table and the summary; (exit status, summary).
    `not_run` set: the run was interrupted with that many modules not run."""
    results = sorted(results, key=lambda r: r[0][0])
    bad = [r for r in results if r[1] != 'OK']
    for (module, _, _), status, ran, secs, text in bad:
        out.write('\n==== %s (%d tests, %.0fs, %s) ====\n' % (module, ran, secs, status))
        out.write(text if text.endswith('\n') else text + '\n')
    pkgs = {}
    for (unit, _, _), status, ran, secs, text in results:
        p = pkgs.setdefault(package_of(unit), {'ok': True, 'tests': 0, 'mods': set(), 'secs': 0.0,
                                               'skipped': 0})
        p['ok'] = p['ok'] and status == 'OK'
        p['tests'] += ran
        p['skipped'] += skipped_of(text)
        p['mods'].add(module_of(unit))
        p['secs'] += secs
    n_modules = sum(len(p['mods']) for p in pkgs.values())
    for p in pkgs.values():
        p['mods'] = len(p['mods'])
    out.write('\nkit_tests: per package (seconds summed over its modules):\n')
    width = max([len(p) for p in pkgs] + [8]) + 6
    for name in sorted(pkgs):
        p = pkgs[name]
        out.write('  %s %-6s %5d tests %3d module(s) %7.0fs%s\n' % (
            ('tests/' + name).ljust(width), 'OK' if p['ok'] else 'FAILED', p['tests'], p['mods'],
            p['secs'], '  %d skipped' % p['skipped'] if p['skipped'] else ''))
    tests = sum(r[2] for r in results)
    how = '%.0fs (%s, %d jobs, %d serial): %d tests in %d module(s), %d package(s)' % (
        wall, label, jobs, n_serial, tests, n_modules, len(pkgs))
    skipped = sum(p['skipped'] for p in pkgs.values())
    if skipped:
        how += ', %d skipped' % skipped
    failed = ', '.join('%s (%s)' % (r[0][0], r[1]) for r in bad)
    if not_run is not None:
        rc = INTERRUPTED
        summary = 'kit_tests: INTERRUPTED, exit %d in %s; %d module(s) not run' % (
            INTERRUPTED, how, not_run)
        if bad:
            summary += '; failed: ' + failed
    elif bad:
        rc = 1
        summary = 'kit_tests: FAILED, exit 1 in %s; failed: %s' % (how, failed)
    else:
        rc = 0
        summary = 'kit_tests: OK in %s' % how
    out.write(summary + '\n')
    out.flush()
    return rc, summary
