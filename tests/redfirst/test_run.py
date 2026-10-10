"""tools/redfirst.py end to end in throwaway repositories (tests/gitfixture.py): the refusals made
before anything exists, and real python runs in a throwaway worktree that is removed after.

Every worktree made or removed here is in the test's own temp folder.
"""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kitlib.redfirst import run as R
from kitlib.redfirst import verdict as V
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_redfirst_entry_for_tests',
                                               os.path.join(KIT, 'tools', 'redfirst.py'))
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

CALC = 'def add(a, b):\n    return a + b\n'
CALC_MUL = CALC + '\n\ndef mul(a, b):\n    return a * b\n'
TEST_MUL = ('import unittest\nfrom calc import mul\n\n\nclass T(unittest.TestCase):\n'
            '    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n')
TEST_ADD = ('import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n'
            '    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n')
CONFIG = {'redfirst': {'runners': [{'glob': 'tests/test_*.py', 'kind': 'python',
                                    'pythonpath': 'tests'},
                                   {'glob': 'src/test_*.py', 'kind': 'python', 'pythonpath': 'src'}]}}


class Case(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.fx.write('.kit.json', json.dumps(CONFIG) + '\n')
        self.fx.write('.gitignore', '.claude/\n')
        self.fx.write('src/calc.py', CALC)
        self.fx.write('tests/calc.py', CALC)
        self.base = self.fx.commit('base', '.kit.json', '.gitignore', 'src/calc.py', 'tests/calc.py')
        self.fx.git('push', '-q', 'origin', 'main')
        self.wt = os.path.realpath(self.fx.worktree('task'))
        self.logs = os.path.join(self.fx.base, 'logs')
        self.home = os.path.join(os.path.realpath(self.fx.root), '.claude', 'worktrees')

    def commit(self, files):
        for rel, text in files.items():
            self.fx.write(rel, text, root=self.wt)
        return self.fx.commit('work', *files, cwd=self.wt)

    def run_tool(self, *args, cleanup=None):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = tool.main(['-C', self.wt, '--log-dir', self.logs] + list(args),
                           cleanup=cleanup or self.here)
        return rc, out.getvalue()

    @staticmethod
    def here(main, tw, base, new, had, wait_s, say):
        return R.cleanup_here(main, tw, base, new, had, 0, say)

    def throwaways(self):
        return [n for n in os.listdir(self.home) if n.startswith(R.PREFIX)]


class Refusals(Case):
    def test_main_checkout_refused(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = tool.main(['-C', self.fx.root, '--tests', 'tests/test_mul.py'])
        self.assertEqual(rc, V.EXIT_USAGE)
        self.assertIn('main checkout', out.getvalue())

    def test_base_equals_head_refused(self):
        rc, out = self.run_tool('--tests', 'tests/test_mul.py')
        self.assertEqual(rc, V.EXIT_USAGE)
        self.assertIn('same commit', out)

    def test_uncommitted_test_file_refused(self):
        self.commit({'tests/test_mul.py': TEST_MUL})
        self.fx.write('tests/test_mul.py', TEST_MUL + '# edit\n', root=self.wt)
        rc, out = self.run_tool('--tests', 'tests/test_mul.py')
        self.assertEqual(rc, V.EXIT_USAGE)
        self.assertIn('uncommitted changes', out)
        self.assertEqual(self.throwaways(), [])

    def test_absent_file_refused(self):
        self.commit({'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'tests/test_other.py')
        self.assertEqual(rc, V.EXIT_USAGE)
        self.assertIn('not committed at head', out)

    def test_no_runner_refused(self):
        self.commit({'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'docs/x.py')
        self.assertEqual(rc, V.EXIT_USAGE)
        self.assertIn('no runner takes it', out)

    def test_base_not_ancestor_refused(self):
        head = self.commit({'tests/test_mul.py': TEST_MUL})
        self.fx.write('other.txt', 'x\n')
        other = self.fx.commit('other', 'other.txt')
        rc, out = self.run_tool('--tests', 'tests/test_mul.py', '--base', other, '--head', head)
        self.assertEqual(rc, V.EXIT_USAGE)
        self.assertIn('not an ancestor', out)

    def test_help_is_never_red(self):
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(tool.main(['--help']), V.EXIT_USAGE)


class Arguments(unittest.TestCase):
    def test_tests_repeat_accumulates(self):
        a = tool.parse_args(['--tests', 'tests/a.py', '--tests', 'tests/b.py::f'])
        self.assertEqual(a.tests, ['tests/a.py', 'tests/b.py::f'])

    def test_tests_one_flag_many_values(self):
        a = tool.parse_args(['--tests', 'tests/a.py', 'tests/b.py'])
        self.assertEqual(a.tests, ['tests/a.py', 'tests/b.py'])

    def test_tests_mixed_forms(self):
        a = tool.parse_args(['--tests', 'a', 'b', '--tests', 'c'])
        self.assertEqual(a.tests, ['a', 'b', 'c'])

    def test_also_repeat_accumulates(self):
        a = tool.parse_args(['--tests', 't', '--also', 'x', '--also', 'y'])
        self.assertEqual(a.also, ['x', 'y'])


class Runs(Case):
    def test_new_test_is_red_and_tree_removed(self):
        self.commit({'tests/calc.py': CALC_MUL, 'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'tests/test_mul.py')
        self.assertEqual(rc, V.EXIT_RED, out)
        self.assertIn('RED    tests/test_mul.py  does not import against the base', out)
        self.assertIn('REDFIRST OK', out)
        self.assertIn('removed', out)
        self.assertEqual(self.throwaways(), [])
        self.assertNotIn(R.PREFIX, self.fx.git('worktree', 'list'))

    def test_passing_test_is_green(self):
        self.commit({'src/test_add.py': TEST_ADD})
        rc, out = self.run_tool('--tests', 'src/test_add.py')
        self.assertEqual(rc, V.EXIT_GREEN, out)
        self.assertIn('GREEN  src/test_add.py', out)
        self.assertEqual(self.throwaways(), [])

    def test_filter_naming_nothing_is_undecided(self):
        # the module imports at the base (calc.add is there), so only the filter can miss
        self.commit({'tests/test_add.py': TEST_ADD})
        rc, out = self.run_tool('--tests', 'tests/test_add.py::T.test_none')
        self.assertEqual(rc, V.EXIT_UNDECIDED, out)
        self.assertIn('NONE   tests/test_add.py::T.test_none', out)

    def test_a_file_the_base_had_is_put_back(self):
        """--also a file the base has: the throwaway gets the head's copy, and the cleanup puts
        the base's back before removing the tree (a changed file would refuse the removal)."""
        self.commit({'tests/calc.py': CALC_MUL, 'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'tests/test_mul.py', '--also', 'tests/calc.py')
        self.assertEqual(rc, V.EXIT_GREEN, out)     # with the head's calc.py the test passes
        self.assertIn('removed', out)
        self.assertEqual(self.throwaways(), [])

    def test_cleanup_in_child_process(self):
        self.commit({'tests/calc.py': CALC_MUL, 'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'tests/test_mul.py', cleanup=R.cleanup_in_child)
        self.assertEqual(rc, V.EXIT_RED, out)
        self.assertIn('removed', out)
        self.assertEqual(self.throwaways(), [])

    def test_left_files_leave_the_tree_and_exit_4(self):
        self.commit({'tests/calc.py': CALC_MUL, 'tests/test_mul.py': TEST_MUL})

        def litter(argv, cwd, env, log):
            with open(os.path.join(cwd, 'stray.txt'), 'w') as f:
                f.write('x')
            return R.default_run(argv, cwd, env, log)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = tool.main(['-C', self.wt, '--log-dir', self.logs, '--tests', 'tests/test_mul.py'],
                           run=litter, cleanup=self.here)
        self.assertEqual(rc, V.EXIT_LEFTOVER, out.getvalue())
        self.assertIn('holds files the run left', out.getvalue())
        self.assertIn('worktree_remove.py redfirst-', out.getvalue())
        left = self.throwaways()
        self.assertEqual(len(left), 1)
        tw = os.path.join(self.home, left[0])
        os.remove(os.path.join(tw, 'stray.txt'))
        self.fx.git('worktree', 'remove', tw)       # the test's own throwaway, in its temp repo


if __name__ == '__main__':
    unittest.main()
