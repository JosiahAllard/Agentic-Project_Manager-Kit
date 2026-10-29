"""The python detector (docs/module-map.md "Detectors"), on fixture trees."""
import unittest

from kitlib.modulemap import check as C
from kitlib.modulemap.detectors import python
from kitlib.modulemap.load import ModuleMap

from tests.modulemap._fixtures import Tree

MAP = {'modules': {'lib': {'paths': ['kitlib']}, 'tools': {'paths': ['tools']},
                   'tests': {'paths': ['tests']}, 'proto': {'paths': ['proto']}}}


def found(tree, mm=None):
    mm = mm or ModuleMap(MAP)
    return [(c.src, c.dst, c.kind, c.path, c.line, c.target)
            for c in python.detect(tree.files(), mm, tree.read())]


class PythonTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree({
            'kitlib/__init__.py': '',
            'kitlib/git/__init__.py': '',
            'kitlib/git/cmd.py': 'import os\n',
            'tests/__init__.py': '',
            'tests/helpers.py': '',
            'tools/util.py': '',
            'proto/wire.py': '',
        })
        self.addCleanup(self.t.cleanup)

    def test_an_absolute_import_of_another_modules_package_is_a_build_crossing(self):
        self.t.write('tools/run.py', 'import os\nimport kitlib.git.cmd\n'
                                     'from kitlib.git import cmd as c, nothing\n')
        self.assertEqual([(s, d, k, l, t) for s, d, k, _, l, t in found(self.t)],
                         [('tools', 'lib', 'build', 2, 'kitlib.git.cmd'),
                          ('tools', 'lib', 'build', 3, 'kitlib.git import cmd')])

    def test_a_relative_import_resolves_from_the_package(self):
        self.t.write('tests/sub/__init__.py', '')
        self.t.write('tests/sub/test_a.py', 'from .. import helpers\nfrom ...tools import util\n'
                                            'from . import nothing\n')
        self.assertEqual([(d, l) for _, d, _, _, l, _ in found(self.t)], [('tools', 2)])

    def test_imports_inside_the_module_or_unresolved_are_skipped(self):
        self.t.write('kitlib/git/refs.py', 'from . import cmd\nfrom kitlib.git import cmd\n'
                                           'import json, unittest.mock\nfrom tools import *\n')
        self.assertEqual([(d, l, t) for _, d, _, _, l, t in found(self.t)],
                         [('tools', 4, 'tools')])

    def test_the_files_own_folder_resolves_first(self):
        self.t.write('tools/proto.py', '')
        self.t.write('tools/run.py', 'import proto\nimport util\n')
        self.assertEqual(found(self.t), [])

    def test_a_file_that_does_not_parse_is_skipped(self):
        self.t.write('tools/bad.py', 'import kitlib.git.cmd\ndef (:\n')
        self.assertEqual(found(self.t), [])

    def test_the_check_runs_the_python_detector(self):
        self.t.write('tools/run.py', 'import proto.wire\n')
        mm = ModuleMap(dict(MAP, messaging=['proto']))
        out = C.check(mm, {}, self.t.files(), self.t.read())
        self.assertEqual(out.code, C.OK, out.lines)
        self.assertIn('1 into messaging', out.lines[0])


if __name__ == '__main__':
    unittest.main()
