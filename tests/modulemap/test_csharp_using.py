"""The csharp-using detector (docs/module-map.md "Detectors")."""
import unittest

from kitlib.modulemap import check as C
from kitlib.modulemap.detectors import csharp_using
from kitlib.modulemap.load import MapError, ModuleMap

from tests.modulemap._fixtures import Tree

MODULES = {'app': {'paths': ['app']}, 'core': {'paths': ['core']}, 'extra': {'paths': ['extra']}}


def mapped(**extra):
    return ModuleMap(dict({'modules': MODULES}, **extra))


def proj(root_ns=None):
    body = '<RootNamespace>%s</RootNamespace>' % root_ns if root_ns else ''
    return '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>%s</PropertyGroup></Project>' % body


class CSharpUsingTest(unittest.TestCase):
    def tree(self, files):
        t = Tree(files)
        self.addCleanup(t.cleanup)
        return t

    def found(self, t, mm):
        return [(c.path, c.line, c.src, c.dst, c.kind, c.target)
                for c in csharp_using.detect(t.files(), mm, t.read())]

    def test_a_using_of_another_modules_namespace_is_a_build_crossing(self):
        t = self.tree({'app/A.cs': 'using System;\nusing Acme.Core.Util;\n'})
        mm = mapped(namespaces={'Acme.Core': 'core'})
        self.assertEqual(self.found(t, mm),
                         [('app/A.cs', 2, 'app', 'core', 'build', 'using Acme.Core.Util;')])

    def test_the_check_refuses_it_unless_an_edge_allows_it(self):
        t = self.tree({'app/A.cs': 'using Acme.Core;\n'})
        refused = mapped(namespaces={'Acme.Core': 'core'})
        out = C.check(refused, {}, t.files(), t.read())
        self.assertEqual(out.code, C.FAIL, out.lines)
        self.assertTrue(any('csharp-using' in ln for ln in out.lines), out.lines)
        edge = {'from': 'app', 'to': 'core', 'kind': 'build', 'why': 'the app uses the core'}
        allowed = mapped(namespaces={'Acme.Core': 'core'}, edges=[edge])
        self.assertEqual(C.check(allowed, {}, t.files(), t.read()).code, C.OK)

    def test_alias_static_and_global_forms(self):
        t = self.tree({'app/A.cs': 'global using Acme.Core;\n'
                                   'using static Acme.Core.Math;\n'
                                   'using U = Acme.Core.Util;\n'
                                   'global using static Acme.Core.Log;\n'
                                   'using var x = Acme.Core.Make();\n'
                                   '    using Acme.Core.Indented;\n'})
        lines = [f[1] for f in self.found(t, mapped(namespaces={'Acme.Core': 'core'}))]
        self.assertEqual(lines, [1, 2, 3, 4, 6])

    def test_a_generic_alias_is_a_crossing(self):
        t = self.tree({'app/A.cs': 'using A = Other.Ns.Box<int>;\n'
                                   'using B = Other.Ns.Box<int?>;\n'
                                   'using C = Other.Ns.Thing?;\n'})
        found = self.found(t, mapped(namespaces={'Other.Ns': 'core'}))
        self.assertEqual([(f[1], f[3]) for f in found], [(1, 'core'), (2, 'core'), (3, 'core')])

    def test_comments_are_skipped(self):
        t = self.tree({'app/A.cs': '// using Acme.Core;\n/* using Acme.Core;\nusing Acme.Core;\n*/\n'
                                   'using Acme.Core; // real\n'})
        self.assertEqual([f[1] for f in self.found(t, mapped(namespaces={'Acme.Core': 'core'}))],
                         [5])

    def test_longest_prefix_wins_and_a_prefix_ends_a_name(self):
        t = self.tree({'app/A.cs': 'using Acme.Core.Extra.Q;\nusing Acme.Corex;\nusing Acme.Core;\n'})
        mm = mapped(namespaces={'Acme.Core': 'core', 'Acme.Core.Extra': 'extra'})
        self.assertEqual([(f[1], f[3]) for f in self.found(t, mm)], [(1, 'extra'), (3, 'core')])

    def test_an_unmapped_namespace_and_the_own_module_are_ignored(self):
        t = self.tree({'app/A.cs': 'using System.Linq;\nusing Other.Lib;\nusing Acme.App.Inner;\n'})
        mm = mapped(namespaces={'Acme.Core': 'core', 'Acme.App': 'app'})
        self.assertEqual(self.found(t, mm), [])

    def test_root_namespaces_map_when_the_map_has_no_prefixes(self):
        t = self.tree({'core/Core.csproj': proj('Acme.Kernel'),
                       'extra/Acme.Extra.csproj': proj(),
                       'app/A.cs': 'using Acme.Kernel.Io;\nusing Acme.Extra;\nusing Core;\n'})
        self.assertEqual([(f[1], f[3]) for f in self.found(t, mapped())],
                         [(1, 'core'), (2, 'extra')])

    def test_a_prefix_map_replaces_the_root_namespaces(self):
        t = self.tree({'core/Core.csproj': proj('Acme.Kernel'),
                       'app/A.cs': 'using Acme.Kernel.Io;\nusing Acme.Core;\n'})
        mm = mapped(namespaces={'Acme.Core': 'core'})
        self.assertEqual([(f[1], f[3]) for f in self.found(t, mm)], [(2, 'core')])

    def test_a_root_namespace_two_modules_claim_names_no_module(self):
        t = self.tree({'core/Core.csproj': proj('Acme.Shared'),
                       'extra/Extra.csproj': proj('Acme.Shared'),
                       'app/A.cs': 'using Acme.Shared;\n'})
        self.assertEqual(self.found(t, mapped()), [])

    def test_a_prefix_naming_no_module_is_refused(self):
        with self.assertRaises(MapError):
            mapped(namespaces={'Acme.Core': 'nowhere'})


if __name__ == '__main__':
    unittest.main()
