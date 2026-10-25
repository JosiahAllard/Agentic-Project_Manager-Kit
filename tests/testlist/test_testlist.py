"""kitlib.testlist and tools/testlist.py: the moves gate's identical-test-names check.
Every case reads result files it wrote into its own temporary git checkout
(tests/gitfixture.py), passed as `-C`."""
import importlib.util
import io
import json
import os
import tempfile
import unittest

from kitlib.testlist import results
from tests.gitfixture import TempRepo, rmtree

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_testlist_entry_for_tests',
                                               os.path.join(KIT, 'tools', 'testlist.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

XML = ('<testsuite><testcase classname="a.B" name="ok"/>'
       '<testcase classname="a.B" name="bad"><failure message="boom"/></testcase>'
       '<testcase classname="a.B" name="off"><skipped/></testcase></testsuite>')
TRX = ('<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>'
       '<UnitTestResult testName="N.T.pass" outcome="Passed"/>'
       '<UnitTestResult testName="N.T.fail" outcome="Failed"/>'
       '<UnitTestResult testName="N.T.skip" outcome="NotExecuted"/>'
       '</Results></TestRun>')


class Tree(unittest.TestCase):
    """A temporary checkout that is both the configuration's home (`-C`) and the results folder."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.top = self.repo.root
        self.xml = self.put('m/build/test-results/test/TEST-a.B.xml', XML)

    def put(self, rel, text):
        path = os.path.join(self.top, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return path

    def set_config(self, data):
        with open(os.path.join(self.top, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump(data, fh)

    def run_main(self, *args, top=True, here=True):
        out = io.StringIO()
        argv = list(args) + (['--dir', self.top] if top else []) + (['-C', self.top] if here else [])
        code = cli.main(argv, out=out)
        return code, out.getvalue()


class TestNames(Tree):
    """A repository's repath TestNames, unchanged but for the module."""

    def test_load_reports_each_names_status_when_asked(self):
        statuses = {}
        tests = results.load_names(self.top, statuses)
        self.assertEqual(tests, ['a.B.bad', 'a.B.off', 'a.B.ok'])
        self.assertEqual(statuses, {'a.B.ok': 'passed', 'a.B.bad': 'failed', 'a.B.off': 'skipped'})

    def test_without_the_dict_load_is_unchanged(self):
        self.assertEqual(results.load_names(self.top), ['a.B.bad', 'a.B.off', 'a.B.ok'])

    def test_names_prints_the_sorted_list(self):
        self.assertEqual(self.run_main('--names'), (0, 'a.B.bad\na.B.off\na.B.ok\n'))

    def test_save_then_diff_lists_added_and_removed(self):
        saved = os.path.join(self.top, 'before.txt')
        self.assertEqual(self.run_main('--save', saved)[0], 0)
        os.utime(saved, (1, 1))                                  # the results are now newer
        self.assertEqual(self.run_main('--diff', saved)[0], 0)
        with open(self.xml, 'w', encoding='utf-8') as fh:
            fh.write(XML.replace('name="off"', 'name="renamed"'))
        code, text = self.run_main('--diff', saved)
        self.assertEqual(code, 1)
        self.assertIn('added 1 | removed 1', text)
        self.assertIn('  - a.B.off', text)
        self.assertIn('  + a.B.renamed', text)

    def test_diff_refuses_when_no_result_is_newer_than_the_save(self):
        saved = os.path.join(self.top, 'before.txt')
        self.run_main('--save', saved)
        os.utime(self.xml, (1, 1))
        code, text = self.run_main('--diff', saved)
        self.assertEqual(code, 2)
        self.assertIn('no test result is newer', text)

    def test_nothing_found_is_exit_2(self):
        empty = tempfile.mkdtemp(prefix='testlist_empty_')
        self.addCleanup(rmtree, empty)
        out = io.StringIO()
        self.assertEqual(cli.main(['--names', '--dir', empty, '-C', self.top], out=out), 2)
        self.assertIn('/tools/gate.py" -C ', out.getvalue())     # the kit's gate, never a repository copy


class MoreThanRepathTested(Tree):

    def test_status_lines_carry_a_tab_and_a_status(self):
        code, text = self.run_main('--names', '--status')
        self.assertEqual(code, 0)
        self.assertEqual(text, 'a.B.bad\tfailed\na.B.off\tskipped\na.B.ok\tpassed\n')

    def test_a_test_that_now_fails_shows_as_removed_and_added_under_status(self):
        saved = os.path.join(self.top, 'before.txt')
        self.run_main('--save', saved, '--status')
        os.utime(saved, (1, 1))
        with open(self.xml, 'w', encoding='utf-8') as fh:
            fh.write(XML.replace('<testcase classname="a.B" name="ok"/>',
                                 '<testcase classname="a.B" name="ok"><error/></testcase>'))
        code, text = self.run_main('--diff', saved, '--status')
        self.assertEqual(code, 1)
        self.assertIn('  - a.B.ok\tpassed', text)
        self.assertIn('  + a.B.ok\tfailed', text)

    def test_trx_results_are_read(self):
        self.put('net/TestResults/run.trx', TRX)
        statuses = {}
        names = results.load_names(self.top, statuses)
        self.assertEqual(names, ['N.T.fail', 'N.T.pass', 'N.T.skip', 'a.B.bad', 'a.B.off', 'a.B.ok'])
        self.assertEqual((statuses['N.T.pass'], statuses['N.T.fail'], statuses['N.T.skip']),
                         ('passed', 'failed', 'skipped'))

    def test_the_worst_status_of_a_repeated_name_wins(self):
        self.put('n/build/test-results/test/TEST-again.xml',
                 '<testsuite><testcase classname="a.B" name="ok"><failure/></testcase></testsuite>')
        statuses = {}
        results.load_names(self.top, statuses)
        self.assertEqual(statuses['a.B.ok'], 'failed')

    def test_a_result_under_a_claude_folder_is_skipped(self):
        self.put('.claude/worktrees/w/m/build/test-results/test/TEST-w.xml',
                 '<testsuite><testcase classname="w.W" name="t"/></testsuite>')
        self.assertNotIn('w.W.t', results.load_names(self.top))

    def test_inside_a_claude_folder_its_results_are_read(self):
        inner = os.path.join(self.top, '.claude', 'worktrees', 'w')
        self.put('.claude/worktrees/w/m/build/test-results/test/TEST-w.xml',
                 '<testsuite><testcase classname="w.W" name="t"/></testsuite>')
        self.assertEqual(results.load_names(inner), ['w.W.t'])

    def test_an_unparsable_result_file_is_skipped(self):
        self.put('z/build/test-results/test/TEST-broken.xml', '<testsuite><testcase')
        self.assertEqual(results.load_names(self.top), ['a.B.bad', 'a.B.off', 'a.B.ok'])

    def test_against_diffs_two_saved_lists_with_no_freshness_check(self):
        old = self.put('old.txt', 'a\nb\n')
        new = self.put('new.txt', 'b\nc\n')
        code, text = self.run_main('--diff', old, '--against', new, top=False, here=False)
        self.assertEqual(code, 1)
        self.assertIn('names before 2 | after 2 | added 1 | removed 1', text)
        self.assertIn('  - a', text)
        self.assertIn('  + c', text)
        same = self.put('same.txt', 'b\na\n')
        self.assertEqual(self.run_main('--diff', old, '--against', same, top=False,
                                       here=False)[0], 0)

    def test_against_without_diff_is_usage(self):
        code, text = self.run_main('--names', '--against', 'x', top=False)
        self.assertEqual(code, 2)
        self.assertIn('--against goes with --diff', text)

    def test_the_default_dir_is_the_checkout(self):
        code, text = self.run_main('--names', top=False)
        self.assertEqual((code, text), (0, 'a.B.bad\na.B.off\na.B.ok\n'))

    def test_a_folder_that_is_no_checkout_refuses(self):
        empty = tempfile.mkdtemp(prefix='testlist_nogit_')
        self.addCleanup(rmtree, empty)
        out = io.StringIO()
        self.assertEqual(cli.main(['--names', '-C', empty], out=out), 2)
        self.assertIn('testlist: refused:', out.getvalue())

    def test_a_bad_option_is_usage(self):
        with self.assertRaises(SystemExit) as cm:
            cli.build_parser().parse_args(['--names', '--save', 'x'])
        self.assertEqual(cm.exception.code, 2)


class TheResultGlobsComeFromConfig(Tree):

    def test_junit_globs(self):
        self.put('out/junit/TEST-x.xml', '<testsuite><testcase classname="x.X" name="t"/></testsuite>')
        self.set_config({'testlist': {'junit': ['out/junit/*.xml']}})
        self.assertEqual(self.run_main('--names'), (0, 'x.X.t\n'))

    def test_trx_globs(self):
        self.put('net/run.trx', TRX)
        self.put('elsewhere/run.trx', TRX.replace('N.T.', 'O.T.'))
        self.set_config({'testlist': {'junit': [], 'trx': ['net/*.trx']}})
        self.assertEqual(self.run_main('--names'), (0, 'N.T.fail\nN.T.pass\nN.T.skip\n'))

    def test_the_hint_names_the_kits_gate_whatever_the_tools_dir(self):
        self.set_config({'repo': {'tools_dir': 'scripts'}, 'testlist': {'junit': ['none/*.xml']}})
        code, text = self.run_main('--names')
        self.assertEqual(code, 2)
        self.assertIn('/tools/gate.py" -C ', text)
        self.assertNotIn('scripts/gate.py', text)

    def refused(self, section):
        self.set_config({'testlist': section})
        code, text = self.run_main('--names')
        self.assertEqual(code, 2)
        self.assertIn('testlist: refused: kit configuration:', text)
        return text

    def test_an_unknown_key_refuses(self):
        self.assertIn('xml', self.refused({'xml': ['*.xml']}))

    def test_a_glob_list_that_is_not_one_refuses(self):
        self.assertIn('junit', self.refused({'junit': '*.xml'}))
        self.assertIn('trx', self.refused({'trx': ['']}))

    def test_both_empty_refuses(self):
        self.assertIn('both empty', self.refused({'junit': [], 'trx': []}))


if __name__ == '__main__':
    unittest.main()
