"""kitlib.tooldeps.selftree and tools/kit_tests.py: which of the kit's own test packages a change
selects (docs/test-tools.md "kit_tests"). Most tests run on a throwaway tree laid out like the
kit's; a few check the kit's real tree, so a table or scan regression shows on the kit itself.
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
import unittest.mock

from kitlib.tooldeps import selftree
from kitlib.tooldeps.deps import ALL
from tests.gitfixture import TempRepo, rmtree
from tests.home import TempHomeCase

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tests_entry_for_tests',
                                               os.path.join(KIT, 'tools', 'kit_tests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

PASSING = 'import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n'

TABLE = {
    'comment': 'fixture table',
    'shared_core': ['git'],
    'full': ['tests/*.py', 'tests/selection.json'],
    'paths': [['data/**', ['leaf']]],
    'by_name': ['hooks/**'],
    'prose': ['docs/**', '*.md'],
}

FILES = {
    'kitlib/__init__.py': '',
    'kitlib/git/__init__.py': "HELPER = 'solo.py'\n",
    'kitlib/leaf/__init__.py': 'import kitlib.git\n',
    'kitlib/mid/__init__.py': 'from kitlib import leaf\n',
    'kitlib/top/__init__.py': 'from ..mid import thing\n',
    'kitlib/child/__init__.py': "CMD = 'python -c \"from kitlib.leaf import x\"'\n",
    'kitlib/lone/__init__.py': "LABEL = 'kitlib.leaf'\n\n\ndef f():\n    \"\"\"Uses kitlib.leaf.\"\"\"\n",
    'tools/leaftool.py': 'from kitlib.leaf import x\nfrom kitlib.git import y\n',
    'tools/wrapper.py': 'import leaftool\n',
    'tests/__init__.py': '',
    'tests/testutil.py': '',
    'tests/selection.json': json.dumps(TABLE),
    'tests/git/__init__.py': '',
    'tests/git/test_git.py': PASSING,
    'tests/leaf/__init__.py': '',
    'tests/leaf/test_leaf.py': PASSING,
    'tests/leaf/helper.py': '',
    'tests/leaf/loose/test_skipped.py': PASSING,
    'tests/mid/__init__.py': '',
    'tests/mid/test_mid.py': PASSING,
    'tests/top/__init__.py': '',
    'tests/top/test_top.py': PASSING,
    'tests/child/__init__.py': '',
    'tests/child/test_child.py': PASSING,
    'tests/lone/__init__.py': '',
    'tests/lone/test_lone.py': PASSING,
    'tests/cli/__init__.py': '',
    'tests/cli/test_cli.py': PASSING + '# runs tools/wrapper.py\n',
    'tests/docsy/__init__.py': '',
    'tests/docsy/test_docs.py': PASSING + '# reads docs/guide.md\n',
    'tests/shared/__init__.py': '',
    'tests/shared/helpers.py': '',
    'tests/shared/test_shared.py': PASSING,
    'tests/user/__init__.py': '',
    'tests/user/test_user.py': 'from tests.shared import helpers  # noqa: F401\n' + PASSING,
    # a test that reaches kitlib only as `from kitlib import X` (no `kitlib.X` text)
    'tests/fromimp/__init__.py': '',
    'tests/fromimp/test_fromimp.py': 'from kitlib import mid as m  # noqa: F401\n' + PASSING,
    # kitlib packages that run a tool by path; the shared core's run is not closed over
    'tools/solo.py': 'import os\n',
    'kitlib/runner/__init__.py': "CMD = ['python', 'tools/solo.py']\n",
    'kitlib/runneruser/__init__.py': 'from kitlib import runner\n',
    'tests/runner/__init__.py': '',
    'tests/runner/test_runner.py': PASSING,
    'tests/runneruser/__init__.py': '',
    'tests/runneruser/test_runneruser.py': PASSING,
}
FIXTURE_TEST_PACKAGES = 13


def write_tree(root):
    for rel, text in FILES.items():
        full = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)


class FixtureTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-selftree-')
        self.addCleanup(rmtree, self.root)
        write_tree(self.root)
        self.tree = selftree.scan(self.root)
        self.cfg = selftree.config(self.root)

    def select(self, *paths):
        return selftree.selection(list(paths), self.tree, self.cfg)

    def test_leaf_change_selects_its_tests_and_its_importers_only(self):
        picked, why, notes = self.select('kitlib/leaf/x.py')
        # leaf; mid imports it, top imports mid; child runs it in a `-c` child; cli names a
        # tool that imports a tool that imports it; fromimp has `from kitlib import mid`. lone
        # only names it in a label and a docstring.
        self.assertEqual(picked, ['child', 'cli', 'fromimp', 'leaf', 'mid', 'top'])
        self.assertEqual(why['leaf'][0], 'own tests of changed kitlib/leaf')
        self.assertIn('kitlib/top imports kitlib/leaf (kitlib/leaf/x.py)', why['top'])
        self.assertEqual(notes, [])

    def test_a_change_with_no_importers_selects_only_its_own_tests(self):
        self.assertEqual(self.select('kitlib/top/a.py')[0], ['top'])

    def test_shared_core_change_selects_everything(self):
        picked, why, _ = self.select('kitlib/git/cmd.py')
        self.assertIs(picked, ALL)
        self.assertEqual(why, ['full suite: shared core: kitlib/git changed (kitlib/git/cmd.py)'])

    def test_shared_core_wins_over_a_leaf_in_the_same_change(self):
        self.assertIs(self.select('kitlib/top/a.py', 'kitlib/git/cmd.py')[0], ALL)

    def test_unmapped_file_selects_everything(self):
        picked, why, _ = self.select('weird/file.txt')
        self.assertIs(picked, ALL)
        self.assertIn('not mapped', why[0])

    def test_kitlib_folder_that_is_not_a_package_selects_everything(self):
        self.assertIs(self.select('kitlib/nope/a.py')[0], ALL)

    def test_test_helper_and_the_table_select_everything(self):
        for path in ('tests/testutil.py', 'tests/selection.json'):
            picked, why, _ = self.select(path)
            self.assertIs(picked, ALL, path)
            self.assertIn('test helper or the selection itself', why[0])

    def test_tool_change_selects_its_package_but_not_the_shared_core(self):
        picked, why, _ = self.select('tools/leaftool.py')
        self.assertEqual(picked, ['cli', 'leaf'])
        self.assertIn('names tools/wrapper.py, which tools/leaftool.py reaches', why['cli'])

    def test_test_package_from_kitlib_import_counts(self):
        self.assertIn('mid', self.tree.tests['fromimp'].kitlib)
        self.assertIn('fromimp', self.select('kitlib/mid/a.py')[0])

    def test_tool_run_by_path_selects_its_runners_and_their_importers(self):
        picked, why, _ = self.select('tools/solo.py')
        # runner runs it, runneruser imports runner; git (shared core) runs it too, but only
        # git's own tests run, not everything that imports git.
        self.assertEqual(picked, ['git', 'runner', 'runneruser'])
        self.assertIn('kitlib/runner runs tools/solo.py, which tools/solo.py reaches',
                      why['runner'])
        self.assertIn('kitlib/git (shared core) runs tools/solo.py, which tools/solo.py reaches',
                      why['git'])

    def test_tool_no_test_reaches_selects_everything(self):
        with open(os.path.join(self.root, 'tools', 'orphan.py'), 'w') as fh:
            fh.write('import kitlib.git\n')
        tree = selftree.scan(self.root)
        self.assertIs(selftree.selection(['tools/orphan.py'], tree, self.cfg)[0], ALL)

    def test_test_package_change_selects_it_and_its_importers(self):
        self.assertEqual(self.select('tests/shared/helpers.py')[0], ['shared', 'user'])
        self.assertEqual(self.select('tests/leaf/test_leaf.py')[0], ['leaf'])

    def test_prose_selects_the_tests_naming_it_or_nothing(self):
        self.assertEqual(self.select('docs/guide.md')[0], ['docsy'])
        picked, _why, notes = self.select('docs/other.md', 'README.md')
        self.assertEqual(picked, [])
        self.assertEqual(notes, ['README.md', 'docs/other.md'])

    def test_prose_does_not_hide_a_code_change(self):
        self.assertEqual(self.select('docs/other.md', 'kitlib/top/a.py')[0], ['top'])

    def test_always_packages_join_any_change_that_is_not_everything(self):
        cfg = dict(self.cfg, always=['lone'])
        sel = lambda *p: selftree.selection(list(p), self.tree, cfg)
        picked, why, notes = sel('docs/other.md')              # prose no test names
        self.assertEqual(picked, ['lone'])
        self.assertEqual(why['lone'], ['reads every tracked file (always, tests/selection.json)'])
        self.assertEqual(notes, ['docs/other.md'])
        self.assertEqual(sel('kitlib/top/a.py')[0], ['lone', 'top'])
        self.assertIs(sel('weird/file.txt')[0], ALL)           # everything stays everything
        self.assertEqual(sel()[0], [])                         # no change, nothing to scan

    def test_always_must_be_a_list_of_strings(self):
        with self.assertRaises(selftree.ConfigError):
            selftree.config(self.root, dict(TABLE, always='scrub'))

    def test_by_name_file_no_test_names_selects_everything(self):
        self.assertIs(self.select('hooks/pre.py')[0], ALL)

    def test_paths_table_adds_packages(self):
        picked, why, _ = self.select('data/x.json')
        self.assertEqual(picked, ['leaf'])
        self.assertEqual(why['leaf'], ['reads data/x.json (tests/selection.json)'])

    def test_test_modules_mirror_discover(self):
        self.assertEqual(selftree.test_modules(self.root, ['leaf', 'user']),
                         ['tests.leaf.test_leaf', 'tests.user.test_user'])

    def test_all_test_modules_mirror_discover_over_the_whole_tree(self):
        mods = selftree.all_test_modules(self.root)
        self.assertIn('tests.leaf.test_leaf', mods)
        self.assertIn('tests.testutil', mods)                       # discover's test*.py takes it
        self.assertNotIn('tests.leaf.loose.test_skipped', mods)     # not a package
        self.assertEqual(len(mods), 14)

    def test_string_refs_only_count_when_they_import(self):
        self.assertEqual(selftree.code_refs("X = 'kitlib.leaf'\n")[0], set())
        self.assertEqual(selftree.code_refs("X = '-m kitlib.leaf.run'\n")[0], {'leaf'})
        self.assertEqual(selftree.code_refs('from kitlib import a, b\n')[0], {'a', 'b'})
        self.assertEqual(selftree.code_refs('def f(:\n kitlib.leaf\n')[0], {'leaf'})


class Config(unittest.TestCase):
    def test_defaults_without_a_table(self):
        cfg = selftree.config('unused', {})
        self.assertEqual(cfg['shared_core'], ['git', 'config', 'proc', 'lock'])
        self.assertEqual(cfg['serial'], [])
        self.assertEqual(cfg['long'], [])

    def test_serial_entries_are_module_and_reason_pairs(self):
        cfg = selftree.config('unused', {'serial': [['tests.a.test_a', 'shares a mutex']]})
        self.assertEqual(cfg['serial'], [('tests.a.test_a', 'shares a mutex')])

    def test_bad_tables_are_refused(self):
        for data in ([], {'nope': []}, {'full': 'tests/*.py'}, {'paths': [['a/**', 'leaf']]},
                     {'shared_core': ['']}, {'serial': ['tests.a.test_a']},
                     {'serial': [['tests.a.test_a']]}, {'serial': [['tests.a.test_a', ' ']]},
                     {'serial': [['a.test_a', 'why']]}, {'long': 'tests.a.test_a'},
                     {'long': ['a.test_a']}, {'long': [['tests.a.test_a']]}):
            with self.assertRaises(selftree.ConfigError, msg=repr(data)):
                selftree.config('unused', data)

    def test_skip_fails_entries_are_module_and_reason_pairs(self):
        self.assertEqual(selftree.config('unused', {})['skip_fails'], [])
        cfg = selftree.config('unused', {'skip_fails': [['tests.a.test_a', 'the proof']]})
        self.assertEqual(cfg['skip_fails'], [('tests.a.test_a', 'the proof')])
        for bad in (['tests.a.test_a'], [['tests.a.test_a']], [['tests.a.test_a', ' ']],
                    [['a.test_a', 'why']], 'tests.a.test_a'):
            with self.assertRaises(selftree.ConfigError, msg=repr(bad)):
                selftree.config('unused', {'skip_fails': bad})

    def test_stale_skip_fails_names_a_module_the_tree_lacks(self):
        pairs = [('tests.a.test_a', 'x'), ('tests.a.test_gone', 'y')]
        self.assertEqual(selftree.stale_skip_fails(pairs, ['tests.a.test_a']), ['tests.a.test_gone'])
        self.assertEqual(selftree.stale_skip_fails(pairs, ['tests.a.test_a', 'tests.a.test_gone']), [])

    def test_malformed_json_is_refused(self):
        root = tempfile.mkdtemp(prefix='kit-selftree-')
        self.addCleanup(rmtree, root)
        os.makedirs(os.path.join(root, 'tests'))
        with open(os.path.join(root, 'tests', 'selection.json'), 'w') as fh:
            fh.write('{')
        with self.assertRaises(selftree.ConfigError):
            selftree.config(root)


class GitAndCli(TempHomeCase):
    def setUp(self):
        super().setUp()         # TempHomeCase: kit_tests' run folder comes from user.make_temp
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        write_tree(self.repo.root)
        self.repo.commit('tree', '.')
        self.repo.git('checkout', '-q', '-b', 'kit/x')

    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(['-C', self.repo.root] + list(argv), out=out)
        return rc, out.getvalue()

    def test_changed_paths_cover_commits_work_tree_and_untracked(self):
        self.repo.write('kitlib/top/a.py', 'A = 1\n')
        self.repo.commit('top', 'kitlib/top/a.py')
        self.repo.write('kitlib/mid/__init__.py', 'from kitlib import leaf\nB = 2\n')
        self.repo.write('docs/new.md', 'new\n')
        self.repo.git('checkout', '-q', 'main')
        self.repo.write('main-only.txt', 'x\n')
        self.repo.commit('main moves on', 'main-only.txt')
        self.repo.git('checkout', '-q', 'kit/x')
        paths, why = selftree.changed_paths('main', self.repo.root)
        self.assertIsNone(why)
        self.assertEqual(paths, ['docs/new.md', 'kitlib/mid/__init__.py', 'kitlib/top/a.py'])

    def test_changed_paths_list_both_sides_of_a_rename(self):
        self.repo.git('mv', 'tests/leaf/helper.py', 'tests/leaf/helper2.py')
        self.repo.git('commit', '-q', '-m', 'rename')
        paths, why = selftree.changed_paths('main', self.repo.root)
        self.assertIsNone(why)
        self.assertEqual(paths, ['tests/leaf/helper.py', 'tests/leaf/helper2.py'])
        tree, cfg = selftree.scan(self.repo.root), selftree.config(self.repo.root)
        self.assertEqual(selftree.selection(paths, tree, cfg)[0], ['leaf'])

    def test_deleted_tool_selects_its_importers_tests(self):
        self.repo.git('rm', '-q', 'tools/leaftool.py')
        self.repo.git('commit', '-q', '-m', 'delete')
        paths, why = selftree.changed_paths('main', self.repo.root)
        self.assertIsNone(why)
        self.assertEqual(paths, ['tools/leaftool.py'])
        tree, cfg = selftree.scan(self.repo.root), selftree.config(self.repo.root)
        self.assertNotIn('leaftool', tree.tools)
        picked, why, _ = selftree.selection(paths, tree, cfg)
        # tools/wrapper.py still imports the deleted tool; tests/cli names wrapper.py
        self.assertEqual(picked, ['cli'])
        self.assertIn('names tools/wrapper.py, which tools/leaftool.py reaches', why['cli'])

    def test_deleted_tool_selects_the_packages_that_ran_it(self):
        self.repo.git('rm', '-q', 'tools/solo.py')
        self.repo.git('commit', '-q', '-m', 'delete')
        paths, _ = selftree.changed_paths('main', self.repo.root)
        self.assertEqual(paths, ['tools/solo.py'])
        tree, cfg = selftree.scan(self.repo.root), selftree.config(self.repo.root)
        picked, why, _ = selftree.selection(paths, tree, cfg)
        # kitlib/runner still names tools/solo.py, so it and its importer run, not everything
        self.assertEqual(picked, ['git', 'runner', 'runneruser'])
        self.assertIn('kitlib/runner runs tools/solo.py, which tools/solo.py reaches',
                      why['runner'])

    def test_deleted_kitlib_package_selects_everything(self):
        self.repo.git('rm', '-q', '-r', 'kitlib/lone')
        self.repo.git('commit', '-q', '-m', 'delete')
        paths, _ = selftree.changed_paths('main', self.repo.root)
        self.assertEqual(paths, ['kitlib/lone/__init__.py'])
        tree, cfg = selftree.scan(self.repo.root), selftree.config(self.repo.root)
        self.assertIs(selftree.selection(paths, tree, cfg)[0], ALL)

    def test_changed_paths_refuse_an_unknown_base(self):
        paths, why = selftree.changed_paths('no-such-branch', self.repo.root)
        self.assertIsNone(paths)
        self.assertIn('merge-base', why)

    def test_plan_prints_each_package_and_its_reason(self):
        self.repo.write('kitlib/mid/x.py', 'X = 1\n')
        rc, out = self.run_cli('--select', '--plan')
        self.assertEqual(rc, 0)
        self.assertIn('kit_tests: --select against the merge-base with main: 1 changed path(s)', out)
        self.assertIn('tests/mid', out)
        self.assertIn('own tests of changed kitlib/mid', out)
        self.assertIn('tests/top', out)
        self.assertIn('kit_tests: selected 3 of %d test package(s), 3 module(s)'
                      % FIXTURE_TEST_PACKAGES, out)

    def test_plan_names_the_full_suite_and_why(self):
        rc, out = self.run_cli('--select', '--changed', 'kitlib/git/a.py', '--plan')
        self.assertEqual(rc, 0)
        self.assertIn('kit_tests: full suite: shared core: kitlib/git changed', out)

    def test_selected_run_runs_only_the_selection(self):
        rc, out = self.run_cli('--select', '--changed', 'kitlib/top/a.py')
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r'kit_tests: OK in \d+s \(selected, ')

    def test_failing_selection_exits_1(self):
        self.repo.write('tests/top/test_top.py', PASSING.replace('pass', 'self.fail("red")'))
        rc, out = self.run_cli('--select', '--changed', 'kitlib/top/a.py')
        self.assertEqual(rc, 1)
        self.assertIn('kit_tests: FAILED, exit 1', out)

    def test_nothing_selected_runs_nothing(self):
        rc, out = self.run_cli('--select', '--changed', 'docs/other.md')
        self.assertEqual(rc, 0)
        self.assertIn('(no test)  docs/other.md: prose no test names', out)
        self.assertIn('kit_tests: OK, nothing selected (1 changed path(s))', out)

    def test_nothing_selected_plan_prints_the_same_ok_line(self):
        rc, out = self.run_cli('--select', '--changed', 'docs/other.md', '--plan')
        self.assertEqual(rc, 0)
        self.assertIn('kit_tests: OK, nothing selected', out)

    def test_git_failure_refuses_and_runs_nothing(self):
        rc, out = self.run_cli('--select', '--base', 'no-such-branch')
        self.assertEqual(rc, 2)
        self.assertIn('no test ran; fix git, or run --full', out)

    def test_bad_table_refuses(self):
        self.repo.write('tests/selection.json', '{"nope": 1}')
        rc, out = self.run_cli('--select', '--changed', 'kitlib/top/a.py', '--plan')
        self.assertEqual(rc, 2)
        self.assertIn('unknown key(s) nope', out)

    def test_full_with_a_bad_table_warns_and_runs_everything(self):
        self.repo.write('tests/selection.json', '{"nope": 1}')
        rc, out = self.run_cli('--full', '--jobs', '2')
        self.assertEqual(rc, 0, out)
        self.assertIn('kit_tests: WARNING: ', out)
        self.assertIn('unknown key(s) nope', out)
        self.assertIn('--full runs every module, none serial', out)
        self.assertRegex(out, r'kit_tests: OK in \d+s \(full, 2 jobs, 0 serial\): 13 tests in 13 '
                              r'module\(s\)')

    def test_full_runs_every_module_in_parallel(self):
        rc, out = self.run_cli('--full', '--jobs', '3')
        self.assertEqual(rc, 0, out)
        self.assertIn('kit_tests: (no tests) tests.testutil defines no test; not run', out)
        self.assertIn('kit_tests: 13 module(s), 3 at a time, then 0 serial', out)
        self.assertRegex(out, r'kit_tests: OK in \d+s \(full, 3 jobs, 0 serial\): 13 tests in 13 '
                              r'module\(s\), 13 package\(s\)')

    def test_jobs_1_runs_one_unittest_process(self):
        rc, out = self.run_cli('--select', '--changed', 'kitlib/top/a.py', '--jobs', '1')
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r'kit_tests: OK in \d+s \(selected, one process\)')
        self.assertNotIn('at a time', out)
        self.repo.write('tests/top/test_top.py', PASSING.replace('pass', 'self.fail("red")'))
        rc, out = self.run_cli('--select', '--changed', 'kitlib/top/a.py', '--jobs', '1')
        self.assertEqual(rc, 1)
        self.assertIn('kit_tests: FAILED, exit 1 in ', out)

    def test_jobs_must_be_at_least_1(self):
        for bad in ('0', '-2'):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as e, unittest.mock.patch('sys.stderr', err):
                cli.main(['-C', self.repo.root, '--full', '--jobs', bad])
            self.assertEqual(e.exception.code, 2)
            self.assertIn('USAGE: --jobs must be at least 1', err.getvalue())

    def test_full_plan_and_usage(self):
        rc, out = self.run_cli('--full', '--plan')
        want = 'kit_tests: full suite (--full)\n'
        if os.environ.get('KIT_FAKE_GIT', '1') == '1':
            # on the fake, --full diffs for its real-git pass and says against what
            want += ("kit_tests: --full: the real-git pass's change list against the merge-base "
                     "with main: 0 changed path(s)\n")
        self.assertEqual((rc, out), (0, want))
        with self.assertRaises(SystemExit) as e, \
                unittest.mock.patch('sys.stderr', io.StringIO()):
            cli.main(['--full', '--changed', 'a'])
        self.assertEqual(e.exception.code, 2)


class KitTree(unittest.TestCase):
    """The kit's own tree and table: the rules the kit's branches rely on."""

    @classmethod
    def setUpClass(cls):
        cls.tree = selftree.scan(KIT)
        cls.cfg = selftree.config(KIT)

    def select(self, *paths):
        return selftree.selection(list(paths), self.tree, self.cfg)[0]

    def test_shared_core_and_selector_run_everything(self):
        for path in ('kitlib/git/cmd.py', 'kitlib/config/load.py', 'kitlib/proc/shield.py',
                     'kitlib/lock/api.py', 'kitlib/tooldeps/selftree.py', 'tools/kit_tests.py',
                     'tests/testutil.py', 'tests/gitfixture.py', 'tests/selection.json'):
            self.assertIs(self.select(path), ALL, path)

    def test_unmapped_runs_everything(self):
        self.assertIs(self.select('weird/file.txt'), ALL)

    def test_a_leaf_is_selected_without_the_full_suite(self):
        picked = self.select('kitlib/stubs/__init__.py')
        self.assertIsNot(picked, ALL)
        self.assertIn('stubs', picked)

    def test_skills_test_sees_every_path_a_skill_can_name(self):
        # tests/skills checks every tools/*.py, kitlib/** and optional/** path a SKILL.md names
        # exists, so a rename or delete of one must run it.
        for path in ('tools/backlog.py', 'tools/no_such_tool.py', 'kitlib/stubs/__init__.py',
                     'optional/comfy/comfy_gen.py'):
            picked = self.select(path)
            self.assertTrue(picked is ALL or 'skills' in picked, path)

    def test_a_file_directly_in_kitlib_selects_everything(self):
        # the skills mapping on kitlib/** must not turn an unrouted kitlib/ file into a skip
        for path in ('kitlib/data.json', 'kitlib/new.py'):
            self.assertIs(self.select(path), ALL, path)

    def test_a_doc_only_change_selects_the_whole_tree_scrubs(self):
        # tests/scrub reads every file `git ls-files` lists: a docs edit with a ruling id in it
        # must reach it though nothing maps docs/** to it.
        for path in ('docs/backlog-store.md', 'README.md', '.gitignore'):
            picked = self.select(path)
            self.assertIsNot(picked, ALL, path)
            self.assertIn('scrub', picked, path)

    def test_every_always_entry_is_a_test_package(self):
        self.assertIn('scrub', self.cfg['always'])
        for pkg in self.cfg['always']:
            self.assertIn(pkg, self.tree.tests)

    def test_every_serial_entry_names_a_test_module(self):
        with open(os.path.join(KIT, 'tests', 'selection.json'), encoding='utf-8') as fh:
            self.assertIn('serial', json.load(fh))      # the key is written, not defaulted
        mods = set(selftree.all_test_modules(KIT))
        for module, why in self.cfg['serial']:
            self.assertIn(module, mods, why)

    def test_the_table_lists_the_slot_treekill_proof_and_none_is_stale(self):
        modules = set(selftree.all_test_modules(KIT))
        self.assertIn('tests.lock.test_slot_treekill', dict(self.cfg['skip_fails']))
        self.assertEqual(selftree.stale_skip_fails(self.cfg['skip_fails'], modules), [])

    def test_every_long_entry_names_a_test_module(self):
        mods = set(selftree.all_test_modules(KIT))
        self.assertTrue(self.cfg['long'])
        for module in self.cfg['long']:
            self.assertIn(module, mods)

    def test_every_test_package_is_selected_by_its_own_change(self):
        for pkg in self.tree.tests:
            picked = self.select('tests/%s/zz.py' % pkg)
            self.assertTrue(picked is ALL or pkg in picked, pkg)


if __name__ == '__main__':
    unittest.main()
