"""tools/redfirst.py end to end in throwaway repositories (tests/gitfixture.py): the refusals made
before anything exists, and real python runs in a throwaway worktree that is removed after.

Every worktree made or removed here is in the test's own temp folder.
"""
import importlib.util
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kitlib.config import load
from kitlib.git import cmd
from kitlib.records import landing
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
    KIT_JSON = CONFIG                               # the fixture's .kit.json
    CALC_AT = ('src/calc.py', 'tests/calc.py')      # where the base has calc.py

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.fx.write('.kit.json', json.dumps(self.KIT_JSON) + '\n')
        self.fx.write('.gitignore', '.claude/\n')
        for rel in self.CALC_AT:
            self.fx.write(rel, CALC)
        self.base = self.fx.commit('base', '.kit.json', '.gitignore', *self.CALC_AT)
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

    def test_no_runner_names_the_written_globs_and_the_key(self):
        """Runners written in .kit.json are used as they are: no tools-folder glob is added, and
        the refusal says the globs are the written ones, with what each matches at head."""
        head = self.commit({'tools/test_mul.py': TEST_MUL, 'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'tools/test_mul.py')
        self.assertEqual(rc, V.EXIT_USAGE, out)
        self.assertIn("refused: --tests 'tools/test_mul.py': no runner takes it; nothing was run", out)
        self.assertIn("glob 'tests/test_*.py' (python): matches 1 file(s) committed at head %s"
                      % head[:10], out)
        self.assertIn("glob 'src/test_*.py' (python): matches no file committed at head", out)
        self.assertIn('these are the globs `.kit.json` "redfirst".runners holds', out)
        self.assertNotIn('tools/*test_*.py', out)
        self.assertNotIn('not set', out)
        self.assertEqual(self.throwaways(), [])

    def test_a_path_outside_the_repo_keeps_its_usage_line(self):
        self.commit({'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', '../test_mul.py')
        self.assertEqual(rc, V.EXIT_USAGE, out)
        self.assertIn('a repo-relative file path', out)
        self.assertNotIn('no runner takes it', out)

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


class UnwrittenRunners(Case):
    """No "redfirst".runners in .kit.json, and the tools folder moved ("repo".tools_dir): the
    schema default glob, then one for test files of the tools folder."""
    KIT_JSON = {'repo': {'tools_dir': 'dev/tools'}}
    CALC_AT = ('dev/tools/calc.py', 'tests/calc.py')

    def test_runners_are_the_default_then_the_tools_folder(self):
        runners, written = R.runners_for(load(self.fx.root))
        self.assertFalse(written)
        self.assertEqual(runners, [{'glob': 'tests/*test_*.py', 'kind': 'python'},
                                   {'glob': 'dev/tools/*test_*.py', 'kind': 'python',
                                    'pythonpath': 'dev/tools'}])

    def test_a_test_in_the_tools_folder_is_found_and_run(self):
        self.commit({'dev/tools/calc.py': CALC_MUL, 'dev/tools/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'dev/tools/test_mul.py')
        self.assertEqual(rc, V.EXIT_RED, out)
        self.assertIn('RED    dev/tools/test_mul.py  does not import against the base', out)
        self.assertEqual(self.throwaways(), [])

    def test_a_tools_folder_test_runs_with_the_tools_folder_on_the_path(self):
        """GREEN proves the module was imported with the tools folder as PYTHONPATH: `from calc
        import add` resolves only there."""
        self.commit({'dev/tools/test_add.py': TEST_ADD})
        rc, out = self.run_tool('--tests', 'dev/tools/test_add.py')
        self.assertEqual(rc, V.EXIT_GREEN, out)
        self.assertIn('GREEN  dev/tools/test_add.py', out)

    def test_a_test_no_default_glob_takes_is_refused_naming_globs_and_key(self):
        self.commit({'spec/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'spec/test_mul.py')
        self.assertEqual(rc, V.EXIT_USAGE, out)
        self.assertIn("refused: --tests 'spec/test_mul.py': no runner takes it; nothing was run", out)
        self.assertIn("glob 'tests/*test_*.py' (python): matches no file committed at head", out)
        self.assertIn("glob 'dev/tools/*test_*.py' (python): matches no file committed at head", out)
        self.assertIn('`.kit.json` "redfirst".runners is not set, so these are the defaults', out)
        self.assertIn('set `.kit.json` "redfirst".runners to an entry whose glob takes the file', out)
        self.assertEqual(self.throwaways(), [])
        self.assertFalse(os.path.exists(self.logs), 'a refusal makes nothing')


    def test_a_second_tests_value_no_runner_takes_is_the_one_refused(self):
        self.commit({'dev/tools/test_add.py': TEST_ADD, 'spec/test_mul.py': TEST_MUL})
        rc, out = self.run_tool('--tests', 'dev/tools/test_add.py', 'spec/test_mul.py')
        self.assertEqual(rc, V.EXIT_USAGE, out)
        self.assertIn("refused: --tests 'spec/test_mul.py': no runner takes it", out)
        self.assertIn("glob 'dev/tools/*test_*.py' (python): matches 1 file(s) committed", out)
        self.assertEqual(self.throwaways(), [])


class FakeConfig(object):
    """What runners_for reads of a loaded configuration."""

    def __init__(self, tools_dir, runners=None, written=None):
        self.tools_dir, self.runners, self.written = tools_dir, runners, written

    def section(self, name):
        return {'runners': list(self.runners if self.runners is not None
                                else [{'glob': 'tests/*test_*.py', 'kind': 'python'}])}

    def raw(self, name):
        return {} if self.written is None else {'runners': self.written}


class DerivedRunner(unittest.TestCase):
    def derived(self, tools_dir, **kw):
        return R.runners_for(FakeConfig(tools_dir, **kw))[0][1:]

    def test_the_tools_folder_is_normalised_before_the_glob_is_made(self):
        want = [{'glob': 'tools/*test_*.py', 'kind': 'python', 'pythonpath': 'tools'}]
        for spelt in ('tools', './tools', ' tools', 'tools/', '.\\tools\\', 'tools/./'):
            self.assertEqual(self.derived(spelt), want, spelt)
        self.assertTrue(V.matches('tools/test_x.py', self.derived('./tools')[0]['glob']))

    def test_a_tools_folder_that_is_the_root_adds_nothing(self):
        for spelt in ('.', './', ' ', ''):
            self.assertEqual(self.derived(spelt), [], repr(spelt))

    def test_a_glob_already_listed_is_not_added_again(self):
        listed = [{'glob': 'Tools/*test_*.py', 'kind': 'python'}]
        self.assertEqual(R.runners_for(FakeConfig('tools', runners=listed)), (listed, False))

    def test_an_empty_written_list_stays_empty_and_the_refusal_says_so(self):
        runners, written = R.runners_for(FakeConfig('tools', runners=[], written=[]))
        self.assertEqual((runners, written), ([], True))
        said = R.no_runner_text('tests/test_x.py', runners, written, 'a' * 40, [])
        self.assertIn('`.kit.json` "redfirst".runners is an empty list', said)
        self.assertNotIn('not set', said)


class WrittenRunners(Case):
    def test_written_runners_are_used_as_they_are(self):
        runners, written = R.runners_for(load(self.fx.root))
        self.assertTrue(written)
        self.assertEqual(runners, CONFIG['redfirst']['runners'])


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


class AKilledGitIsNoAnswer(Case):
    """`rev-parse --verify --quiet` said "no such commit" as exit 1 with
    no output, which a git ended from outside also leaves. Both ways git is started here are
    stubbed (cmd._run, and cmd.subprocess_run, which rev() used), so the old code meets the kill
    too and fails on what it says, not because real git answered."""

    def test_a_killed_read_of_a_rev_is_refused_as_unread_not_no_commit(self):
        real_run, real_sp = cmd.spawner(), cmd.subprocess_run
        spec = 'HEAD^{commit}'

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] == 'rev-parse' and argv[-1] == spec:
                return 1, b'', b''
            return real_run(argv, cwd=cwd, input=input, timeout=timeout, env=env)

        def sp(argv, **kw):
            if 'rev-parse' in argv and argv[-1] == spec:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return real_sp(argv, **kw)
        with mock.patch.object(cmd, '_run', side_effect=run), \
                mock.patch.object(cmd, 'subprocess_run', side_effect=sp):
            with self.assertRaises(V.Usage) as cm:
                R.rev(self.wt, 'HEAD')
        said = str(cm.exception)
        self.assertNotIn('does not name a commit', said)
        self.assertIn('whether HEAD names a commit in %s could not be read' % self.wt, said)
        self.assertIn('printed nothing', said)

    def test_a_rev_that_names_nothing_is_still_a_usage_error(self):
        with self.assertRaises(V.Usage) as cm:
            R.rev(self.wt, 'no-such-branch')
        self.assertIn('usage: no-such-branch does not name a commit', str(cm.exception))


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

    def test_red_with_the_landing_lock_held_and_the_head_unpushed(self):
        """new_worktree refuses a base with no pushed branch; this tool needs neither the lock
        free nor a pushed head (the `route` line of kitlib/worktree/new.py points here)."""
        self.commit({'tests/calc.py': CALC_MUL, 'tests/test_mul.py': TEST_MUL})
        self.assertEqual(self.fx.git('for-each-ref', '--format=%(refname)', 'refs/remotes').strip(),
                         'refs/remotes/origin/main', 'only main is pushed, not the branch under test')
        lock = landing.LandingLock.for_config(self.fx.root, load(self.fx.root))
        lock.take('full', ['feat/landing'])
        self.assertIsNotNone(lock.read())
        rc, out = self.run_tool('--tests', 'tests/test_mul.py')
        self.assertEqual(rc, V.EXIT_RED, out)
        self.assertIn('REDFIRST OK', out)
        self.assertEqual(self.throwaways(), [])

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
        self.assertRegex(out.getvalue(), r'python "[^"]*/tools/worktree_remove\.py" -C "[^"]+" redfirst-')
        left = self.throwaways()
        self.assertEqual(len(left), 1)
        tw = os.path.join(self.home, left[0])
        os.remove(os.path.join(tw, 'stray.txt'))
        self.fx.git('worktree', 'remove', tw)       # the test's own throwaway, in its temp repo


if __name__ == '__main__':
    unittest.main()
