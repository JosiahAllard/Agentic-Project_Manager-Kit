"""The csproj and paths detectors (docs/module-map.md "Detectors"), on fixture trees."""
import unittest

from kitlib.modulemap.detectors import csproj, paths
from kitlib.modulemap.load import ROOT

from tests.modulemap._fixtures import Tree, csproj as proj, mapped


def found(detector, tree, mm=None):
    mm = mm or mapped()
    return [(c.src, c.dst, c.kind, c.path, c.line, c.target)
            for c in detector.detect(tree.files(), mm, tree.read())]


class CsprojTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree({
            'node/Fleet.Node.Core/Fleet.Node.Core.csproj': proj(),
            'node/Fleet.Node.Tests/TestSupport/Probe.cs': 'class P {}',
            'worker/src/Main.java': 'class M {}',
            'docs/quests.md': '# q',
            'shared/protocol/cases.json': '{}',
            'brain/Fleet.Brain/Fleet.Brain.csproj': proj(),
        })
        self.addCleanup(self.t.cleanup)

    def test_each_include_kind_is_its_crossing_kind(self):
        self.t.write('brain/Tests/Tests.csproj', proj(
            '<ProjectReference Include="..\\..\\node\\Fleet.Node.Core\\Fleet.Node.Core.csproj" />',
            '<Compile Include="..\\..\\node\\Fleet.Node.Tests\\TestSupport\\Probe.cs" Link="S\\Probe.cs" />',
            '<None Include="../../worker/src/Main.java" Link="x/Main.java" CopyToOutputDirectory="PreserveNewest" />',
            '<Content Include="../../docs/quests.md" />'))
        got = found(csproj, self.t)
        self.assertEqual([(s, d, k, l) for s, d, k, _, l, _ in got],
                         [('brain', 'node', 'build', 3), ('brain', 'node', 'source', 4),
                          ('brain', 'worker', 'data', 5), ('brain', ROOT, 'data', 6)])

    def test_inside_the_module_or_into_messaging_is_reported_only_when_it_leaves(self):
        self.t.write('brain/Tests/Tests.csproj', proj(
            '<ProjectReference Include="..\\Fleet.Brain\\Fleet.Brain.csproj" />',
            '<None Include="../../shared/protocol/cases.json" />'))
        got = found(csproj, self.t)
        self.assertEqual([(s, d) for s, d, *_ in got], [('brain', 'protocol')])

    def test_a_glob_is_one_crossing_read_from_its_literal_part(self):
        self.t.write('brain/Tests/Tests.csproj', proj(
            '<Compile Include="../../node/**/*.cs" />', '<None Include="../../*.md" />'))
        got = found(csproj, self.t)
        self.assertEqual([(d, k) for _, d, k, *_ in got], [('node', 'source'), (ROOT, 'data')])

    def test_comments_properties_update_items_and_paths_outside_the_repo_are_skipped(self):
        self.t.write('brain/Tests/Tests.csproj', proj(
            '<!-- <ProjectReference Include="../../node/Fleet.Node.Core/Fleet.Node.Core.csproj" /> -->',
            '<None Include="$(RepoRoot)/worker/src/Main.java" />',
            '<None Update="../../worker/src/Main.java" />',
            '<None Include="../../../elsewhere/x.json" />'))
        self.assertEqual(found(csproj, self.t), [])

    def test_a_semicolon_list_is_one_crossing_per_entry(self):
        self.t.write('brain/Tests/Tests.csproj', proj(
            '<None Include="../../worker/src/Main.java;../../docs/quests.md" />'))
        self.assertEqual([d for _, d, *_ in found(csproj, self.t)], ['worker', ROOT])

    def test_a_project_no_module_holds_is_not_read(self):
        self.t.write('App.csproj', proj('<Compile Include="brain/**/*.cs" />'))
        self.assertEqual(found(csproj, self.t), [])


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree({
            'worker/src/main/java/W.java': 'class W {}',
            'docs/reference/quests/README.md': '# q',
            'brain/docs/security.md': '# s',
            'brain/Fleet.Brain.Tests/fixtures/a.json': '{}',
            'shared/protocol/cases.json': '{}',
            'Fleet.slnx': '<Solution />',
        })
        self.addCleanup(self.t.cleanup)

    def test_a_literal_naming_another_modules_file_is_a_data_crossing(self):
        self.t.write('brain/Fleet.Brain.Tests/ReadTests.cs',
                     'class T {\n  string a = "worker/src/main/java/W.java";\n'
                     '  string b = @"docs\\reference\\quests";\n}\n')
        got = found(paths, self.t)
        self.assertEqual([(d, k, l, t) for _, d, k, _, l, t in got],
                         [('worker', 'data', 2, 'worker/src/main/java/W.java'),
                          (ROOT, 'data', 3, 'docs\\reference\\quests')])

    def test_a_relative_climb_is_resolved_from_the_files_folder(self):
        self.t.write('brain/Fleet.Brain.Tests/Rel.cs', 'var p = "../../worker/src/main/java/W.java";')
        self.assertEqual([d for _, d, *_ in found(paths, self.t)], ['worker'])

    def test_a_path_inside_the_module_or_into_messaging_or_absent_is_no_crossing_of_another_module(self):
        self.t.write('brain/Fleet.Brain.Tests/In.cs',
                     'var a = "brain/docs/security.md"; var b = "fixtures/a.json";\n'
                     'var c = "shared/protocol/cases.json"; var d = "worker/src/gone.java";\n'
                     'var e = "https://example.com/worker/src"; var f = "**/*.java";')
        got = found(paths, self.t)
        self.assertEqual([d for _, d, *_ in got], ['protocol'])

    def test_a_root_marker_or_a_tree_listing_is_a_tree_crossing(self):
        self.t.write('brain/Fleet.Brain.Tests/Climb.cs',
                     'while (!File.Exists(Path.Combine(d, "Fleet.slnx"))) d = Up(d);\n'
                     'Directory.Exists(Path.Combine(d, ".git"));\n'
                     'Run("git", "ls-files");\n')
        got = found(paths, self.t)
        self.assertEqual([(d, k, l) for _, d, k, _, l, _ in got],
                         [(ROOT, 'tree', 1), (ROOT, 'tree', 2), (ROOT, 'tree', 3)])

    def test_a_map_root_marker_is_added(self):
        self.t.write('brain/Fleet.Brain.Tests/M.cs', 'var m = "REPO_ROOT.marker";')
        self.assertEqual(found(paths, self.t), [])
        mm = mapped({'root_markers': ['REPO_ROOT.marker']})
        self.assertEqual([k for _, _, k, *_ in found(paths, self.t, mm)], ['tree'])

    def test_only_test_sources_that_are_code_are_read(self):
        self.t.write('brain/Fleet.Brain/Prod.cs', 'var p = "worker/src/main/java/W.java";')
        self.t.write('brain/Fleet.Brain.Tests/notes.md', '"worker/src/main/java/W.java"')
        self.assertEqual(found(paths, self.t), [])
        mm = mapped({'test_sources': ['brain/Fleet.Brain/*']})
        self.assertEqual([p for _, _, _, p, *_ in found(paths, self.t, mm)], ['brain/Fleet.Brain/Prod.cs'])

    def test_single_quotes_are_literals_in_python_not_in_csharp(self):
        self.t.write('brain/tests/test_x.py', "p = 'worker/src/main/java/W.java'\n")
        self.t.write('brain/Fleet.Brain.Tests/Q.cs', "char c = 'worker/src/main/java/W.java';\n")
        self.assertEqual([p for _, _, _, p, *_ in found(paths, self.t)], ['brain/tests/test_x.py'])


if __name__ == '__main__':
    unittest.main()
