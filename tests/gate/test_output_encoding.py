"""The gate's console output on a console that cannot encode every character (cp1252), and the
result record of a gate an exception stopped (docs/gate.md "Result record"). Four `gate.py --full` runs of another repository ended `error`, "UnicodeEncodeError:
'charmap' codec can't encode character '\\u2733'", their records' log null: a line the gate printed
(a step's reason quoting its log, or a console window's title) had a character cp1252 has no byte
for, and the exception reached the record before the run's log folder did."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import result, runner, steps
from tests import gitfixture, inproc
from tests.gate import fixtures
from tests.home import result_drive

GATE_PY = os.path.join(steps.KIT_TOOLS, 'gate.py')
STAR = '✳'     # the character those runs could not print: cp1252 has no byte for it
REASON = 'WARN  unit (1.0 s): warn line: %s Claude Code is done' % STAR
WINDOW = 'console window: "%s Claude Code" (pid 1234) still open' % STAR


def cp1252_stream():
    """A stream as a redirected Windows stdout is: cp1252, strict."""
    return io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict', newline='\n')


def written(stream):
    stream.flush()
    return stream.buffer.getvalue().decode('cp1252')


class Cp1252Console(unittest.TestCase):
    """tools/gate.py with stdout and stderr in cp1252: a character cp1252 has no byte for is
    written as a \\u escape, never raised."""

    def test_a_step_reason_and_a_window_title_are_printed_escaped_not_raised(self):
        gate_py = inproc.load_tool('gate')
        seen = []

        def fake_gate(root, say=None, **kw):
            seen.append(root)
            say(REASON)                 # runner.run_steps: a step's reasons quote its log
            say(WINDOW)                 # runner.window_results: console window titles
            return G.Outcome(G.OK, ['GATE OK (fake) %s' % STAR])

        out, err = cp1252_stream(), cp1252_stream()
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ), \
                mock.patch.object(sys, 'stdout', out), mock.patch.object(sys, 'stderr', err), \
                mock.patch.object(gate_py, 'Store'), \
                mock.patch.object(gate_py.runner, 'branch_gate', fake_gate):
            rc = gate_py.main(['-C', root])
        text = written(out)
        self.assertEqual(rc, G.OK, (text, written(err)))
        self.assertEqual(len(seen), 1)
        self.assertIn(REASON.replace(STAR, '\\u2733'), text)
        self.assertIn(WINDOW.replace(STAR, '\\u2733'), text)
        self.assertIn('GATE OK (fake) \\u2733', text)

    def test_the_entry_block_escapes_too(self):
        """The real process boundary: a redirected stream in cp1252 (PYTHONIOENCODING), through the
        `__main__` block, prints a branch name cp1252 cannot encode."""
        repo = gitfixture.TempRepo()
        self.addCleanup(repo.cleanup)
        repo.write('a.txt', 'a\n')
        tip = repo.commit('a', 'a.txt')
        result_drive(self)      # the machine config's dev_drive: the result folder's drive
        env = dict(fixtures.test_env(), PYTHONIOENCODING='cp1252')
        env.pop('PYTHONUTF8', None)
        r = subprocess.run([sys.executable, GATE_PY, 'wait-result', 'kit/' + STAR, '--tip', tip,
                            '--timeout', '0', '-C', repo.root],
                           capture_output=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
        out, err = r.stdout.decode('cp1252'), r.stderr.decode('cp1252', 'replace')
        self.assertNotIn('UnicodeEncodeError', err)
        self.assertEqual(r.returncode, result.TIMEOUT, (out, err))
        self.assertIn('TIMEOUT: no final gate result record for kit/\\u2733 at', out)


class ErrorRecordNamesItsLogs(fixtures.GateRepoCase):
    """runner.branch_gate stopped by an exception: its `error` record's log is the folder the run
    was writing, once that folder was made."""

    def test_a_gate_stopped_by_an_exception_records_the_log_folder_it_wrote(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        boom = UnicodeEncodeError('charmap', STAR, 0, 1, 'character maps to <undefined>')
        with mock.patch.object(runner, 'window_results', side_effect=boom):
            with self.assertRaises(UnicodeEncodeError):
                runner.branch_gate(wt, log_dir=self.logs, book_kw=self.book_kw)
        rec = result.read(wt, 'kit/feature')[0]
        self.assertEqual((rec['verdict'], rec['tip'], rec['code']), ('error', tip, None))
        self.assertIn('UnicodeEncodeError', rec['summary'])
        self.assertIsNotNone(rec['log'], rec)
        self.assertTrue(os.path.isfile(os.path.join(rec['log'], 'unit.log')), rec['log'])
        under = os.path.normcase(os.path.abspath(self.logs)) + os.sep
        self.assertTrue(os.path.normcase(os.path.abspath(rec['log'])).startswith(under),
                        rec['log'])

    def test_one_stopped_before_its_log_folder_was_made_records_none(self):
        wt, tip = self.branch('early', {'a.py': 'x = 1\n'})
        with mock.patch.object(runner, 'new_log_dir', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                runner.branch_gate(wt, log_dir=self.logs, book_kw=self.book_kw)
        rec = result.read(wt, 'kit/early')[0]
        self.assertEqual((rec['verdict'], rec['tip'], rec['log']), ('error', tip, None))
        self.assertIn('RuntimeError: boom', rec['summary'])


if __name__ == '__main__':
    unittest.main()
