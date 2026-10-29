"""kitlib.modulemap.check and tools/module_map.py (docs/module-map.md "--check", "--baseline")."""
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

from tests.gitfixture import TempRepo, fixture_env
from tests.modulemap._fixtures import MAP, Tree, csproj as proj, mapped

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import module_map as cli  # noqa: E402

TESTS_CSPROJ = proj(
    '<ProjectReference Include="../../node/Core/Core.csproj" />',
    '<Compile Include="../../node/Tests/Probe.cs" />',
    '<Compile Include="../../node/Tests/Other.cs" />',
    '<None Include="../../shared/protocol/cases.json" />')
FILES = {
    'brain/Tests/Tests.csproj': TESTS_CSPROJ,
    'brain/Tests/ReadWorker.cs': 'var p = "worker/src/W.java";\n',
    'node/Core/Core.csproj': proj(),
    'node/Tests/Probe.cs': 'class P {}',
    'node/Tests/Other.cs': 'class O {}',
    'worker/src/W.java': 'class W {}',
    'shared/protocol/cases.json': '{}',
}
BUILD_EDGE = {'from': 'brain', 'to': 'node', 'kind': 'build', 'why': 'the tests drive the node'}
SOURCE_RATCHET = {'id': 'brain-source-node', 'from': 'brain', 'to': 'node', 'kinds': ['source'],
                  'goal': 0}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.t = Tree(FILES)
        self.addCleanup(self.t.cleanup)

    def check(self, mm, ceilings=None):
        return C.check(mm, ceilings or {}, self.t.files(), self.t.read())

    def baseline(self, mm, ceilings=None):
        return C.baseline(mm, ceilings or {}, self.t.files(), self.t.read())


class CheckTest(Fixture):
    def test_a_crossing_with_no_edge_or_ratchet_is_refused(self):
        out = self.check(mapped())
        self.assertEqual(out.code, C.FAIL)
        self.assertEqual(out.lines[0], 'module_map: 5 crossing(s): 1 into messaging, 0 on allowed '
                         'edges, 0 under ratchets, 4 refused')
        self.assertIn('REFUSED  brain -> node  build  brain/Tests/Tests.csproj:3  '
                      '(csproj: ../../node/Core/Core.csproj)', out.lines)
        self.assertIn('REFUSED  brain -> worker  data  brain/Tests/ReadWorker.cs:1  '
                      '(paths: worker/src/W.java)', out.lines)
        self.assertEqual(out.lines[-1], 'module_map: FAIL')

    def test_an_edge_allows_only_its_own_kind(self):
        out = self.check(mapped({'edges': [BUILD_EDGE]}))
        self.assertIn('1 on allowed edges', out.lines[0])
        self.assertFalse(any('brain -> node  build' in l for l in out.lines))
        self.assertTrue(any('REFUSED  brain -> node  source' in l for l in out.lines))

    def test_a_ratchet_at_its_ceiling_passes_and_over_it_fails(self):
        mm = mapped({'edges': [BUILD_EDGE, dict(BUILD_EDGE, to='worker', kind='data')],
                     'ratchets': [SOURCE_RATCHET]})
        out = self.check(mm, {'brain-source-node': 2})
        self.assertEqual(out.code, C.OK, out.lines)
        self.assertIn('ratchet brain-source-node  brain -> node  count 2 / ceiling 2 / goal 0  ok',
                      out.lines)
        out = self.check(mm, {'brain-source-node': 1})
        self.assertEqual(out.code, C.FAIL)
        self.assertIn('ratchet brain-source-node  brain -> node  count 2 / ceiling 1 / goal 0  '
                      'OVER CEILING', out.lines)
        self.assertIn('  brain/Tests/Tests.csproj:4  (csproj: ../../node/Tests/Probe.cs)', out.lines)
        out = self.check(mm)
        self.assertEqual(out.code, C.FAIL, 'a ratchet with no ceiling has ceiling 0')

    def test_a_ratchet_under_its_ceiling_asks_for_it_lowered(self):
        mm = mapped({'edges': [BUILD_EDGE, dict(BUILD_EDGE, to='worker', kind='data')],
                     'ratchets': [SOURCE_RATCHET]})
        out = self.check(mm, {'brain-source-node': 5, 'gone': 1})
        self.assertEqual(out.code, C.OK)
        self.assertIn('ratchet brain-source-node  brain -> node  count 2 / ceiling 5 / goal 0  '
                      'lower the ceiling to 2', out.lines)
        self.assertIn('note: the ceilings file names gone, which no ratchet in the map has',
                      out.lines)

    def test_a_ratchet_filter_by_kind_or_detector_leaves_the_rest_refused(self):
        mm = mapped({'ratchets': [{'id': 'w', 'from': 'brain', 'to': 'worker', 'detector': 'paths',
                                   'goal': 0},
                                  dict(SOURCE_RATCHET, detector='paths')]})
        s = C.sort_crossings(mm, C.scan(mm, self.t.files(), self.t.read()))
        self.assertEqual([c.dst for c in s.ratcheted['w']], ['worker'])
        self.assertEqual(sorted(c.kind for c in s.refused), ['build', 'source', 'source'])


class BaselineTest(Fixture):
    def test_the_baseline_proposes_one_ratchet_per_kind_and_its_ceiling(self):
        out = self.baseline(mapped({'ratchets': [SOURCE_RATCHET]}), {'brain-source-node': 9})
        self.assertEqual(out.code, C.OK)
        self.assertEqual(out.lines[0], 'module_map baseline: 1 existing ratchet(s), 2 new')
        self.assertIn('{"id": "brain-build-node", "from": "brain", "to": "node", "kinds": ["build"], '
                      '"goal": 0}', out.lines)
        self.assertIn('{"id": "brain-data-worker", "from": "brain", "to": "worker", "kinds": '
                      '["data"], "goal": 0}', out.lines)
        ceil = json.loads(out.lines[out.lines.index('ceilings:') + 1])
        self.assertEqual(ceil, {'brain-source-node': 2, 'brain-build-node': 1,
                                'brain-data-worker': 1})

    def test_a_proposed_id_never_repeats_an_existing_one(self):
        mm = mapped({'ratchets': [dict(SOURCE_RATCHET, id='brain-build-node', kinds=['source'])]})
        ceil = json.loads(self.baseline(mm).lines[-1])
        self.assertIn('brain-build-node-x', ceil)

    def test_root_is_named_root_in_an_id(self):
        self.assertEqual(C.ratchet_id('tools', 'tree', '(root)'), 'tools-tree-root')


class Repo(unittest.TestCase):
    """A real repository (tests/gitfixture.py) with .kit.json, for map_paths and the CLI."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        for rel, text in FILES.items():
            self.repo.write(rel, text)
        self.repo.write('docs/module-map.json', json.dumps(dict(MAP, edges=[
            BUILD_EDGE, dict(BUILD_EDGE, to='worker', kind='data')], ratchets=[SOURCE_RATCHET])))
        self.repo.write('docs/module-ceilings.json', json.dumps({'brain-source-node': 2}))

    def config(self, owner_paths=('docs/module-map.json',), **modules):
        sections = {'owner': {'paths': list(owner_paths)}}
        if modules:
            sections['modules'] = modules
        self.repo.write('.kit.json', json.dumps(sections))

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['-C', self.root] + list(argv))
        return code, out.getvalue()


class MapPathsTest(Repo):
    def test_the_map_must_be_an_owner_path(self):
        self.config(owner_paths=['AGENTS.md'], map='docs/module-map.json')
        with self.assertRaisesRegex(MapError, 'not an owner path'):
            C.map_paths(self.root, load_config(self.root))
        m, c, notes = C.map_paths(self.root, load_config(self.root), require_owner=False)
        self.assertTrue(m.endswith('module-map.json'))
        self.assertIsNone(c)

    def test_no_map_configured_refuses(self):
        self.config()
        with self.assertRaisesRegex(MapError, 'no "modules".map'):
            C.map_paths(self.root, load_config(self.root))

    def test_a_map_on_the_command_line_is_a_dry_run(self):
        self.config()
        m, c, notes = C.map_paths(self.root, load_config(self.root), map_path='x.json')
        self.assertEqual(m, 'x.json')
        self.assertIn('dry run', notes[0])

    def test_the_section_names_the_ceilings_file(self):
        self.config(map='docs/module-map.json', ceilings='docs/module-ceilings.json')
        m, c, notes = C.map_paths(self.root, load_config(self.root))
        self.assertTrue(c.endswith('module-ceilings.json'))
        self.assertEqual(notes, [])


class CliTest(Repo):
    def test_check_passes_on_a_clean_map_and_exits_0(self):
        self.config(map='docs/module-map.json', ceilings='docs/module-ceilings.json')
        code, out = self.cli('--check')
        self.assertEqual(code, 0, out)
        self.assertIn('module_map: OK', out)

    def test_a_new_crossing_fails_the_check_with_exit_1(self):
        self.config(map='docs/module-map.json', ceilings='docs/module-ceilings.json')
        self.repo.write('brain/Tests/Climb.cs', 'var s = "../../worker/src/W.java"; var g = ".git";\n')
        code, out = self.cli('--check')
        self.assertEqual(code, 1, out)
        self.assertIn('REFUSED  brain -> (root)  tree  brain/Tests/Climb.cs:1', out)

    def test_an_unlocked_map_or_broken_config_exits_2(self):
        self.config(owner_paths=['AGENTS.md'], map='docs/module-map.json')
        code, out = self.cli('--check')
        self.assertEqual(code, 2)
        self.assertIn('module_map: NOT CHECKED: the module map docs/module-map.json is not an '
                      'owner path', out)
        self.repo.write('.kit.json', json.dumps({'modules': {'mapp': 'x'}}))
        code, out = self.cli('--check')
        self.assertEqual(code, 2)
        self.assertIn('module_map: refused: kit configuration', out)

    def test_baseline_needs_no_owner_path_and_exits_0(self):
        self.config(owner_paths=['AGENTS.md'], map='docs/module-map.json')
        self.repo.write('brain/Tests/More.cs', 'var p = "worker/src/W.java";\n')
        code, out = self.cli('--baseline')
        self.assertEqual(code, 0, out)
        self.assertIn('"brain-source-node": 2', out)

    def test_map_given_on_the_command_line(self):
        self.config()
        code, out = self.cli('--check', '--map', self.repo.path('docs', 'module-map.json'),
                             '--ceilings', self.repo.path('docs', 'module-ceilings.json'))
        self.assertEqual(code, 0, out)
        self.assertIn('dry run', out)

    def test_usage_errors_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(['-C', self.root])
        self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
