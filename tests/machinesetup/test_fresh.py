"""machine_setup's default mode (docs/machine-setup.md "Default mode"; --fresh is its old name): a
machine with no older kit clone. Against a fake home, a fake PM repo and a kit that is a temp git repository with a bare
remote (the plugin is installed from the export of its pushed main, kitlib.pluginsource); the
Windows side is a FreshHost whose `claude` runner is kitlib.pluginsource.claude.Runner over a fake
subprocess.run, so no claude process starts. RealRunner runs Host.plugin_runner's real path against
a fake claude.cmd. One test stats (never reads) the real account settings.json files around a run,
to show none of this touches them.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib import pluginsource
from kitlib.machinesetup import extras, settings
from kitlib.pluginsource import claude as pclaude
from tests import gitfixture
from tests.home import machine_config, pm_repo, temp_home
from tests.machinesetup import test_machinesetup as T
from tests.platforms import windows_only

PID = 'pm-kit@pm-kit'
MANIFEST = b'{\n  "name": "pm-kit",\n  "version": "0.1.0"\n}\n'
MARKETPLACE = ('{\n  "name": "pm-kit",\n  "owner": {"name": "x"},\n'
               '  "plugins": [{"name": "pm-kit", "source": "./"}]\n}\n')


def _prepare_kit(repo):
    """What a fresh survey and the export read of a kit: the hooks, reserve_ui.pyw, plugin.json and
    marketplace.json; committed and pushed."""
    rels = ['boot/hook.py', 'reserve_ui.pyw', '.claude-plugin/marketplace.json']
    for rel in rels[:-1]:
        repo.write(rel, 'x\n')
    repo.write('.claude-plugin/marketplace.json', MARKETPLACE)
    path = os.path.join(repo.root, '.claude-plugin', 'plugin.json')
    with open(path, 'wb') as fh:
        fh.write(MANIFEST)
    repo.commit('kit shape', '.claude-plugin/plugin.json', *rels)
    repo.git('push', '-q', 'origin', 'main')


def load(path):
    with open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8-sig'))


def guard_in(data, kit):
    want = T.fwd(os.path.join(kit, 'boot', 'hook.py'))
    hooks = (data.get('hooks') or {}).get('PreToolUse') or []
    return any(want in h.get('args', []) for g in hooks for h in g.get('hooks', []))


class FakeClaude(object):
    """subprocess.run for claude.Runner. Records (folder, CLAUDE_CONFIG_DIR or None, args, guard.py's
    entry already in that folder's settings.json); `plugin install` writes what the real CLI was
    seen to write (enabledPlugins, extraKnownMarketplaces) unless `enables` is False. `fail` maps
    (folder basename, first three args joined) to a return code."""

    def __init__(self, home, kit):
        self.home, self.kit = home, kit
        self.calls, self.fail, self.enables = [], {}, True

    def __call__(self, cmd, env=None, **_kw):
        args = list(cmd[1:])
        cfg = env.get('CLAUDE_CONFIG_DIR')
        folder = cfg or os.path.join(self.home, '.claude')
        path = os.path.join(folder, 'settings.json')
        layout, data = dict(settings.NEW_LAYOUT), {}
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                layout, data = settings.read(f.read())
        self.calls.append((folder, cfg, args, guard_in(data, self.kit)))
        rc = self.fail.get((os.path.basename(folder), ' '.join(args[:3])), 0)
        if rc == 0 and args[:2] == ['plugin', 'install'] and self.enables:
            data.setdefault('enabledPlugins', {})[args[2]] = True
            data.setdefault('extraKnownMarketplaces', {})['pm-kit'] = {'source': {'source': 'directory',
                                                                                   'path': 'x'}}
            with open(path, 'wb') as f:
                f.write(settings.write(layout, data))
        return subprocess.CompletedProcess(cmd, rc, b'fake claude\n')


class FreshHost(T.FakeHost):
    """FakeHost plus the `claude` runner: the real claude.Runner (so env_for decides the
    environment) over FakeClaude. `cli` False: no claude on PATH."""

    def __init__(self, kit, home):
        super(FreshHost, self).__init__(kit)
        self.cli = True
        self.fake = FakeClaude(home, kit)

    def plugin_runner(self, home):
        return pclaude.Runner(home, claude='claude.cmd' if self.cli else '', run_proc=self.fake)


@windows_only('the wrapper and the kit are Windows only')
class FreshBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-fresh-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = gitfixture.TempRepo(bare_remote=True, prepare=_prepare_kit,
                                        prepare_key='machinesetup-fresh-kit-v1')
        self.addCleanup(self.repo.cleanup)
        self.kit = self.repo.root
        self.home = os.path.join(self.root, 'home')
        self.desk = os.path.join(self.root, 'desktop')
        self.temp = os.path.join(self.root, 'agent-temp')
        self.pmrepo = os.path.join(self.root, 'pmrepo')
        self.export = os.path.join(self.root, 'export')
        for d in (self.desk, self.temp, os.path.join(self.pmrepo, '.claude')):
            os.makedirs(d)
        self.c1 = os.path.join(self.home, '.claude')                  # a new account: no settings.json
        self.c2 = os.path.join(self.home, '.claude-two')              # an account with its own hooks
        os.makedirs(self.c1)
        os.makedirs(self.c2)
        pm_repo(self, path=self.pmrepo)              # the fenced pointer's PM repository: the machine config is its .pm-kit
        cache = mock.patch.dict(os.environ, gitfixture.ps_cache_env(self.home))   # real powershell: no cache in the cwd
        cache.start()
        self.addCleanup(cache.stop)
        self.machine_config({'plugin': {'export_dir': self.export}})
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
        self.host = FreshHost(self.kit, self.home)

    def machine_config(self, data):
        machine_config(self.home, data, case=self)

    def ctx(self, **kw):
        return M.Context(self.home, self.kit, fresh=True, agent_temp=self.temp, desktop=self.desk,
                         host=self.host, today=T.TODAY, **kw)

    def run_it(self, apply=False, **kw):
        code, lines = M.run(self.ctx(**kw), apply=apply)
        return code, '\n'.join(lines)

    def snapshot(self):
        out = {}
        for top in (self.home, self.pmrepo, self.desk, self.export):
            for dirpath, _, files in os.walk(top):
                for n in files:
                    out[os.path.join(dirpath, n)] = T.readb(os.path.join(dirpath, n))
        out['host'] = (dict(self.host.env), list(self.host.calls), self.host.acl_fixed,
                       list(self.host.fake.calls))
        return out

    def guard_groups(self):
        return self.host.guard_entry(self.kit)['PreToolUse']

    def state(self):
        return pluginsource.read_state(self.export)

    def migrate_calls(self, folder, cfg):
        return [(folder, cfg, list(st), True) for st in pluginsource.migrate_steps(self.export)]


class Fresh(FreshBase):
    def test_fresh_creates_settings_with_guard_entry_where_none_exists(self):
        self.host.cli = False                                        # plugin step: a note, not a write
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('%s: create it; add the guard\'s PreToolUse entry' % self.s1, out)
        want = json.dumps({'hooks': {'PreToolUse': self.guard_groups()}}, indent=2) + '\n'
        self.assertEqual(T.readb(self.s1), want.encode('utf-8'))     # indent 2, LF, no BOM, final newline
        self.assertEqual([n for n in os.listdir(self.c1) if n.endswith('.bak')], [])
        self.assertIn('%s: the kit plugin is not enabled and no claude CLI is on PATH; once it is, run %s'
                      % (self.c1, pluginsource.migrate_cmd(self.c1)), out)
        self.assertFalse(os.path.exists(self.export))                # no claude: no export either

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
        self.assertIn(' kit:        none (the default mode)', out)        # the old-kit header line
        self.assertIn('change(s) to make', out)
        self.assertIn('+        "matcher": "startup|resume|compact"', out)  # the PM hook's diff
        self.assertIn('kitlib.pluginsource.ensure_export', out)
        tolerated = "-s user, the remove's failure tolerated; "
        self.assertIn('claude plugin install %s %sCLAUDE_CONFIG_DIR unset' % (PID, tolerated), out)
        self.assertIn('claude plugin install %s %sCLAUDE_CONFIG_DIR=%s' % (PID, tolerated, self.c2), out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.s1))
        self.assertFalse(os.path.exists(self.export))
        self.assertEqual(self.host.fake.calls, [])

    def test_fresh_pm_hook_goes_only_to_pm_settings_and_is_noted_without_it(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('no --pm-settings', out)
        self.assertIn('step 7', out)
        self.assertFalse(os.path.exists(self.pm))
        code, out = self.run_it(apply=True, pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_OK, out)
        script = T.fwd(os.path.join(self.kit, 'boot', 'hook.py'))
        self.assertEqual(load(self.pm), {'hooks': {'SessionStart': [{'matcher': 'startup|resume|compact', 'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': [script, 'claude', 'SessionStart', 'pm']}]}]}})
        for s in (self.s1, self.s2):
            self.assertNotIn(b'"pm"', T.readb(s))
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

    def test_fresh_refuses_the_pm_hook_already_in_the_sibling_or_an_account_settings(self):
        """The kit's own PM hook (boot/hook.py claude SessionStart pm) in the PM repo's
        .claude/settings.json (the PM session loads it next to settings.local.json), or an old
        pm_session_start.py in an account settings.json under any event: the checklist would run
        twice, so --pm-settings is refused."""
        entry = T.fwd(os.path.join(self.kit, 'boot', 'hook.py'))
        sibling = os.path.join(self.pmrepo, '.claude', 'settings.json')
        T.touch(sibling, json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': [entry, 'claude', 'SessionStart', 'pm']}]}]}},
            indent=2) + '\n')
        script = entry + ' claude SessionStart pm'
        before = self.snapshot()
        code, out = self.run_it(apply=True, pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('%s already runs %s; with --pm-settings the PM checklist would run twice' % (sibling, script),
                      out)
        self.assertEqual(self.snapshot(), before)
        os.remove(sibling)
        script = T.fwd(os.path.join(self.root, 'old', 'hooks', 'pm_session_start.py'))
        doc = dict(self.doc2, hooks=dict(self.doc2['hooks'], UserPromptSubmit=[{'hooks': [
            {'type': 'command', 'command': 'python "%s"' % script}]}]))
        with open(self.s2, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2)
        before = self.snapshot()
        code, out = self.run_it(apply=True, pm_settings=self.pm)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('%s already runs %s; with --pm-settings' % (self.s2, script), out)
        self.assertEqual(self.snapshot(), before)
        code, out = self.run_it(apply=True)                          # without --pm-settings: no refusal
        self.assertEqual(code, M.EXIT_OK, out)

    def test_fresh_plugin_from_the_export_once_per_account_after_settings_writes(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.host.fake.calls,                       # ~/.claude: CLAUDE_CONFIG_DIR unset
                         self.migrate_calls(self.c1, None) + self.migrate_calls(self.c2, self.c2))
        self.assertEqual(pluginsource.migrate_steps(self.export)[2],
                         ['plugin', 'install', PID, '-s', 'user'])
        state = self.state()
        sha = self.repo.git('rev-parse', 'refs/remotes/origin/main')
        self.assertEqual(state['sha'], sha)
        self.assertTrue(pluginsource.migrated(state, self.c1))
        self.assertTrue(pluginsource.migrated(state, self.c2))
        self.assertEqual(pluginsource.current_folder(self.export), sha[:12])
        done = [l for l in out.splitlines() if l.startswith('done: ')]
        settings_done = max(i for i, l in enumerate(done) if 'the guard' in l)
        export_done = min(i for i, l in enumerate(done) if 'ensure_export' in l)
        plugin_done = min(i for i, l in enumerate(done) if 'enable the kit plugin' in l)
        self.assertLess(settings_done, export_done)
        self.assertLess(export_done, plugin_done)
        for s in (self.s1, self.s2):
            self.assertIs(load(s)['enabledPlugins'][PID], True)
        code, out = self.run_it(apply=True)                          # true and migrated: skipped
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(len(self.host.fake.calls), 6)
        doc = load(self.s2)
        doc['enabledPlugins'][PID] = False                           # the owner turned it off
        with open(self.s2, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2)
        before = self.snapshot()
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('%s: enabledPlugins["%s"] is false; left as is' % (self.s2, PID), out)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_reinstalls_a_plugin_enabled_from_the_kit_checkout(self):
        """enabledPlugins true with no migrated record (installed from the checkout, the retired way:
        plugin_refresh --apply refuses such a folder) is installed again from the export."""
        doc = dict(self.doc2, enabledPlugins={PID: True})
        with open(self.s2, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('%s: the kit plugin is enabled but was not installed from the export' % self.c2, out)
        self.assertEqual(self.host.fake.calls,
                         self.migrate_calls(self.c1, None) + self.migrate_calls(self.c2, self.c2))
        self.assertTrue(pluginsource.migrated(self.state(), self.c2))

    def test_fresh_plugin_install_that_does_not_enable_stops(self):
        self.host.fake.enables = False
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('machine-setup: STOPPED at: %s: enable the kit plugin' % self.c1, out)
        self.assertIn('is not true', out)
        self.assertIn('restore: copy "', out)
        self.assertIn('to run it again: %s (it removes the marketplace first, its failure tolerated)'
                      % pluginsource.migrate_cmd(self.c1), out)

    def test_fresh_tolerates_a_failed_marketplace_remove(self):
        self.host.fake.fail[('.claude-two', 'plugin marketplace remove')] = 1
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(len(self.host.fake.calls), 6)
        self.assertTrue(pluginsource.migrated(self.state(), self.c2))

    def test_fresh_failed_install_stops_with_the_rerun_and_a_rerun_finishes(self):
        self.host.fake.fail[('.claude-two', 'plugin install pm-kit@pm-kit')] = 1
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('machine-setup: STOPPED at: %s: enable the kit plugin' % self.c2, out)
        self.assertIn('FAILED %s: claude plugin install %s -s user exited 1: fake claude' % (self.c2, PID), out)
        self.assertIn(pluginsource.migrate_cmd(self.c2), out)
        state = self.state()
        self.assertTrue(pluginsource.migrated(state, self.c1))
        self.assertFalse(pluginsource.migrated(state, self.c2))
        self.host.fake.fail.clear()
        n = len(self.host.fake.calls)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.host.fake.calls[n:], self.migrate_calls(self.c2, self.c2))
        self.assertTrue(pluginsource.migrated(self.state(), self.c2))

    def test_fresh_refuses_a_bad_export_root_or_state(self):
        self.machine_config({'plugin': {'export_dir': os.path.join(self.kit, 'export')}})
        before = self.snapshot()
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('REFUSED - the plugin export (docs/plugin-source.md): ', out)
        self.assertEqual(self.snapshot(), before)
        self.machine_config({'plugin': {'export_dir': self.export}})
        T.touch(os.path.join(self.export, pluginsource.STATE), 'not json\n')
        before = self.snapshot()
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('REFUSED - the plugin export (docs/plugin-source.md): cannot read', out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.host.fake.calls, [])

    def test_fresh_default_folders_are_the_account_folders_and_config_dir_limits_them(self):
        backup = os.path.join(self.home, '.claude-backup')
        os.makedirs(backup)
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('config folders: %s' % ', '.join(pclaude.account_folders(self.home)), out)
        self.assertIn(backup, out.splitlines()[3])
        self.assertIn('note: config folders: every ~/.claude and ~/.claude-* folder (a backup copy too)', out)
        code, out = self.run_it(config_dirs=[self.c1, self.c2])
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertNotIn(backup, out)
        self.assertNotIn('a backup copy too', out)

    def test_no_account_folder_refuses_in_both_modes(self):
        shutil.rmtree(self.c1)
        shutil.rmtree(self.c2)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('REFUSED - no Claude account folder under %s' % self.home, out)
        old = os.path.join(self.root, 'old-kit')
        os.makedirs(old)
        code, lines = M.run(M.Context(self.home, self.kit, old_kit=old, agent_temp=self.temp,
                                      desktop=self.desk, host=self.host, today=T.TODAY), apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, lines)
        self.assertIn('machine-setup: REFUSED - no Claude account folder under %s' % self.home, lines[-2])

    def test_a_failed_create_leaves_no_tmp(self):
        path = os.path.join(self.c1, 'settings.json')
        with mock.patch.object(settings.os, 'rename', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                settings._create(path, b'{}\n')
        self.assertEqual(os.listdir(self.c1), [])

    def test_a_failed_replace_leaves_no_tmp(self):
        bak = self.s2 + '.machine-setup-%s.bak' % T.TODAY
        with mock.patch.object(settings.os, 'replace', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                settings._write(self.s2, self.raw2, b'{}\n', bak)
        self.assertEqual(sorted(os.listdir(self.c2)), sorted(['settings.json', os.path.basename(bak)]))
        self.assertEqual(T.readb(self.s2), self.raw2)

    def test_fresh_config_dir_outside_the_refresh_set_is_a_note_not_an_install(self):
        """A --config-dir folder that is not one of pluginsource.claude.account_folders: neither
        tools/plugin_refresh.py nor pm_start would ever update its plugin, so it is not installed;
        the note gives the hand commands and the export is still made for them."""
        other = os.path.join(self.root, 'other-config')
        os.makedirs(other)
        self.assertNotIn(other, pclaude.account_folders(self.home))
        code, out = self.run_it(apply=True, config_dirs=[self.c1, other])
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('note: %s is outside the plugin refresh set' % other, out)
        self.assertIn('CLAUDE_CONFIG_DIR=%s: claude plugin marketplace remove pm-kit; claude plugin '
                      'marketplace add %s; claude plugin install %s -s user' % (other, self.export, PID), out)
        self.assertIn('claude plugin marketplace update pm-kit; claude plugin update %s' % PID, out)
        self.assertNotIn('%s: enable the kit plugin' % other, out)
        self.assertEqual(self.host.fake.calls, self.migrate_calls(self.c1, None))
        self.assertTrue(guard_in(load(os.path.join(other, 'settings.json')), self.kit))
        self.assertNotIn('enabledPlugins', load(os.path.join(other, 'settings.json')))
        self.assertFalse(pluginsource.migrated(self.state(), other))
        shutil.rmtree(self.export)
        code, out = self.run_it(apply=True, config_dirs=[other])          # only the outside folder
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('done: export ', out)
        self.assertEqual(len(self.host.fake.calls), 3)                   # no claude run for it
        self.assertTrue(os.path.isfile(os.path.join(self.export, pluginsource.STATE)))

    def test_fresh_makes_no_shortcut_unless_asked(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertFalse(os.path.exists(os.path.join(self.desk, extras.SHORTCUT)))
        self.assertIn('machine schedule shortcut: not made in the default mode (--shortcut makes it)', out)

    def test_fresh_runs_shortcut_and_acl_but_not_the_lock_env(self):
        self.host.env[extras.LOCK_ENV] = 'somewhere-else'
        code, out = self.run_it(apply=True, shortcut=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertTrue(os.path.isfile(os.path.join(self.desk, extras.SHORTCUT)))
        self.assertTrue(self.host.acl_fixed)
        self.assertEqual(self.host.env, {extras.LOCK_ENV: 'somewhere-else', 'AGENT_TEMP': self.temp})
        self.assertEqual([c[1] for c in self.host.calls if c[0] == 'env'], ['AGENT_TEMP'])
        self.assertIn('KIT_MACHINE_LOCK left as is in fresh mode', out)
        self.assertIn('add agent_temp_sweep = ', out)                     # agent temp init runs fresh too
        self.assertIn('sweep dry run (nothing removed): ', out)          # the sweep task by default
        self.assertEqual(len([c for c in self.host.calls if c[0] == 'task']), 1)


SLEEPER = '''import os, sys, time
sys.path.insert(0, sys.argv[2])
from kitlib.proc import process
with open(sys.argv[1], 'w') as f:
    f.write('%d %d' % (os.getpid(), process.proc_ctime(os.getpid())))
time.sleep(60)
'''


@windows_only('the kit is Windows only')
class RealRunner(unittest.TestCase):
    """extras.Host.plugin_runner, the path --apply runs (claude.Runner over extras.run_tree), against
    fake claude .cmd files: the argv, the environment (no CLAUDE_CONFIG_DIR for ~/.claude, even
    when the caller's has one), the return code, and a timeout that ends the .cmd's child too."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-runner-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, 'home')
        self.c1 = os.path.join(self.home, '.claude')
        self.c2 = os.path.join(self.home, '.claude-two')
        for d in (self.c1, self.c2):
            os.makedirs(d)
        self.log = os.path.join(self.root, 'claude.log')
        self.environ = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(self.root, 'inherited'))

    def cmd(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, 'w', encoding='ascii', newline='\r\n') as fh:
            fh.write('@echo off\n' + body)
        return path

    def runner(self, claude, timeout=None):
        return extras.Host().plugin_runner(self.home, claude=claude, timeout=timeout, environ=self.environ)

    def test_argv_env_and_return_code(self):
        ok = self.cmd('claude.cmd', '>>"%s" echo [%%CLAUDE_CONFIG_DIR%%] %%*\nexit /b 0\n' % self.log)
        r = self.runner(ok)
        self.assertEqual(r.run_proc, extras.run_tree)
        self.assertEqual(r.run(['plugin', 'install', PID, '-s', 'user'], self.c1), (0, ''))
        self.assertEqual(r.run(['plugin', 'marketplace', 'add', self.root], self.c2), (0, ''))
        with open(self.log, encoding='ascii') as fh:
            self.assertEqual(fh.read().splitlines(), [
                '[] plugin install %s -s user' % PID,
                '[%s] plugin marketplace add %s' % (self.c2, self.root)])
        bad = self.cmd('fail.cmd', 'echo it broke\nexit /b 3\n')
        self.assertEqual(self.runner(bad).run(['plugin', 'install', PID], self.c2), (3, 'it broke\r\n'))

    def test_a_timeout_ends_the_whole_tree(self):
        pidfile = os.path.join(self.root, 'pid.txt')
        sleeper = os.path.join(self.root, 'sleeper.py')
        with open(sleeper, 'w', encoding='utf-8') as fh:
            fh.write(SLEEPER)
        slow = self.cmd('slow.cmd', '"%s" "%s" "%s" "%s"\n' % (sys.executable, sleeper, pidfile, T.KIT))
        t0 = time.monotonic()
        rc, text = self.runner(slow, timeout=8).run(['plugin', 'install', PID], self.c2)
        took = time.monotonic() - t0
        self.assertEqual((rc, text), (None, 'claude ran past 8 s'))
        self.assertLess(took, 40)                                    # not the sleeper's 60 s
        with open(pidfile, encoding='ascii') as fh:
            pid, ctime = (int(x) for x in fh.read().split())
        from kitlib.proc import process
        end = time.monotonic() + 10
        while process.alive(pid, ctime) and time.monotonic() < end:
            time.sleep(0.2)
        self.assertFalse(process.alive(pid, ctime), 'the .cmd\'s python child outlived the timeout')


ALIASES = ('python', 'pythonw', 'py', 'python3')


class FreshTool(FreshBase):
    def ps1(self, args, env=None, python=True):
        """The wrapper, given this interpreter as -Python unless `python` is False: never the
        `python` alias, which with a temp LOCALAPPDATA makes the Python install manager install."""
        ps = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'WindowsPowerShell', 'v1.0',
                          'powershell.exe')
        ps1 = os.path.join(T.KIT, 'installer', 'machine-setup.ps1')
        args = list(args) + (['-Python', sys.executable] if python else [])
        return subprocess.run([ps, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ps1] + args,
                              capture_output=True, text=True, env=env)

    def alias_trap(self):
        """(a PATH whose python, pythonw, py and python3 are .cmd fakes that append their name to a
        mark file and exit 97, with every PATH folder holding a real one of those .exe left out;
        the mark file)."""
        fake = os.path.join(self.root, 'fake-python')
        os.makedirs(fake)
        mark = os.path.join(self.root, 'alias-started.txt')
        for n in ALIASES:
            with open(os.path.join(fake, n + '.cmd'), 'w', encoding='ascii', newline='\r\n') as fh:
                fh.write('@echo off\n>>"%s" echo %%~n0 %%*\nexit /b 97\n' % mark)
        keep = [d for d in os.environ.get('PATH', '').split(os.pathsep)
                if d.strip() and not any(os.path.lexists(os.path.join(d, n + '.exe')) for n in ALIASES)]
        return os.pathsep.join([fake] + keep), mark

    def test_no_python_alias_is_started(self):
        """The wrapper with -Python and the tool's whole dry run (guard.py --settings-entry, the
        shortcut read) under a temp LOCALAPPDATA start no python, pythonw or py from PATH: a
        .cmd fake would leave the mark, and a bare 'python' to CreateProcess finds no .exe and
        fails the run. Without -Python the wrapper runs the PATH python, which the fake catches."""
        path, mark = self.alias_trap()
        lad = os.path.join(self.root, 'localappdata')
        desk = os.path.join(self.root, 'desktop-2')
        os.makedirs(lad)
        os.makedirs(desk)
        env = dict(os.environ, PATH=path, USERPROFILE=self.home, HOME=self.home, LOCALAPPDATA=lad)
        env.pop('CLAUDE_CONFIG_DIR', None)
        # A temp agent folder beside the temp home: the user environment's AGENT_TEMP may hold this
        # temp home, a folder the sweep (and so the setup) refuses.
        args = ['-Fresh', '-PmSettings', self.pm, '-HomeDir', self.home, '-Desktop', desk,
                '-AgentTemp', os.path.join(self.root, 'agent-temp')]
        r = self.ps1(args, env=env)
        self.assertEqual(r.returncode, M.EXIT_OK, r.stdout + r.stderr)
        self.assertIn('DRY RUN', r.stdout)
        self.assertIn('config folders: %s, %s' % (self.c1, self.c2), r.stdout)
        self.assertFalse(os.path.exists(mark), 'a python alias was started: %s' % (
            T.readb(mark) if os.path.exists(mark) else b''))
        self.assertEqual(os.listdir(lad), [])
        r = self.ps1(args, env=env, python=False)
        self.assertEqual(r.returncode, 97, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(mark))
        r = self.ps1(args + ['-Python', os.path.join(self.root, 'none.exe')], env=env, python=False)
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        self.assertIn('is not a file', r.stdout)

    def test_fresh_and_old_kit_together_is_usage(self):
        r = subprocess.run([sys.executable, T.TOOL, '--fresh', '--old-kit', self.root], capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE)
        self.assertIn('not allowed with', r.stderr)
        r = subprocess.run([sys.executable, T.TOOL, '--old-kit', self.root, '--pm-settings', self.pm],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE)
        self.assertIn('--pm-settings goes with the default mode only', r.stderr)
        r = self.ps1(['-OldKit', self.root, '-Fresh'])
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        self.assertIn('usage - give -OldKit <', r.stdout)
        self.assertIn('> or -Fresh, not both', r.stdout)
        r = self.ps1(['-OldKit', self.root, '-PmSettings', self.pm])
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        self.assertIn('-PmSettings goes with the default mode only', r.stdout)
        self.assertTrue(M.Context(self.home, self.kit).fresh)              # no mode: the default
        with self.assertRaises(ValueError):
            M.Context(self.home, self.kit, self.root, fresh=True)

    def test_the_default_mode_needs_no_switch(self):
        """No -Fresh and no -OldKit is the default mode, through the wrapper and the tool."""
        missing = os.path.join(self.home, '.claude-none')
        for r in (self.ps1(['-HomeDir', self.home, '-ConfigDir', missing, '-OwnerEmail', 'o@example.invalid']),
                  subprocess.run([sys.executable, T.TOOL, '--home', self.home, '--config-dir', missing],
                                 capture_output=True, text=True)):
            self.assertEqual(r.returncode, M.EXIT_REFUSED, r.stdout + r.stderr)
            self.assertIn('REFUSED - no config folder at %s' % missing, r.stdout)
            self.assertIn(' kit:        none (the default mode)', r.stdout)

    def test_wrapper_passes_fresh_and_pm_settings(self):
        """A refusal that stops before anything is read (a missing config folder) proves -Fresh,
        -PmSettings, -HomeDir and -ConfigDir reach the tool."""
        missing = os.path.join(self.home, '.claude-none')
        r = self.ps1(['-Fresh', '-PmSettings', self.pm, '-HomeDir', self.home, '-ConfigDir', missing])
        self.assertEqual(r.returncode, M.EXIT_REFUSED, r.stdout + r.stderr)
        self.assertIn('REFUSED - no config folder at %s' % missing, r.stdout)
        self.assertIn(' kit:        none (the default mode)', r.stdout)   # the old-kit header line
        self.assertIn('pm settings:    %s' % self.pm, r.stdout)
        self.assertIn('DRY RUN', r.stdout)

    def test_the_default_home_account_settings_are_untouched(self):
        """The home the process would fall back to (USERPROFILE and HOME: a seeded temp_home under
        the home fence, never the real one) keeps its ~/.claude/settings.json and every account
        folder's settings.json byte for byte, size and mtime, across a --fresh --apply run on temp
        folders and a --fresh dry run whose home and LOCALAPPDATA are other temp folders."""
        default_home = temp_home(self, prefix='kit-default-home-')
        for n in ('.claude', '.claude-acct'):
            os.makedirs(os.path.join(default_home, n))
            with open(os.path.join(default_home, n, 'settings.json'), 'wb') as f:
                f.write(b'{"model": "opus"}\r\n')
        self.assertEqual(os.path.expanduser('~'), default_home)

        def stats():
            out = {}
            for d in M.dedupe([os.path.join(default_home, '.claude')] + M.default_config_dirs(default_home)):
                p = os.path.join(d, 'settings.json')
                try:
                    st = os.stat(p)
                    out[p] = (st.st_size, st.st_mtime_ns, T.readb(p))
                except OSError:
                    out[p] = None
            return out
        before = stats()
        self.assertEqual(sorted(before), sorted(os.path.join(default_home, n, 'settings.json')
                                                for n in ('.claude', '.claude-acct')))
        sys.path.insert(0, os.path.dirname(T.TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(T.TOOL))
        import machine_setup
        argv = ['--fresh', '--kit', self.kit, '--home', self.home, '--agent-temp', self.temp,
                '--desktop', self.desk, '--pm-settings', self.pm, '--apply']
        with mock.patch.object(extras, 'Host', lambda: self.host), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(machine_setup.main(argv), M.EXIT_OK)
        self.assertEqual(len(self.host.fake.calls), 6)
        lad = os.path.join(self.root, 'localappdata')
        os.makedirs(lad)
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home, LOCALAPPDATA=lad)
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
