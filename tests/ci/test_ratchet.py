"""kitlib.ci.ratchet and tools/ci_ratchet.py: the suite's failing tests against a baseline.

Each case builds a small test tree in a temp folder (its own `tests` package) and runs the real
child on it: `python -c` running `unittest discover -s tests -t .` there."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from kitlib.ci import ratchet

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSING = '''
import unittest
class A(unittest.TestCase):
    def test_ok(self):
        pass
'''
FAILING = '''
import unittest
class B(unittest.TestCase):
    def test_bad(self):
        self.assertEqual(1, 2)
    def test_fine(self):
        pass
'''
ERRORING = '''
import unittest
class C(unittest.TestCase):
    def test_raises(self):
        raise RuntimeError('boom')
'''
UNEXPECTED = '''
import unittest
class D(unittest.TestCase):
    @unittest.expectedFailure
    def test_passes_anyway(self):
        pass
'''
SLOW = '''
import time, unittest
class S(unittest.TestCase):
    def test_sleeps(self):
        time.sleep(60)
'''
CRASH = '''
import os, unittest
class K(unittest.TestCase):
    def test_exits(self):
        os._exit(3)
'''
MARKED = '''
import unittest
from tests.platforms import posix_only, windows_only
@windows_only('a junction is a Windows feature')
class W(unittest.TestCase):
    def test_w(self):
        pass
class P(unittest.TestCase):
    @posix_only('a file mode bit')
    def test_p(self):
        pass
    @posix_only('a file mode bit')
    def test_q(self):
        pass
'''

BAD = 'tests.test_b.B.test_bad'


def load_cli():
    spec = importlib.util.spec_from_file_location('ci_ratchet_cli',
                                                  os.path.join(KIT, 'tools', 'ci_ratchet.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tree(unittest.TestCase):
    """A temp checkout holding a `tests` package with the given modules."""

    def tree(self, init='', **modules):
        root = tempfile.mkdtemp(prefix='ratchet-')
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, 'tests'))
        self.write(root, 'tests/__init__.py', init)
        for name, text in modules.items():
            self.write(root, 'tests/%s.py' % name, text)
        return root

    def write(self, root, rel, text, dedent=True):
        path = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(textwrap.dedent(text) if dedent else text)
        return path

    def run_suite(self, root, timeout=120):
        return ratchet.run_suite(root, timeout=timeout)


class Runs(Tree):

    def test_green_on_the_baseline(self):
        root = self.tree(test_a=PASSING, test_b=FAILING)
        run = self.run_suite(root)
        self.assertIsNone(run.broken, run.output)
        self.assertEqual(run.failing, [BAD])
        self.assertEqual(run.ran, 3)
        self.assertEqual(ratchet.compare(run.failing, [BAD]), ([], []))

    def test_a_planted_failure_is_named(self):
        root = self.tree(test_a=PASSING, test_b=FAILING)
        new, stale = ratchet.compare(self.run_suite(root).failing, [])
        self.assertEqual((new, stale), ([BAD], []))

    def test_an_error_is_a_failure(self):
        run = self.run_suite(self.tree(test_c=ERRORING))
        self.assertEqual(run.failing, ['tests.test_c.C.test_raises'])

    def test_an_unexpected_success_is_a_failure(self):
        run = self.run_suite(self.tree(test_a=PASSING, test_d=UNEXPECTED))
        self.assertEqual(run.failing, ['tests.test_d.D.test_passes_anyway'])

    def test_an_import_failure_is_a_failure(self):
        root = self.tree(test_a=PASSING, test_broken='raise ImportError("no such backend")\n')
        run = self.run_suite(root)
        self.assertIsNone(run.broken, run.output)
        self.assertEqual(len(run.imports), 1, run.imports)
        self.assertTrue(run.imports[0].endswith('tests.test_broken'), run.imports)
        self.assertIn(run.imports[0], run.failing)
        self.assertEqual(ratchet.compare(run.failing, []), (run.failing, []))

    def test_a_failing_tests_package_is_a_broken_run(self):
        """However the baseline reads, a tests package that does not import is never green."""
        root = self.tree(init='raise AttributeError("WinDLL")\n', test_a=PASSING)
        run = self.run_suite(root)
        self.assertIn('the tests package failed to import', run.broken)
        self.assertIn('WinDLL', run.broken)
        self.assertEqual(run.failing, [])

    def test_a_run_of_no_test_is_broken(self):
        run = self.run_suite(self.tree())
        self.assertIn('no test ran', run.broken)

    def test_a_stale_entry_fails(self):
        root = self.tree(test_a=PASSING, test_b=FAILING)
        failing = self.run_suite(root).failing
        gone, passes = 'tests.test_gone.G.test_x', 'tests.test_a.A.test_ok'
        self.assertEqual(ratchet.compare(failing, sorted([BAD, gone, passes])),
                         ([], sorted([gone, passes])))

    def test_a_timeout_is_a_broken_run(self):
        run = self.run_suite(self.tree(test_s=SLOW), timeout=3)
        self.assertIn('timed out', run.broken)

    def test_a_child_that_dies_is_a_broken_run(self):
        run = self.run_suite(self.tree(test_a=PASSING, test_k=CRASH))
        self.assertIn('no result line', run.broken)
        self.assertIn('exit 3', run.broken)

    def test_markers_are_counted_by_reason(self):
        root = self.tree(test_m=MARKED)
        shutil.copyfile(os.path.join(KIT, 'tests', 'platforms.py'),
                        os.path.join(root, 'tests', 'platforms.py'))
        run = self.run_suite(root)
        self.assertIsNone(run.broken, run.output)
        self.assertEqual(run.markers, [['posix', 'a file mode bit', 2],
                                       ['windows', 'a junction is a Windows feature', 1]])

    def test_the_session_binding_is_not_passed_on(self):
        probe = ('import os, unittest\n'
                 'class E(unittest.TestCase):\n'
                 '    def test_env(self):\n'
                 '        self.assertNotIn("PM_KIT_ROOT", os.environ)\n'
                 '        self.assertEqual(os.environ.get("KIT_RATCHET_PROBE"), "kept")\n')
        root = self.tree(test_e=probe)
        env = dict(os.environ, PM_KIT_ROOT=r'C:\session\kit', KIT_RATCHET_PROBE='kept')
        run = ratchet.run_suite(root, timeout=120, environ=env)
        self.assertEqual((run.broken, run.failing), (None, []), run.output)


class Baselines(Tree):

    def baseline(self, text):
        root = self.tree()
        return self.write(root, 'tests/base.txt', text, dedent=False)

    def test_ids_comments_and_blank_lines(self):
        path = self.baseline('# until slice 2\n\ntests.a.A.test_x\ntests.b.B.test_y\n')
        self.assertEqual(ratchet.read_baseline(path), ['tests.a.A.test_x', 'tests.b.B.test_y'])

    def test_unsorted_duplicate_or_padded_is_refused(self):
        for text, why in (('tests.b\ntests.a\n', 'sorted'), ('tests.a\ntests.a\n', 'twice'),
                          (' tests.a\n', 'space'), ('tests.a \n', 'space'),
                          ('tests.a\ttests.b\n', 'space')):
            with self.subTest(text=text):
                with self.assertRaises(ratchet.Refused) as cm:
                    ratchet.read_baseline(self.baseline(text))
                self.assertIn(why, str(cm.exception))

    def test_a_missing_file_is_refused_unless_it_is_the_default(self):
        root = self.tree()
        missing = os.path.join(root, 'tests', 'none.txt')
        with self.assertRaises(ratchet.Refused):
            ratchet.read_baseline(missing)
        self.assertEqual(ratchet.read_baseline(missing, default=True), [])

    def test_the_default_follows_the_platform(self):
        root = self.tree()
        self.assertEqual(ratchet.default_baseline(root, 'linux'),
                         os.path.join(root, 'tests', 'linux-baseline.txt'))
        self.assertEqual(ratchet.default_baseline(root, 'win32'),
                         os.path.join(root, 'tests', 'windows-baseline.txt'))
        self.assertIsNone(ratchet.default_baseline(root, 'darwin'))

    def test_the_kit_s_linux_baseline_reads(self):
        path = os.path.join(KIT, 'tests', 'linux-baseline.txt')
        self.assertEqual(ratchet.read_baseline(path), [])
        with open(path, encoding='utf-8') as f:
            self.assertIn('broken run', f.read())


class Cli(Tree):

    def main(self, *argv):
        out = io.StringIO()
        rc = load_cli().main(list(argv), out=out)
        return rc, out.getvalue()

    def test_exit_codes(self):
        root = self.tree(test_a=PASSING, test_b=FAILING)
        green = self.write(root, 'base-green.txt', BAD + '\n')
        empty = self.write(root, 'base-empty.txt', '# none\n')
        stale = self.write(root, 'base-stale.txt', BAD + '\ntests.test_gone.G.test_x\n')
        rc, out = self.main('-C', root, '--baseline', green)
        self.assertEqual(rc, 0, out)
        self.assertIn('ci_ratchet: GREEN', out)
        rc, out = self.main('-C', root, '--baseline', empty)
        self.assertEqual(rc, 1, out)
        self.assertIn('ci_ratchet: NEW FAILURE: ' + BAD, out)
        rc, out = self.main('-C', root, '--baseline', stale)
        self.assertEqual(rc, 1, out)
        self.assertIn('ci_ratchet: STALE: tests.test_gone.G.test_x', out)

    def test_a_broken_run_is_2(self):
        root = self.tree(init='raise AttributeError("WinDLL")\n', test_a=PASSING)
        listed = self.write(root, 'base.txt', 'tests\n')
        rc, out = self.main('-C', root, '--baseline', listed)
        self.assertEqual(rc, 2, out)
        self.assertIn('ci_ratchet: BROKEN RUN: the tests package failed to import', out)

    def test_a_malformed_baseline_is_2(self):
        root = self.tree(test_a=PASSING)
        bad = self.write(root, 'base.txt', 'tests.b\ntests.a\n')
        rc, out = self.main('-C', root, '--baseline', bad)
        self.assertEqual(rc, 2, out)
        self.assertIn('ci_ratchet: refused:', out)

    def test_usage_is_2(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            load_cli().main(['--no-such-flag'], out=io.StringIO())
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('USAGE: unrecognized arguments: --no-such-flag', err.getvalue())


class Imports(unittest.TestCase):
    """The ratchet must run where kitlib.proc cannot import (Linux before slice 2): neither it nor
    its entry point loads kitlib.proc (beyond the console hook `import kitlib` loads on Windows),
    kitlib.config or kitlib.git."""

    def test_no_proc_config_or_git(self):
        code = ('import json, runpy, sys\n'
                'sys.path.insert(0, sys.argv[1])\n'
                'import kitlib.ci.ratchet\n'
                'runpy.run_path(sys.argv[2], run_name="not_main")\n'
                'print(json.dumps(sorted(n for n in sys.modules if n.split(".")[0] == "kitlib")))\n')
        r = subprocess.run([sys.executable, '-I', '-c', code, KIT,
                            os.path.join(KIT, 'tools', 'ci_ratchet.py')],
                           cwd=KIT, capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(r.returncode, 0, r.stderr)
        loaded = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn('kitlib.ci.ratchet', loaded)
        allowed = {'kitlib.proc', 'kitlib.proc.console'} if os.name == 'nt' else set()
        bad = [n for n in loaded if n.split('.')[:2] in (['kitlib', 'proc'], ['kitlib', 'config'],
                                                         ['kitlib', 'git']) and n not in allowed]
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
