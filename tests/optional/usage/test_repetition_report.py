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
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'usage')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)            # a run from this folder: tests.home and kitlib
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)
from kitlib.config import user as kuser  # noqa: E402
from tests import home as thome  # noqa: E402
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

    def no_config_dir_env(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('CLAUDE_CONFIG_DIR', None)

    def test_no_config_dir_reads_the_homes_claude_folder(self):
        home = thome.temp_home(self)
        self.no_config_dir_env()
        repo = os.path.join(home, 'Repo')
        key = re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(repo)).lower()
        F.write(os.path.join(home, '.claude', 'projects', key, 's.jsonl'),
                [F.turn('m', '2026-10-04T00:00:00Z', content=[F.shell('t', 'python tools/gate.py')])])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, 'argv', ['repetition_report.py', repo]):
            RR.main()
        self.assertIn('1 transcripts | read: %s' % os.path.join(home, '.claude'), out.getvalue())

    def test_no_config_dir_is_fenced(self):
        home = thome.fence_decoy(self)
        self.no_config_dir_env()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                mock.patch.object(sys, 'argv', ['repetition_report.py', os.path.join(home, 'Repo')]):
            with self.assertRaises(kuser.HomeFenced):
                RR.main()


if __name__ == '__main__':
    unittest.main()
