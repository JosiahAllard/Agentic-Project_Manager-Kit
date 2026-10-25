"""The parent side of the parallel tools-test runner. Ported from the PM kit's
tools/pytests.py; every function takes the checkout root explicitly (there is no module ROOT) and
writes its log to the stream it is given.

A test module is a `test*.py` of the tools folder (`.kit.json` "repo".tools_dir) that
kitlib.tooldeps' scan counts as one; a name may also be `module.Class` or `module.Class.test`.

Units. What `python -m unittest discover` did, split up: a unit is one test module, or one
class-shard of a module in `shards` (whole TestCase classes round-robin, so class and module
fixtures stay intact), or - with `split_classes` - one TestCase class of every other module (the
module is imported in this process to list them; one that fails to import stays whole). Every unit
runs in its own new `python <kit>/tools/pytests.py --child ...` process - never a reused worker,
because tests chdir, patch os.environ and swap module globals - with cwd the checkout root and the
tools folder first on sys.path, as discover had it. Up to `jobs` units run at once (threads that
only wait on their child); units in `serial` (a module or a class) run afterwards, one at a time.

The log (`run`): each unit's full output in sorted unit order under `==== <unit> (<n> tests, <s>s,
OK|FAILED|TIMEOUT) ====`, a timing table, then
    pysel floor N        the static `def test_` count of the names run (a single test id: 1;
                         `module.Class`: the class's own test_ methods)
    Ran N tests in Xs    the sum over every unit
    OK | FAILED (units: a, b)
A unit that ran no test FAILS (named): an empty run proves nothing. Each skipped test adds a
`PY SKIP <id> <reason>` line to its unit's output after the unittest summary. Nothing is written
to disk: children are read through pipes.

The kill. A unit still running after `unit_timeout` seconds is killed and reported TIMEOUT (a
FAIL, named). Each child is started by kitlib.proc.job.Job.start: CREATED SUSPENDED, put in its
own job (kill-on-close, no breakaway), shielded while still suspended, then resumed - so not one
of its instructions, nor those of a launcher-type python.exe that re-executes the real
interpreter, runs outside the job, and TerminateJobObject ends exactly that job. No pid is ever
looked up or killed by number. The same kill runs when a unit exited but a leftover process of its
own still holds its output pipe open after LINGER seconds.

The shield (`shield_unit`, kitlib.proc.shield): the unit's own process denies PROCESS_TERMINATE to
Everyone for its whole life, so a stale `taskkill /PID <n> /T /F` meant for a dead shell cannot
end it; the job's kills need no access to the process and still end it. The runner never lifts
it: when the runner ends, its jobs close and KILL_ON_JOB_CLOSE ends every unit and what it
started - so end the runner, never a unit, by its pid. Only the unit's own process is shielded,
not what it starts. A unit that cannot be shielded runs unshielded, with a note.

The pid hold (`PidHoldRun`, kitlib.proc.shield.PidHold), opt-in only (PID_HOLD_FLAG, `--pid-hold`;
off by default; kitlib.gate.steps refuses it in a gate step). The shield cannot cover what a unit
starts: a test's git, made a moment after a shell died, can be given that shell's pid, and the
second stale taskkill then ends it with exit 1 and no output - the same shape as `merge-base
--is-ancestor`'s NO (kitlib.reviewpack.tips' GOTCHA). Unheld, the
ancestry answer at least is no longer misread: kitlib.git.refs.is_ancestor asks `rev-list -n 1`
(and confirms a NO with `merge-base --independent`), whose answers all exit 0, and gives no answer
for a killed git; other exit-1 answers (a missing
ref under `--quiet`, `merge-base` finding none) still look like a kill. Opted in, `run`
(and tools/kit_tests.py) keeps this process's hold - warmed for WARM seconds, then published -
before the first unit starts and for the rest of its life: no pid of this user's processes is
reused until 3 s after its process exits, so the stale kill names an exited process and ends
nothing. A run that inherits a running hold (an ancestor published one) makes none. A hold that
cannot be made, or that stops during the run, leaves the run going unheld, and the log says
`pid hold: FAILED ... NOT held` with the reason, at the start and again before the summary. Its
cost: docs/test-tools.md "The pid shield".

`.kit.json` "tool_tests" keys read here (the section is shared with kitlib.tooldeps; the kit schema,
kitlib/config/schema.py, checks their types; `settings` adds the checks below):
  shards         {"test_x": 4}        modules split into N class-shards (no `.` in a name)
  serial         ["test_x", "test_y.C"]  units run one at a time after the pool
  split_classes  false                true: every other module runs one unit per TestCase class
  jobs_max       6                    default jobs = min(jobs_max, max(2, cpus // jobs_divisor))
  jobs_divisor   4
  jobs_env       null                 an environment variable that, when set to an integer,
                                      overrides the default jobs (--jobs overrides both). Kept as
                                      the PM kit had it; it sets a job count, never the root or
                                      the configuration (a tool never takes those from env).
  unit_timeout   3600                 seconds before a unit is killed (TIMEOUT)
"""
import ast
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from kitlib.config import ConfigError, load as load_config
from kitlib.config import schema
from kitlib.config.types import check as check_type
from kitlib.git import cmd as gitcmd
from kitlib.proc import shield as pid_shield
from kitlib.proc import win32
from kitlib.proc.job import Job, JobError

SECTION = 'tool_tests'
KEYS = ('shards', 'serial', 'split_classes', 'jobs_max', 'jobs_divisor', 'jobs_env', 'unit_timeout')
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENTRY = os.path.join(KIT_ROOT, 'tools', 'pytests.py')    # the child's command
LINGER = 30                        # seconds a finished unit's leftovers may hold its pipe
CREATE_SUSPENDED = win32.CREATE_SUSPENDED
RAN = re.compile(r'^Ran (\d+) tests? in ', re.M)
TEST_DEF = re.compile(r'^\s*def test_', re.M)
FLOOR_LINE = 'pysel floor %d'
SKIP_LINE = 'PY SKIP %s %s'


def settings(kit=None, data=None):
    """The runner's keys of the "tool_tests" section: from `kit` (a kitlib.config Config) or `data`
    (a raw section, checked against the kit schema here), over the schema's defaults, with
    `serial` a frozenset. Raises ConfigError."""
    w = '.kit.json "%s"' % SECTION
    if data is None:
        if kit is None:
            raise ValueError('settings needs a loaded configuration or the section data')
        sec = kit.section(SECTION)
    else:
        if not isinstance(data, dict):
            raise ConfigError('%s must be an object' % w)
        data = {k: v for k, v in data.items() if v is not None}
        check_type(schema.section_type(SECTION), data, w)
        sec = dict(schema.defaults(SECTION))
        sec.update(data)
    out = {k: sec.get(k) for k in KEYS}
    out['shards'] = dict(out['shards'] or {})
    if any('.' in k for k in out['shards']):
        raise ConfigError('%s.shards must map module names (no `.`) to whole numbers >= 1' % w)
    if any(not x for x in out['serial'] or ()):
        raise ConfigError('%s.serial must be a list of module or module.Class names' % w)
    if out['jobs_env'] is not None and not out['jobs_env'].strip():
        raise ConfigError('%s.jobs_env must be null or an environment variable name' % w)
    out['serial'] = frozenset(out['serial'] or ())
    return out


DEFAULTS = settings(data={})


# --- units --------------------------------------------------------------------------------------

def _module_file(module, dirs):
    for d in dirs:
        p = os.path.join(d, module + '.py')
        if os.path.isfile(p):
            return p
    return None


def classes_of(module, dirs):
    """The `module.Class` ids of a test module's TestCase classes, in load order, or [module]
    when it does not import (its own process then reports the error) or has none. Imports it in
    this process with `dirs` first on sys.path."""
    saved = list(sys.path)
    sys.path[:0] = [d for d in dirs if d]
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(module)
    except Exception:  # noqa: BLE001 - reported by the run itself
        return [module]
    finally:
        sys.path[:] = saved
    classes = []

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            else:
                cid = t.id().rsplit('.', 1)[0]
                if cid not in classes:
                    classes.append(cid)
    walk(suite)
    failed = [c for c in classes if 'FailedTest' in c or 'ModuleImportFailure' in c]
    return [module] if failed or not classes else classes


def units(modules, cfg=None, dirs=()):
    """(name, shard, of) for each name: a module in `shards` split into that many, with
    `split_classes` every other whole module split into its classes (`classes_of`)."""
    cfg = cfg or DEFAULTS
    out = []
    for m in modules:
        n = cfg['shards'].get(m, 1) if '.' not in m else 1
        if n > 1:
            out.extend((m, i, n) for i in range(1, n + 1))
        elif cfg['split_classes'] and '.' not in m:
            out.extend((c, 1, 1) for c in classes_of(m, dirs))
        else:
            out.append((m, 1, 1))
    return out


def unit_name(unit):
    m, i, n = unit
    return m if n == 1 else '%s[%d/%d]' % (m, i, n)


def _serial(unit, cfg):
    return unit[0] in cfg['serial'] or unit[0].split('.')[0] in cfg['serial']


def child_command(unit, audit, root, path=None):
    """The argv of a unit's process: the kit's entry point in --child mode on checkout `root`."""
    m, i, n = unit
    cmd = [sys.executable, ENTRY, '--child', m, '-C', root]
    if path:
        cmd += ['--path', path]
    if n > 1:
        cmd += ['--shard', '%d/%d' % (i, n)]
    return cmd + (['--audit'] if audit else [])


# --- one unit -----------------------------------------------------------------------------------

def process_dacl(handle):
    """(DACL pointer, security descriptor to LocalFree) of a process (kitlib.proc.shield; a seam
    the tests patch)."""
    return pid_shield.process_dacl(handle)


WARM = 3.0          # seconds the pid hold runs before the first unit (pids freed before it began)
PID_HOLD_FLAG = '--pid-hold'    # the opt-in of pytests.py and kit_tests.py; the gate refuses it
NOT_HELD = 'a stale taskkill can still end a process a test starts'


class PidHoldRun(object):
    """A test run's opted-in pid hold (PID_HOLD_FLAG; off by default, never in the gate).

    `start()` before the first unit: keeps this process's hold (kitlib.proc.shield.hold(), or
    `make()`) running, warmed for WARM seconds and published, or finds a running hold this process
    inherited (shield.inherited) and makes none. `end()` after the last unit says whether the run
    stayed held to its end. Each returns the one line to print, starting `pid hold: `. A hold that
    cannot be made, or stops during the run, never ends the run: the run goes on unheld, and both
    lines say `FAILED` and `NOT held` with the reason."""

    def __init__(self, make=None, inherited=None):
        self.make = make or pid_shield.hold
        self.inherited = inherited or pid_shield.inherited
        self.held = None            # the PidHold this run made and published
        self.borrowed = False       # an ancestor's running hold covers the run
        self.failed = None          # why no hold was made

    def start(self):
        try:
            if self.inherited():
                self.borrowed = True
                return 'pid hold: on (a running hold this process inherited)'
            held = self.make()
            held.warm(WARM)
            held.publish()
        except Exception as e:      # noqa: BLE001 - any failure is reported, never a crash
            self.failed = _why(e)
            return 'pid hold: FAILED, this run is NOT held (%s): %s' % (self.failed, NOT_HELD)
        self.held = held
        return 'pid hold: on (warmed %g s, published)' % WARM

    def end(self):
        if self.failed is not None:
            return 'pid hold: FAILED at the start, this run was NOT held (%s)' % self.failed
        if self.borrowed:
            try:
                still = self.inherited()
            except Exception as e:  # noqa: BLE001
                return 'pid hold: FAILED, cannot tell whether the inherited hold ran to the end (%s)' \
                    % _why(e)
            if not still:
                return ('pid hold: FAILED during the run, the inherited hold ended: units after it '
                        'ended were NOT held')
            return 'pid hold: held to the end of the run (inherited)'
        if self.held is not None and getattr(self.held, 'stopped', False):
            return ('pid hold: FAILED during the run, units after it stopped were NOT held (%s)'
                    % (getattr(self.held, 'error', None) or 'closed'))
        return 'pid hold: held to the end of the run'


def _why(e):
    text = str(e)
    return '%s: %s' % (type(e).__name__, text) if text else type(e).__name__


def shield_unit(proc):
    """Shield the unit's own process from kills by pid for its whole life (kitlib.proc.shield).
    Called by Job.start while the child is still suspended; raises OSError, having written
    nothing, when it cannot."""
    pid_shield.release(pid_shield.shield(proc, process_dacl))


def run_unit(unit, audit, timeout, path=None, root=None, cmd=None, env=None):
    """(unit, status, tests run, seconds, output). status: OK, FAILED or TIMEOUT. `env`: the
    child's environment (default: this process's)."""
    root = root or os.getcwd()
    cmd = cmd or child_command(unit, audit, root, path)
    t = time.time()
    try:
        job = Job(kill_on_close=True)
    except OSError as e:
        # Never run a unit that could not then be killed as a whole: FAIL it, named.
        return (unit, 'FAILED', 0, time.time() - t,
                'pytests: no job object for this unit (%s); not run\n' % e)
    notes = []

    def before_resume(p):
        try:
            shield_unit(p)
        except OSError as e:
            notes.append('pytests: could not shield %s from kills by pid (%s); it ran '
                         'unshielded' % (unit_name(unit), e))
    try:
        try:
            # Suspended until it is in its job (and shielded): nothing it runs, and nothing it
            # starts, is ever outside.
            proc = job.start(cmd, before_resume=before_resume, cwd=root, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        except JobError as e:
            # Job.start killed it through its own handle while it was still suspended.
            notes.append('pytests: could not place the child in its job (%s); it was killed' % e)
            return unit, 'FAILED', 0, time.time() - t, '\n'.join(notes) + '\n'
        except OSError as e:
            return unit, 'FAILED', 0, time.time() - t, 'pytests: could not start %s: %s\n' % (cmd, e)
        begun, lived = time.time(), None
        chunks = []
        reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
            lived = time.time() - begun
        except subprocess.TimeoutExpired:
            timed_out = True
            notes.append('pytests: TIMEOUT - %s still running after %ds; killed it and every '
                         'process it started' % (unit_name(unit), timeout))
            job.terminate()
            proc.wait()
        reader.join(LINGER)
        if reader.is_alive():
            notes.append('pytests: a process %s started still held its output %ds after it '
                         'exited; killed it' % (unit_name(unit), LINGER))
            job.terminate()
            reader.join()
        proc.stdout.close()
        proc.wait()
    finally:
        job.close()
    raw = chunks[0] if chunks else b''
    out = raw.decode('utf-8', 'replace').replace('\r\n', '\n')
    ran = sum(int(x) for x in RAN.findall(out))
    status = 'TIMEOUT' if timed_out else ('OK' if proc.returncode == 0 else 'FAILED')
    if status == 'FAILED' and not RAN.search(out):
        # No unittest summary: say how the process ended, so a kill from outside is told apart.
        rc = proc.returncode
        notes.append('pytests: %s ran as pid %d and exited %d (0x%08X) %.2fs after it started, '
                     'with no unittest summary%s' % (
                         unit_name(unit), proc.pid, rc, rc & 0xFFFFFFFF, lived,
                         '' if raw else ' and no output at all: no test ran. Exit code 1 with '
                         'no output is what a kill from outside (TerminateProcess, taskkill /F) '
                         'leaves; see kitlib.pytests.runner, The shield'))
    elif status == 'OK' and not ran:
        # A unit that ran nothing proves nothing.
        status = 'FAILED'
        notes.append('pytests: %s ran no test; an empty unit is a FAIL' % unit_name(unit))
    if notes:
        out += '\n' + '\n'.join(notes) + '\n'
    return unit, status, ran, time.time() - t, out


# --- the run ------------------------------------------------------------------------------------

def floor_of(names, dirs):
    """The static test count of `names`: a module's `def test_` lines, a `module.Class`'s own
    test_ methods, 1 for a `module.Class.test` id. A file that cannot be read counts 0."""
    n = 0
    for name in names:
        parts = name.split('.')
        if len(parts) >= 3:
            n += 1
            continue
        path = _module_file(parts[0], dirs)
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except (OSError, TypeError):
            continue
        if len(parts) == 1:
            n += len(TEST_DEF.findall(text))
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == parts[1]:
                n += sum(1 for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and f.name.startswith('test_'))
    return n


def tools_path(root, tools_rel=None):
    """The checkout's tools folder (`tools_rel`, or its `.kit.json` "repo".tools_dir)."""
    if tools_rel is None:
        tools_rel = load_config(root).tools_dir
    return os.path.join(root, *tools_rel.replace('\\', '/').strip('/').split('/'))


def run(modules, root, audit=False, jobs=None, timeout=None, out=None, runner=None, path=None,
        cfg=None, tools_rel=None, hold=None):
    """Run the units of `modules` (checkout `root`), write the log to `out`, return the exit
    status (0 or 1). `path`: a folder of test modules searched before the tools folder. `hold`:
    a PidHoldRun (the opt-in --pid-hold) or None (no hold); its start line is written before any
    unit starts, its end line before the floor line."""
    out = out or sys.stdout
    if hold is not None:
        out.write('pytests: %s\n' % hold.start())
        out.flush()
    runner = runner or run_unit
    cfg = cfg or DEFAULTS
    timeout = timeout or cfg['unit_timeout']
    dirs = [d for d in (path, tools_path(root, tools_rel)) if d]
    todo = units(modules, cfg, dirs)
    pooled = [u for u in todo if not _serial(u, cfg)]
    serial = [u for u in todo if _serial(u, cfg)]
    jobs = jobs or min(max(1, len(pooled)), default_jobs(cfg=cfg))
    # Longest first (source size as the guess), so the slowest unit does not start last.
    pooled.sort(key=lambda u: -_weight(u, dirs))
    start = time.time()

    def one(u):
        try:
            return runner(u, audit, timeout, path=path, root=root)
        except Exception as e:      # noqa: BLE001  (a runner bug FAILs its unit, named, not the log)
            return u, 'FAILED', 0, 0.0, 'pytests: the runner itself failed on this unit: %r\n' % (e,)

    results = []
    if pooled:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(one, pooled))
    results += [one(u) for u in serial]
    wall = time.time() - start
    results.sort(key=lambda r: (r[0][0], r[0][1]))
    for unit, status, ran, secs, text in results:
        out.write('==== %s (%d tests, %ds, %s) ====\n' % (unit_name(unit), ran, secs, status))
        out.write(text if text.endswith('\n') else text + '\n')
    out.write('\nunit timings (%d units, %d jobs, %s):\n' % (len(results), jobs,
                                                           'audit' if audit else 'no audit'))
    for unit, status, ran, secs, _ in sorted(results, key=lambda r: -r[3]):
        out.write('  %7.1fs  %5d  %-8s %s\n' % (secs, ran, status, unit_name(unit)))
    bad = [unit_name(r[0]) for r in results if r[1] != 'OK']
    if hold is not None:
        out.write('pytests: %s\n' % hold.end())
    out.write(FLOOR_LINE % floor_of(modules, dirs) + '\n')
    out.write('Ran %d tests in %.1fs\n' % (sum(r[2] for r in results), wall))
    out.write(('FAILED (units: %s)\n' % ', '.join(bad)) if bad else 'OK\n')
    out.flush()
    return 1 if bad else 0


def default_jobs(cpus=None, cfg=None, environ=None):
    """Conservative for a shared machine (other agents' gates and builds run beside this):
    `jobs_env` when that variable holds an integer (at least 1), else min(jobs_max, max(2,
    cpus // jobs_divisor)) - by default a quarter of the logical CPUs, at least 2 and at most 6."""
    cfg = cfg or DEFAULTS
    environ = os.environ if environ is None else environ
    if cfg['jobs_env']:
        try:
            return max(1, int(environ[cfg['jobs_env']]))
        except (KeyError, ValueError):
            pass
    return min(cfg['jobs_max'], max(2, (cpus or os.cpu_count() or 2) // cfg['jobs_divisor']))


def _weight(unit, dirs):
    m, _, n = unit
    try:
        return os.path.getsize(_module_file(m.split('.')[0], dirs)) / n
    except (OSError, TypeError):
        return 0


# --- --select: the changed paths ----------------------------------------------------------------

def stderr_cause(err):
    """The stderr line to quote: the first `fatal:`/`error:` line (a trailing `hint:` would hide
    it), else the last non-empty line, else 'no stderr'."""
    lines = [l.strip() for l in (err or '').splitlines() if l.strip()]
    return next((l for l in lines if l.startswith(('fatal:', 'error:'))), lines[-1] if lines else 'no stderr')


class Refused(Exception):
    """git could not say what changed; the message is the refusal to print."""


def changed_paths(base, root, run=gitcmd.subprocess_run):
    """The changed paths against the merge-base of `base` and HEAD in checkout `root`, plus
    untracked files. Raises Refused, naming the git command, when git fails or prints no
    merge-base."""

    def git(*args):
        try:
            r = run(['git'] + list(args), cwd=root, capture_output=True, text=True, encoding='utf-8',
                    errors='replace')
            out = r.stdout or ''
            why = None if r.returncode == 0 else 'exit %s: %s' % (r.returncode, stderr_cause(r.stderr))
            if why is None and args[:1] == ('merge-base',) and not out.strip():
                why = 'exit 0 but no merge-base printed'
        except (OSError, subprocess.SubprocessError) as e:
            out, why = '', '%s: %s' % (type(e).__name__, e)
        if why is not None:
            raise Refused('pytests: REFUSED - `git %s` failed (%s). The changed-file list picks the '
                          'tests; an unread one cannot say which tests a change reaches. Fix git and '
                          're-run.' % (' '.join(args), why))
        return out
    mb = git('merge-base', base, 'HEAD').strip()
    out = git('diff', '--name-only', '--no-renames', '-z', mb) + \
        git('ls-files', '-z', '--others', '--exclude-standard')
    return {p for p in out.split('\0') if p}
