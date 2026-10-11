"""kit_tests --list: every test id the loader finds, and the tests skipped by decorator
(docs/test-tools.md "kit_tests").

Nothing runs. One child process loads each module with unittest's loader, with cwd the checkout
as a unit would load it (so this process imports none of them), and walks the suites. A skip is
found only when the class or the test method carries `__unittest_skip__` (skip, skipIf and
skipUnless); a `self.skipTest()` inside a test body is a runtime skip and is not found, so the
skip count is a lower bound. A module that fails to import loads as one failing test; it is
listed and named, so a comparison cannot lose its tests unseen.
"""
import json
import os
import subprocess
import sys

LIST_TIMEOUT = 600
_MARK = 'kit-tests-list: '
_CHILD = '\n'.join([
    'import json, sys, unittest',
    'def walk(s):',
    '    for t in s:',
    '        if isinstance(t, unittest.TestSuite):',
    '            yield from walk(t)',
    '        else:',
    '            yield t',
    'out = []',
    'for m in sys.argv[1:]:',
    '    try:',
    '        s = unittest.defaultTestLoader.loadTestsFromName(m)',
    '    except Exception as e:',
    '        out.append([m, "load error: %s" % e, 1, None])',
    '        continue',
    '    for t in walk(s):',
    '        skip = None',
    '        meth = getattr(t, getattr(t, "_testMethodName", ""), None)',
    '        for o in (type(t), meth):',
    '            if getattr(o, "__unittest_skip__", False):',
    '                skip = getattr(o, "__unittest_skip_why__", "")',
    '        broken = type(t).__name__ == "_FailedTest"',
    '        out.append([t.id(), None, 1 if broken else 0, skip])',
    'print(%r + json.dumps(out))' % _MARK,
])


def collect(root, modules, environ=None, timeout=LIST_TIMEOUT):
    """`[(id, broken, skip)]` for `modules`, sorted by id: `broken` is True for a module that
    failed to import (its one failing test), `skip` the reason of a decorator skip else None.
    None when the child failed or timed out."""
    if not modules:
        return []
    env = dict(os.environ if environ is None else environ)
    try:
        r = subprocess.run([sys.executable, '-c', _CHILD] + list(modules), cwd=root, env=env,
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed(r.stdout.splitlines()):
        if line.startswith(_MARK):
            try:
                rows = json.loads(line[len(_MARK):])
            except ValueError:
                return None
            return sorted((i, bool(b), s) for i, _e, b, s in rows)
    return None


def render(entries, modules):
    """The output lines: one id per line, sorted; then the summary; then one line per skipped
    test and one per module that failed to import. The same tests give the same lines."""
    ids = [i for i, _b, _s in entries]
    skips = [(i, s) for i, _b, s in entries if s is not None]
    broken = [i for i, b, _s in entries if b]
    lines = list(ids)
    lines.append('kit_tests: list: %d test(s) in %d module(s), %d static skip(s) (runtime skips '
                 'not counted), %d module(s) failed to import'
                 % (len(ids), len(modules), len(skips), len(broken)))
    lines.extend('kit_tests: skip: %s (%s)' % (i, s) for i, s in skips)
    lines.extend('kit_tests: import failed: %s' % i for i in broken)
    return lines
