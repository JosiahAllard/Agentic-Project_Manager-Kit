"""kitlib.gate.land: a landing's merged gate runs every step that is not manual, as the author's
`gate.py --full` selects them, whatever paths the merge changes (docs/gate.md "Landing"): a step
gated on globs the branch did not touch (an asset import a fresh landing tree needs before the
engine tests) still runs. Each runs its cmd; only a strict landing runs its full_cmd."""
import unittest

from kitlib import gate as G
from kitlib.records import gate_ok
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, LandCase

FAILS = ['{python}', '-c', 'import sys; print("ok"); sys.exit(1)']
GATE = {'steps': [
    {'name': 'unit', 'always': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$'},
    # globbed on paths the branch never changes; its full_cmd fails, and a landing that is not
    # strict runs its cmd
    {'name': 'asset-import', 'globs': ['assets/*'], 'cmd': fixtures.RED_AWARE,
     'full_cmd': FAILS, 'pass': '^ok$'},
    {'name': 'engine', 'manual': True, 'cmd': FAILS},
    {'name': 'tools-tests', 'globs': ['tools/*'], 'cmd': FAILS,
     'skip_if_selected': ['tools-tests-all']},
    {'name': 'tools-tests-all', 'globs': [], 'cmd': fixtures.RED_AWARE, 'pass': '^ok$'},
]}


class SelectsEveryStep(LandCase):
    GATE = GATE

    def test_a_globbed_step_the_merge_does_not_touch_runs(self):
        # before the fix the landing's step list was ['unit']: it lacked asset-import (and
        # tools-tests-all), which the author's --full gate ran
        _, tip = self.branch('feature', {'scripts/a.py': 'x = 1\n'})
        said = []
        out = self.lander(BRANCH, self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual([s['name'] for s in rec['steps']],
                         ['unit', 'asset-import', 'tools-tests-all'])
        line = [l for l in said if l.startswith('merged-tree gate at ')]
        self.assertTrue(line and line[0].endswith(': unit, asset-import, tools-tests-all'), said)


if __name__ == '__main__':
    unittest.main()
