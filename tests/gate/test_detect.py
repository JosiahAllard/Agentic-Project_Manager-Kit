"""kitlib.gate.detect: the auto-detected gate. Each detector over a fixture checkout in a temp
folder (no git), the file listing, and effective(): a configured "gate" section wins whole."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.config.load import Config
from kitlib.gate import detect, runner
from kitlib.gate import steps as S
from tests import gitfixture

PY_TEST = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n'


class Tree(unittest.TestCase):
    """self.root: an empty temp folder; write() fills it; found() detects over it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-detect-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, files):
        for rel, text in files.items():
            path = os.path.join(self.root, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)

    def found(self, files=None):
        """{stack: {step name: step}} detected in the folder."""
        self.write(files or {})
        got = detect.detect(detect.walk_files(self.root), detect.reader(self.root))
        return {name: {s['name']: s for s in steps} for name, steps in got}

    def steps(self, stack, files):
        return self.found(files).get(stack, {})


class Python(Tree):
    def test_tests_with_no_pytest_run_unittest_discover_in_their_folder(self):
        got = self.steps('python', {'tests/test_a.py': PY_TEST, 'src/app.py': 'x = 1\n'})
        self.assertEqual(list(got), ['python-tests'])
        s = got['python-tests']
        self.assertEqual(s['cmd'], ['{python}', '-m', 'unittest', 'discover', '-s', 'tests'])
        self.assertTrue(s['always'])
        self.assertNotIn('requires', s)

    def test_tests_at_the_root_discover_from_the_root(self):
        got = self.steps('python', {'test_a.py': PY_TEST})
        self.assertEqual(got['python-tests']['cmd'], ['{python}', '-m', 'unittest', 'discover'])

    def test_a_test_folder_is_used_too(self):
        got = self.steps('python', {'test/test_a.py': PY_TEST})
        self.assertEqual(got['python-tests']['cmd'][-2:], ['-s', 'test'])

    def test_pyproject_with_pytest_config_runs_pytest(self):
        got = self.steps('python', {'pyproject.toml': '[tool.pytest.ini_options]\naddopts = "-q"\n',
                                    'tests/test_a.py': 'def test_a():\n    pass\n'})
        s = got['python-tests']
        self.assertEqual(s['cmd'], ['{python}', '-m', 'pytest'])
        self.assertEqual(s['requires'], [['{python}', '-c', 'import pytest']])
        self.assertTrue(s['requires_strict'])     # a missing runner fails: never a silent pass

    def test_every_pytest_sign_runs_pytest(self):
        signs = [{'pytest.ini': '[pytest]\n'},
                 {'tests/conftest.py': ''},
                 {'setup.cfg': '[tool:pytest]\ntestpaths = tests\n'},
                 {'tox.ini': '[pytest]\n'},
                 {'requirements-dev.txt': 'flake8\npytest>=7\n'},
                 {'requirements.txt': 'pytest-cov\n'},
                 {'requirements/test.txt': 'pytest==8.0\n'},
                 {'pyproject.toml': '[project.optional-dependencies]\ntest = ["pytest"]\n'},
                 {'tests/test_b.py': 'import pytest\n'}]
        for i, sign in enumerate(signs):
            with self.subTest(sign=sign):
                self.root = tempfile.mkdtemp(prefix='kit-detect-%d-' % i)
                self.addCleanup(shutil.rmtree, self.root, True)
                files = dict(sign)
                files.setdefault('tests/test_a.py', PY_TEST)
                got = self.steps('python', files)
                self.assertEqual(got['python-tests']['cmd'], ['{python}', '-m', 'pytest'])

    def test_a_word_containing_pytest_is_not_pytest(self):
        got = self.steps('python', {'requirements.txt': 'mypytester\nnotpytest\n',
                                    'tests/test_a.py': PY_TEST})
        self.assertEqual(got['python-tests']['cmd'][2], 'unittest')

    def test_python_with_no_tests_and_no_linter_is_not_detected(self):
        self.assertEqual(self.found({'pyproject.toml': '[project]\nname = "x"\n',
                                     'app.py': 'x = 1\n'}), {})

    def test_ruff_and_flake8_only_when_configured(self):
        got = self.steps('python', {'tests/test_a.py': PY_TEST})
        self.assertEqual(list(got), ['python-tests'])
        got = self.steps('python', {'pyproject.toml': '[tool.ruff]\nline-length = 100\n',
                                    'setup.cfg': '[flake8]\nmax-line-length = 100\n'})
        self.assertEqual(list(got), ['python-tests', 'python-ruff', 'python-flake8'])
        self.assertEqual(got['python-ruff']['cmd'], ['ruff', 'check', '.'])
        self.assertEqual(got['python-ruff']['requires'], ['ruff'])
        self.assertNotIn('requires_strict', got['python-ruff'])     # a missing linter skips
        self.assertEqual(got['python-flake8']['cmd'], ['flake8'])

    def test_ruff_toml_and_a_flake8_file(self):
        got = self.steps('python', {'ruff.toml': '', '.flake8': '[flake8]\n'})
        self.assertEqual(list(got), ['python-ruff', 'python-flake8'])


class Node(Tree):
    PKG = {'scripts': {'test': 'jest', 'lint': 'eslint .', 'typecheck': 'tsc --noEmit',
                       'build': 'tsc'}}

    def pkg(self, scripts=None, **files):
        files['package.json'] = json.dumps(self.PKG if scripts is None else {'scripts': scripts})
        return files

    def test_test_lint_and_typecheck_scripts_with_npm_by_default(self):
        got = self.steps('node', self.pkg())
        self.assertEqual(list(got), ['node-test', 'node-lint', 'node-typecheck'])    # no build
        npm = 'npm.cmd' if os.name == 'nt' else 'npm'
        self.assertEqual(got['node-test']['cmd'], [npm, 'run', 'test'])
        self.assertEqual(got['node-test']['requires'], ['npm'])
        self.assertTrue(got['node-test']['requires_strict'])
        self.assertNotIn('requires_strict', got['node-lint'])

    def test_the_lockfile_picks_the_package_manager(self):
        for lock, pm in (('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lockb', 'bun'),
                         ('bun.lock', 'bun'), ('package-lock.json', 'npm')):
            with self.subTest(lock=lock):
                self.root = tempfile.mkdtemp(prefix='kit-detect-')
                self.addCleanup(shutil.rmtree, self.root, True)
                got = self.steps('node', self.pkg(**{lock: ''}))
                self.assertEqual(got['node-test']['requires'], [pm])
                self.assertEqual(os.path.splitext(got['node-test']['cmd'][0])[0], pm)

    def test_npm_inits_placeholder_test_is_not_a_test(self):
        got = self.steps('node', self.pkg({'test': 'echo "Error: no test specified" && exit 1',
                                           'lint': 'eslint .'}))
        self.assertEqual(list(got), ['node-lint'])

    def test_no_scripts_or_bad_json_detects_nothing(self):
        self.assertEqual(self.found(self.pkg({'build': 'tsc'})), {})
        self.write({'package.json': '{not json'})
        self.assertEqual(self.found(), {})

    def test_a_nested_package_json_is_not_the_root(self):
        self.assertEqual(self.found({'web/package.json': json.dumps(self.PKG)}), {})

    def test_node_modules_is_never_read(self):
        got = self.found(self.pkg({'lint': 'eslint .'},
                                  **{'node_modules/x/test_x.py': PY_TEST,
                                     'node_modules/x/Cargo.toml': ''}))
        self.assertEqual(list(got), ['node'])


class RustGoDotnet(Tree):
    def test_rust_is_cargo_test_plus_fmt_when_rustfmt_is_configured(self):
        got = self.steps('rust', {'Cargo.toml': '[package]\nname = "x"\n'})
        self.assertEqual(list(got), ['rust-test'])
        self.assertEqual(got['rust-test']['cmd'], ['cargo', 'test', '--workspace'])
        self.assertTrue(got['rust-test']['requires_strict'])
        got = self.steps('rust', {'rustfmt.toml': 'edition = "2021"\n'})
        self.assertEqual(got['rust-fmt']['cmd'], ['cargo', 'fmt', '--all', '--check'])

    def test_go_is_test_and_vet(self):
        got = self.steps('go', {'go.mod': 'module x\n'})
        self.assertEqual([s['cmd'] for s in got.values()],
                         [['go', 'test', './...'], ['go', 'vet', './...']])

    def test_dotnet_tests_the_one_solution(self):
        got = self.steps('dotnet', {'App.sln': '', 'src/App/App.csproj': '<Project/>',
                                    'tests/App.Tests/App.Tests.csproj': '<Project/>'})
        self.assertEqual(list(got), ['dotnet-test'])
        self.assertEqual(got['dotnet-test']['cmd'],
                         ['dotnet', 'test', 'App.sln'] + detect.NO_REUSE)
        self.assertEqual(got['dotnet-test']['requires'], ['dotnet'])

    def test_dotnet_several_solutions_get_a_step_each(self):
        got = self.steps('dotnet', {'A.sln': '', 'B Tools.slnx': ''})
        self.assertEqual(list(got), ['dotnet-test-A', 'dotnet-test-B-Tools'])
        self.assertEqual(got['dotnet-test-B-Tools']['cmd'][2], 'B Tools.slnx')

    def test_dotnet_with_no_solution_tests_its_test_projects(self):
        got = self.steps('dotnet', {'src/App/App.csproj': '<Project/>',
                                    'check/Spec.fsproj':
                                    '<PackageReference Include="Microsoft.NET.Test.Sdk"/>'})
        self.assertEqual(list(got), ['dotnet-test'])
        self.assertEqual(got['dotnet-test']['cmd'][2], 'check/Spec.fsproj')

    def test_dotnet_with_no_solution_and_no_test_project_is_not_detected(self):
        self.assertEqual(self.found({'src/App/App.csproj': '<Project/>'}), {})

    def test_dotnet_paths_that_would_read_as_an_option_or_placeholder_are_not_arguments(self):
        got = self.steps('dotnet', {'-p:Foo=1.sln': '', '{root}.sln': '', 'App.sln': ''})
        self.assertEqual([s['cmd'][2] for s in got.values()], ['App.sln'])

    def test_dotnet_test_projects_of_one_name_get_distinct_steps(self):
        got = self.steps('dotnet', {'a/Tests.csproj': '<Project/>', 'b/Tests.csproj': '<Project/>'})
        self.assertEqual(list(got), ['dotnet-test-Tests', 'dotnet-test-Tests-2'])
        self.assertEqual([s['cmd'][2] for s in got.values()], ['a/Tests.csproj', 'b/Tests.csproj'])


class Several(Tree):
    def test_every_stack_runs_in_a_fixed_order(self):
        got = detect.detect(*self.listing({'go.mod': '', 'Cargo.toml': '',
                                           'package.json': '{"scripts": {"test": "jest"}}',
                                           'tests/test_a.py': PY_TEST, 'X.sln': ''}))
        self.assertEqual([name for name, _ in got], list(detect.STACKS))
        names = [s['name'] for _, steps in got for s in steps]
        self.assertEqual(len(names), len(set(names)))

    def test_nothing_found_is_an_empty_list(self):
        self.assertEqual(detect.detect(*self.listing({'README.md': '# x\n'})), [])

    def listing(self, files):
        self.write(files)
        return detect.walk_files(self.root), detect.reader(self.root)


class Files(Tree):
    def test_the_walk_skips_build_folders_and_nested_repositories(self):
        self.write({'a.py': '', 'sub/b.py': '', 'node_modules/c.js': '', '.git/HEAD': '',
                    'nested/.git': 'gitdir: x', 'nested/d.py': '', 'obj/e.cs': ''})
        self.assertEqual(detect.walk_files(self.root), ['a.py', 'sub/b.py'])

    def test_the_checkout_listing_is_gits_when_it_answers(self):
        self.write({'tests/test_a.py': PY_TEST, 'ignored/test_b.py': PY_TEST})
        with mock.patch.object(detect.cmd, 'run_text',
                               return_value=('tests/test_a.py\0Cargo.toml\0', None)) as git:
            paths, read = detect.checkout_files(self.root)
        self.assertEqual(paths, ['Cargo.toml', 'tests/test_a.py'])
        self.assertEqual(git.call_args[0][0][:2], ['ls-files', '-z'])
        self.assertEqual(read('tests/test_a.py'), PY_TEST)
        self.assertIsNone(read('Cargo.toml'))         # listed but not on disk

    def test_untracked_files_under_build_folders_are_not_listed_tracked_ones_are(self):
        answers = {'--cached': 'bin/test_tool.py\0tests/test_a.py\0',
                   '--others': '.venv/lib/test_x.py\0node_modules/p/package.json\0new_test.py\0'}
        with mock.patch.object(detect.cmd, 'run_text',
                               side_effect=lambda args, *a, **k: (answers[args[2]], None)):
            paths, _ = detect.checkout_files(self.root)
        self.assertEqual(paths, ['bin/test_tool.py', 'new_test.py', 'tests/test_a.py'])

    def test_with_no_git_answer_the_folder_is_walked(self):
        self.write({'go.mod': 'module x\n'})
        with mock.patch.object(detect.cmd, 'run_text', return_value=(None, 'not a repository')):
            paths, _ = detect.checkout_files(self.root)
        self.assertEqual(paths, ['go.mod'])


def config(gate=None, written=False):
    raw = {'gate': gate} if written else {}
    return Config('C:/repo', 'C:/repo/.kit.json', raw, {'gate': None} if written else {})


class Effective(Tree):
    def files(self, files):
        self.write(files)
        return detect.walk_files(self.root), detect.reader(self.root)

    def test_no_gate_section_is_auto_with_the_detected_steps(self):
        eff = detect.effective(config(), self.root, self.files({'tests/test_a.py': PY_TEST}))
        self.assertEqual((eff.source, eff.stacks), ('auto', ['python']))
        self.assertEqual([s['name'] for s in eff.gate['steps']], ['python-tests'])
        self.assertEqual(eff.gate['kind'], config().section('gate')['kind'])   # the defaults
        self.assertFalse(detect.nothing_detected(eff))
        self.assertIn('auto-detected gate: python', detect.label(eff))
        self.assertTrue(detect.label(eff).startswith('source: auto'))

    def test_a_gate_section_wins_whole_and_gets_no_detected_step(self):
        mine = {'steps': [{'name': 'unit', 'always': True, 'cmd': ['{python}', '-c', 'pass']}]}
        eff = detect.effective(config(mine, written=True), self.root,
                               self.files({'tests/test_a.py': PY_TEST, 'go.mod': ''}))
        self.assertEqual((eff.source, eff.stacks), ('gate.json', []))
        self.assertEqual([s['name'] for s in eff.gate['steps']], ['unit'])
        self.assertEqual(detect.label(eff), 'source: gate.json')

    def test_an_empty_gate_section_is_still_configured(self):
        eff = detect.effective(config({}, written=True), self.root,
                               self.files({'tests/test_a.py': PY_TEST}))
        self.assertEqual((eff.source, eff.gate['steps']), ('gate.json', []))
        self.assertFalse(detect.nothing_detected(eff))   # it refuses as before, never passes

    def test_nothing_detected_is_labelled_loudly(self):
        eff = detect.effective(config(), self.root, self.files({'README.md': 'x'}))
        self.assertTrue(detect.nothing_detected(eff))
        self.assertIn('NO checks', detect.label(eff))
        ok, line = runner.verdict([], no_checks=True)
        self.assertTrue(ok)
        self.assertEqual(line, runner.NO_CHECKS)
        self.assertIn('NO checks ran', line)
        self.assertFalse(runner.verdict([], no_checks=False)[1] == runner.NO_CHECKS)

    def test_detected_steps_build_and_select_like_configured_ones(self):
        eff = detect.effective(config(), self.root, self.files({
            'pyproject.toml': '[tool.pytest.ini_options]\n', 'tests/test_a.py': PY_TEST,
            'go.mod': ''}))
        built = S.build(eff.gate, 'C:/repo', 'base', 'tools')
        chosen, why = S.select(eff.gate, built, ['README.md'])     # always: any change
        self.assertEqual([s.name for s in chosen], ['python-tests', 'go-test', 'go-vet'])
        self.assertEqual(set(why.values()), {'always'})
        py = chosen[0]
        self.assertNotIn('{python}', py.cmd[0])
        self.assertNotIn('{python}', py.requires[0][0])        # the probe is substituted too
        self.assertTrue(py.requires_strict)
        self.assertFalse(chosen[2].requires_strict)


class Owed(Tree):
    """owed(): the steps the base detects and the tree under test does not are kept."""

    def detected(self, files):
        sub = tempfile.mkdtemp(dir=self.root)
        for rel, text in files.items():
            path = os.path.join(sub, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        return detect.detect(detect.walk_files(sub), detect.reader(sub))

    def test_deleted_tests_and_scripts_are_kept_from_the_base(self):
        base = self.detected({'tests/test_a.py': PY_TEST, 'ruff.toml': '',
                              'package.json': '{"scripts": {"test": "jest", "lint": "eslint"}}'})
        mine = self.detected({'package.json': '{"scripts": {"test": "exit 0"}}'})
        found, kept = detect.owed(mine, base)
        steps = {s['name']: s for _, ss in found for s in ss}
        self.assertEqual(kept, ['python-tests', 'python-ruff', 'node-lint'])
        self.assertEqual(list(steps), ['python-tests', 'python-ruff', 'node-test', 'node-lint'])
        self.assertNotIn('warn', steps['python-tests'])      # a kept test step fails as before
        self.assertTrue(steps['python-ruff']['warn'])          # a kept linter only warns
        self.assertTrue(steps['node-lint']['warn'])

    def test_the_tree_under_test_keeps_its_own_argv(self):
        base = self.detected({'tests/test_a.py': PY_TEST})
        mine = self.detected({'test/test_a.py': PY_TEST})
        found, kept = detect.owed(mine, base)
        self.assertEqual(kept, [])
        self.assertEqual(found[0][1][0]['cmd'][-2:], ['-s', 'test'])

    def test_a_renamed_solution_is_not_a_dropped_check(self):
        found, kept = detect.owed(self.detected({'New.sln': ''}), self.detected({'Old.sln': ''}))
        self.assertEqual((kept, [s['cmd'][2] for s in found[0][1]]), ([], ['New.sln']))
        found, kept = detect.owed(self.detected({}), self.detected({'Old.sln': ''}))
        self.assertEqual(kept, ['dotnet-test'])

    def test_the_label_names_what_was_kept(self):
        eff = detect.Effective({'steps': [{}]}, detect.AUTO, ['python'], ('python-tests',))
        self.assertIn('kept from the base', detect.label(eff))
        self.assertIn('python-tests', detect.label(eff))


class BaseCommit(unittest.TestCase):
    """effective(base=...) over a real repository: the base commit's tree is read from git."""

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('tests/test_a.py', PY_TEST)
        self.repo.write('package.json', '{"scripts": {"test": "jest"}}')
        self.base = self.repo.commit('tests', 'tests/test_a.py', 'package.json')

    def test_a_branch_that_deletes_the_tests_still_owes_them(self):
        self.repo.git('rm', '-q', 'tests/test_a.py', 'package.json')
        self.repo.git('commit', '-q', '-m', 'no tests')
        self.assertEqual(detect.effective(config(), self.repo.root).stacks, [])
        eff = detect.effective(config(), self.repo.root, base=self.base)
        self.assertEqual(eff.stacks, ['python', 'node'])
        self.assertEqual(eff.kept, ('python-tests', 'node-test'))
        self.assertFalse(detect.nothing_detected(eff))
        self.assertIn('kept from the base', detect.label(eff))

    def test_a_base_git_cannot_read_refuses(self):
        with self.assertRaises(detect.DetectError):
            detect.effective(config(), self.repo.root, base='0' * 40)

    def test_a_configured_gate_never_reads_the_base(self):
        eff = detect.effective(config({}, written=True), self.repo.root, base='0' * 40)
        self.assertEqual(eff.source, 'gate.json')


if __name__ == '__main__':
    unittest.main()
