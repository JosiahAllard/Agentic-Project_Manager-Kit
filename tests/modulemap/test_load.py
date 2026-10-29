"""kitlib.modulemap.load: the map file's shape, its rules and module_of (docs/module-map.md "The map file")."""
import json
import unittest

from kitlib.modulemap.load import ROOT, MapError, ModuleMap, load_ceilings, load_map

from tests.modulemap._fixtures import MAP, Tree, mapped


def with_(**kw):
    data = json.loads(json.dumps(MAP))
    data.update(kw)
    return data


class ModuleOfTest(unittest.TestCase):
    def test_a_path_is_in_the_module_of_its_deepest_matching_folder(self):
        mm = ModuleMap({'modules': {'shared': {'paths': ['shared']},
                                    'protocol': {'paths': ['shared/protocol']}}})
        self.assertEqual(mm.module_of('shared/protocol/a.json'), ('protocol', 'shared/protocol'))
        self.assertEqual(mm.module_of('shared/other/b.txt'), ('shared', 'shared'))

    def test_a_glob_entry_matches_a_folder_and_case_does_not_matter(self):
        mm = mapped()
        self.assertEqual(mm.module_of('Shared/Fleet.Protocol.Tests/X.cs')[0], 'protocol')
        self.assertEqual(mm.module_of('shared\\protocol\\x.json')[0], 'protocol')

    def test_a_path_no_module_holds_is_root(self):
        self.assertEqual(mapped().module_of('docs/reference/a.md'), (ROOT, ''))

    def test_a_folder_named_by_two_modules_refuses(self):
        mm = ModuleMap({'modules': {'a': {'paths': ['x']}, 'b': {'paths': ['x*']}}})
        with self.assertRaisesRegex(MapError, 'more than one module'):
            mm.module_of('x/f.cs')


class RulesTest(unittest.TestCase):
    def refuses(self, data, text):
        with self.assertRaises(MapError) as cm:
            ModuleMap(data)
        self.assertIn(text, str(cm.exception))

    def test_an_unknown_key_refuses(self):
        self.refuses(with_(edgez=[]), 'unknown key(s) edgez')

    def test_modules_are_required(self):
        self.refuses({}, 'needs "modules"')

    def test_an_edge_needs_a_reason_and_known_modules(self):
        self.refuses(with_(edges=[{'from': 'brain', 'to': 'node', 'kind': 'build'}]), 'needs "why"')
        self.refuses(with_(edges=[{'from': 'brain', 'to': 'nope', 'kind': 'build', 'why': 'w'}]),
                     'names nope, which is not a module')

    def test_an_edge_kind_is_build_data_or_tree(self):
        self.refuses(with_(edges=[{'from': 'brain', 'to': 'node', 'kind': 'source', 'why': 'w'}]),
                     'kind')

    def test_a_ratchet_needs_a_goal(self):
        self.refuses(with_(ratchets=[{'id': 'r', 'from': 'brain', 'to': 'node'}]), 'needs "goal"')

    def test_a_goal_above_zero_needs_the_owners_reason(self):
        self.refuses(with_(ratchets=[{'id': 'r', 'from': 'brain', 'to': 'node', 'goal': 3}]),
                     'goal above 0 needs')
        ModuleMap(with_(ratchets=[{'id': 'r', 'from': 'brain', 'to': 'node', 'goal': 3,
                                   'goal_reason': 'the owner: a real floor'}]))

    def test_a_ratchet_id_is_written_once(self):
        r = {'id': 'r', 'from': 'brain', 'to': 'node', 'goal': 0}
        self.refuses(with_(ratchets=[r, dict(r)]), 'written twice')

    def test_messaging_names_a_module(self):
        self.refuses(with_(messaging=['wire']), 'messaging names wire')

    def test_root_may_be_an_edge_or_ratchet_end(self):
        mm = ModuleMap(with_(edges=[{'from': 'brain', 'to': '(root)', 'kind': 'tree', 'why': 'w'}]))
        self.assertTrue(mm.edge_for('brain', ROOT, 'tree'))
        self.assertIsNone(mm.edge_for('brain', ROOT, 'data'))

    def test_a_path_outside_the_repo_refuses(self):
        self.refuses({'modules': {'a': {'paths': ['../x']}}}, 'not repo-relative')

    def test_an_absolute_path_refuses_before_it_is_normalised(self):
        self.refuses({'modules': {'a': {'paths': ['/etc']}}}, "path '/etc' is not repo-relative")
        self.refuses({'modules': {'a': {'paths': ['\\etc']}}}, 'not repo-relative')

    def test_two_modules_naming_one_literal_path_refuse_at_load(self):
        self.refuses({'modules': {'a': {'paths': ['shared/x']}, 'b': {'paths': ['Shared\\X/']}}},
                     'path Shared/X is named by more than one module (a, b)')

    def test_a_note_key_in_modules_is_not_a_module(self):
        mm = ModuleMap(with_(modules=dict(MAP['modules'], _note='why these four', comment='c')))
        self.assertEqual(sorted(mm.modules), ['brain', 'node', 'protocol', 'worker'])


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_the_map_and_ceilings_files_load(self):
        p = self.t.write('docs/module-map.json', MAP)
        self.assertEqual(sorted(load_map(p).modules), ['brain', 'node', 'protocol', 'worker'])
        c = self.t.write('docs/module-ceilings.json', {'r': 3, '_note': 'x'})
        self.assertEqual(load_ceilings(c), {'r': 3})
        self.assertEqual(load_ceilings(None), {})

    def test_a_missing_or_broken_file_refuses(self):
        with self.assertRaisesRegex(MapError, 'does not exist'):
            load_map(self.t.root + '/nope.json')
        p = self.t.write('m.json', '{"modules": {}, "modules": {}}')
        with self.assertRaisesRegex(MapError, 'written twice'):
            load_map(p)
        c = self.t.write('c.json', {'r': -1})
        with self.assertRaises(MapError):
            load_ceilings(c)


if __name__ == '__main__':
    unittest.main()
