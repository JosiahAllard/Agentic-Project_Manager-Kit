"""kitlib.docs.codemap: grouping, scanning and the file listing (from the PM kit's
tools/test_codemap.py; its KIT_REPO_ROOT case is dropped with the env override)."""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.docs import codemap
from kitlib.git import cmd
from tests.gitfixture import TempRepo, rmtree


class Group(unittest.TestCase):
    def test_a_java_source_root_keeps_four_package_levels_as_a_real_path(self):
        self.assertEqual(codemap.group('app/src/main/java/com/example/core/net/wire'),
                         'app/src/main/java/com/example/core/net')

    def test_a_kotlin_test_root_is_grouped_the_same_way(self):
        self.assertEqual(codemap.group('src/test/kotlin/org/demo/a'), 'src/test/kotlin/org/demo/a')

    def test_anything_else_keeps_three_folder_levels(self):
        self.assertEqual(codemap.group('svc/Core/Net/Wire'), 'svc/Core/Net')
        self.assertEqual(codemap.group('tools'), 'tools')


class Scan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='codemap-test-')
        self.addCleanup(rmtree, self.root)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)

    def test_types_and_members_of_a_csharp_file(self):
        self.write('src/A.cs', 'public sealed class Alpha\n{\n    public void Run(int x)\n    {\n        if (x) { }\n    }\n}\n')
        types, members, n = codemap.scan(self.root, 'src/A.cs')
        self.assertEqual(types, [('Alpha', 'class', 1)])
        self.assertEqual(members, [('Run', 3)])
        self.assertEqual(n, 7)

    def test_python_defs_and_classes_are_members(self):
        self.write('tools/x.py', 'class Box(object):\n    def open(self):\n        pass\n\ndef main():\n    pass\n')
        types, members, _ = codemap.scan(self.root, 'tools/x.py')
        self.assertEqual(types, [('Box', 'class', 1)])
        self.assertEqual([m[0] for m in members], ['open', 'main'])

    def test_an_unreadable_file_is_empty_not_an_error(self):
        self.assertEqual(codemap.scan(self.root, 'no/such/file.py'), ([], [], 0))

    def test_gdscript_class_names_inner_classes_and_funcs(self):
        self.write('game/ball.gd', 'extends Node\nclass_name Ball\n\nfunc _ready():\n\tpass\n'
                                   'static func make(x):\n\treturn x\nclass Inner:\n\tfunc go():\n\t\tpass\n')
        types, members, n = codemap.scan(self.root, 'game/ball.gd')
        self.assertEqual(types, [('Ball', 'class_name', 2), ('Inner', 'class', 8)])
        self.assertEqual(members, [('_ready', 4), ('make', 6), ('go', 9)])
        self.assertEqual(n, 10)

    def test_files_keeps_source_extensions_and_drops_skipped_folders(self):
        r = cmd.Result(0, b'a/b.gd\na/c.gdshader\n.godot/x.gd\nbuild/y.java\nsrc/Z.java\nREADME.md\n', '')
        with mock.patch.object(codemap.cmd, 'call', return_value=r):
            self.assertEqual(codemap.files(self.root), (['a/b.gd', 'a/c.gdshader', 'src/Z.java'], None))


class Run(unittest.TestCase):
    """run() over a real repository: tracked and untracked-not-ignored files, every mode."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('src/A.cs', 'public class Alpha\n{\n    public void Run()\n    {\n    }\n}\n')
        self.repo.commit('a', 'src/A.cs')
        self.repo.write('tools/new.py', 'def fresh():\n    pass\n')       # untracked, not ignored
        self.repo.write('.gitignore', 'ignored/\n')
        self.repo.write('ignored/x.py', 'def hidden():\n    pass\n')

    def lines(self, **kw):
        got = []
        return codemap.run(self.repo.root, got.append, **kw), got

    def test_find_sees_tracked_and_untracked_files_but_not_ignored_ones(self):
        rc, got = self.lines(find='fresh')
        self.assertEqual((rc, got), (0, ['tools/new.py:1  fresh()', '1 hits']))
        rc, got = self.lines(find='hidden')
        self.assertEqual((rc, got), (0, ['0 hits']))

    def test_find_matches_a_type_by_substring_and_a_member_exactly(self):
        rc, got = self.lines(find='alp')
        self.assertEqual(got, ['src/A.cs:1  class Alpha', '1 hits'])
        rc, got = self.lines(find='ru')
        self.assertEqual(got, ['0 hits'])

    def test_file_lists_types_then_members(self):
        rc, got = self.lines(file='src\\A.cs')
        self.assertEqual(got, ['src/A.cs  6 lines', '      1  class Alpha', '      3    Run()'])

    def test_dir_lists_each_file_with_its_types(self):
        rc, got = self.lines(dir='src/')
        self.assertEqual(rc, 0)
        self.assertEqual([l.split() for l in got], [['6', 'A.cs', 'Alpha']])

    def test_the_top_view_has_a_header_and_one_row_per_folder(self):
        rc, got = self.lines()
        self.assertTrue(got[0].startswith('files  lines  folder  | largest files'))
        self.assertEqual(sorted(l.split('|')[0].split()[2] for l in got[1:]), ['src', 'tools'])

    def test_a_folder_git_cannot_list_exits_2_with_the_reason(self):
        not_repo = tempfile.mkdtemp(prefix='codemap-norepo-')
        self.addCleanup(rmtree, not_repo)
        got = []
        self.assertEqual(codemap.run(not_repo, got.append, find='x'), 2)
        self.assertTrue(got[0].startswith('codemap: cannot list the files of'), got)


if __name__ == '__main__':
    unittest.main()
