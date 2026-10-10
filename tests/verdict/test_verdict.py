"""Tests for verdict: green needs all three legs (exit 0, the pass line, no fail line), and the
printed line puts the verdict before the quoted output."""
import unittest

from kitlib import verdict

OK = 'DOC BUDGET OK (x, 3 .md files)'
FAIL = 'DOC BUDGET FAILED: over budget and grew.'


class Judge(unittest.TestCase):
    def test_all_three_legs_pass(self):
        self.assertEqual(verdict.judge(0, OK), (True, ''))

    def test_the_pass_line_with_a_nonzero_exit_is_red(self):
        passed, why = verdict.judge(1, OK)
        self.assertFalse(passed)
        self.assertEqual(why, 'exit 1')

    def test_exit_zero_without_the_pass_line_is_red(self):
        # A stub, or output that never arrived, must not read green.
        for text in ('', None, 'advice prose only'):
            passed, why = verdict.judge(0, text)
            self.assertFalse(passed, text)
            self.assertIn("no 'DOC BUDGET OK' line", why)

    def test_a_fail_line_anywhere_is_red_even_beside_the_pass_line(self):
        passed, why = verdict.judge(0, FAIL + '\n' + OK)
        self.assertFalse(passed)
        self.assertEqual(why, "'DOC BUDGET FAILED' in its output")

    def test_every_failed_leg_is_named(self):
        passed, why = verdict.judge(2, FAIL + '\nadvice prose')
        self.assertFalse(passed)
        self.assertEqual(why, "exit 2, no 'DOC BUDGET OK' line in its output, "
                              "'DOC BUDGET FAILED' in its output")

    def test_other_pass_and_fail_strings(self):
        self.assertEqual(verdict.judge(0, 'GATE PASSED', ok='GATE PASSED', fail='GATE FAILED'),
                         (True, ''))
        self.assertFalse(verdict.judge(0, OK, ok='GATE PASSED', fail='GATE FAILED')[0])


class Line(unittest.TestCase):
    def test_a_green_run_quotes_its_last_line(self):
        self.assertEqual(verdict.line('docs', 0, 'measuring\n\n' + OK), 'docs      ' + OK)

    def test_a_red_run_puts_the_verdict_before_the_quote(self):
        text = FAIL + '\n  CLAUDE.md: over\nadvice prose'
        self.assertEqual(verdict.line('docs', 1, text),
                         "docs      FAILED (exit 1, no 'DOC BUDGET OK' line in its output, "
                         "'DOC BUDGET FAILED' in its output) advice prose")
        self.assertTrue(verdict.line('docs', 1, text, keep=2, from_start=True)
                        .endswith(') ' + FAIL + ' / CLAUDE.md: over'))

    def test_no_output_says_so(self):
        self.assertTrue(verdict.line('docs', 0, '').endswith(') no output'))

    def test_width_pads_the_label(self):
        self.assertTrue(verdict.line('d', 0, OK, width=3).startswith('d   DOC'))


class Compared(unittest.TestCase):
    def test_nothing_compared_is_its_own_exit_whatever_the_differences(self):
        code, text = verdict.compared(0, 0, 'files')
        self.assertEqual(code, verdict.EXIT_NOTHING_COMPARED)
        self.assertIn('NOTHING COMPARED: 0 files made', text)

    def test_same_and_different(self):
        self.assertEqual(verdict.compared(3, 0), (0, 'SAME: 3 of 3 comparisons agree'))
        self.assertEqual(verdict.compared(3, 1), (1, 'DIFFERENT: 1 of 3 comparisons differ'))

    def test_a_bad_tally_raises(self):
        for count, diffs in ((-1, 0), (1, -1), (1, 2), (True, 0), (1.0, 0)):
            with self.assertRaises(ValueError):
                verdict.compared(count, diffs)

    def test_judge_reads_nothing_compared_as_red(self):
        passed, why = verdict.judge(verdict.EXIT_NOTHING_COMPARED, 'DOC BUDGET OK')
        self.assertFalse(passed)
        self.assertEqual(why, 'exit 4 (nothing compared)')


if __name__ == '__main__':
    unittest.main()
