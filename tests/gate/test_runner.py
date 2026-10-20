"""kitlib.gate.runner: the branch gate's zero-count warning (kitlib.gate.steps.zero_count_passes),
a WARNING line for a step `pass` pattern a zero-test run also matches. A warning only: the step's
own result decides the gate, and a check that raises never stops it."""
import json
from unittest import mock

from kitlib import gate as G
from kitlib.gate import runner
from tests.gate import fixtures


def _step(pattern):
    """A step that prints a unittest summary of 3 tests and exits 1 when RED is in its tree."""
    return {'name': 'unit', 'always': True, 'pass': pattern,
            'cmd': ['{python}', '-c', 'import os, sys; print("Ran 3 tests in 0.010s"); print("OK"); '
                                      'sys.exit(1 if os.path.exists("RED") else 0)']}


class _Case(fixtures.GateRepoCase):
    def gate(self, wt):
        said = []
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs, say=said.append)
        return out, said


class ZeroCountPass(_Case):
    GATE = {'steps': [_step(r'(?m)^Ran \d+ tests? in')]}

    def warnings(self, said):
        return [line for line in said if line.startswith('WARNING: step unit "pass"')]

    def test_passing_step_warns_and_the_gate_passes(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        out, said = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.verdict.startswith('GATE OK'), out.verdict)
        warned = self.warnings(said)
        self.assertEqual(len(warned), 1, said)
        self.assertIn('accepts a zero-test run', warned[0])
        self.assertIn('Ran 0 tests', warned[0])

    def test_failing_step_warns_and_the_gate_fails_by_the_step(self):
        wt, _ = self.branch('feature', {'RED': 'x\n'})
        out, said = self.gate(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.verdict.startswith('GATE FAILED: unit'), out.verdict)
        self.assertEqual(len(self.warnings(said)), 1, said)


class FlooredPass(_Case):
    GATE = {'steps': [_step(r'(?m)^Ran [1-9]\d* tests? in')]}

    def test_a_floored_pattern_prints_no_warning(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        out, said = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual([line for line in said if line.startswith('WARNING')], [], said)

    def test_a_check_that_raises_warns_once_and_the_gate_still_runs(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        boom = TypeError('first argument must be string or compiled pattern')
        with mock.patch.object(runner.S, 'zero_count_passes', side_effect=boom) as check:
            out, said = self.gate(wt)
        self.assertTrue(check.called)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.verdict.startswith('GATE OK'), out.verdict)
        warned = [line for line in said if line.startswith('WARNING')]
        self.assertEqual(len(warned), 1, said)
        self.assertIn('could not run', warned[0])
        self.assertIn('TypeError', warned[0])

    def test_a_non_ascii_warning_is_said_as_ascii(self):
        """A warning reaches say() escaped to ASCII: a console that cannot encode the pattern
        (a cp1252 stdout) never raises out of the gate."""
        lines = ['step unit "pass" \'✓ \\\\d+\' accepts a zero-test run']
        with mock.patch.object(runner.S, 'zero_count_passes', return_value=lines):
            (line,) = runner.zero_count_warnings({})
        self.assertEqual(line, line.encode('ascii').decode('ascii'))
        self.assertIn('\\u2713', line)
        with mock.patch.object(runner.S, 'zero_count_passes', side_effect=ValueError('✓')):
            (line,) = runner.zero_count_warnings({})
        self.assertIn('\\u2713', line)

    def test_a_non_string_pass_is_refused_by_the_config_before_the_check(self):
        """A `pass` that is no string never reaches zero_count_passes: kitlib.config refuses it
        (schema Regexes), so the gate is REFUSED, never a crash."""
        wt = self.repo.worktree('feature')
        self.repo.write('.kit.json', json.dumps({'gate': {'steps': [_step(5)]}}) + '\n', root=wt)
        self.repo.commit('bad pass', '.kit.json', cwd=wt)
        with mock.patch.object(runner.S, 'zero_count_passes') as check:
            out, _ = self.gate(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('pass', out.lines[0])
        self.assertFalse(check.called)
