"""kitlib.redfirst.verdict: runner matching, command building and the verdicts (pure)."""
import os
import subprocess
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

    def test_surrounding_whitespace_is_stripped(self):
        s = V.parse_selector('tests/pkg/test_x.py::Case.test_a\r', RUNNERS)
        self.assertEqual((s.text, s.filter), ('tests/pkg/test_x.py::Case.test_a', 'Case.test_a'))

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


class GradleCmdReparse(unittest.TestCase):
    """gradlew.bat runs in cmd.exe, which reads its command line again: a gradle word holding
    one of cmd.exe's characters is refused before anything runs."""

    @unittest.skipUnless(os.name == 'nt', 'cmd.exe runs a .bat on Windows only')
    def test_injected_filter_runs_no_second_command_through_gradlew_bat(self):
        # The real shape: a scratch gradlew.bat run with the argv command() builds. Without the
        # refusal cmd.exe splits the line at the & and runs the echo itself: a word with no blank
        # is not quoted, and a quote in a quoted one (list2cmdline's \", which cmd.exe does not
        # read as an escape) ends cmd.exe's quoting.
        shapes = {'a&echo.INJECTED1': '&', 'x" & echo INJECTED2 & "y': '"'}
        with tempfile.TemporaryDirectory() as tw:
            with open(os.path.join(tw, 'gradlew.bat'), 'w', newline='\r\n') as fh:
                fh.write('@echo off\necho ARGS %*\n')
            for n, (filt, char) in enumerate(shapes.items(), 1):
                with self.subTest(filt=filt):
                    try:
                        s = V.parse_selector('engine/core/src/test/FooTest.java::' + filt, RUNNERS)
                        argv = V.command(s, tw, tw, 1)[0]
                    except V.Usage as e:
                        refused = str(e)
                    else:
                        out = subprocess.run(argv, cwd=tw, capture_output=True, text=True,
                                             stdin=subprocess.DEVNULL, timeout=60).stdout
                        lines = [l.strip() for l in out.splitlines()]
                        self.assertNotIn('INJECTED%d' % n, lines,
                                         'cmd.exe ran the injected echo: %r' % out)
                        self.fail('the filter reached gradlew.bat: %r' % out)
                    self.assertIn('the filter %r holds %s' % (filt, char), refused)
                    self.assertIn('nothing was run', refused)

    def test_every_cmd_character_in_the_filter_refuses(self):
        for c in '&|<>^%!()"\n':
            with self.assertRaisesRegex(V.Usage, 'cmd.exe reads again', msg=repr(c)):
                V.parse_selector('engine/core/src/test/FooTest.java::a%sb' % c, RUNNERS)

    def test_line_break_is_named(self):
        with self.assertRaisesRegex(V.Usage, 'holds a line break'):
            V.parse_selector('engine/core/src/test/FooTest.java::a\nwhoami', RUNNERS)

    def test_default_filter_from_a_file_name_with_a_cmd_character_refuses(self):
        with self.assertRaisesRegex(V.Usage, r"default filter '\*\.a&whoami' \(from the file name\) holds &"):
            V.parse_selector('engine/core/src/test/a&whoami.java', RUNNERS)

    def test_runner_args_and_project_with_a_cmd_character_refuse(self):
        bad_args = dict(GRADLE, args=['-Px=a|b'])
        with self.assertRaisesRegex(V.Usage, r'"args" word .*holds \|'):
            V.parse_selector('engine/core/src/test/FooTest.java', [bad_args])
        bad_project = dict(GRADLE, project=':core&x')
        with self.assertRaisesRegex(V.Usage, 'the project .*holds &'):
            V.parse_selector('engine/core/src/test/FooTest.java', [bad_project])

    def test_real_gradle_filters_pass(self):
        for filt in ('com.x.FooTest', 'com.x.FooTest.bar*', '*Outer$Inner*', 'a_b-c.d', '*.my test*'):
            s = V.parse_selector('engine/core/src/test/FooTest.java::' + filt, RUNNERS)
            self.assertEqual(V.command(s, 'TW', 'LOGS', 1)[0][4], filt)

    def test_python_and_dotnet_filters_are_not_checked(self):
        # neither runs through a batch file: a dotnet filter's & and | are its own operators
        s = V.parse_selector('server/App.Tests/FooTests.cs::Name=A|Name=B', RUNNERS)
        self.assertIn('Name=A|Name=B', V.command(s, 'TW', 'LOGS', 1)[0])


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

    def test_a_load_failure_with_a_cr_in_the_name_is_not_red(self):
        """A CRLF selector puts a CR in the name unittest prints; the load failure must still
        count as the filter naming nothing, never as a red test."""
        nofilter = ('ERROR: T.test_x\r (unittest.loader._FailedTest.T.test_x\r)\r\n'
                    'AttributeError\r\nRan 1 test in 0s\r\n')
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
        p = os.path.join(self.dir, *name.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
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

    def gradle(self, rc, since, tw=None):
        sel = V.parse_selector('engine/core/src/test/FooTest.java', RUNNERS)
        return V.judge(sel, rc, '', tw or self.dir, self.dir, 1, [sel.path], since)

    def test_gradle_reads_a_test_task_not_named_test(self):
        """A module whose test task writes build/test-results/<task>/ is judged from that XML."""
        self.put('engine/core/build/test-results/integrationTest/TEST-a.FooTest.xml',
                 '<testsuite tests="3" failures="1" errors="0"/>')
        self.assertEqual(self.gradle(1, time.time() - 60), (V.RED, '1 of 3 test(s) failed'))

    def test_gradle_counts_the_own_class_and_its_nested_ones_of_this_run(self):
        """The selector's class (any package, or none) and its nested classes add up across task
        folders; its XML older than the run, and a class whose name only ends in it, do not."""
        base = 'engine/core/build/test-results/'
        self.put(base + 'test/TEST-a.FooTest.xml', '<testsuite tests="2" failures="0" errors="0"/>')
        self.put(base + 'test/TEST-a.FooTest$Inner.xml',
                 '<testsuite tests="1" failures="0" errors="1"/>')
        self.put(base + 'unitTest/TEST-FooTest.xml', '<testsuite tests="1" failures="0" errors="0"/>')
        self.put(base + 'otherTest/TEST-a.FooTest.xml',
                 '<testsuite tests="9" failures="9" errors="0"/>', age=3600)
        self.put(base + 'test/TEST-a.BarFooTest.xml', '<testsuite tests="5" failures="5" errors="0"/>')
        self.assertEqual(self.gradle(1, time.time() - 60), (V.RED, '1 of 4 test(s) failed'))

    def test_gradle_sibling_task_failure_of_another_class_is_not_red(self):
        """`--tests` filters only the named task; a sibling test task in the same build runs its
        whole suite, and its failures are no proof about the selector's test."""
        base = 'engine/core/build/test-results/'
        self.put(base + 'test/TEST-a.FooTest.xml', '<testsuite tests="2" failures="0" errors="0"/>')
        self.put(base + 'integrationTest/TEST-a.OtherIT.xml',
                 '<testsuite tests="3" failures="3" errors="0"/>')
        self.assertEqual(self.gradle(0, time.time() - 60), (V.GREEN, '2 test(s) passed'))
        self.assertEqual(self.gradle(1, time.time() - 60)[0], V.ERROR)

    def test_gradle_sibling_task_passing_another_class_is_not_green(self):
        self.put('engine/core/build/test-results/integrationTest/TEST-a.OtherIT.xml',
                 '<testsuite tests="4" failures="0" errors="0"/>')
        self.assertEqual(self.gradle(0, time.time() - 60), (V.NONE, 'no test ran'))

    def test_gradle_stale_failure_in_another_task_does_not_turn_green_red(self):
        base = 'engine/core/build/test-results/'
        self.put(base + 'test/TEST-a.FooTest.xml', '<testsuite tests="2" failures="0" errors="0"/>')
        self.put(base + 'slowTest/TEST-a.FooTest.xml',
                 '<testsuite tests="4" failures="4" errors="0"/>', age=3600)
        self.assertEqual(self.gradle(0, time.time() - 60), (V.GREEN, '2 test(s) passed'))

    def test_gradle_unparseable_xml_of_any_class_in_any_task_folder_is_error(self):
        """Fails closed: a broken file of the selector's class, or of another class."""
        base = 'engine/core/build/test-results/'
        self.put(base + 'test/TEST-a.FooTest.xml', '<testsuite tests="2" failures="0" errors="0"/>')
        for name in ('integrationTest/TEST-a.FooTest$Inner.xml', 'otherTest/TEST-a.OtherIT.xml'):
            p = self.put(base + name, '<testsuite tests="2"')
            self.assertEqual(self.gradle(0, time.time() - 60)[0], V.ERROR, name)
            os.remove(p)

    def test_gradle_module_root_with_glob_characters(self):
        """A tree whose path holds `[` is searched as a literal folder, not a glob pattern."""
        tw = os.path.join(self.dir, 'm[1]')
        self.put('m[1]/engine/core/build/test-results/test/TEST-a.FooTest.xml',
                 '<testsuite tests="3" failures="1" errors="0"/>')
        self.assertEqual(self.gradle(1, time.time() - 60, tw), (V.RED, '1 of 3 test(s) failed'))

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
