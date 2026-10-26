"""The ratchet: the suite's failing tests held to a baseline that only shrinks (docs/test-tools.md
"Platform markers and the ratchet"; docs/linux-support-plan.md section 4).

  run_suite(root, timeout, environ)  one child runs the whole suite under `root`: a Run
  read_baseline(path, default)       the baseline's ids; Refused when it is malformed
  compare(failing, baseline)         (new, stale): failing ids not in it, its ids not failing
  default_baseline(root, platform)   tests/linux-baseline.txt, tests/windows-baseline.txt or None

The child is `python -c _CHILD` in `root`: it imports the tests package (a package that does not
import is a broken run, never a pass), runs `unittest discover -s tests -t .` and prints one line,
MARK and a JSON object: the failing ids (failures, errors and unexpected successes; a subtest's
failure counts as its test's), the import failures among them (unittest's _FailedTest ids), the
number of tests run and the platform markers by reason (tests/platforms.py REASONS). A child that
dies, times out or prints no such line is a broken run, as is one that ran no test.

This module must import where kitlib.proc cannot (Linux, before slice 2 of the plan): it uses
subprocess with an explicit creationflags= and loads no kitlib.proc beyond kitlib/__init__'s
console hook, and no kitlib.config or kitlib.git.
The child's environment is the caller's without the session's kit binding
(kitlib.tooldeps.childenv.scrubbed); the tests package sets its own home and time fences."""
import collections
import json
import os
import subprocess
import sys
import tempfile

from kitlib.tooldeps import childenv

MARK = 'kit-ci-ratchet: '
RUN_TIMEOUT = 3600                      # seconds for the whole suite in one child
TAIL = 40                               # lines of the child's output kept for a broken run
LINUX_BASELINE = 'tests/linux-baseline.txt'
WINDOWS_BASELINE = 'tests/windows-baseline.txt'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

Run = collections.namedtuple('Run', 'broken failing imports ran markers output')
Run.__doc__ = """One run of the suite. `broken`: None, or why the run says nothing about the tests
(then `failing` is empty). `failing`, `imports`: sorted ids. `ran`: tests run. `markers`: sorted
[platform, reason, count]. `output`: the last TAIL lines of the child's output."""


class Refused(Exception):
    """A baseline that cannot be read or is malformed."""


_CHILD = r'''
import collections, json, sys, traceback, unittest
MARK = sys.argv[1]


def say(**fields):
    sys.__stdout__.write('\n' + MARK + json.dumps(fields, sort_keys=True) + '\n')
    sys.__stdout__.flush()


try:
    import tests                                    # noqa: F401
except BaseException:
    trace = traceback.format_exc()
    say(broken='the tests package failed to import: ' + trace.strip().splitlines()[-1],
        trace=trace)
    raise SystemExit(0)


def walk(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from walk(t)
        else:
            yield t


def ident(test):
    return getattr(test, 'test_case', test).id()    # a subtest counts as its test


suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
imports = sorted({t.id() for t in walk(suite) if type(t).__name__ == '_FailedTest'})
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
failing = {ident(t) for t, _tb in result.failures + result.errors}
failing |= {ident(t) for t in result.unexpectedSuccesses}
platforms = sys.modules.get('tests.platforms')
counts = collections.Counter(getattr(platforms, 'REASONS', {}).values())
say(failing=sorted(failing | set(imports)), imports=imports, ran=result.testsRun,
    markers=sorted([kind, reason, n] for (kind, reason), n in counts.items()))
'''


def default_baseline(root, platform=None):
    """The baseline for `platform` (default sys.platform) under `root`, or None for a platform
    with none."""
    platform = sys.platform if platform is None else platform
    if platform.startswith('linux'):
        rel = LINUX_BASELINE
    elif platform == 'win32':
        rel = WINDOWS_BASELINE
    else:
        return None
    return os.path.join(root, *rel.split('/'))


def read_baseline(path, default=False):
    """The ids in the baseline at `path`: one per line, sorted, none twice, no tab and no space at
    either end; blank lines and lines starting '#' are skipped. A missing file is no ids when it is
    the `default` one, else Refused, as is any malformed line."""
    try:
        with open(path, encoding='utf-8', newline='') as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        if default:
            return []
        raise Refused('no baseline at %s' % path)
    except (OSError, UnicodeDecodeError) as e:
        raise Refused('cannot read the baseline %s: %s' % (path, e))
    ids, seen = [], set()
    for n, line in enumerate(lines, 1):
        if not line or line.startswith('#'):
            continue
        where = '%s line %d' % (path, n)
        if line != line.strip() or '\t' in line:
            raise Refused('%s: %r has a tab, or a space at an end' % (where, line))
        if line in seen:
            raise Refused('%s: %s is listed twice' % (where, line))
        if ids and line < ids[-1]:
            raise Refused('%s: %s is not sorted (it comes before %s)' % (where, line, ids[-1]))
        seen.add(line)
        ids.append(line)
    return ids


def compare(failing, baseline):
    """(new, stale), sorted: the failing ids the baseline does not list, and the baseline's ids
    that do not fail (the test passes or is gone)."""
    failing, baseline = set(failing), set(baseline)
    return sorted(failing - baseline), sorted(baseline - failing)


def _broken(why, output):
    return Run(why, [], [], 0, [], output)


def _tail(*files):
    text = []
    for f in files:
        f.seek(0)
        text.extend(f.read().decode('utf-8', 'replace').splitlines())
    return '\n'.join(text[-TAIL:])


def _result(stdout):
    """The child's result object from its stdout, or None."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARK):
            try:
                found = json.loads(line[len(MARK):])
            except ValueError:
                return None
            return found if isinstance(found, dict) else None
    return None


def _markers(markers):
    """The result's markers, checked: a list of [platform, reason, count]; ValueError else."""
    if not isinstance(markers, list):
        raise ValueError('markers is not a list: %r' % (markers,))
    for m in markers:
        if not (isinstance(m, list) and len(m) == 3 and isinstance(m[0], str)
                and isinstance(m[1], str) and type(m[2]) is int):
            raise ValueError('a marker is not [platform, reason, count]: %r' % (m,))
    return markers


def run_suite(root, timeout=RUN_TIMEOUT, environ=None):
    """Run the whole suite under `root` in one child: a Run. `environ` (default os.environ) is
    scrubbed of the session's kit binding. The child's output goes to files, not pipes, so a test's
    leftover process cannot hold the wait open."""
    env = childenv.scrubbed(os.environ if environ is None else environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([sys.executable, '-c', _CHILD, MARK], cwd=root, env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=NO_WINDOW)
        try:
            rc = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return _broken('the run timed out after %ds' % timeout, _tail(out, err))
        output = _tail(out, err)
        out.seek(0)
        found = _result(out.read().decode('utf-8', 'replace'))
    if found is None:
        return _broken('the child left no result line (exit %s)' % rc, output)
    if found.get('broken'):
        return _broken(found['broken'], found.get('trace') or output)
    try:
        failing, imports = sorted(found['failing']), sorted(found['imports'])
        ran, markers = int(found['ran']), _markers(found['markers'])
    except (KeyError, TypeError, ValueError) as e:
        return _broken('the result line is malformed: %s' % e, output)
    if ran == 0:
        return _broken('no test ran', output)
    return Run(None, failing, imports, ran, markers, output)
