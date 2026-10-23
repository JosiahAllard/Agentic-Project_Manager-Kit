"""tools/land.py on a console that cannot encode every character (cp1252), and the log a
landing's `error` result record names (docs/gate.md "Result record"). The follow-up of the gate's
own fix (tests/gate/test_output_encoding.py): land.py run directly printed through a strict cp1252
stream, and Lander.run wrote its `error` record with no log while its final record names one."""
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land, result
from tests import inproc
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, HANG_S, KIT_ROOT, LandCase
from tests.gate.test_land_submit import SubmitCase

LAND_PY = os.path.join(KIT_ROOT, 'tools', 'land.py')
STAR = '✳'     # a character cp1252 has no byte for
ESCAPED = '\\u2733'


def cp1252_stream():
    """A stream as a redirected Windows stdout is: cp1252, strict."""
    return io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict', newline='\n')


def written(stream):
    stream.flush()
    return stream.buffer.getvalue().decode('cp1252')


class Cp1252Console(unittest.TestCase):
    """tools/land.py with stdout and stderr in cp1252: a character cp1252 has no byte for is
    written as a \\u escape, never raised."""

    def test_a_landing_line_is_printed_escaped_not_raised(self):
        land_py = inproc.load_tool('land')
        seen = []

        def fake_land(root, branch, expect_main, tip, say=None, **kw):
            seen.append(branch)
            say('merged-tree gate: a step\'s reason quoting its log: %s' % STAR)
            return G.Outcome(G.OK, ['LANDED %s (fake) %s' % (branch, STAR)])

        out, err = cp1252_stream(), cp1252_stream()
        with mock.patch.object(sys, 'stdout', out), mock.patch.object(sys, 'stderr', err), \
                mock.patch.object(land_py, 'Store'), \
                mock.patch.object(land_py.landmod, 'land', fake_land):
            rc = land_py.main([BRANCH, '--expect-tip', 'a' * 40, '-C', KIT_ROOT])
        text = written(out)
        self.assertEqual(rc, G.OK, (text, written(err)))
        self.assertEqual(seen, [BRANCH])
        self.assertIn('a step\'s reason quoting its log: %s' % ESCAPED, text)
        self.assertIn('LANDED %s (fake) %s' % (BRANCH, ESCAPED), text)


class Cp1252EntryBlock(LandCase):
    """The real process boundary: land.py run as a script with its streams redirected in cp1252
    (PYTHONIOENCODING) names a landed branch cp1252 cannot encode."""

    def test_the_sweep_names_a_branch_cp1252_cannot_encode(self):
        _, tip = self.branch('done', {'done.py': 'd\n'})
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/kit/%s' % (tip, STAR))
        self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge kit/done', 'kit/done')
        self.repo.git('push', '-q', 'origin', 'main')
        env = dict(fixtures.test_env(), PYTHONIOENCODING='cp1252')
        env.pop('PYTHONUTF8', None)
        r = subprocess.run([sys.executable, LAND_PY, '--sweep-landed', '-C', self.repo.root],
                           capture_output=True, env=env, timeout=HANG_S,
                           stdin=subprocess.DEVNULL)
        out, err = r.stdout.decode('cp1252'), r.stderr.decode('cp1252', 'replace')
        self.assertNotIn('UnicodeEncodeError', err)
        self.assertEqual(r.returncode, G.OK, (out, err))
        self.assertIn('kit/%s:' % ESCAPED, out)


class ErrorRecordNamesItsLog(LandCase):
    """Lander.run stopped by an exception or Ctrl+C: its `error` record names the log its final
    record would have named - the merged gate's log folder once the merged gate returned."""

    def test_ctrl_c_after_the_merged_gate_records_its_log_folder(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        lander = self.lander(BRANCH, self.main, tip)
        with mock.patch.object(land.Lander, 'unchanged', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                lander.run()
        rec, why = result.read(self.repo.root, BRANCH, 'land')
        self.assertIsNotNone(rec, why)
        self.assertEqual((rec['verdict'], rec['tip'], rec['code']), ('error', tip, None))
        self.assertIn('KeyboardInterrupt', rec['summary'])
        self.assertIsNotNone(rec['log'], rec)
        self.assertTrue(os.path.isdir(rec['log']), rec['log'])
        under = os.path.normcase(os.path.abspath(self.logs)) + os.sep
        self.assertTrue(os.path.normcase(os.path.abspath(rec['log'])).startswith(under),
                        rec['log'])
        self.assertEqual(self.remote_main(), self.main)      # nothing was pushed


class SubmittedErrorRecordNamesItsOutput(SubmitCase):
    """A submitted landing stopped by an exception before its merged gate: its `error` record
    names the submission's output, as its `running` record did."""

    def test_an_exception_before_the_gate_records_the_submissions_output(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, detach = self.submit([BRANCH], [tip], expect_main=self.main)
        self.assertEqual(out.code, G.OK, out.lines)
        output = out.facts['output']
        self.assertEqual(self.record(BRANCH)['log'], output)
        sub = self.restored(out, detach, [BRANCH], [tip], expect_main=self.main)
        lander = self.lander(BRANCH, self.main, tip, submitted=sub)

        def boom():
            raise RuntimeError('boom')
        lander.preconditions = boom
        with self.assertRaises(RuntimeError):
            lander.run()
        rec = self.record(BRANCH)
        self.assertEqual((rec['verdict'], rec['code']), ('error', None))
        self.assertIn('RuntimeError: boom', rec['summary'])
        self.assertEqual(rec['log'], output)


if __name__ == '__main__':
    unittest.main()
