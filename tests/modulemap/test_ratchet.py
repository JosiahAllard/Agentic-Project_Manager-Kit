"""kitlib.modulemap.ratchet: ceilings held to the base, raises in the owner's map
(docs/module-map.md "Raises")."""
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
from kitlib.modulemap.load import ModuleMap

from tests.gitfixture import TempRepo, fixture_env
from tests.modulemap._fixtures import MAP, csproj as proj

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import module_map as cli  # noqa: E402

FILES = {
    'brain/Tests/Tests.csproj': proj('<Compile Include="../../node/Tests/A.cs" />',
                                     '<Compile Include="../../node/Tests/B.cs" />'),
    'node/Tests/A.cs': 'class A {}',
    'node/Tests/B.cs': 'class B {}',
}
RATCHET = {'id': 'brain-source-node', 'from': 'brain', 'to': 'node', 'kinds': ['source'],
           'goal': 0}
MAP_REL = 'docs/module-map.json'
CEIL_REL = 'docs/module-ceilings.json'


class BaseTest(unittest.TestCase):
    """main holds the map and ceiling 2; the test works on branch `topic` cut from it."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.write_all()
        self.repo.commit('map', '.')
        self.base = self.repo.head()
        self.repo.git('checkout', '-q', '-b', 'topic')

    def write_all(self):
        for rel, text in FILES.items():
            self.repo.write(rel, text)
        self.repo.write('.kit.json', json.dumps({'owner': {'paths': [MAP_REL]},
                                                 'modules': {'map': MAP_REL,
                                                             'ceilings': CEIL_REL}}))
        self.write_map(ratchets=[RATCHET])
        self.write_ceilings({'brain-source-node': 2})

    def write_map(self, **extra):
        self.repo.write(MAP_REL, json.dumps(dict(MAP, **extra)))

    def write_ceilings(self, data):
        self.repo.write(CEIL_REL, json.dumps(data))

    def run_check(self, base_rev='main'):
        return C.run(self.root, load_config(self.root), 'check', base_rev=base_rev)

    def test_a_ceiling_held_or_lowered_passes(self):
        out = self.run_check()
        self.assertEqual(out.code, C.OK, out.lines)
        self.write_ceilings({'brain-source-node': 2, '_note': 'x'})
        self.assertEqual(self.run_check().code, C.OK)

    def test_a_ceiling_raised_above_the_base_is_refused(self):
        self.repo.write('node/Tests/C.cs', 'class C {}')
        self.repo.write('brain/Tests/Tests.csproj', proj(
            '<Compile Include="../../node/Tests/A.cs" />',
            '<Compile Include="../../node/Tests/B.cs" />',
            '<Compile Include="../../node/Tests/C.cs" />'))
        self.write_ceilings({'brain-source-node': 3})
        out = self.run_check()
        self.assertEqual(out.code, C.FAIL, out.lines)
        self.assertIn('RAISED  ratchet brain-source-node  ceiling 3, the base main (%s) has 2: only '
                      'the owner raises a ceiling, with a "raises" entry in the map'
                      % self.base[:10], out.lines)

    def test_a_raise_in_the_owners_map_allows_up_to_its_ceiling(self):
        self.write_ceilings({'brain-source-node': 3})
        self.write_map(ratchets=[RATCHET], raises=[
            {'ratchet': 'brain-source-node', 'ceiling': 3, 'why': 'the owner: one more for now',
             'approval': 'A-100'}])
        self.assertEqual(self.run_check().code, C.OK)
        self.write_ceilings({'brain-source-node': 4})
        out = self.run_check()
        self.assertEqual(out.code, C.FAIL)
        self.assertTrue(any('the raise allows 3' in l for l in out.lines), out.lines)

    def test_a_spent_raise_is_named(self):
        self.write_map(ratchets=[RATCHET], raises=[
            {'ratchet': 'brain-source-node', 'ceiling': 2, 'why': 'w'}])
        out = self.run_check()
        self.assertEqual(out.code, C.OK)
        self.assertIn('note: the raise for brain-source-node (ceiling 2) is spent: the base '
                      'already has 2; the owner may remove it', out.lines)

    def test_a_ratchet_new_in_the_map_takes_any_ceiling(self):
        new = {'id': 'brain-data-node', 'from': 'brain', 'to': 'node', 'kinds': ['data'],
               'goal': 0}
        self.write_map(ratchets=[RATCHET, new])
        self.write_ceilings({'brain-source-node': 2, 'brain-data-node': 7})
        out = self.run_check()
        self.assertEqual(out.code, C.OK, out.lines)
        self.assertIn('note: ratchet brain-data-node is new in the map: its ceiling 7 is not '
                      'compared with the base', out.lines)

    def test_a_ratchet_with_no_ceiling_at_the_base_is_held_at_0(self):
        other = {'id': 'brain-data-node', 'from': 'brain', 'to': 'node', 'kinds': ['data'],
                 'goal': 0}
        self.repo.git('checkout', '-q', 'main')
        self.write_map(ratchets=[RATCHET, other])
        self.repo.commit('second ratchet, no ceiling', MAP_REL)
        self.repo.git('checkout', '-q', 'topic')
        self.repo.git('merge', '-q', 'main')
        self.write_ceilings({'brain-source-node': 2, 'brain-data-node': 1})
        out = self.run_check()
        self.assertEqual(out.code, C.FAIL, out.lines)
        self.assertTrue(any(l.startswith('RAISED  ratchet brain-data-node  ceiling 1') and
                            'has 0' in l for l in out.lines), out.lines)

    def test_the_merge_base_is_read_not_the_base_tip(self):
        self.repo.git('checkout', '-q', 'main')
        self.write_ceilings({'brain-source-node': 9})
        self.repo.commit('main raises (an owner commit)', CEIL_REL)
        self.repo.git('checkout', '-q', 'topic')
        self.write_ceilings({'brain-source-node': 9})
        self.assertEqual(self.run_check().code, C.FAIL, 'topic was cut before the raise')

    def test_no_map_at_the_base_compares_nothing(self):
        self.repo.git('checkout', '-q', '-b', 'fresh', self.repo.initial)
        self.write_all()
        self.write_ceilings({'brain-source-node': 5})
        out = self.run_check(base_rev=self.repo.initial)
        self.assertEqual(out.code, C.OK, out.lines)
        self.assertTrue(any('has no module map docs/module-map.json: ceilings are not compared'
                            in l for l in out.lines), out.lines)

    def test_an_unreadable_base_refuses(self):
        with self.assertRaisesRegex(MapError, 'cannot read the base no-such-rev'):
            self.run_check(base_rev='no-such-rev')

    def test_a_broken_ceilings_file_at_the_base_refuses(self):
        self.repo.git('checkout', '-q', 'main')
        self.repo.write(CEIL_REL, '{"brain-source-node": -1}')
        self.repo.commit('broken', CEIL_REL)
        with self.assertRaises(MapError):
            self.run_check(base_rev='HEAD')

    def test_with_no_base_given_the_main_branch_is_the_base(self):
        self.write_ceilings({'brain-source-node': 3})
        self.assertEqual(self.run_check(base_rev=None).code, C.FAIL)

    def test_the_cli_takes_base_and_exits_2_on_a_bad_one(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['-C', self.root, '--check', '--base', 'no-such-rev'])
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn('module_map: NOT CHECKED: cannot read the base no-such-rev', out.getvalue())
        self.write_ceilings({'brain-source-node': 3})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['-C', self.root, '--check', '--base', 'main']), 1)


class RaiseRulesTest(unittest.TestCase):
    def refuses(self, raises, text):
        with self.assertRaises(MapError) as cm:
            ModuleMap(dict(MAP, ratchets=[RATCHET], raises=raises))
        self.assertIn(text, str(cm.exception))

    def test_a_raise_names_a_ratchet_once_and_gives_a_reason(self):
        self.refuses([{'ratchet': 'nope', 'ceiling': 1, 'why': 'w'}],
                     'raises names nope, which no ratchet has')
        x = {'ratchet': 'brain-source-node', 'ceiling': 1, 'why': 'w'}
        self.refuses([x, dict(x)], 'ratchet brain-source-node is raised twice')
        self.refuses([{'ratchet': 'brain-source-node', 'ceiling': 1}], 'needs "why"')


if __name__ == '__main__':
    unittest.main()
