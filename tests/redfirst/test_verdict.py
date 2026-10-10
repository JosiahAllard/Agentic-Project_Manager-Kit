"""kitlib.redfirst.verdict: runner matching, command building and the verdicts (pure)."""
import os
import tempfile
import time
import unittest

from kitlib.redfirst import verdict as V

PY = {'glob': 'tests/*test_*.py', 'kind': 'python'}
TOOLS = {'glob': 'tools/test_*.py', 'kind': 'python', 'pythonpath': 'tools'}
GRADLE = {'glob': 'engine/core/src/test/*.java', 'kind': 'gradle', 'dir': 'engine/core',
          'project': ':core', 'args': ['--no-build-cache']}
DOTNET = {'glob': 'server/App.Tests/*.cs', 'kind': 'dotnet',
          'csproj': 'server/App.Tests/App.Tests.csproj', 'args': ['--no-incremental']}
RUNNERS = [TOOLS, PY, GRADLE, DOTNET]


class Selectors(unittest.TestCase):
    def test_python_module_under_root(self):
        s = V.parse_selector('tests/pkg/test_x.py::Case.test_a', RUNNERS)
        self.assertEqual((s.kind, s.target, s.filter), ('python', 'tests.pkg.test_x', 'Case.test_a'))

    def test_python_module_under_pythonpath(self):
        s = V.parse_selector('tools/test_y.py', RUNNERS)
        self.assertEqual((s.target, s.filter), ('test_y', ''))

    def test_first_match_wins(self):
        s = V.parse_selector('tools/test_y.py', [PY, {'glob': 'tools/*', 'kind': 'python'}, TOOLS])
        self.assertEqual(s.target, 'tools.test_y')

    def test_glob_is_case_blind_and_crosses_slash(self):
        s = V.parse_selector('Engine/Core/src/test/a/b/FooTest.java', RUNNERS)
        self.assertEqual((s.kind, s.target), ('gradle', ':core'))

    def test_backslashes_are_slashes(self):
        self.assertEqual(V.parse_selector('tests\\test_x.py', RUNNERS).path, 'tests/test_x.py')

    def test_no_runner_refuses(self):
        with self.assertRaisesRegex(V.Usage, 'no runner takes it'):
            V.parse_selector('docs/x.md', RUNNERS)

    def test_absolute_or_parent_paths_refuse(self):
        for bad in ('/tests/test_x.py', 'C:/x/test_x.py', 'tests/../test_x.py', '::f'):
            with self.assertRaises(V.Usage, msg=bad):
                V.parse_selector(bad, RUNNERS)

    def test_runner_missing_its_keys_refuses(self):
        with self.assertRaisesRegex(V.Usage, 'no dir, project'):
            V.parse_selector('a/FooTest.java', [{'glob': '*.java', 'kind': 'gradle'}])

    def test_python_file_outside_pythonpath_refuses(self):
        with self.assertRaisesRegex(V.Usage, 'no unittest module'):
            V.parse_selector('lib/test_x.py', [{'glob': '*.py', 'kind': 'python', 'pythonpath': 'tools'}])

    def test_module_of(self):
        self.assertEqual(V.module_of('a/b/test_c.py', '.'), 'a.b.test_c')
        self.assertEqual(V.module_of('a/b/test_c.py', 'a'), 'b.test_c')
        self.assertIsNone(V.module_of('a/b-c/test.py', '.'))
        self.assertIsNone(V.module_of('a/test.txt', '.'))


class Commands(unittest.TestCase):
    def test_python(self):
        s = V.parse_selector('tools/test_y.py::T', RUNNERS)
        argv, cwd, env = V.command(s, 'TW', 'LOGS', 1, python='PY')
        self.assertEqual(argv, ['PY', '-m', 'unittest', '-v', 'test_y.T'])
        self.assertEqual(cwd, 'TW')
        self.assertEqual(env['PYTHONPATH'], os.path.normpath('TW/tools'))

    def test_gradle_default_filter_and_args(self):
        s = V.parse_selector('engine/core/src/test/FooTest.java', RUNNERS)
        argv, _, _ = V.command(s, 'TW', 'LOGS', 1)
        self.assertEqual(argv[1:], [':core:test', '--offline', '--tests', '*.FooTest', '--no-build-cache'])

    def test_dotnet_bare_filter_and_trx(self):
        s = V.parse_selector('server/App.Tests/FooTests.cs::Bar', RUNNERS)
        argv, _, _ = V.command(s, 'TW', 'LOGS', 2)
        self.assertIn('FullyQualifiedName~Bar', argv)
        self.assertIn('trx;LogFileName=2-FooTests.trx', argv)
        self.assertEqual(argv[-1], '--no-incremental')

    def test_dotnet_expression_filter_kept(self):
        s = V.parse_selector('server/App.Tests/FooTests.cs::Name=X', RUNNERS)
        self.assertIn('Name=X', V.command(s, 'TW', 'LOGS', 1)[0])


class Verdicts(unittest.TestCase):
    def test_python(self):
        self.assertEqual(V.python_verdict(0, 'Ran 2 tests in 0.1s\nOK')[0], V.GREEN)
        self.assertEqual(V.python_verdict(1, 'Ran 2 tests in 0.1s\nFAILED')[0], V.RED)
        self.assertEqual(V.python_verdict(1, 'boom')[0], V.ERROR)
        self.assertEqual(V.python_verdict(5, 'Ran 0 tests in 0s\nNO TESTS RAN')[0], V.NONE)
        imp = ('ERROR: m (unittest.loader._FailedTest.m)\nImportError: Failed to import test '
               'module: m\nRan 1 test in 0s\nFAILED')
        self.assertEqual(V.python_verdict(1, imp), (V.RED, 'does not import against the base'))
        nomod = ('ERROR: m (unittest.loader._FailedTest.m)\nImportError: Failed to import test '
                 'module: m\nTraceback (most recent call last):\n  File "m.py", line 1\n'
                 "    from calc import mul\nImportError: cannot import name 'mul' from 'calc'\n"
                 'Ran 1 test in 0s\nFAILED')
        self.assertEqual(V.python_verdict(1, nomod),
                         (V.RED, "does not import against the base: ImportError: cannot import "
                                 "name 'mul' from 'calc'"))
        gone = imp.replace('module: m\n', "module: m\nModuleNotFoundError: No module named 'x'\n")
        self.assertEqual(V.python_verdict(1, gone)[1],
                         "does not import against the base: ModuleNotFoundError: No module "
                         "named 'x'")
        nofilter = 'ERROR: x (unittest.loader._FailedTest.x)\nAttributeError\nRan 1 test in 0s'
        self.assertEqual(V.python_verdict(1, nofilter)[0], V.NONE)

    def test_names_copied(self):
        copied = ['src/FooTest.java']
        self.assertTrue(V.names_copied('/w/src/FooTest.java:3: error: no symbol', copied))
        self.assertFalse(V.names_copied('/w/src/BarFooTest.java:3: error: x', copied))
        self.assertFalse(V.names_copied('/w/src/FooTest.java:3: warning: error()', copied))
        self.assertTrue(V.names_copied('e: file:///C:/w/src/FooTest.java:3:1 Unresolved', copied))
        self.assertTrue(V.names_copied('C:\\w\\T\\A.cs(3,4): error CS0103: x', ['T/A.cs']))

    def test_gradle(self):
        self.assertEqual(V.gradle_verdict(1, 'No tests found for given includes', None, [])[0], V.NONE)
        log = "Execution failed for task ':core:compileTestJava'\n/w/a/FooTest.java:1: error: x"
        self.assertEqual(V.gradle_verdict(1, log, None, ['a/FooTest.java'])[0], V.RED)
        self.assertEqual(V.gradle_verdict(1, log, None, ['a/Other.java'])[0], V.ERROR)
        self.assertEqual(V.gradle_verdict(1, '', (3, 1), [])[0], V.RED)
        self.assertEqual(V.gradle_verdict(0, '', (3, 0), [])[0], V.GREEN)
        self.assertEqual(V.gradle_verdict(1, '', None, [])[0], V.ERROR)

    def test_dotnet(self):
        self.assertEqual(V.dotnet_verdict(1, 'No test matches the given testcase filter', None, [])[0], V.NONE)
        self.assertEqual(V.dotnet_verdict(1, 'T/A.cs(1,2): error CS1: x', None, ['T/A.cs'])[0], V.RED)
        self.assertEqual(V.dotnet_verdict(1, 'B.cs(1,2): error CS1: x', None, ['T/A.cs'])[0], V.ERROR)
        self.assertEqual(V.dotnet_verdict(1, '', (4, 2), [])[0], V.RED)
        self.assertEqual(V.dotnet_verdict(0, '', (4, 0), [])[0], V.GREEN)
        self.assertEqual(V.dotnet_verdict(0, '', (0, 0), [])[0], V.NONE)

    def test_exit_code(self):
        self.assertEqual(V.exit_code([V.RED, V.RED], True), V.EXIT_RED)
        self.assertEqual(V.exit_code([V.RED, V.GREEN, V.ERROR], True), V.EXIT_GREEN)
        self.assertEqual(V.exit_code([V.RED, V.NONE], True), V.EXIT_UNDECIDED)
        self.assertEqual(V.exit_code([], True), V.EXIT_UNDECIDED)
        self.assertEqual(V.exit_code([V.RED], False), V.EXIT_LEFTOVER)


class ResultFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-redfirst-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.dir, ignore_errors=True))

    def put(self, name, text, age=0):
        p = os.path.join(self.dir, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def test_junit_counts_this_run_only(self):
        self.put('TEST-a.xml', '<testsuite tests="2" failures="1" errors="0"/>')
        self.put('TEST-old.xml', '<testsuite tests="9" failures="9" errors="0"/>', age=3600)
        self.assertEqual(V.junit_counts(self.dir, time.time() - 60), (2, 1))

    def test_junit_unparseable_is_none(self):
        self.put('TEST-a.xml', '<testsuite tests="2"')
        self.assertIsNone(V.junit_counts(self.dir))

    def test_trx_counts(self):
        p = self.put('r.trx', '<TestRun><ResultSummary><Counters total="5" failed="1" error="1"/>'
                              '</ResultSummary></TestRun>')
        self.assertEqual(V.trx_counts(p), (5, 2))
        self.assertIsNone(V.trx_counts(p, time.time() + 60))
        self.assertIsNone(V.trx_counts(os.path.join(self.dir, 'none.trx')))


if __name__ == '__main__':
    unittest.main()
