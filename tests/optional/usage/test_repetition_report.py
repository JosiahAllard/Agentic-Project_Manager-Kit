"""Tests for repetition_report.py on made-up transcripts.

    python -m unittest test_repetition_report -v
"""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'usage')
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)
import _fixture as F  # noqa: E402
import repetition_report as RR  # noqa: E402


class Shapes(unittest.TestCase):
    def test_paths_hashes_and_numbers_are_normalised(self):
        self.assertEqual(RR.shape('cd D:/work/Repo && git -C D:/x log --oneline -5'), 'git log --oneline')
        self.assertEqual(RR.shape('python tools/gate.py --tier 3'), 'python tools/gate.py --tier')
        self.assertEqual(RR.shape('git show 1a2b3c4d'), 'git show <sha>')

    def test_hand_rolled_gate_calls_count_against_gate_py(self):
        rx = RR.REPLACED['gate.py']
        for cmd in ('godot --headless --script t.gd', 'dotnet test x.sln', 'gradlew test', 'pytest -q',
                    'cargo test', 'test.bat e2e'):
            self.assertTrue(re.search(rx, RR.shape(cmd)), cmd)
        self.assertFalse(re.search(rx, RR.shape('git status')))


class Report(unittest.TestCase):
    def test_reads_every_config_dir_given_and_names_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, 'Repo')
            key = re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(repo)).lower()
            dirs = []
            for i, cmds in enumerate((['godot --headless -s a.gd', 'python tools/gate.py'],
                                      ['godot --headless -s b.gd'])):
                d = os.path.join(tmp, 'acct%d' % i)
                dirs.append(d)
                content = [F.shell('t%d%d' % (i, j), c) for j, c in enumerate(cmds)]
                F.write(os.path.join(d, 'projects', key, 's.jsonl'),
                        [F.turn('m%d' % i, '2026-10-04T00:00:00Z', content=content)])
            out = io.StringIO()
            argv = ['repetition_report.py', repo, '--config-dir', dirs[0], '--config-dir', dirs[1]]
            with contextlib.redirect_stdout(out), mock.patch.object(sys, 'argv', argv):
                RR.main()
            text = out.getvalue()
            self.assertIn('2 transcripts | read: %s, %s' % (dirs[0], dirs[1]), text)
            self.assertRegex(text, r'\n\s+1 \|\s+2 \| tools/gate\.py')     # 1 call, 2 hand-rolled


if __name__ == '__main__':
    unittest.main()
