"""kitlib.clock, the time fence (docs/flaky-tests.md): a fenced process gets the ceiling for a
degrade budget, an unmarked one the budget itself; the marker reaches this process (tests/__init__.py),
the units of tools/kit_tests.py (child_env) and a kit child started with the test's environment,
and no child built without it; and no code outside the test runner sets it."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import clock
from kitlib.tooldeps import selfrun

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tests_entry_for_clock_tests',
                                               os.path.join(KIT, 'tools', 'kit_tests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def unmarked(env=None):
    return {k: v for k, v in (os.environ if env is None else env).items() if k.upper() != clock.FENCE_ENV}


class Fence(unittest.TestCase):
    def test_this_test_process_is_fenced(self):
        self.assertEqual(os.environ.get(clock.FENCE_ENV), '1')
        self.assertTrue(clock.fenced())
        self.assertEqual(clock.budget(1.0), clock.CEILING)

    def test_an_unmarked_process_gets_the_budget(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(clock.FENCE_ENV, None)
            self.assertFalse(clock.fenced())
            self.assertEqual(clock.budget(1.0), 1.0)
            self.assertEqual(clock.budget(0.25), 0.25)

    def test_only_one_fences(self):
        for value in ('0', '', 'yes', 'true', ' 1'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {clock.FENCE_ENV: value}):
                self.assertFalse(clock.fenced())
                self.assertEqual(clock.budget(1.0), 1.0)

    def test_a_given_mapping_is_read(self):
        self.assertTrue(clock.fenced({clock.FENCE_ENV: '1'}))
        self.assertFalse(clock.fenced({}))

    def test_the_ceiling_is_past_the_unit_timeout(self):
        # a real hang under the fence still ends, as a unit TIMEOUT
        self.assertGreater(clock.CEILING, selfrun.UNIT_TIMEOUT)


class Children(unittest.TestCase):
    def child(self, code, env):
        r = subprocess.run([sys.executable, '-c', code], cwd=KIT, env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    BUDGET = 'from kitlib import clock; print(clock.budget(1.0))'

    def test_a_kit_child_with_the_tests_environment_is_fenced(self):
        self.assertEqual(self.child(self.BUDGET, dict(os.environ)), str(clock.CEILING))

    def test_a_kit_child_built_without_the_marker_is_not(self):
        self.assertEqual(self.child(self.BUDGET, unmarked()), '1.0')

    def test_importing_the_tests_package_marks_the_process(self):
        code = 'import os, tests; print(os.environ.get(%r))' % clock.FENCE_ENV
        self.assertEqual(self.child(code, unmarked()), '1')
        self.assertEqual(self.child(code, dict(unmarked(), **{clock.FENCE_ENV: '0'})), '0')


class KitTests(unittest.TestCase):
    """tools/kit_tests.py marks every test child, for the reason it marks the home fence: a pool
    worker replaces its environment for each unit, so tests/__init__.py's marker would not reach it."""

    def test_the_units_get_the_marker_and_kit_tests_stays_unmarked(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(clock.FENCE_ENV, None)
            self.assertEqual(cli.child_env().get(clock.FENCE_ENV), '1')
            self.assertEqual(cli.units_env('1').get(clock.FENCE_ENV), '1')
            self.assertEqual(cli.units_env('0').get(clock.FENCE_ENV), '1')
            self.assertNotIn(clock.FENCE_ENV, os.environ)

    def test_an_outer_value_is_kept(self):
        with mock.patch.dict(os.environ, {clock.FENCE_ENV: '0'}):
            self.assertEqual(cli.child_env().get(clock.FENCE_ENV), '0')


NAMES = re.compile(r'KIT_TIME_FENCE|clock\.FENCE_ENV|from kitlib\.clock import|kitlib\.clock\.FENCE_ENV')
SKIP = {'.git', 'tests', '__pycache__'}


def naming(root):
    """(the files under `root` that name the marker, how many text files were read): every file
    of any extension or none, root files included, outside tests/, .git and a checkout's
    .claude/worktrees; a file with a NUL byte (binary) is not text."""
    found, scanned = set(), 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace('\\', '/')
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP and not (rel == '.claude' and d == 'worktrees')]
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, 'rb') as f:
                data = f.read()
            if b'\0' in data:
                continue
            scanned += 1
            if NAMES.search(data.decode('utf-8', errors='replace')):
                found.add(os.path.relpath(path, root).replace('\\', '/'))
    return found, scanned


class Production(unittest.TestCase):
    """No code a real run executes sets the marker: outside tests/, only kitlib/clock.py (which
    defines it), tools/kit_tests.py (the test runner) and the design doc name it."""
    ALLOWED = {'kitlib/clock.py', 'tools/kit_tests.py', 'docs/flaky-tests.md'}

    def test_only_the_test_runner_names_the_marker(self):
        found, scanned = naming(KIT)
        self.assertGreater(scanned, 500)
        self.assertEqual(found, self.ALLOWED)

    def test_a_marker_in_any_file_kind_is_found(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        planted = ['hosts/codex/shell/agents/worker.toml', 'githooks/pre-push', 'reserve_ui.pyw',
                   'templates/ci/build.yml', 'templates/run.cmd']
        for rel in planted + ['tests/test_x.py', '.git/config', '.claude/worktrees/w/a.py']:
            path = os.path.join(root, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write('set KIT_TIME_FENCE=1\n')
        with open(os.path.join(root, 'blob.bin'), 'wb') as f:
            f.write(b'\0KIT_TIME_FENCE')
        found, scanned = naming(root)
        self.assertEqual(found, set(planted))
        self.assertEqual(scanned, len(planted))


if __name__ == '__main__':
    unittest.main()
