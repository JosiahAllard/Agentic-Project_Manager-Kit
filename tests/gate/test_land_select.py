"""kitlib.gate.land: which steps a landing's merged gate runs (`.kit.json` landing.select,
docs/gate.md "Landing"). By default every step that is not manual, as the author's
`gate.py --full` selects them, whatever paths the merge changes: a step gated on globs the branch
did not touch (an asset import a fresh landing tree needs before the engine tests) still runs.
landing.select "diff" keeps the selection by the merge's changed paths."""
import unittest

from kitlib import gate as G
from kitlib.records import gate_ok
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, LandCase

FAILS = ['{python}', '-c', 'import sys; print("ok"); sys.exit(1)']
GATE = {'steps': [
    {'name': 'unit', 'always': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$'},
    # globbed on paths the branch never changes; its full_cmd fails, and a landing that is not
    # strict runs its cmd, as before
    {'name': 'asset-import', 'globs': ['assets/*'], 'cmd': fixtures.RED_AWARE,
     'full_cmd': FAILS, 'pass': '^ok$'},
    {'name': 'engine', 'manual': True, 'cmd': FAILS},
    {'name': 'tools-tests', 'globs': ['tools/*'], 'cmd': FAILS,
     'skip_if_selected': ['tools-tests-all']},
    {'name': 'tools-tests-all', 'globs': [], 'cmd': fixtures.RED_AWARE, 'pass': '^ok$'},
]}


class SelectCase(LandCase):
    GATE = GATE

    def land_scripts_only(self):
        _, tip = self.branch('feature', {'scripts/a.py': 'x = 1\n'})
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        return [s['name'] for s in rec['steps']], said


class SelectFullByDefault(SelectCase):
    def test_a_globbed_step_the_merge_does_not_touch_runs(self):
        # before the fix the landing's step list was ['unit']: it lacked asset-import (and
        # tools-tests-all), which the author's --full gate ran
        names, said = self.land_scripts_only()
        self.assertEqual(names, ['unit', 'asset-import', 'tools-tests-all'])
        line = [l for l in said if l.startswith('merged-tree gate at ')]
        self.assertTrue(line and line[0].endswith(': unit, asset-import, tools-tests-all'), said)


class SelectDiff(SelectCase):
    KIT = dict(LandCase.KIT, landing=dict(LandCase.KIT['landing'], select='diff'))

    def test_diff_selects_by_the_changed_paths(self):
        names, _ = self.land_scripts_only()
        self.assertEqual(names, ['unit'])


if __name__ == '__main__':
    unittest.main()
