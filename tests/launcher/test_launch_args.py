"""launch.py handoff --state: an existing file only (arguments and exit codes)."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402


class HandoffState(unittest.TestCase):
    def test_a_missing_file_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, 'no-such-state.md')
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as e:
                launch.parser().parse_args(['handoff', '--session', 'alpha', '--account', '2', '--state', missing])
            self.assertEqual(e.exception.code, 2)
            self.assertIn('no such file: ' + missing, err.getvalue())

    def test_a_relative_path_is_made_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'state-r2.md'), 'w', encoding='utf-8') as f:
                f.write('note\n')
            here = os.getcwd()
            os.chdir(d)
            try:
                expected = os.path.join(os.getcwd(), 'state-r2.md')
                a = launch.parser().parse_args(['handoff', '--session', 'alpha', '--account', '2',
                                                '--state', 'state-r2.md'])
            finally:
                os.chdir(here)
            self.assertTrue(os.path.isabs(a.state))
            self.assertEqual(os.path.normcase(a.state), os.path.normcase(expected))

    def test_an_existing_file_is_taken_and_no_state_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            note = os.path.join(d, 'state-r2.md')
            with open(note, 'w', encoding='utf-8') as f:
                f.write('note\n')
            a = launch.parser().parse_args(['handoff', '--session', 'alpha', '--account', '2', '--state', note])
            self.assertEqual(a.state, note)
        a = launch.parser().parse_args(['handoff', '--session', 'alpha', '--account', '2'])
        self.assertIsNone(a.state)


if __name__ == '__main__':
    unittest.main()
