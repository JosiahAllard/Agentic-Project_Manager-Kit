"""machine_setup --fresh (docs/machine-setup.md "Fresh machine (--fresh)"): a machine with no older
kit clone. Against a fake home, a fake PM repo and a fake kit in a temp folder; the Windows side and the
`claude plugin` CLI are a FreshHost, so no claude process starts. One test stats (never reads) the
real account settings.json files around a run, to show none of this touches them.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.machinesetup import extras, settings
from kitlib.config import user as kuser
from tests.machinesetup import test_machinesetup as T

PID = 'pm-kit@pm-kit'


def load(path):
    with open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8-sig'))


def guard_in(data, kit):
    want = T.fwd(os.path.join(kit, 'claude', 'hooks', 'guard.py'))
    hooks = (data.get('hooks') or {}).get('PreToolUse') or []
    return any(want in h.get('args', []) for g in hooks for h in g.get('hooks', []))


class FreshHost(T.FakeHost):
    """FakeHost plus the plugin CLI: plugin_install writes what the real CLI was seen to write
    (enabledPlugins and extraKnownMarketplaces) and records whether guard.py's entry was already in
    that settings.json, which shows the plugin step runs after the settings step."""

    def __init__(self, kit):
        super(FreshHost, self).__init__(kit)
        self.cli = True
        self.installs = []
        self.enables = True

    def claude(self):
        return 'claude.exe' if self.cli else None

    def plugin_install(self, config_dir, kit, plugin_id):
        path = os.path.join(config_dir, 'settings.json')
        with open(path, 'rb') as f:
            layout, data = settings.read(f.read())
        self.installs.append((config_dir, plugin_id, guard_in(data, kit)))
        if not self.enables:
            return
        data.setdefault('enabledPlugins', {})[plugin_id] = True
        data.setdefault('extraKnownMarketplaces', {})['pm-kit'] = {'source': {'source': 'directory', 'path': kit}}
        with open(path, 'wb') as f:
            f.write(settings.write(layout, data))


@unittest.skipUnless(sys.platform == 'win32', 'the wrapper and the kit are Windows only')
class FreshBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-fresh-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, 'home')
        self.kit = os.path.join(self.root, 'kit')
        self.desk = os.path.join(self.root, 'desktop')
        self.temp = os.path.join(self.root, 'agent-temp')
        self.pmrepo = os.path.join(self.root, 'pmrepo')
        for d in (self.desk, self.temp, os.path.join(self.pmrepo, '.claude')):
            os.makedirs(d)
        for rel in ('claude/hooks/guard.py', 'claude/hooks/pm_session_start.py',
                    'claude/hooks/coordinator_session_start.py', 'reserve_ui.pyw'):
            T.touch(os.path.join(self.kit, *rel.split('/')))
        T.touch(os.path.join(self.kit, '.claude-plugin', 'marketplace.json'),
                json.dumps({'name': 'pm-kit', 'plugins': [{'name': 'pm-kit', 'source': './'}]}))
        self.c1 = os.path.join(self.home, '.claude')                  # a new account: no settings.json
        self.c2 = os.path.join(self.home, '.claude-two')              # an account with its own hooks
        os.makedirs(self.c1)
        os.makedirs(self.c2)
        os.makedirs(kuser.config_dir(self.home))
        self.s1 = os.path.join(self.c1, 'settings.json')
        self.s2 = os.path.join(self.c2, 'settings.json')
        self.pm = os.path.join(self.pmrepo, '.claude', 'settings.local.json')
        elsewhere = T.fwd(os.path.join(self.root, 'elsewhere'))
        self.doc2 = {
            'model': 'opus',
            'hooks': {
                'PreToolUse': [{'matcher': 'Bash', 'hooks': [
                    {'type': 'command', 'command': 'python %s/hooks/no_root_search.py' % elsewhere}]}],
                'SessionStart': [{'hooks': [{'type': 'command', 'command': 'python "%s/hooks/coordinator_session_start.py"' % elsewhere}]}],
            },
            'statusLine': {'type': 'command', 'command': 'powershell -File "%s/statusline-usage.ps1"' % elsewhere},
        }
        self.raw2 = (json.dumps(self.doc2, indent=4) + '\n').replace('\n', '\r\n').encode('utf-8')
        with open(self.s2, 'wb') as f:
            f.write(self.raw2)
        self.host = FreshHost(self.kit)

    def ctx(self, **kw):
        return M.Context(self.home, self.kit, fresh=True, agent_temp=self.temp, desktop=self.desk,
                         host=self.host, today=T.TODAY, **kw)

    def run_it(self, apply=False, **kw):
        code, lines = M.run(self.ctx(**kw), apply=apply)
        return code, '\n'.join(lines)

    def snapshot(self):
        out = {}
        for top in (self.home, self.pmrepo, self.desk):
            for dirpath, _, files in os.walk(top):
                for n in files:
                    out[os.path.join(dirpath, n)] = T.readb(os.path.join(dirpath, n))
        out['host'] = (dict(self.host.env), list(self.host.calls), self.host.acl_fixed, list(self.host.installs))
        return out

    def guard_groups(self):
        return self.host.guard_entry(self.kit)['PreToolUse']


class Fresh(FreshBase):
    def test_fresh_creates_settings_with_guard_entry_where_none_exists(self):
        self.host.cli = False                                        # plugin step: a note, not a write
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('%s: create it; add guard.py\'s PreToolUse entry' % self.s1, out)
        want = json.dumps({'hooks': {'PreToolUse': self.guard_groups()}}, indent=2) + '\n'
        self.assertEqual(T.readb(self.s1), want.encode('utf-8'))     # indent 2, LF, no BOM, final newline
        self.assertEqual([n for n in os.listdir(self.c1) if n.endswith('.bak')], [])
        self.assertIn('no claude CLI is on PATH', out)
        self.assertIn('claude plugin install %s' % PID, out)

    def test_fresh_appends_and_never_removes_or_rewrites(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        after = load(self.s2)
        self.assertEqual(after['model'], 'opus')
        self.assertEqual(after['statusLine'], self.doc2['statusLine'])
        self.assertEqual(after['hooks']['SessionStart'], self.doc2['hooks']['SessionStart'])
        self.assertEqual(after['hooks']['PreToolUse'][:1], self.doc2['hooks']['PreToolUse'])
        self.assertEqual(after['hooks']['PreToolUse'][1:], self.guard_groups())
        raw = T.readb(self.s2)
        self.assertIn(b'\r\n    "model": "opus"', raw)                  # its layout kept: CRLF, indent 4
        self.assertEqual(T.readb(self.s2 + '.machine-setup-%s.bak' % T.TODAY), self.raw2)
        plugin_bak = load(self.s2 + '.machine-setup-%s-plugin.bak' % T.TODAY)
        self.assertTrue(guard_in(plugin_bak, self.kit))              # taken after the settings write
        self.assertNotIn('enabledPlugins', plugin_bak)

    def test_fresh_second_run_has_nothing_to_do(self):
        code, out = self.run_it(apply=True, pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_OK, out)
        before = self.snapshot()
        code, out = self.run_it(apply=True, pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('machine-setup: nothing to do', out)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_dry_run_writes_nothing(self):
        T.touch(self.pm, '{}\n')
        before = self.snapshot()
        code, out = self.run_it(pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('DRY RUN', out)
        self.assertIn(' kit:        none (--fresh)', out)                 # the old-kit header line
        self.assertIn('change(s) to make', out)
        self.assertIn('+        "matcher": "startup|resume|compact"', out)  # the PM hook's diff
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.s1))
        self.assertEqual(self.host.installs, [])

    def test_fresh_pm_hook_goes_only_to_pm_settings_and_is_noted_without_it(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('no --pm-settings', out)
        self.assertIn('step 7', out)
        self.assertFalse(os.path.exists(self.pm))
        code, out = self.run_it(apply=True, pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_OK, out)
        script = T.fwd(os.path.join(self.kit, 'claude', 'hooks', 'pm_session_start.py'))
        self.assertEqual(load(self.pm), {'hooks': {'SessionStart': [{'matcher': 'startup|resume|compact', 'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': [script]}]}]}})
        for s in (self.s1, self.s2):
            self.assertNotIn(b'pm_session_start', T.readb(s))
        before = self.snapshot()
        code, out = self.run_it(apply=True, pm_settings=self.s2)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('is an account settings.json', out)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_refuses_a_pm_session_start_hook_at_another_path(self):
        other = T.fwd(os.path.join(self.root, 'old', 'hooks', 'pm_session_start.py'))
        T.touch(self.pm, json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': 'python "%s"' % other}]}]}}, indent=2) + '\n')
        before = self.snapshot()
        code, out = self.run_it(apply=True, pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('a SessionStart hook already runs %s' % other, out)
        self.assertIn('machine-setup: nothing changed', out)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_plugin_enabled_once_per_account_after_settings_writes(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.host.installs, [(self.c1, PID, True), (self.c2, PID, True)])
        done = [l for l in out.splitlines() if l.startswith('done: ')]
        settings_done = max(i for i, l in enumerate(done) if 'guard.py' in l)
        plugin_done = min(i for i, l in enumerate(done) if 'enable the kit plugin' in l)
        self.assertLess(settings_done, plugin_done)
        for s in (self.s1, self.s2):
            self.assertIs(load(s)['enabledPlugins'][PID], True)
        code, out = self.run_it(apply=True)                          # true: skipped
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(len(self.host.installs), 2)
        doc = load(self.s2)
        doc['enabledPlugins'][PID] = False                           # the owner turned it off
        with open(self.s2, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2)
        before = self.snapshot()
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('%s: enabledPlugins["%s"] is false; left as is' % (self.s2, PID), out)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_plugin_install_that_does_not_enable_stops(self):
        self.host.enables = False
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('machine-setup: STOPPED at: %s: enable the kit plugin' % self.c1, out)
        self.assertIn('is not true', out)
        self.assertIn('claude plugin uninstall %s' % PID, out)

    def test_fresh_runs_shortcut_and_acl_but_not_the_lock_env(self):
        self.host.env[extras.LOCK_ENV] = 'somewhere-else'
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertTrue(os.path.isfile(os.path.join(self.desk, extras.SHORTCUT)))
        self.assertTrue(self.host.acl_fixed)
        self.assertEqual(self.host.env, {extras.LOCK_ENV: 'somewhere-else'})
        self.assertNotIn('env', [c[0] for c in self.host.calls])
        self.assertIn('KIT_MACHINE_LOCK left as is in fresh mode', out)


class FreshTool(FreshBase):
    def ps1(self, args, env=None):
        ps = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'WindowsPowerShell', 'v1.0',
                          'powershell.exe')
        ps1 = os.path.join(T.KIT, 'installer', 'machine-setup.ps1')
        return subprocess.run([ps, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ps1] + args,
                              capture_output=True, text=True, env=env)

    def test_fresh_and_old_kit_together_or_neither_is_usage(self):
        r = subprocess.run([sys.executable, T.TOOL], capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE)
        self.assertIn('--fresh', r.stderr)
        r = subprocess.run([sys.executable, T.TOOL, '--fresh', '--old-kit', self.root], capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE)
        self.assertIn('not allowed with', r.stderr)
        r = subprocess.run([sys.executable, T.TOOL, '--old-kit', self.root, '--pm-settings', self.pm],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE)
        self.assertIn('--pm-settings goes with --fresh only', r.stderr)
        for args in ([], ['-OldKit', self.root, '-Fresh']):
            r = self.ps1(args)
            self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
            self.assertIn('usage - give -OldKit <', r.stdout)
            self.assertIn('> or -Fresh (not both)', r.stdout)
        r = self.ps1(['-OldKit', self.root, '-PmSettings', self.pm])
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        self.assertIn('-PmSettings goes with -Fresh only', r.stdout)
        with self.assertRaises(ValueError):
            M.Context(self.home, self.kit)
        with self.assertRaises(ValueError):
            M.Context(self.home, self.kit, self.root, fresh=True)

    def test_wrapper_passes_fresh_and_pm_settings(self):
        """A refusal that stops before anything is read (a missing config folder) proves -Fresh,
        -PmSettings, -HomeDir and -ConfigDir reach the tool."""
        missing = os.path.join(self.home, '.claude-none')
        r = self.ps1(['-Fresh', '-PmSettings', self.pm, '-HomeDir', self.home, '-ConfigDir', missing])
        self.assertEqual(r.returncode, M.EXIT_REFUSED, r.stdout + r.stderr)
        self.assertIn('REFUSED - no config folder at %s' % missing, r.stdout)
        self.assertIn(' kit:        none (--fresh)', r.stdout)            # the old-kit header line
        self.assertIn('pm settings:    %s' % self.pm, r.stdout)
        self.assertIn('DRY RUN', r.stdout)

    def test_the_real_account_settings_are_untouched(self):
        """The real ~/.claude/settings.json and every real account folder's settings.json keep their
        size and mtime across a --fresh --apply run on temp folders and a --fresh dry run whose home
        is temp. Stat only: their content is never read."""
        real_home = os.path.expanduser('~')

        def stats():
            out = {}
            for d in M.dedupe([os.path.join(real_home, '.claude')] + M.default_config_dirs(real_home)):
                p = os.path.join(d, 'settings.json')
                try:
                    st = os.stat(p)
                    out[p] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    out[p] = None
            return out
        before = stats()
        self.assertIn(os.path.join(real_home, '.claude', 'settings.json'), before)
        sys.path.insert(0, os.path.dirname(T.TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(T.TOOL))
        import machine_setup
        argv = ['--fresh', '--kit', self.kit, '--home', self.home, '--agent-temp', self.temp,
                '--desktop', self.desk, '--pm-settings', self.pm, '--apply']
        with mock.patch.object(extras, 'Host', lambda: self.host), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(machine_setup.main(argv), M.EXIT_OK)
        self.assertEqual(len(self.host.installs), 2)
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home)
        env.pop('CLAUDE_CONFIG_DIR', None)
        desk = os.path.join(self.root, 'desktop-2')                  # the real Host reads its shortcuts
        os.makedirs(desk)
        r = subprocess.run([sys.executable, T.TOOL, '--fresh', '--desktop', desk],
                           capture_output=True, text=True, env=env, encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, M.EXIT_OK, r.stdout + r.stderr)
        self.assertIn('DRY RUN', r.stdout)
        self.assertIn('config folders: %s, %s' % (self.c1, self.c2), r.stdout)
        self.assertEqual(stats(), before)


if __name__ == '__main__':
    unittest.main()
