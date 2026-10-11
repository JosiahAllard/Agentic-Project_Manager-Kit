"""tools/plugin_refresh.py end to end: a temp home and LOCALAPPDATA, a fake claude.cmd first on
PATH that logs each call; the dry run writes nothing, --apply logs the per-folder calls."""
import os
import subprocess
import sys
import unittest

from tests import gitfixture
from tests.pluginsource import Kit

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'plugin_refresh.py')


def tree(root):
    """{relative path: bytes} of every file under root ({} when it does not exist)."""
    out = {}
    for d, _s, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            with open(p, 'rb') as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.k = Kit(self)
        self.lad = os.path.join(self.k.base, 'lad')
        self.bin = os.path.join(self.k.base, 'bin')
        self.log = os.path.join(self.k.base, 'claude.log')
        os.makedirs(self.lad)
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, 'claude.cmd'), 'w', encoding='ascii', newline='\r\n') as fh:
            fh.write('@echo off\n>>"%s" echo [%%CLAUDE_CONFIG_DIR%%] %%*\nexit /b 0\n' % self.log)
        self.default, self.two = self.k.folders('.claude', '.claude-two')
        self.root = os.path.join(self.lad, 'pm-kit', 'plugin')

    def run_tool(self, *args):
        env = gitfixture.fixture_env({'USERPROFILE': self.k.home, 'HOME': self.k.home,
                                      'LOCALAPPDATA': self.lad})
        env.pop('CLAUDE_CONFIG_DIR', None)
        env['PATH'] = self.bin + os.pathsep + env.get('PATH', '')
        return subprocess.run([sys.executable, TOOL, '--kit', self.k.kit] + list(args), env=env,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=120)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8', errors='replace') as fh:
            return [l.strip() for l in fh if l.strip()]

    def test_dry_run_writes_nothing_and_apply_runs_claude_per_folder(self):
        os.makedirs(self.root)
        before = tree(self.lad)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('DRY RUN', r.stdout)
        self.assertEqual(tree(self.lad), before)
        self.assertEqual(self.calls(), [])
        r = self.run_tool('--apply')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [
            '[] plugin marketplace update pm-kit', '[] plugin update pm-kit@pm-kit',
            '[%s] plugin marketplace update pm-kit' % self.two,
            '[%s] plugin update pm-kit@pm-kit' % self.two])
        sha = self.k.repo.git('rev-parse', 'refs/remotes/origin/main')
        self.assertTrue(os.path.isdir(os.path.join(self.root, sha[:12])))

    def test_bad_usage_exits_2(self):
        self.assertEqual(self.run_tool('--nope').returncode, 2)


if __name__ == '__main__':
    unittest.main()
