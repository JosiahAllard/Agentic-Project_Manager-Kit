"""The auto-detected gate end to end (kitlib.gate.detect through kitlib.gate.runner and land): a
scratch repository with no "gate" section gates and lands on its detected steps, labelled
`source: auto` in the report, the gate-OK record and the result record; a configured gate is
labelled `gate.json` and gets no detected step."""
import unittest

from kitlib import gate as G
from kitlib.gate import land, result, runner
from kitlib.records import gate_ok
from tests.gate import fixtures

PASSING = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n'
FAILING = ('import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n'
           '        self.fail("red")\n')


class AutoCase(fixtures.GateRepoCase):
    GATE = None         # no "gate" section: the gate detects the stack

    def gate(self, wt, **kw):
        kw.setdefault('log_dir', self.logs)
        return runner.branch_gate(wt, book_kw=self.book_kw, **kw)

    def recorded(self, wt, tip):
        rec, why = gate_ok.read(wt, tip)
        self.assertIsNone(why)
        return rec


class AutoBranchGate(AutoCase):
    def test_detected_python_tests_run_and_the_records_say_auto(self):
        wt, tip = self.branch('feature', {'tests/test_a.py': PASSING})
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.verdict.startswith('GATE OK'), out.verdict)
        self.assertEqual(out.source, 'auto')
        self.assertTrue(out.lines[-1].startswith('source: auto (auto-detected gate: python'),
                        out.lines)
        self.assertEqual([r['name'] for r in out.results], ['python-tests'])
        rec = self.recorded(wt, tip)
        self.assertEqual((rec['source'], [s['name'] for s in rec['steps']]),
                         ('auto', ['python-tests']))
        self.assertIsNone(runner.verify_record(wt, tip)[1])
        final, _ = result.read(wt, 'kit/feature')
        self.assertEqual((final['verdict'], final['source']), ('pass', 'auto'))

    def test_a_red_detected_test_fails_the_gate(self):
        wt, tip = self.branch('feature', {'tests/test_a.py': FAILING})
        out = self.gate(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.verdict.startswith('GATE FAILED: python-tests'), out.verdict)
        self.assertEqual(gate_ok.read(wt, tip), (None, 'none'))
        self.assertEqual(result.read(wt, 'kit/feature')[0]['source'], 'auto')

    def test_nothing_detected_passes_and_says_no_checks_ran(self):
        wt, tip = self.branch('feature', {'notes.md': '# notes\n'})
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, runner.NO_CHECKS)
        self.assertEqual(out.lines[0], runner.NO_CHECKS)
        self.assertIn('NO checks', out.lines[-1])
        self.assertEqual(out.results, [])
        rec = self.recorded(wt, tip)
        self.assertEqual((rec['source'], rec['steps']), ('auto', []))

    def test_plan_lists_the_detected_steps(self):
        wt, _ = self.branch('feature', {'tests/test_a.py': PASSING})
        out = self.gate(wt, plan=True)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('RUN   python-tests (always)', out.lines)
        self.assertTrue(out.lines[-1].startswith('source: auto'), out.lines)


class AutoLanding(AutoCase):
    def test_a_landing_gates_the_merged_tree_by_detection(self):
        _, tip = self.branch('feature', {'tests/test_a.py': PASSING})
        out = land.Lander(self.repo.root, 'kit/feature', self.main, tip, git_env=self.env,
                          book_kw=self.book_kw, log_dir=self.logs).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('source: auto (auto-detected gate: python; a "gate" section in .kit.json '
                      'or tools/gate.json replaces it)', out.lines)
        rec = self.recorded(self.repo.root, out.merge)
        self.assertEqual((rec['source'], [s['name'] for s in rec['steps']]),
                         ('auto', ['python-tests']))
        final, _ = result.read(self.repo.root, 'kit/feature', 'land')
        self.assertEqual((final['verdict'], final['source']), ('pass', 'auto'))


class ConfiguredGate(fixtures.GateRepoCase):
    def test_a_configured_gate_gets_no_detected_step_and_says_gate_json(self):
        wt, tip = self.branch('feature', {'tests/test_a.py': FAILING, 'go.mod': 'module x\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)        # the red test is not one of its steps
        self.assertEqual([r['name'] for r in out.results], ['unit'])
        self.assertEqual((out.source, out.lines[-1]), ('gate.json', 'source: gate.json'))
        rec, _ = gate_ok.read(wt, tip)
        self.assertEqual(rec['source'], 'gate.json')
        self.assertEqual(result.read(wt, 'kit/feature')[0]['source'], 'gate.json')


if __name__ == '__main__':
    unittest.main()
