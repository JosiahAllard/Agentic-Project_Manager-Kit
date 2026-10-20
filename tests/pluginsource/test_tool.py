"""tools/plugin_refresh.py end to end: a temp home and LOCALAPPDATA, a fake claude.cmd first on
PATH that logs each call; the dry run writes nothing, --apply logs the per-folder calls."""
import json
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
        self.pf = os.path.join(self.k.base, 'pf')

    def run_tool(self, *args):
        env = gitfixture.fixture_env({'USERPROFILE': self.k.home, 'HOME': self.k.home,
                                      'LOCALAPPDATA': self.lad})
        env.pop('CLAUDE_CONFIG_DIR', None)
        env['ProgramW6432'] = env['ProgramFiles'] = self.pf     # never the machine's managed settings
        env['PATH'] = self.bin + os.pathsep + env.get('PATH', '')
        return subprocess.run([sys.executable, TOOL, '--kit', self.k.kit] + list(args), env=env,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=120)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8', errors='replace') as fh:
            return [l.strip() for l in fh if l.strip()]

    def test_dry_run_writes_nothing_migrate_one_folder_then_apply(self):
        os.makedirs(self.root)
        before = tree(self.lad)
        r = self.run_tool('--migrate')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('DRY RUN', r.stdout)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)      # no folder migrated yet
        self.assertIn('DRY RUN', r.stdout)
        self.assertEqual(tree(self.lad), before)
        self.assertEqual(self.calls(), [])
        r = self.run_tool('--migrate', '--apply', '--folder', self.two)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [
            '[%s] plugin marketplace remove pm-kit' % self.two,
            '[%s] plugin marketplace add %s' % (self.two, self.root),
            '[%s] plugin install pm-kit@pm-kit -s user' % self.two])
        os.remove(self.log)
        r = self.run_tool('--apply')                 # the default folder is not migrated yet
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        # the hint names the kit's own tool in full (docs/multi-host-plan.md section 7 decision 21)
        self.assertIn('REFUSED %s: not migrated to the export yet: python "%s" --migrate --apply '
                      '--folder "%s"' % (self.default, TOOL.replace('\\', '/'), self.default),
                      r.stdout)
        self.assertEqual(self.calls(), [])           # two is current already
        self.k.repo.write('later.txt', 'z\n')
        self.k.repo.commit('later', 'later.txt')
        self.k.repo.git('push', '-q', 'origin', 'main')
        r = self.run_tool('--apply', '--folder', self.two)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [
            '[%s] plugin marketplace update pm-kit' % self.two,
            '[%s] plugin update pm-kit@pm-kit' % self.two])
        sha = self.k.repo.git('rev-parse', 'refs/remotes/origin/main')
        self.assertTrue(os.path.isdir(os.path.join(self.root, sha[:12])))

    def test_a_folder_that_is_not_an_account_folder_is_refused(self):
        r = self.run_tool('--apply', '--folder', self.two, '--folder', os.path.join(self.k.base, 'nope'))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('REFUSED: not an account folder on this machine: %s'
                      % os.path.join(self.k.base, 'nope'), r.stdout)
        self.assertFalse(os.path.exists(self.root))
        self.assertEqual(self.calls(), [])

    def test_a_marketplace_path_off_the_export_is_a_warning_and_nothing_is_rewritten(self):
        elsewhere = os.path.join(self.k.base, 'elsewhere')
        known = os.path.join(self.two, 'plugins', 'known_marketplaces.json')
        managed = os.path.join(self.pf, 'ClaudeCode', 'managed-settings.json')
        for path, data in ((known, {'pm-kit': {'installLocation': elsewhere}}),
                           (managed, {'extraKnownMarketplaces': {'pm-kit': {'source': {'path': elsewhere}}}})):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(data, fh)
        before = tree(self.k.base)
        r = self.run_tool('--folder', self.two)
        self.assertIn('WARNING: pm-kit marketplace in %s is %s, not the export %s'
                      % (self.two, elsewhere, self.root), r.stdout)
        self.assertIn('WARNING: pm-kit marketplace in the managed settings is %s' % elsewhere, r.stdout)
        self.assertIn('DRY RUN', r.stdout)
        self.assertEqual(tree(self.k.base), before)

    def test_bad_usage_exits_2(self):
        self.assertEqual(self.run_tool('--nope').returncode, 2)

    def test_prune_caches_with_migrate_exits_2(self):
        r = self.run_tool('--prune-caches', '--migrate')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('--prune-caches and --migrate', r.stderr)


if __name__ == '__main__':
    unittest.main()
