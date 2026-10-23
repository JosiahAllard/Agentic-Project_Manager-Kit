"""The machine-wide managed settings file (kitlib.machinesetup.managed; docs/machine-setup.md
"Managed settings"): the --managed-settings mode that renders the kit's template into it, the
default mode's note on whether it is in sync, the account guard entry left out when that file
carries the guard. The managed file, the kit,
its template and the home are all in a temp folder (FakeHost.managed); nothing here reads or writes
the real managed settings file, and one test proves a run leaves it as it was.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.config import user as kuser
from kitlib.machinesetup import extras, managed, settings
from tests.machinesetup import test_fresh as F
from tests.machinesetup import test_machinesetup as T

TEMPLATE = {
    'permissions': {'allow': ['Artifact']},
    'hooks': {'PreToolUse': [{'matcher': 'Bash|PowerShell', 'hooks': [
        {'type': 'command', 'command': '{pythonw}', 'args': ['{kit}/boot/hook.py', 'claude', 'PreToolUse']}]}]},
    'statusLine': {'type': 'command', 'command': 'powershell -NoProfile -File {kit}/optional/usage/statusline-usage.ps1',
                   'refreshInterval': 60},
    'enabledPlugins': {'pm-kit@pm-kit': True},
    'extraKnownMarketplaces': {'pm-kit': {'source': {'source': 'directory', 'path': '{plugin_export}'}}},
    'autoMode': {'allow': ['$defaults', 'a rule']},
}


def dump(path, data, bom=False, crlf=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, indent=2) + '\n'
    if crlf:
        text = text.replace('\n', '\r\n')
    with open(path, 'wb') as f:
        f.write((b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8'))


def load(path):
    with open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8-sig'))


def guard_doc(kit):
    return {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
        {'type': 'command', 'command': T.PYW, 'args': [T.fwd(os.path.join(kit, 'boot', 'hook.py')), 'claude',
                                                       'PreToolUse']}]}]}}


class ManagedBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-managed-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.kit = os.path.join(self.root, 'kit')
        self.home = os.path.join(self.root, 'home')
        self.export = os.path.join(self.root, 'export')
        for rel in ('boot/hook.py', 'optional/usage/statusline-usage.ps1'):
            T.touch(os.path.join(self.kit, *rel.split('/')))
        os.makedirs(os.path.join(self.home, '.claude'))
        os.makedirs(kuser.config_dir(self.home))
        self.config({'plugin': {'export_dir': self.export}})
        self.source = os.path.join(self.kit, *managed.source().split('/'))
        dump(self.source, TEMPLATE)
        self.host = T.FakeHost(self.kit)
        self.host.managed = os.path.join(self.root, 'Program Files', 'ClaudeCode', 'managed-settings.json')
        self.host.admin = True
        self.target = self.host.managed

    def config(self, data):
        with open(os.path.join(kuser.config_dir(self.home), kuser.FILE_NAME), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def overlay(self, data):
        dump(os.path.join(kuser.config_dir(self.home), managed.OVERLAY), data)

    def ctx(self):
        return M.ManagedContext(self.home, self.kit, host=self.host, today=T.TODAY)

    def run_it(self, apply=False):
        code, lines = M.run_managed(self.ctx(), apply=apply)
        return code, '\n'.join(lines)

    def rendered(self):
        k = T.fwd(self.kit)
        return {
            'permissions': {'allow': ['Artifact']},
            'hooks': {'PreToolUse': [{'matcher': 'Bash|PowerShell', 'hooks': [
                {'type': 'command', 'command': T.PYW, 'args': [k + '/boot/hook.py', 'claude', 'PreToolUse']}]}]},
            'statusLine': {'type': 'command',
                           'command': 'powershell -NoProfile -File %s/optional/usage/statusline-usage.ps1' % k,
                           'refreshInterval': 60},
            'enabledPlugins': {'pm-kit@pm-kit': True},
            'extraKnownMarketplaces': {'pm-kit': {'source': {'source': 'directory',
                                                             'path': os.path.normpath(self.export)}}},
            'autoMode': {'allow': ['$defaults', 'a rule']},
        }


class ManagedMode(ManagedBase):
    def test_no_template_is_a_note_and_nothing_to_do(self):
        os.remove(self.source)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('no %s in the kit' % managed.source(), out)
        self.assertIn('managed-settings: nothing to do', out)
        self.assertFalse(os.path.lexists(self.target))

    def test_creates_a_missing_file_then_nothing_to_do(self):
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('DRY RUN', out)
        self.assertIn('create it', out)
        self.assertFalse(os.path.lexists(self.target))
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(load(self.target), self.rendered())
        code, out = self.run_it()
        self.assertIn('managed-settings: nothing to do', out)
        self.assertIn('in sync', out)

    def test_a_different_file_is_replaced_in_its_layout_with_a_backup(self):
        old = {'permissions': {'allow': ['Bash']}, 'hooks': {}}
        dump(self.target, old, bom=True, crlf=True)
        with open(self.target, 'rb') as f:
            raw = f.read()
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('-      "Bash"', out.replace('\r', ''))           # the diff
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        with open(self.target, 'rb') as f:
            new = f.read()
        self.assertTrue(new.startswith(b'\xef\xbb\xbf'))
        self.assertIn(b'\r\n', new)
        self.assertEqual(load(self.target), self.rendered())
        bak = '%s.machine-setup-%s.bak' % (self.target, T.TODAY)
        self.assertEqual(T.readb(bak), raw)
        self.assertIn('managed-settings: nothing to do', self.run_it()[1])

    def test_a_file_with_keys_the_template_lacks_is_refused_and_kept(self):
        dump(self.target, {'model': 'opus', 'env': {'A': 'b'}, 'permissions': {'allow': ['Bash']}})
        before = T.readb(self.target)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('env, model', out)
        self.assertIn('add them to the template', out)
        self.assertEqual(T.readb(self.target), before)

    def test_nested_keys_the_template_lacks_are_refused_by_path_and_kept(self):
        have = self.rendered()
        have['autoMode'] = {'allow': ['x'], 'environment': ['e'], 'soft_deny': ['s']}
        dump(self.target, have)
        before = T.readb(self.target)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('autoMode.environment, autoMode.soft_deny', out)
        self.assertIn('add them to the template', out)
        self.assertEqual(T.readb(self.target), before)

    def test_a_nested_key_the_overlay_holds_is_not_refused(self):
        self.overlay({'autoMode': {'environment': ['here']}})
        have = self.rendered()
        have['autoMode'] = {'allow': ['x'], 'environment': ['e']}
        dump(self.target, have)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(load(self.target)['autoMode']['environment'], ['here'])

    def test_a_subtree_under_a_template_value_that_is_no_object_is_refused_at_its_key(self):
        dump(self.target, dict(self.rendered(), permissions={'allow': {'deep': 1}}))
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn(': permissions.allow;', out)

    def test_extra_paths_walk_objects_but_not_lists(self):
        data = {'a': {'b': 1}, 'l': [{'x': 1}], 's': 1}
        have = {'a': {'b': 2, 'c': {'d': 1}}, 'l': [{'x': 1, 'y': 2}], 's': {'t': 1}, 'z': 0}
        self.assertEqual(managed._extra_paths(have, data), ['a.c', 's', 'z'])

    def test_a_file_differing_only_in_key_order_is_in_sync(self):
        have = self.rendered()
        dump(self.target, dict(reversed(list(have.items()))))
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('in sync', out)
        self.assertIn('managed-settings: nothing to do', out)

    def test_a_block_the_merge_only_moved_is_not_a_removed_line_in_the_diff(self):
        env = {'GIT_AUTHOR_NAME': 'Agent'}
        self.overlay({'env': env})
        have = dict(self.rendered(), env=env)
        have = dict([('env', have.pop('env'))] + list(have.items()))     # env first, as before
        have['permissions'] = {'allow': ['Bash']}
        dump(self.target, have)
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('-      "Bash"', out.replace('\r', ''))
        changed = [l for l in out.splitlines() if l.startswith(('-', '+'))]
        self.assertNotIn('GIT_AUTHOR_NAME', '\n'.join(changed))

    def test_ordered_like_follows_the_old_order_and_appends_new_keys(self):
        data = {'b': {'y': 1, 'x': 2, 'n': 3}, 'a': 1, 'new': 2, 'l': [{'q': 1}]}
        got = managed._ordered_like(data, {'a': 0, 'b': {'x': 0, 'y': 0}, 'gone': 1})
        self.assertEqual(list(got), ['a', 'b', 'new', 'l'])
        self.assertEqual(list(got['b']), ['x', 'y', 'n'])

    def test_a_key_the_overlay_holds_is_not_dropped(self):
        self.overlay({'model': 'opus'})
        dump(self.target, {'model': 'sonnet'})
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(load(self.target)['model'], 'opus')

    def test_a_file_that_is_no_json_is_refused_and_kept(self):
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, 'wb') as f:
            f.write(b'{nope')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('is not JSON', out)
        self.assertEqual(T.readb(self.target), b'{nope')

    def test_a_file_that_is_no_object_is_refused(self):
        dump(self.target, ['a'])
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('does not hold a JSON object', out)

    def test_no_overlay_is_a_warning_naming_the_home(self):
        code, out = self.run_it()
        self.assertIn('WARNING: no overlay found at %s:' % managed.overlay_path(self.home), out)
        self.overlay({})
        self.assertNotIn('WARNING', self.run_it()[1])

    def test_apply_is_refused_unless_elevated(self):
        self.host.admin = False
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('not elevated', out)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('REFUSED - not elevated', out)
        self.assertFalse(os.path.lexists(self.target))

    def test_a_file_changed_since_the_survey_is_not_replaced(self):
        dump(self.target, {'permissions': {'allow': ['Bash']}})
        plan = managed.survey(self.ctx(), apply=True)
        self.assertEqual(plan.refusals, [])
        dump(self.target, {'model': 'sonnet'})
        with self.assertRaises(RuntimeError) as e:
            plan.changes[0].do()
        self.assertIn('changed since the survey', str(e.exception))
        self.assertEqual(load(self.target), {'model': 'sonnet'})

    def test_a_template_that_is_no_json_object_is_refused(self):
        with open(self.source, 'w', encoding='utf-8') as f:
            f.write('[1, 2]')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('does not hold a JSON object', out)
        with open(self.source, 'w', encoding='utf-8') as f:
            f.write('{nope')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertFalse(os.path.lexists(self.target))

    def test_a_linked_target_is_refused(self):
        real = os.path.join(self.root, 'real.json')
        dump(real, {'model': 'opus'})
        os.makedirs(os.path.dirname(self.target))
        os.link(real, self.target)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('hard link', out)
        self.assertEqual(load(real), {'model': 'opus'})

    def test_the_overlay_merges_over_the_template(self):
        """<home>/.pm-kit/managed-settings.json: env (the agents' git identity) and any other key
        merged over the template, objects key by key, lists and values replaced."""
        env = {'GIT_AUTHOR_NAME': 'Agent', 'GIT_AUTHOR_EMAIL': 'agent@example.invalid'}
        self.overlay({'env': env, 'autoMode': {'environment': ['here']}, 'permissions': {'allow': ['Other']}})
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        got = load(self.target)
        self.assertEqual(got['env'], env)
        self.assertEqual(got['autoMode'], {'allow': ['$defaults', 'a rule'], 'environment': ['here']})
        self.assertEqual(got['permissions'], {'allow': ['Other']})
        self.assertEqual(got['hooks'], self.rendered()['hooks'])

    def test_a_bad_overlay_is_refused(self):
        self.overlay(['x'])
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn(managed.OVERLAY, out)
        self.overlay({'env': {'GIT_AUTHOR_NAME': 3}})
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('"env"', out)
        self.assertFalse(os.path.lexists(self.target))

    def test_a_status_line_through_a_tools_link_is_repointed_to_the_kit(self):
        tpl = dict(TEMPLATE, statusLine={'type': 'command', 'command':
                                         'powershell -File %USERPROFILE%/.claude/tools/usage/statusline-usage.ps1'})
        dump(self.source, tpl)
        self.assertEqual(self.run_it(apply=True)[0], M.EXIT_OK)
        self.assertEqual(load(self.target)['statusLine']['command'],
                         'powershell -File %s/optional/usage/statusline-usage.ps1' % T.fwd(self.kit))

    def test_a_template_without_the_guard_is_noted(self):
        tpl = dict(TEMPLATE)
        del tpl['hooks']
        dump(self.source, tpl)
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('carries no guard entry', out)

    def test_the_real_managed_file_is_left_alone(self):
        real = extras.Host().managed_path()

        def stat():
            if not real:
                return None
            try:
                st = os.stat(real)
                return st.st_size, st.st_mtime_ns
            except OSError:
                return None
        before = stat()
        self.assertEqual(self.run_it(apply=True)[0], M.EXIT_OK)
        if real:
            self.assertNotEqual(os.path.normcase(self.target), os.path.normcase(real))
        self.assertEqual(stat(), before)

    def test_a_host_with_no_managed_file_has_nothing_to_install(self):
        self.host.managed = None
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('has no machine-wide managed settings file: nothing to install', out)
        self.assertFalse(os.path.lexists(self.target))


class RealHost(unittest.TestCase):
    def test_the_template_is_the_default_hosts(self):
        from kitlib.host import registry
        self.assertEqual(managed.source(), 'hosts/%s/managed-settings.json' % registry.default())

    def test_the_managed_path_is_under_program_files(self):
        with mock.patch.dict(os.environ, {'ProgramFiles': 'X:\\PF'}):
            os.environ.pop('ProgramW6432', None)
            self.assertEqual(extras.Host().managed_path(), 'X:\\PF\\ClaudeCode\\managed-settings.json')

    def test_program_w6432_is_read_first(self):
        from hosts.claude.adapter.host import ClaudeAdapter
        env = {'ProgramW6432': 'X:\\PF64', 'ProgramFiles': 'X:\\PF32'}
        self.assertEqual(ClaudeAdapter().managed_settings_path(env), 'X:\\PF64\\ClaudeCode\\managed-settings.json')
        self.assertEqual(ClaudeAdapter().managed_settings_path({'ProgramFiles': 'X:\\PF32'}),
                         'X:\\PF32\\ClaudeCode\\managed-settings.json')

    def test_no_program_files_is_no_managed_file(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('ProgramFiles', None)
            os.environ.pop('ProgramW6432', None)
            self.assertIsNone(extras.Host().managed_path())

    def test_a_host_adapter_without_the_method_has_no_managed_file(self):
        """managed_settings_path is optional: the default host's adapter is asked with getattr."""
        from kitlib.host import registry
        fake = types.SimpleNamespace(ADAPTER=object())
        with mock.patch.dict(sys.modules, {'machinesetup_fake_adapter': fake}), \
                mock.patch.object(registry, 'host', lambda name, *a: types.SimpleNamespace(
                    adapter='machinesetup_fake_adapter')):
            self.assertIsNone(extras.Host().managed_path())

    def test_is_admin_answers_a_bool(self):
        self.assertIn(extras.Host().is_admin(), (True, False))


class DefaultModeNote(ManagedBase):
    """The default mode never writes the managed file: it notes whether it is in sync."""

    def fresh_ctx(self):
        host = F.FreshHost(self.kit, self.home)                      # the plugin step's fake claude
        host.managed, host.admin = self.host.managed, self.host.admin
        self.host = host
        return M.Context(self.home, self.kit, fresh=True, host=self.host, today=T.TODAY,
                         agent_temp=os.path.join(self.root, 'temp'), desktop=self.root)

    def test_a_missing_file_names_the_command(self):
        text = '\n'.join(M.survey(self.fresh_ctx()).lines())
        self.assertIn('is missing; it is made from the kit template', text)
        self.assertIn('--managed-settings --apply', text)

    def test_out_of_sync_names_the_command(self):
        dump(self.target, {'model': 'opus'})
        plan = M.survey(self.fresh_ctx())
        text = '\n'.join(plan.lines())
        self.assertIn('differs from the kit template', text)
        self.assertIn('--managed-settings --apply', text)
        self.assertFalse(any(c.text.startswith(self.target) for c in plan.changes))

    def test_in_sync_says_so(self):
        dump(self.target, self.rendered())
        text = '\n'.join(M.survey(self.fresh_ctx()).lines())
        self.assertIn('in sync with the kit template', text)


class OldKitGuard(T.Base):
    """The switch from an older kit clone leaves the guard out of the accounts when the managed
    file carries it."""

    def setUp(self):
        super(OldKitGuard, self).setUp()
        self.host.managed = os.path.join(self.root, 'pf', 'managed-settings.json')

    def guarded(self, path):
        return any(T.fwd(os.path.join(self.kit, 'boot', 'hook.py')) in h.get('args', [])
                   for g in (load(path).get('hooks') or {}).get('PreToolUse', []) for h in g.get('hooks', []))

    def test_no_account_guard_when_the_managed_file_has_it(self):
        dump(self.host.managed, guard_doc(self.kit))
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('the guard\'s entry is in the managed settings file', out)
        self.assertFalse(self.guarded(self.s1))
        self.assertFalse(self.guarded(self.s2))

    def test_without_a_managed_file_the_guard_is_added(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertTrue(self.guarded(self.s1))

    def test_an_unreadable_managed_file_is_refused(self):
        os.makedirs(os.path.dirname(self.host.managed))
        with open(self.host.managed, 'w') as f:
            f.write('{nope')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('REFUSED - %s' % self.host.managed, out)


class FreshGuard(F.FreshBase):
    def setUp(self):
        super(FreshGuard, self).setUp()
        self.host.managed = os.path.join(self.root, 'pf', 'managed-settings.json')

    def test_no_account_guard_when_the_managed_file_has_it(self):
        dump(self.host.managed, guard_doc(self.kit))
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertFalse(F.guard_in(load(self.s1), self.kit))
        self.assertFalse(F.guard_in(load(self.s2), self.kit))

    def test_the_plugin_enabled_in_the_managed_file_still_installs_per_account(self):
        dump(self.host.managed, {'enabledPlugins': {F.PID: True}})
        plan = M.survey(self.ctx())
        self.assertTrue(any('enable the kit plugin' in c.text for c in plan.changes))
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(sum('enable the kit plugin' in l for l in out.splitlines() if l.startswith('done: ')), 2)

    def test_both_carrying_the_guard_is_noted(self):
        dump(self.host.managed, guard_doc(self.kit))
        doc = dict(self.doc2)
        doc['hooks'] = dict(doc['hooks'], PreToolUse=doc['hooks']['PreToolUse'] + self.guard_groups())
        dump(self.s2, doc)
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('%s: the guard\'s entry is also in the managed settings file' % self.s2, out)
        self.assertIn('runs twice', out)

    def test_another_kits_guard_in_the_managed_file_is_noted_and_none_added(self):
        dump(self.host.managed, guard_doc(os.path.join(self.root, 'other-kit')))
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('names another kit\'s guard', out)
        self.assertFalse(F.guard_in(load(self.s2), self.kit))


class ManagedTool(ManagedBase):
    def main(self, argv):
        sys.path.insert(0, os.path.dirname(T.TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(T.TOOL))
        import machine_setup
        buf = io.StringIO()
        with mock.patch.object(extras, 'Host', lambda: self.host), contextlib.redirect_stdout(buf):
            code = machine_setup.main(argv)
        return code, buf.getvalue()

    def test_the_flag_runs_the_managed_mode(self):
        code, out = self.main(['--managed-settings', '--kit', self.kit, '--home', self.home, '--apply'])
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('managed-settings: APPLY', out)
        self.assertEqual(load(self.target), self.rendered())

    def test_other_options_are_usage(self):
        for extra in (['--config-dir', self.home], ['--owner-email', 'a@example.invalid'], ['--old-kit', self.kit]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.main(['--managed-settings', '--home', self.home] + extra)[0], M.EXIT_USAGE)

    def test_the_wrapper_passes_the_switch(self):
        ps = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'WindowsPowerShell', 'v1.0',
                          'powershell.exe')
        ps1 = os.path.join(T.KIT, 'installer', 'machine-setup.ps1')
        base = [ps, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ps1, '-Python',
                sys.executable]
        r = subprocess.run(base + ['-ManagedSettings', '-OldKit', self.kit], capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        r = subprocess.run(base + ['-ManagedSettings', '-HomeDir', self.home, '-ConfigDir', self.home],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        self.assertIn('--managed-settings', r.stderr + r.stdout)


if __name__ == '__main__':
    unittest.main()
