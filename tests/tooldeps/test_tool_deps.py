"""kitlib.tooldeps and tools/tool_deps.py: the scan, both selection modes ("path", one repository's;
"tokens", an invented repository's), every `.kit.json` "tool_tests" key, and the audit a repository
runs on its own tools. The configuration and the checkout root are passed explicitly, and the
pieces tables are tests/tooldeps/piecetable.py's stand-ins.

Every test runs on a throwaway tools folder and an explicit configuration: the repositories' own
audits of their real tools (`tool_deps.py --check`) stay in each repository.
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

from kitlib.config import ConfigError
from kitlib.tooldeps import deps as tool_deps
from tests.gitfixture import TempRepo
from tests.tooldeps.piecetable import OLD, both_tables

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tool_deps_entry_for_tests',
                                               os.path.join(KIT, 'tools', 'tool_deps.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

TESTCASE = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n'

# An invented "path"-mode repository's "tool_tests" section: extra files, pinned paths and pieces, the
# solution pin, build-graph and test-source patterns, whole-tree and untested entries. The selection
# tests below run against these values.
BUILD_GRAPH = (r'(?:^|/)(?:build\.gradle\.kts|settings\.gradle\.kts|[^/]+\.csproj|'
               r'Directory\.[^/]+\.props|[^/]+\.slnx)$')
TEST_SOURCE = r'(?:^|/)src/(?:test|testFixtures)/.+\.java$|(?:^|/)[^/]*Tests[^/]*/(?:.+/)?[^/]+\.cs$'
R_DATA = {
    'extra': {'ex_budget.json': ['ex_budget', 'ex_index'], 'ex-tokens.json': ['ex_tokens'],
              'ex_pieces.json': ['ex_pieces'], 'ex_gate.json': ['ex_gate'],
              'README.md': ['test_ex_store', 'test_ex_daily']},
    'pinned': [
        ['docs/ex/STORE.md', ['test_ex_store']],
        ['docs/ex/STORE_PROCESS.md', ['test_ex_store']],
        ['docs/ex/PLAN.md', ['test_ex_store']],
        ['docs/INDEX.md', ['test_ex_index']],
        ['docs/ops/REPO.md', ['test_ex_index']],
        ['settings.gradle.kts', ['test_ex_gate', 'test_ex_pieces']],
        ['scripts/', ['test_ex_scripts', 'test_ex_republish']],
        ['scripts/ex-container.sh', ['test_ex_trainer']],
        ['gradle.properties', ['test_ex_bump']],
        [{'piece': 'model-sums', 'path': ''}, ['test_ex_deploy']],
        [{'piece': 'aux-scripts', 'path': 'src/test/java/'}, ['test_ex_gate']],
        [{'piece': 'link', 'path': 'src/main/java/org/example/ExProbe.java'},
         ['test_ex_report']],
        [{'piece': 'control-ops', 'path': '/'}, ['test_ex_republish']],
        [{'piece': 'trainer', 'path': 'env/'}, ['test_ex_trainer']],
        [{'piece': 'trainer', 'path': 'train/'}, ['test_ex_trainer']],
        [{'solution': True}, ['test_ex_pieces']],
    ],
    'patterns': [{'regex': BUILD_GRAPH, 'modules': ['test_ex_gate', 'test_ex_reads']},
                 {'regex': TEST_SOURCE, 'modules': ['test_ex_reads']}],
    'whole_tree': {'test_ex_gate.GraphOracle': 'the scope covers every input; tree-shape'},
    'untested': ['ex_cache', 'ex_check', 'ex_map'],
}
# An invented repository's "tokens" mode: full-suite trigger files, a .ps1 scanner and a kill-site
# scanner that any .ps1 change adds, doc scans that always run, its sources, skips and ignores.
DOC_SCANS = ['test_ex_doc_scan']
G_DATA = {
    'mentions': 'tokens',
    'selector_files': ['tool_deps.py', 'pytests.py', 'gate.py', 'ex_guard.py'],
    'patterns': [{'regex': r'\.ps1$', 'modules': ['test_ex_ps1_scan', 'test_ex_kill_scan']}],
    'always_run': DOC_SCANS,
    'guard_tests': [],
    'unmapped_tool': 'all',
    'sources': ['tools/**/*.py', 'tools/**/*.ps1', 'tools/**/*.cmd', 'tools/**/*.bat', 'tools/hooks/*',
                'test.bat'],
    'skip_dirs': ['__pycache__', '_ex_dumps', '_ex_previews'],
    'ignore': ['tools/**/*.md', 'tools/**/*.txt'],
}


class Fixture(unittest.TestCase):
    """A throwaway repository root with a tools/ folder, so these never drift with a real graph."""
    DATA = R_DATA
    GIT = False                 # a git checkout root (TempRepo), for the entry point's -C

    def setUp(self):
        if self.GIT:
            repo = TempRepo()
            self.addCleanup(repo.cleanup)
            self.root = repo.root
        else:
            self.root = tempfile.mkdtemp(prefix='tool_deps_test_')
            self.addCleanup(shutil.rmtree, self.root, True)
        self.dir = os.path.join(self.root, 'tools')
        os.makedirs(self.dir)
        self.cfg = tool_deps.config(data=self.DATA)

    def write(self, name, text, under=None):
        path = os.path.join(under or self.dir, *name.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(textwrap.dedent(text))

    def graph(self):
        return tool_deps.scan(self.dir, self.cfg, self.root)

    def select(self, *changed, merged=False):
        return tool_deps.select(set(changed), self.graph(), merged=merged, pieces=OLD, cfg=self.cfg)


class Selection(Fixture):
    """The "path" repository's selection tests ("path" mode), on its own table values."""

    def setUp(self):
        super().setUp()
        self.write('a.py', 'X = 1\n')
        self.write('b.py', 'import a\n')
        self.write('c.py', 'Y = 2\n')
        self.write('test_a.py', 'import a\n' + TESTCASE)
        self.write('test_b.py', 'import b\n' + TESTCASE)
        self.write('test_c.py', 'import c\n' + TESTCASE)
        self.write('test_tool_deps.py', TESTCASE)    # the guard test (guard_tests default)

    def test_a_changed_tool_selects_its_tests_and_dependents_and_nothing_else(self):
        self.assertEqual(self.select('tools/a.py'), ['test_a', 'test_b', 'test_tool_deps'])

    def test_a_tool_nobody_else_imports_selects_only_its_own_test(self):
        self.assertEqual(self.select('tools/b.py'), ['test_b', 'test_tool_deps'])

    def test_a_changed_test_module_selects_itself(self):
        self.assertEqual(self.select('tools/test_c.py'), ['test_c', 'test_tool_deps'])

    def test_a_literal_script_name_is_an_edge(self):
        self.write('test_run.py', "import subprocess\nCMD = ['python', 'tools/c.py']\n" + TESTCASE)
        self.assertIn('test_run', self.select('tools/c.py'))

    def test_a_python_c_import_is_an_edge(self):
        self.write('test_dash_c.py', 'CMD = ["python", "-c", "import c; c.main()"]\n' + TESTCASE)
        self.assertIn('test_dash_c', self.select('tools/c.py'))

    def test_a_docstring_or_assert_message_saying_import_is_not_an_edge(self):
        # Prose is not a `python -c` child, so it neither selects the test
        # nor makes c count as tested.
        os.remove(os.path.join(self.dir, 'test_c.py'))
        self.write('test_prose.py', '"""Checks that `import c` still works."""\n'
                   'class T(__import__("unittest").TestCase):\n'
                   '    def test_x(self):\n'
                   '        self.assertTrue(True, "run: from c import Y")\n')
        graph = self.graph()
        self.assertNotIn('test_prose', tool_deps.select({'tools/c.py'}, graph))
        self.assertNotIn('c', graph.tested())

    def test_a_python_c_argument_read_through_a_name_or_a_format_is_an_edge(self):
        os.remove(os.path.join(self.dir, 'test_c.py'))
        self.write('test_probe.py', 'import sys\nprobe = "import sys; import c; %s" % sys.path\n'
                   'CMD = (sys.executable, "-c", probe)\n' + TESTCASE)
        graph = self.graph()
        self.assertIn('test_probe', tool_deps.select({'tools/c.py'}, graph))
        self.assertIn('c', graph.tested())

    def test_a_python_c_argument_the_scan_cannot_resolve_falls_back_to_every_string(self):
        # A '-c' whose code is a parameter, `+=`, an attribute, a Name from a Name, or comes after
        # a `+`: that module reads as before (any string saying import counts), never fewer tests.
        cases = {
            'param': 'def run(code):\n    return [PY, "-c", code]\nX = "import c"\n',
            'aug': 'code = "x"\ncode += "; import c"\nCMD = [PY, "-c", code]\n',
            'attr': 'class K:\n    CODE = "import c"\nCMD = [PY, "-c", K.CODE]\n',
            'alias': 'a = "import c"\nb = a\nCMD = [PY, "-c", b]\n',
            'plus': 'CODE = "import c"\nCMD = [PY, "-c"] + [CODE]\n',
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                if os.path.exists(os.path.join(self.dir, 'test_c.py')):
                    os.remove(os.path.join(self.dir, 'test_c.py'))
                self.write('test_fb.py', text + TESTCASE)
                self.assertIn('test_fb', self.select('tools/c.py'))
                self.assertIn('c', self.graph().tested())

    def test_a_test_naming_a_tool_only_by_file_name_does_not_count_as_testing_it(self):
        # The literal still selects the test, but it does not make the
        # untested audit count c as tested (c's own test_c is removed, so nothing imports it).
        os.remove(os.path.join(self.dir, 'test_c.py'))
        self.write('test_run.py', "import subprocess\nCMD = ['python', 'tools/c.py']\n" + TESTCASE)
        graph = self.graph()
        self.assertIn('test_run', tool_deps.select({'tools/c.py'}, graph))
        self.assertNotIn('c', graph.tested())
        self.assertIn('a', graph.tested())

    def test_an_import_or_python_c_import_counts_as_testing_it(self):
        for name in ('test_b.py', 'test_c.py'):
            os.remove(os.path.join(self.dir, name))
        self.write('test_dash_c.py', 'CMD = ["python", "-c", "import b; b.main()"]\n' + TESTCASE)
        self.write('test_dyn.py', 'M = __import__("c")\n' + TESTCASE)
        tested = self.graph().tested()
        for m in ('a', 'b', 'c'):
            with self.subTest(module=m):
                self.assertIn(m, tested)

    def test_a_declared_dynamic_import_counts_as_testing_it(self):
        # A dynamic_imports entry is an import the scan cannot read: it lands in Graph.imports and
        # so in tested(), the same as a literal import would.
        os.remove(os.path.join(self.dir, 'test_c.py'))
        self.write('test_plugins.py', 'import importlib\n\ndef load(n):\n'
                                      '    return importlib.import_module(n)\n' + TESTCASE)
        self.assertNotIn('c', self.graph().tested())
        self.cfg = tool_deps.config(data=dict(R_DATA, dynamic_imports={'test_plugins': [['c'], 'plugins']}))
        graph = self.graph()
        self.assertIn('c', graph.imports['test_plugins'])
        self.assertIn('c', graph.tested())

    def test_a_function_level_import_is_an_edge(self):
        self.write('d.py', 'def f():\n    from c import Y\n    return Y\n')
        self.write('test_d.py', 'import d\n' + TESTCASE)
        self.assertEqual(self.select('tools/c.py'), ['test_c', 'test_d', 'test_tool_deps'])

    def test_a_test_importing_a_test_depends_on_it(self):
        self.write('test_e.py', 'from test_a import T as _T\n' + TESTCASE)
        self.assertEqual(self.select('tools/test_a.py'), ['test_a', 'test_e', 'test_tool_deps'])

    def test_a_shared_helper_selects_every_test_using_it(self):
        self.write('_helper.py', 'def h():\n    pass\n')
        self.write('test_f.py', 'import _helper\n' + TESTCASE)
        self.write('test_g.py', 'from _helper import h\n' + TESTCASE)
        self.assertEqual(self.select('tools/_helper.py'), ['test_f', 'test_g', 'test_tool_deps'])

    def test_a_tool_naming_a_test_file_is_not_a_dependency(self):
        # A tool's allow-list can name test files as data; the edge would make every test
        # of that tool depend on the named test module.
        self.write('lister.py', "ALLOW = ('tools/test_c.py',)\n")
        self.write('test_lister.py', 'import lister\n' + TESTCASE)
        self.assertEqual(self.select('tools/test_c.py'), ['test_c', 'test_tool_deps'])

    def test_a_test_without_testcase_is_a_tool(self):
        self.write('test_report.py', 'def main():\n    pass\n')    # a test*.py with no TestCase
        self.assertNotIn('test_report', self.graph().tests)

    def test_an_unknown_tools_path_selects_all(self):
        for path in ('tools/gone.py', 'tools/sub/x.py', 'tools/new.json', 'tools/notes.txt'):
            with self.subTest(path=path):
                self.assertEqual(self.select(path), tool_deps.ALL)

    def test_a_selector_file_selects_all(self):
        for name in self.cfg['selector_files']:
            with self.subTest(name=name):
                self.write(name, 'Z = 0\n')
                self.assertEqual(self.select('tools/' + name), tool_deps.ALL)

    def test_merged_selects_the_branch_selection_plus_the_whole_tree_classes(self):
        # A landing's merged gate is no longer the whole suite for any tools path: the change's own
        # selection over base..merge, and the whole_tree classes, which no edge can map.
        self.write('test_ex_gate.py', 'import unittest\n\nclass GraphOracle(unittest.TestCase):\n'
                                      '    def test_x(self):\n        pass\n')
        self.assertEqual(self.select('tools/b.py'), ['test_b', 'test_tool_deps'])
        self.assertEqual(self.select('tools/b.py', merged=True),
                         ['test_b', 'test_tool_deps', 'test_ex_gate.GraphOracle'])

    def test_merged_adds_no_whole_tree_class_its_module_already_runs_or_that_is_gone(self):
        self.write('test_ex_gate.py', 'import unittest\nimport c\n\nclass GraphOracle(unittest.TestCase):\n'
                                      '    def test_x(self):\n        pass\n')
        self.assertEqual(self.select('tools/c.py', merged=True), ['test_c', 'test_ex_gate', 'test_tool_deps'])
        os.remove(os.path.join(self.dir, 'test_ex_gate.py'))
        self.assertEqual(self.select('tools/b.py', merged=True), ['test_b', 'test_tool_deps'])

    def test_merged_still_selects_all_for_a_selector_file_or_an_unknown_tools_path(self):
        self.write('tool_deps.py', 'Z = 0\n')
        self.assertEqual(self.select('tools/tool_deps.py', merged=True), tool_deps.ALL)
        self.assertEqual(self.select('tools/sub/x.py', merged=True), tool_deps.ALL)

    def test_merged_with_only_a_pinned_path_selects_its_modules(self):
        self.assertEqual(self.select('docs/ex/STORE.md', merged=True),
                         ['test_ex_store', 'test_tool_deps'])

    def test_a_non_tools_unpinned_path_selects_nothing(self):
        self.assertEqual(self.select('docs/architecture.md', 'mesh/Node.cs'), [])

    def test_a_pinned_path_selects_exactly_its_module(self):
        self.assertEqual(self.select('docs/INDEX.md'), ['test_ex_index', 'test_tool_deps'])
        # aux-scripts' test sources are also test sources: the literal-scan slice too.
        self.assertEqual(self.select(OLD.path('aux-scripts', 'src/test/java/org/X.java')),
                         ['test_ex_gate', 'test_ex_reads', 'test_tool_deps'])

    def test_a_tools_extra_file_selects_its_readers_dependents(self):
        self.write('ex_budget.py', 'import json\n')
        self.write('ex_index.py', 'X = 1\n')
        self.write('test_ex_budget.py', 'import ex_budget\n' + TESTCASE)
        self.write('test_ex_index.py', 'import ex_index\n' + TESTCASE)
        self.write('ex_tokens.py', 'X = 1\n')
        self.write('test_ex_tokens.py', 'import ex_tokens\n' + TESTCASE)
        self.assertEqual(self.select('tools/ex_budget.json'),
                         ['test_ex_budget', 'test_ex_index', 'test_tool_deps'])
        self.assertEqual(self.select('tools/ex-tokens.json'),
                         ['test_ex_tokens', 'test_tool_deps'])

    def test_a_tool_no_test_depends_on_still_selects_test_tool_deps(self):
        # Review finding 1: this returned [] and the gate dropped the python-tests step.
        self.write('lonely.py', 'X = 1\n')
        self.assertEqual(self.select('tools/lonely.py'), ['test_tool_deps'])

    def test_a_new_untested_tool_still_selects_test_tool_deps(self):
        self.write('fresh.py', 'import a\n')
        self.assertEqual(self.select('tools/fresh.py'), ['test_tool_deps'])

    def test_a_literal_import_module_is_an_edge(self):
        self.write('test_dyn.py', 'import importlib\nM = importlib.import_module("c")\n' + TESTCASE)
        self.write('test_dunder.py', 'M = __import__("c")\n' + TESTCASE)
        self.assertEqual(self.select('tools/c.py'),
                         ['test_c', 'test_dunder', 'test_dyn', 'test_tool_deps'])

    def test_a_computed_import_is_reported_and_a_declared_one_is_an_edge(self):
        self.write('loader.py', 'import importlib\n\ndef load(n):\n    return importlib.import_module(n)\n'
                                'def old(n):\n    return __import__("x" + n)\n')
        self.write('test_loader.py', 'import loader\n' + TESTCASE)
        with open(os.path.join(self.dir, 'loader.py')) as fh:
            tree = tool_deps.ast.parse(fh.read())
        self.assertEqual(tool_deps.dynamic_imports(tree), [4, 6])
        self.assertEqual(self.select('tools/c.py'), ['test_c', 'test_tool_deps'])
        self.cfg = tool_deps.config(data=dict(R_DATA, dynamic_imports={'loader': [['c'], 'plugins by name']}))
        self.assertEqual(self.select('tools/c.py'), ['test_c', 'test_loader', 'test_tool_deps'])

    def test_backslash_paths_are_normalised(self):
        self.assertEqual(self.select('tools\\b.py'), ['test_b', 'test_tool_deps'])

    def test_the_selection_says_why(self):
        g = self.graph()
        self.assertEqual(tool_deps.selection({'tools/b.py'}, g)[1], '1 changed path(s) -> 2 test module(s)')
        self.assertIn('tools/gone.py is not a top-level', tool_deps.selection({'tools/gone.py'}, g)[1])
        self.assertIn('tools/tool_deps.py changed', tool_deps.selection({'tools/tool_deps.py'}, g)[1])


@both_tables
class PinnedInPieces(unittest.TestCase):
    """The "path" repository's in-piece pinned entries resolve through the pieces table: the same modules
    are selected before and after its folder move, each at its piece's path."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = tool_deps.config(data=R_DATA)
        cls.graph = tool_deps.Graph({'test_tool_deps': set()}, {'test_tool_deps'}, cfg=cls.cfg)

    def select(self, *changed):
        return tool_deps.select(set(changed), self.graph, pieces=self.table)

    def in_piece(self):
        return [((loc['piece'], loc['path']), mods) for loc, mods in self.cfg['pinned']
                if isinstance(loc, dict) and 'piece' in loc]

    def test_each_in_piece_entry_selects_its_modules(self):
        import re
        for (pid, rel), mods in self.in_piece():
            path = self.P(pid, rel + ('x.java' if rel.endswith('/') else ''))
            with self.subTest(path=path):
                extra = {'test_ex_reads'} if re.search(TEST_SOURCE, path) else set()
                self.assertEqual(self.select(path), sorted(set(mods) | {'test_tool_deps'} | extra))

    def test_the_solution_file_selects_test_ex_pieces(self):
        self.assertIn('test_ex_pieces', self.select(self.table.solution()))

    def test_every_build_graph_file_selects_test_ex_gate(self):
        # GraphOracle (in test_ex_gate) must run on the branch that adds a
        # dependency edge, so every file it parses selects test_ex_gate, a new module's included.
        paths = ['settings.gradle.kts', 'Directory.Build.props', 'Directory.Packages.props',
                 self.table.solution(), 'brand-new/build.gradle.kts', 'brand/New.Thing/New.Thing.csproj']
        paths += [d + '/build.gradle.kts' for _, d in self.table.gradle_pieces()]
        paths += [self.P(p['id'], p['id'] + '.csproj') for p in self.table.pieces
                  if p.get('dotnet') and p['id'].startswith('Mesh.')]
        for path in paths:
            with self.subTest(path=path):
                self.assertIn('test_ex_gate', self.select(path))
        for path in ('settings.gradle', 'docs/build.gradle.kts.md', 'control/Mesh.Control/Program.cs',
                     'shared/x.csproj.user'):
            with self.subTest(path=path):
                self.assertNotIn('test_ex_gate', self.select(path))

    def test_a_test_source_change_selects_the_literal_scan_slice_only(self):
        # A new runtime read in test code is checked on its own branch.
        paths = [self.P(m, 'src/test/java/org/X.java') for m, _ in self.table.gradle_pieces()]
        paths += [self.P('core-api', 'src/testFixtures/java/org/F.java'),
                  self.P('Mesh.Control.Tests', 'Backup/New.cs'), self.P('Mesh.Node.Tests', 'New.cs'),
                  self.P('Mesh.Protocol.Tests', 'New.cs')]
        for path in paths:
            with self.subTest(path=path):
                got = self.select(path)
                self.assertIn('test_ex_reads', got)
                if not path.startswith(self.P('aux-scripts', '')):   # pinned to test_ex_gate on its own
                    self.assertNotIn('test_ex_gate', got)
        for path in (self.P('Mesh.Control', 'Program.cs'), self.P('link', 'src/main/java/org/X.java')):
            with self.subTest(path=path):
                self.assertNotIn('test_ex_reads', self.select(path))
        # A build-graph file can make a project a test project, so it
        # runs the slice's TEST_SOURCE naming guard too (a new non-*Tests* test project's .csproj).
        for path in (self.P('Mesh.Control.Tests', 'Mesh.Control.Tests.csproj'), 'brand/New.Checks/New.Checks.csproj',
                     'settings.gradle.kts', 'brand-new/build.gradle.kts'):
            with self.subTest(path=path):
                self.assertIn('test_ex_reads', self.select(path))

    def test_the_old_path_selects_nothing_once_moved(self):
        for (pid, rel), _ in self.in_piece():
            was = self.table.get(pid).get('was')
            if was:
                with self.subTest(piece=pid):
                    old = was + ('/' + rel if rel else '') + ('x' if rel.endswith(('/', '.java')) else '')
                    self.assertEqual(self.select(old), [])


class Tokens(unittest.TestCase):
    """The "tokens" mode's tests, on the invented repository's G_DATA values. Its fixture test modules
    carry a TestCase here: the kit counts a test module by its TestCase, not by its name."""

    FILES = {
        'tools/lib.py': 'X = 1\n',
        'tools/mid.py': 'import lib\n',
        'tools/test_mid.py': 'import mid\n' + TESTCASE,
        'tools/runner.ps1': '# names nothing here: lib.py\n& python tools\\lib.py\n',
        'tools/test_runner.py': 'SCRIPT = "tools/runner.ps1"\n' + TESTCASE,
        'tools/outer.ps1': '& powershell -File tools\\runner.ps1\n',
        'tools/test_outer.py': 'SCRIPT = "outer.ps1"\n' + TESTCASE,
        'tools/test_docstring_only.py': '"""Mentions lib.py and runner.ps1 for the reader only."""\n# lib.py\n'
                                        + TESTCASE,
        'tools/lonely.py': 'print(1)\n',
        'tools/test_uses_gone.py': 'import gone\n' + TESTCASE,
        'tools/scratch_folder/case.json': '{}\n',
        'tools/test_data.py': 'FOLDER = "scratch_folder"\n' + TESTCASE,
    }

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='tool_deps_tokens_')
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel, text in self.FILES.items():
            path = os.path.join(self.root, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(text)
        self.cfg = tool_deps.config(data=G_DATA)

    def graph(self):
        return tool_deps.scan(os.path.join(self.root, 'tools'), self.cfg, self.root)

    def selection(self, *changed):
        return tool_deps.selection(set(changed), self.graph())

    def pick(self, *changed):
        names = self.selection(*changed)[0]
        return names if names == tool_deps.ALL else [n for n in names if n not in DOC_SCANS]

    def test_imports_are_followed_transitively(self):
        self.assertIn('test_mid', self.pick('tools/lib.py'))

    def test_a_script_naming_a_changed_file_brings_its_tests_one_hop_only(self):
        picked = self.pick('tools/lib.py')
        self.assertIn('test_runner', picked)          # runner.ps1 runs lib.py; test_runner names runner.ps1
        self.assertNotIn('test_outer', picked)        # outer.ps1 -> runner.ps1 is a second hop
        self.assertIn('test_outer', self.pick('tools/runner.ps1'))

    def test_comments_and_docstrings_are_not_mentions(self):
        self.assertNotIn('test_docstring_only', self.pick('tools/lib.py'))
        self.assertNotIn('test_docstring_only', self.pick('tools/runner.ps1'))

    def test_a_folder_name_maps_the_files_under_it(self):
        self.assertEqual(self.pick('tools/scratch_folder/case.json'), ['test_data'])

    def test_a_deleted_file_reaches_what_still_imports_it(self):
        self.assertEqual(self.pick('tools/gone.py'), ['test_uses_gone'])

    def test_a_changed_test_selects_itself(self):
        self.assertEqual(self.pick('tools/test_mid.py'), ['test_mid'])

    def test_full_triggers_select_everything(self):
        for name in self.cfg['selector_files']:
            with self.subTest(trigger=name):
                self.assertEqual(self.pick('tools/' + name), tool_deps.ALL)

    def test_an_unmapped_tool_selects_everything(self):
        names, why = self.selection('tools/lonely.py')
        self.assertEqual(names, tool_deps.ALL)
        self.assertIn('nothing maps tools/lonely.py', why)

    def test_unmapped_is_judged_per_file_in_a_multi_file_change(self):
        names, why = self.selection('tools/lib.py', 'tools/lonely.py')
        self.assertEqual(names, tool_deps.ALL)
        self.assertIn('nothing maps tools/lonely.py', why)
        self.assertNotIn('tools/lib.py', why)              # lib.py has importers: mapped
        names, why = self.selection('tools/lib.py', 'tools/mid.py')
        self.assertNotIn('full suite', why)

    def test_any_ps1_change_adds_the_ps1_scanner(self):
        self.assertIn('test_ex_ps1_scan', self.pick('tools/outer.ps1'))
        self.assertNotIn('test_ex_ps1_scan', self.pick('tools/lib.py'))

    def test_any_ps1_change_adds_the_kill_scanner(self):
        # A new kill site in a .ps1 that no test names must still meet the kill-site scanner.
        self.assertIn('test_ex_kill_scan', self.pick('tools/outer.ps1'))
        self.assertNotIn('test_ex_kill_scan', self.pick('tools/lib.py'))

    def test_doc_scans_always_run_and_docs_select_nothing_else(self):
        names, _ = self.selection('tools/notes.md', 'docs/x.md')
        self.assertEqual(names, DOC_SCANS)
        names, _ = self.selection('tools/lib.py')
        self.assertEqual(names[-len(DOC_SCANS):], DOC_SCANS)

    # ---- kit additions for the mode
    def test_a_root_script_source_is_scanned_and_selected_by_name(self):
        with open(os.path.join(self.root, 'test.bat'), 'w') as fh:
            fh.write('rem runs lib.py in a comment only\npython tools\\runner.ps1\n')
        g = self.graph()
        self.assertIn('test.bat', g.sources)
        self.assertEqual(self.pick('test.bat'), [])                       # nothing names test.bat
        self.assertNotIn('test_mid', self.pick('test.bat'))

    def test_skip_dirs_are_never_scanned(self):
        os.makedirs(os.path.join(self.root, 'tools', '_ex_dumps'))
        with open(os.path.join(self.root, 'tools', '_ex_dumps', 'dump.py'), 'w') as fh:
            fh.write('import lib\n')
        self.assertNotIn('tools/_ex_dumps/dump.py', self.graph().sources)
        self.cfg = tool_deps.config(data=dict(G_DATA, skip_dirs=['__pycache__']))
        self.assertIn('tools/_ex_dumps/dump.py', self.graph().sources)

    def test_the_tokens_mode_still_builds_the_path_graph_for_the_audit(self):
        g = self.graph()
        self.assertIn('mid', g.tested())
        self.assertIn('lib', g.closure('test_mid'))


class StaticRealTreeJoins(Fixture):
    """`real_tree_joins`: the static half of the pinned-input honesty check."""

    def test_joins_through_a_bound_root_a_loop_and_a_module_constant(self):
        self.write('tool.py', "ROOT = '/r'\nNAME = 'LIST.tsv'\n")
        self.write('test_tool.py', textwrap.dedent('''\
            import os
            import tool
            def f():
                a = os.path.join(tool.ROOT, 'docs', 'a.md')
                root = tool.ROOT
                for rel in ('x/b.md', 'x/c.md'):
                    os.path.join(root, rel.replace('/', os.sep))
                os.path.join(tool.ROOT, 'd', tool.NAME)
                os.path.join(tool.ROOT, 'e', unknown())
                os.path.join('/elsewhere', 'z.md')
            '''))
        found = [p for _, p in tool_deps.real_tree_joins(os.path.join(self.dir, 'test_tool.py'),
                                                        {'tool'}, self.dir)]
        self.assertEqual(sorted(found), ['?', 'd/LIST.tsv', 'docs/a.md', 'x/b.md', 'x/c.md'])


class Config(Fixture):
    """Every "tool_tests" key: its default, and set."""
    DATA = {}

    def test_no_section_is_the_defaults(self):
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
        cfg = tool_deps.config(self.root)
        self.assertEqual(cfg['mentions'], 'path')
        self.assertEqual(cfg['selector_files'], ['tool_deps.py', 'pytests.py'])
        self.assertEqual(cfg['guard_tests'], ['test_tool_deps'])
        self.assertEqual(cfg['unmapped_tool'], 'guard')
        self.assertEqual(cfg['skip_dirs'], ['__pycache__'])
        for k in ('extra', 'whole_tree', 'dynamic_imports'):
            self.assertEqual(cfg[k], {})
        for k in ('pinned', 'patterns', 'untested', 'always_run', 'sources', 'ignore'):
            self.assertEqual(cfg[k], [])

    def test_the_section_is_read_from_kit_json_at_the_root(self):
        with open(os.path.join(self.root, '.kit.json'), 'w') as fh:
            json.dump({'tool_tests': {'always_run': ['test_x'], 'shards': {'test_x': 4}}}, fh)
        self.assertEqual(tool_deps.config(self.root)['always_run'], ['test_x'])

    def test_bad_values_refuse(self):
        bad = [{'nonsense': 1}, {'mentions': 'words'}, {'unmapped_tool': 'some'},
               {'sources': ['tools/*.ps1']},                     # sources without tokens mode
               {'selector_files': 'gate.py'}, {'extra': []}, {'extra': {'x.json': 'a'}},
               {'pinned': [['docs/x.md']]}, {'pinned': [[{'piece': 'p'}, ['test_a']]]},
               {'patterns': [{'regex': '(', 'modules': ['test_a']}]}, {'patterns': [{'modules': []}]},
               {'whole_tree': {'test_a': 'why'}}, {'whole_tree': {'test_a.T': ''}},
               {'dynamic_imports': {'m': ['c', 'why']}}, {'always_run': [3]}]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ConfigError):
                    tool_deps.config(data=data)

    def test_pytests_keys_are_allowed_here(self):
        tool_deps.config(data={k: None for k in tool_deps.PYTESTS_KEYS})

    def test_selector_files_set(self):
        self.write('a.py', 'X = 1\n')
        self.write('test_a.py', 'import a\n' + TESTCASE)
        self.write('tool_deps.py', 'X = 1\n')
        self.write('test_sel.py', 'import tool_deps\n' + TESTCASE)
        self.assertEqual(self.select('tools/a.py'), ['test_a'])
        self.assertEqual(self.select('tools/tool_deps.py'), tool_deps.ALL)      # the default
        self.cfg = tool_deps.config(data={'selector_files': ['a.py']})
        self.assertEqual(self.select('tools/a.py'), tool_deps.ALL)
        self.assertEqual(self.select('tools/tool_deps.py'), ['test_sel'])      # an ordinary tool now

    def test_guard_tests_default_is_added_only_when_it_exists(self):
        self.write('a.py', 'X = 1\n')
        self.write('test_a.py', 'import a\n' + TESTCASE)
        self.assertEqual(self.select('tools/a.py'), ['test_a'])
        self.write('test_tool_deps.py', TESTCASE)
        self.assertEqual(self.select('tools/a.py'), ['test_a', 'test_tool_deps'])

    def test_guard_tests_set_and_empty(self):
        self.write('a.py', 'X = 1\n')
        self.write('lonely.py', 'X = 1\n')
        self.write('test_a.py', 'import a\n' + TESTCASE)
        self.write('test_guard.py', TESTCASE)
        self.cfg = tool_deps.config(data={'guard_tests': ['test_guard']})
        self.assertEqual(self.select('tools/a.py'), ['test_a', 'test_guard'])
        self.assertEqual(self.select('tools/lonely.py'), ['test_guard'])
        self.cfg = tool_deps.config(data={'guard_tests': []})
        self.assertEqual(self.select('tools/a.py'), ['test_a'])
        self.assertEqual(self.select('tools/lonely.py'), tool_deps.ALL)     # no guard: unmapped is ALL

    def test_unmapped_tool_all(self):
        self.write('lonely.py', 'X = 1\n')
        self.write('test_tool_deps.py', TESTCASE)
        self.assertEqual(self.select('tools/lonely.py'), ['test_tool_deps'])
        self.cfg = tool_deps.config(data={'unmapped_tool': 'all'})
        self.assertEqual(self.select('tools/lonely.py'), tool_deps.ALL)
        self.assertEqual(self.select('tools/test_tool_deps.py'), ['test_tool_deps'])   # a test maps itself

    def test_always_run_is_appended_in_order_unless_selected(self):
        self.write('a.py', 'X = 1\n')
        self.write('test_a.py', 'import a\n' + TESTCASE)
        self.write('test_b.py', TESTCASE)
        self.cfg = tool_deps.config(data={'always_run': ['test_b.T', 'test_a'], 'guard_tests': []})
        self.assertEqual(self.select('tools/a.py'), ['test_a', 'test_b.T'])
        self.assertEqual(self.select('docs/x.md'), ['test_b.T', 'test_a'])
        self.assertEqual(self.select('tools/b.json'), tool_deps.ALL)          # ALL holds them all

    def test_ignore_selects_nothing(self):
        self.write('test_tool_deps.py', TESTCASE)
        self.assertEqual(self.select('tools/notes.md'), tool_deps.ALL)
        self.cfg = tool_deps.config(data={'ignore': ['tools/**/*.md']})
        self.assertEqual(self.select('tools/notes.md', 'tools/sub/deep/x.md'), [])

    def test_extra_set(self):
        self.write('reader.py', 'X = 1\n')
        self.write('test_reader.py', 'import reader\n' + TESTCASE)
        self.assertEqual(self.select('tools/data.json'), tool_deps.ALL)
        self.cfg = tool_deps.config(data={'extra': {'data.json': ['reader']}})
        self.assertEqual(self.select('tools/data.json'), ['test_reader'])

    def test_pinned_paths_prefixes_and_patterns(self):
        self.cfg = tool_deps.config(data={'pinned': [['docs/a.md', ['test_a']], ['spec/', ['test_s']]],
                                          'patterns': [{'regex': r'\.proto$', 'modules': ['test_p']}]})
        self.assertEqual(self.select('docs/a.md'), ['test_a'])
        self.assertEqual(self.select('spec/x/y.md', 'spec'), ['test_s'])
        self.assertEqual(self.select('docs/b.md'), [])
        self.assertEqual(self.select('wire/m.proto'), ['test_p'])

    def test_a_piece_pin_without_a_table_refuses(self):
        cfg = tool_deps.config(data={'pinned': [[{'piece': 'p', 'path': ''}, ['test_a']]]})
        with self.assertRaises(ConfigError):
            tool_deps.pinned(cfg)
        g = tool_deps.Graph({}, set(), cfg=cfg)
        # An unmapped tools path is ALL before any pin is read.
        self.assertEqual(tool_deps.select({'tools/x.py'}, g, merged=True), tool_deps.ALL)  # not read
        with self.assertRaises(ConfigError):
            tool_deps.select({'docs/x.md'}, g)

    def test_a_piece_pin_resolves_through_the_table_passed(self):
        cfg = tool_deps.config(data={'pinned': [[{'piece': 'link', 'path': 'x/'}, ['test_b']],
                                                [{'solution': True}, ['test_s']]]})
        self.assertEqual(tool_deps.pinned(cfg, OLD), [(OLD.path('link', 'x/'), ('test_b',)),
                                                      (OLD.solution(), ('test_s',))])

    def test_the_tools_dir_comes_from_the_repo_section(self):
        with open(os.path.join(self.root, '.kit.json'), 'w') as fh:
            json.dump({'repo': {'tools_dir': 'scripts/py'}}, fh)
        self.write('scripts/py/a.py', 'X = 1\n', under=self.root)
        self.write('scripts/py/test_a.py', 'import a\n' + TESTCASE, under=self.root)
        g = tool_deps.scan(None, tool_deps.config(self.root), self.root)
        self.assertEqual(g.tools_rel, 'scripts/py')
        self.assertEqual(tool_deps.select({'scripts/py/a.py'}, g), ['test_a'])
        self.assertEqual(tool_deps.select({'tools/a.py'}, g), [])        # not the tools dir here


class Audit(Fixture):
    """`audit` (`--check`): the honesty checks a repository runs on its own tools, on a fixture."""
    DATA = {'extra': {'data.json': ['a']}, 'pinned': [['docs/x.md', ['test_a']]],
            'untested': ['lonely'], 'whole_tree': {'test_a.T': 'reads the whole tree'}}

    def setUp(self):
        super().setUp()
        self.write('a.py', "ROOT = '/r'\n")
        self.write('lonely.py', 'X = 1\n')
        self.write('data.json', '{}\n')
        self.write('test_a.py', "import os\nimport a\n\ndef f():\n    os.path.join(a.ROOT, 'docs', 'x.md')\n"
                   + TESTCASE)
        self.write('test_tool_deps.py', TESTCASE)
        self.write('docs/x.md', 'x\n', under=self.root)

    def audit(self, **data):
        cfg = tool_deps.config(data=dict(self.DATA, **data))
        return tool_deps.audit(self.root, cfg=cfg, tools_dir=self.dir)

    def test_an_honest_fixture_has_no_problem(self):
        self.assertEqual(self.audit(), [])

    def test_each_break_is_named(self):
        cases = [
            ({'untested': []}, 'tool lonely: no test imports it'),
            ({'untested': ['lonely', 'a']}, '"untested" entry a is stale (it is tested)'),
            ({'untested': ['lonely', 'gone']}, '"untested" entry gone is stale (no such tool)'),
            ({'extra': {'nope.json': ['a']}}, '"extra" file nope.json does not exist'),
            ({'extra': {'data.json': ['zz']}}, '"extra" data.json: reader zz is not a tools module'),
            ({'pinned': [['docs/x.md', ['test_a']], ['docs/y.md', ['test_a']]]}, '"pinned" path docs/y.md'),
            ({'pinned': [['docs/x.md', ['test_a', 'test_q']]]}, '"pinned" docs/x.md: test_q is not a test'),
            ({'pinned': []}, 'test_a.py:5 reads docs/x.md, which no table names'),
            ({'whole_tree': {'test_a.Gone': 'why'}}, '"whole_tree" class test_a.Gone does not exist'),
            ({'always_run': ['test_q']}, '"always_run": test_q is not a test module'),
            ({'ignore': ['tools/test_a.py']}, 'test module test_a is not selected by a change to itself'),
            ({'dynamic_imports': {'a': [['lonely'], 'why']}}, '"dynamic_imports" entry a: no computed import'),
        ]
        for data, want in cases:
            with self.subTest(data=data):
                got = self.audit(**data)
                self.assertTrue(any(want in p for p in got), got)

    def test_an_undeclared_computed_import_is_named_and_a_declared_one_passes(self):
        self.write('loader.py', 'import importlib\n\ndef load(n):\n    return importlib.import_module(n)\n')
        self.write('test_loader.py', 'import loader\n' + TESTCASE)
        self.assertEqual(self.audit(), ['loader: a computed import (line 4) the scan cannot follow - '
                                        'declare it in "dynamic_imports"'])
        self.assertEqual(self.audit(dynamic_imports={'loader': [['a'], 'plugins']}), [])

    def test_every_test_module_the_scan_finds_is_one_discover_loads(self):
        # The scan and unittest discovery agree on what a test module is, on a fixture: a TestCase-less test*.py
        # is a tool to both.
        self.write('test_zz_tooldeps_fixture.py', TESTCASE)
        self.write('test_zz_tooldeps_report.py', 'def main():\n    pass\n')
        before = set(sys.modules)
        try:
            found = set()
            for suite in unittest.TestLoader().discover(self.dir, pattern='test_zz_tooldeps*.py',
                                                        top_level_dir=self.dir):
                for case in _cases(suite):
                    found.add(type(case).__module__)
        finally:
            for m in set(sys.modules) - before:
                del sys.modules[m]
            if self.dir in sys.path:
                sys.path.remove(self.dir)
        graph = self.graph()
        self.assertEqual(found, {t for t in graph.tests if t.startswith('test_zz_')})


def _cases(suite):
    if isinstance(suite, unittest.TestSuite):
        for s in suite:
            yield from _cases(s)
    else:
        yield suite


class Cli(Fixture):
    DATA = {}
    GIT = True

    def run_main(self, *argv, here=None):
        out = io.StringIO()
        rc = cli.main(list(argv) + ['-C', here or self.root], out=out)
        return rc, out.getvalue()

    def setUp(self):
        super().setUp()
        self.write('a.py', 'X = 1\n')
        self.write('test_a.py', 'import a\n' + TESTCASE)

    def test_paths_graph_and_check(self):
        self.assertEqual(self.run_main('tools/a.py'), (0, 'test_a\n'))
        self.assertEqual(self.run_main('docs/x.md'), (0, '(none)\n'))
        rc, out = self.run_main('--graph')
        self.assertEqual((rc, out), (0, 'a -> -\ntest_a [test] -> a\n'))
        self.assertEqual(self.run_main('--check'), (0, 'tool_deps: OK\n'))
        self.write('lonely.py', 'X = 1\n')
        rc, out = self.run_main('--check')
        self.assertEqual(rc, 1)
        self.assertIn('tool lonely: no test imports it', out)
        self.assertIn('tool_deps: 1 problem(s)', out)

    def test_a_bad_config_refuses_with_exit_2(self):
        with open(os.path.join(self.root, '.kit.json'), 'w') as fh:
            json.dump({'tool_tests': {'mentions': 'words'}}, fh)
        rc, out = self.run_main('tools/a.py')
        self.assertEqual(rc, 2)
        self.assertTrue(out.startswith('tool_deps: refused: kit configuration:'), out)

    def test_a_piece_pin_refuses_with_exit_2(self):
        with open(os.path.join(self.root, '.kit.json'), 'w') as fh:
            json.dump({'tool_tests': {'pinned': [[{'piece': 'p', 'path': ''}, ['test_a']]]}}, fh)
        rc, out = self.run_main('docs/x.md')
        self.assertEqual(rc, 2)
        self.assertIn('no pieces table was given', out)

    def test_the_tools_dir_is_the_checkouts(self):
        with open(os.path.join(self.root, '.kit.json'), 'w') as fh:
            json.dump({'repo': {'tools_dir': 'scripts'}}, fh)
        self.write('scripts/b.py', 'X = 1\n', under=self.root)
        self.write('scripts/test_b.py', 'import b\n' + TESTCASE, under=self.root)
        self.assertEqual(self.run_main('scripts/b.py'), (0, 'test_b\n'))

    def test_a_folder_that_is_no_checkout_refuses(self):
        empty = tempfile.mkdtemp(prefix='tool_deps_nogit_')
        self.addCleanup(shutil.rmtree, empty, True)
        rc, out = self.run_main('tools/a.py', here=empty)
        self.assertEqual(rc, 2)
        self.assertIn('tool_deps: refused:', out)

    def test_usage_is_exit_2(self):
        for argv in ([], ['--graph', 'tools/a.py'], ['--graph', '--check']):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm:
                    cli.main(argv + ['-C', self.root], out=io.StringIO())
                self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
