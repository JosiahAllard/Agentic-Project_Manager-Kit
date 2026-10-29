"""kitlib.modulemap.scope and tools/module_map.py --scope (docs/module-map.md "The scope
report")."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from kitlib.config import load as load_config
from kitlib.modulemap import MapError
from kitlib.modulemap import check as C
from kitlib.modulemap import scope

from tests.gitfixture import TempRepo, fixture_env
from tests.modulemap._fixtures import MAP, mapped

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import module_map as cli  # noqa: E402

EDGES = [{'from': 'brain', 'to': 'node', 'kind': 'build', 'why': 'tests drive a node'}]
RATCHETS = [{'id': 'worker-data-node', 'from': 'worker', 'to': 'node', 'kinds': ['data'],
             'goal': 0}]
MAP_REL = 'docs/module-map.json'


class ScopeOfTest(unittest.TestCase):
    def setUp(self):
        self.mm = mapped({'edges': EDGES, 'ratchets': RATCHETS})

    def test_each_path_maps_to_its_module_and_nothing_widens_without_a_way_in(self):
        s = scope.scope_of(self.mm, ['brain/a.cs', 'README.md'])
        self.assertEqual(dict(s.paths), {'brain/a.cs': 'brain', 'README.md': '(root)'})
        self.assertEqual(s.changed, ['(root)', 'brain'])
        self.assertEqual(s.widen, [])

    def test_a_module_widens_to_those_reaching_it_by_edge_or_ratchet(self):
        s = scope.scope_of(self.mm, ['node/n.cs'])
        self.assertEqual(s.changed, ['node'])
        self.assertEqual(s.widen, [('brain', 'node', 'via edge build: tests drive a node'),
                                   ('worker', 'node', 'via ratchet worker-data-node')])

    def test_a_changed_dependent_is_not_widened_to(self):
        s = scope.scope_of(self.mm, ['node/n.cs', 'brain/b.cs'])
        self.assertEqual(s.widen, [('worker', 'node', 'via ratchet worker-data-node')])

    def test_a_messaging_change_widens_to_every_module(self):
        s = scope.scope_of(self.mm, ['shared/protocol/m.proto'])
        self.assertEqual(s.changed, ['protocol'])
        self.assertEqual(s.widen, [(m, 'protocol', 'via messaging')
                                   for m in ('brain', 'node', 'worker')])

    def test_a_layering_ratchet_inside_one_module_widens_nothing(self):
        mm = mapped({'ratchets': [{'id': 'w', 'from': 'worker', 'to': 'worker',
                                   'detector': 'java-imports', 'goal': 0, 'files': ['worker/*'],
                                   'imports': ['org.x']}]})
        self.assertEqual(scope.scope_of(mm, ['worker/A.java']).widen, [])

    def test_the_report_names_paths_widenings_and_a_summary(self):
        lines = scope.report(scope.scope_of(self.mm, ['node/n.cs']), 'main (abc)')
        self.assertEqual(lines, [
            'module_map scope: base main (abc), 1 changed path(s) in 1 module(s)',
            '  node/n.cs -> node',
            'widen: brain <- node  via edge build: tests drive a node',
            'widen: worker <- node  via ratchet worker-data-node',
            'module_map scope: changed node; widened by 2: brain, worker'])
        self.assertEqual(scope.report(scope.scope_of(self.mm, []), 'main (abc)')[-1],
                         'module_map scope: changed none; widened by 0')


class ScopeRepoTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.repo.write(MAP_REL, json.dumps(dict(MAP, edges=EDGES)))
        self.repo.write('.kit.json', json.dumps({'modules': {'map': MAP_REL}}))
        self.repo.write('node/n.cs', 'class N {}')
        self.repo.commit('map', '.')
        self.repo.git('checkout', '-q', '-b', 'topic')

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['-C', self.root] + list(argv))
        return code, out.getvalue()

    def test_committed_uncommitted_and_untracked_changes_count(self):
        self.repo.write('worker/w.cs', 'class W {}')
        self.repo.commit('worker', 'worker/w.cs')
        self.repo.write('node/n.cs', 'class N2 {}')
        self.repo.write('brain/new.cs', 'class B {}')
        out = C.run(self.root, load_config(self.root), 'scope', base_rev='main')
        self.assertEqual(out.code, C.OK, out.lines)
        self.assertIn('  worker/w.cs -> worker', out.lines)
        self.assertIn('  node/n.cs -> node', out.lines)
        self.assertIn('  brain/new.cs -> brain', out.lines)
        self.assertEqual(out.lines[-1], 'module_map scope: changed brain, node, worker; widened '
                                        'by 0')

    def test_the_default_base_is_the_main_branch_and_no_owner_path_is_needed(self):
        self.repo.write('node/n.cs', 'class N2 {}')
        code, out = self.cli('--scope')
        self.assertEqual(code, 0, out)
        self.assertIn('widen: brain <- node  via edge build: tests drive a node', out)
        self.assertIn('module_map scope: changed node; widened by 1: brain', out)

    def test_no_change_selects_nothing(self):
        code, out = self.cli('--scope', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('0 changed path(s) in 0 module(s)', out)

    def test_a_bad_base_exits_2(self):
        with self.assertRaisesRegex(MapError, 'cannot read the base no-such-rev'):
            C.run(self.root, load_config(self.root), 'scope', base_rev='no-such-rev')
        code, out = self.cli('--scope', '--base', 'no-such-rev')
        self.assertEqual(code, 2, out)
        self.assertIn('module_map: NOT CHECKED: cannot read the base no-such-rev', out)

    def test_base_with_baseline_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(['-C', self.root, '--baseline', '--base', 'main'])
        self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
