"""kitlib.config.policy: heavy programs, roles, the kinds table, the repo hook module and its gate
checks."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import config, lockmcp
from kitlib.config import policy
from kitlib.host import Action
from tests.config.helpers import TempTree


def ev(payload):
    """A Claude Code PreToolUse payload as the guard sees it (hosts.claude.adapter.events.parse)."""
    from hosts.claude.adapter import events
    return events.parse('PreToolUse', payload)


class TreeCase(unittest.TestCase):

    def load(self, kit=None, files=None):
        t = TempTree(kit, files)
        self.addCleanup(t.cleanup)
        return config.load(t.root)


class HeavyTest(TreeCase):

    def test_godot_is_heavy_by_any_path_and_case(self):
        entries = policy.heavy_programs()
        for prog in ('godot', 'C:\\Tools\\Godot_v4.4-stable_mono_win64.exe', '/opt/godot/godot.x86_64'):
            self.assertEqual(policy.heavy_match(prog, [], entries)['kind'], 'app-run', prog)

    def test_godot_glob_names_godot_builds_only(self):
        entries = policy.heavy_programs()
        for prog in ('godot.exe', 'Godot_v4.3-stable_win64_console.exe', 'godot_mono'):
            self.assertEqual(policy.heavy_match(prog, [], entries)['kind'], 'app-run', prog)
        for prog in ('godotenv', 'godotenv.exe', 'godot-tools', 'godots'):
            self.assertIsNone(policy.heavy_match(prog, [], entries), prog)

    def test_blender_is_heavy_only_in_the_background(self):
        entries = policy.heavy_programs()
        self.assertIsNone(policy.heavy_match('blender.exe', ['scene.blend'], entries))
        self.assertEqual(policy.heavy_match('blender.exe', ['-b', 'x.blend'], entries)['kind'], 'render')
        self.assertEqual(policy.heavy_match('blender', ['--background'], entries)['kind'], 'render')

    def test_dotnet_is_heavy_when_it_compiles(self):
        entries = policy.heavy_programs()
        for prog, args in (('dotnet', ['build', 'x.sln']), ('C:/sdk/dotnet.exe', ['test', '--nologo']),
                           ('dotnet', ['run', '--project', 'app']), ('DOTNET', ['publish', '-c', 'Release'])):
            self.assertEqual(policy.heavy_match(prog, args, entries)['kind'], 'build', (prog, args))
        for args in (['--info'], ['--version'], ['--list-sdks'], ['new', 'console'], ['nuget', 'locals', 'all', '--list']):
            self.assertIsNone(policy.heavy_match('dotnet', args, entries), args)

    def test_gradle_is_heavy_but_its_information_flags(self):
        entries = policy.heavy_programs()
        for prog, args in (('./gradlew', ['test']), ('gradlew.bat', ['build', '--console=plain']),
                           ('gradle', ['assemble']), ('C:\\tools\\gradle.bat', [':core:check'])):
            self.assertEqual(policy.heavy_match(prog, args, entries)['kind'], 'build', (prog, args))
        for prog, args in (('gradlew', ['--version']), ('gradle', ['-v']), ('gradlew.bat', ['--stop']),
                           ('./gradlew', ['--help'])):
            self.assertIsNone(policy.heavy_match(prog, args, entries), (prog, args))
        self.assertIsNone(policy.heavy_match('gradle-wrapper.jar', [], entries))

    def test_an_unlisted_program_is_not_heavy(self):
        self.assertIsNone(policy.heavy_match('python.exe', ['-m', 'unittest'], policy.heavy_programs()))

    @mock.patch('kitlib.platform.NAME', 'windows')
    def test_repo_entries_follow_the_defaults_and_cannot_remove_them(self):
        cfg = self.load({'heavy': {'programs': [
            {'program': 'test.bat', 'kind': 'engine-test', 'why': 'runs Godot tests'},
            {'program': 'graphbuild.bat', 'kind': 'graph-build', 'unless_args': ['--help', '--dry-run'],
             'why': 'builds the world graph', '_note': 'dropped'}]}})
        entries = policy.heavy_programs(cfg)
        self.assertEqual([e['program'] for e in entries],
                         ['godot', 'godot.*', 'godot_*', 'blender*', 'dotnet', 'dotnet.exe', 'gradle', 'gradle.*',
                          'gradlew', 'gradlew.*', 'test.bat', 'graphbuild.bat'])
        self.assertNotIn('_note', entries[-1])
        self.assertEqual(policy.heavy_match('.\\TEST.BAT', ['fast'], entries)['kind'], 'engine-test')
        self.assertEqual(policy.heavy_match('graphbuild.bat', [], entries)['kind'], 'graph-build')
        self.assertIsNone(policy.heavy_match('graphbuild.bat', ['--dry-run'], entries))
        self.assertIsNone(policy.heavy_match('graphbuild.bat', ['--help=all'], entries))

    def test_a_heavy_entry_without_a_reason_refuses(self):
        t = TempTree({'heavy': {'programs': [{'program': 'x.bat', 'kind': 'build'}]}})
        self.addCleanup(t.cleanup)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(t.root)
        self.assertIn('needs "why"', str(cm.exception))

    def test_the_first_matching_entry_wins(self):
        entries = [{'program': 'a*', 'kind': 'one', 'when_args': ['-x']},
                   {'program': 'a*', 'kind': 'two'}]
        self.assertEqual(policy.heavy_match('ab', ['-x'], entries)['kind'], 'one')
        self.assertEqual(policy.heavy_match('ab', [], entries)['kind'], 'two')


WINDOWS_DEFAULTS = ['godot', 'godot.*', 'godot_*', 'blender*', 'dotnet', 'dotnet.exe', 'gradle',
                    'gradle.*', 'gradlew', 'gradlew.*']


class LinuxHeavyTest(unittest.TestCase):
    """Godot and Blender as Linux names them (docs/linux-support-plan.md section 3, finding 8)."""

    def setUp(self):
        patcher = mock.patch('kitlib.platform.NAME', 'linux')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entries = policy.heavy_programs()

    def kind(self, prog, args):
        entry = policy.heavy_match(prog, args, self.entries)
        return entry and entry['kind']

    def test_linux_godot_names_are_heavy(self):
        for prog, args in (('godot', []), ('godot4', []), ('/usr/bin/godot3', ['--path', 'game']),
                           ('Godot_v4.3-stable_linux.x86_64', ['--headless']),
                           ('Godot4.AppImage', []), ('Godot.AppImage', ['--headless']),
                           ('flatpak', ['run', 'org.godotengine.Godot', '--headless']),
                           ('flatpak', ['--user', 'run', 'org.godotengine.Godot']),
                           ('/usr/bin/flatpak', ['run', '--branch=stable', 'org.godotengine.GodotSharp']),
                           ('/var/lib/flatpak/exports/bin/org.godotengine.Godot', ['-e'])):
            self.assertEqual(self.kind(prog, args), 'app-run', (prog, args))

    def test_flatpak_management_and_other_apps_are_not_heavy(self):
        for prog, args in (('flatpak', ['run', 'org.gnome.Calculator']), ('flatpak', ['list']),
                           ('flatpak', ['install', 'flathub', 'org.godotengine.Godot']),
                           ('flatpak', ['update', 'org.godotengine.Godot']),
                           ('flatpak', ['info', 'org.godotengine.GodotSharp']),
                           ('flatpak', ['uninstall', 'org.blender.Blender', '-y']),
                           ('flatpak', ['remote-info', 'flathub', 'org.godotengine.Godot']),
                           ('flatpak', ['remote-info', 'flathub', 'org.blender.Blender', '-b']),
                           ('flatpak', ['mask', 'org.godotengine.GodotSharp']),
                           ('godotenv', []), ('godot-tools', []), ('godots', [])):
            self.assertIsNone(self.kind(prog, args), (prog, args))

    def test_blender_is_heavy_only_in_the_background(self):
        for prog, args in (('blender', ['-b', 'x.blend']), ('/usr/bin/blender', ['--background']),
                           ('flatpak', ['run', 'org.blender.Blender', '-b', 'x.blend']),
                           ('flatpak', ['run', '--branch=stable', 'org.blender.Blender', '--background']),
                           ('/var/lib/flatpak/exports/bin/org.blender.Blender', ['-b', 'x.blend'])):
            self.assertEqual(self.kind(prog, args), 'render', (prog, args))
        for prog, args in (('blender', ['x.blend']), ('flatpak', ['run', 'org.blender.Blender']),
                           ('flatpak', ['run', 'org.blender.Blender', 'x.blend']),
                           ('flatpak', ['run', 'org.gimp.GIMP', '-b', '(gimp-quit 0)']),
                           ('flatpak', ['install', 'flathub', 'org.blender.Blender']),
                           ('/var/lib/flatpak/exports/bin/org.blender.Blender', ['x.blend'])):
            self.assertIsNone(self.kind(prog, args), (prog, args))


class WindowsHeavyUnchangedTest(unittest.TestCase):
    """The Linux names are added on Linux only: on Windows the list and every result stay as they
    were (godot[0-9]* would make godot4.exe heavy there)."""

    def setUp(self):
        patcher = mock.patch('kitlib.platform.NAME', 'windows')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entries = policy.heavy_programs()

    def test_the_windows_defaults_are_the_same_list(self):
        self.assertEqual([e['program'] for e in self.entries], WINDOWS_DEFAULTS)
        self.assertEqual(self.entries, list(policy.HEAVY_DEFAULTS))

    def test_windows_results_do_not_change(self):
        for prog, args, kind in (
                ('godot.exe', [], 'app-run'), ('C:\\Tools\\Godot_v4.4-stable_mono_win64.exe', [], 'app-run'),
                ('Godot_v4.3-stable_win64_console.exe', ['--headless'], 'app-run'),
                ('godot_mono', [], 'app-run'), ('Godot.AppImage', [], 'app-run'),
                ('blender.exe', ['-b', 'x.blend'], 'render'), ('blender', ['--background'], 'render'),
                ('godot4.exe', [], None), ('godot4', [], None), ('Godot4.exe', ['--path', 'game'], None),
                ('godot3.exe', [], None), ('godotenv.exe', [], None), ('blender.exe', ['x.blend'], None),
                ('org.godotengine.Godot', [], None), ('flatpak', ['run', 'org.godotengine.Godot'], None),
                ('flatpak', ['run', 'org.blender.Blender', '-b'], None),
                ('org.blender.Blender', ['-b', 'x.blend'], None)):
            entry = policy.heavy_match(prog, args, self.entries)
            self.assertEqual(entry and entry['kind'], kind, (prog, args))


class RolesTest(TreeCase):
    """Roles are read off a kitlib.host.Action; ev() builds one from a Claude Code payload through
    Claude's adapter, so these still pin what each real payload means."""

    def test_the_role_is_read_off_the_neutral_action(self):
        self.assertEqual(policy.role_name(Action('edit')), 'main')
        self.assertEqual(policy.role_name(Action('edit', agent='a', agent_role='lander'),
                                          {'lander': {}, 'subagent': {}}), 'lander')
        self.assertEqual(policy.role_name(Action('edit', agent_role='lander'), {'lander': {}}), 'main')

    def test_the_main_session_and_a_subagent(self):
        self.assertEqual(policy.role_name(ev({})), 'main')
        self.assertEqual(policy.role_name(None), 'main')
        self.assertEqual(policy.role_name(ev({'agent_id': 'a1', 'agent_type': 'worker'})), 'subagent')
        self.assertTrue(policy.permissions(ev({}))['edit_main_checkout'])
        self.assertFalse(policy.permissions(ev({'agent_id': 'a1'}))['edit_main_checkout'])

    def test_a_roles_key_under_a_renamed_roles_old_or_new_name(self):
        """kitlib.content.ALIASES: a .kit.json roles key `opus-worker` holds the agent now called
        senior-worker, and a key `senior-worker` an agent still sent as opus-worker."""
        for key, kind in (('opus-worker', 'senior-worker'), ('senior-worker', 'opus-worker'),
                          ('senior-worker', 'senior-worker'), ('opus-worker', 'opus-worker')):
            with self.subTest(key=key, kind=kind):
                table = {key: {}, 'subagent': {}}
                self.assertEqual(policy.role_name(Action('edit', agent='a', agent_role=kind), table), key)
        both = {'opus-worker': {}, 'senior-worker': {}}
        self.assertEqual(policy.role_name(Action('edit', agent='a', agent_role='opus-worker'), both), 'opus-worker')
        self.assertEqual(policy.role_name(Action('edit', agent='a', agent_role='worker'), {'opus-worker': {}}),
                         'subagent')

    def test_an_agent_cannot_claim_the_main_role(self):
        self.assertEqual(policy.role_name(ev({'agent_id': 'a1', 'agent_type': 'main'})), 'subagent')

    def test_repo_roles_lay_over_the_defaults(self):
        cfg = self.load({'roles': {'lander': {'edit_main_checkout': True, 'why': 'landers merge there'},
                                   'records': {'why': 'docs only'},
                                   'main': {'edit_main_checkout': False, 'why': 'locked down'},
                                   '_about': 'notes allowed'}})
        table = policy.roles(cfg)
        self.assertNotIn('_about', table)
        self.assertTrue(policy.permissions(ev({'agent_id': 'a', 'agent_type': 'lander'}), table)['edit_main_checkout'])
        self.assertFalse(policy.permissions(ev({'agent_id': 'a', 'agent_type': 'records'}), table)['edit_main_checkout'])
        self.assertFalse(policy.permissions(ev({}), table)['edit_main_checkout'])
        self.assertEqual(policy.role_name(ev({'agent_id': 'a', 'agent_type': 'worker'}), table), 'subagent')

    def test_the_plugin_prefixed_agent_type_names_the_same_role(self):
        # docs/guard.md "Agent names": the kit plugin bundles its agents as <plugin>:<name>
        # (kitlib.agentname.PREFIX; tests/agentname pins it to this value)
        p = lockmcp.PLUGIN_NAME + ':'
        table = policy.roles(self.load({'roles': {'lander': {'edit_main_checkout': True, 'why': 'merges'}}}))
        self.assertEqual(policy.role_name(ev({'agent_id': 'a', 'agent_type': p + 'lander'}), table), 'lander')
        self.assertTrue(policy.permissions(ev({'agent_id': 'a', 'agent_type': p + 'lander'}), table)['edit_main_checkout'])
        for kind in ('other:lander', p + p + 'lander', p.upper() + 'lander', p, p + 'main',
                     p.rstrip(':') + 'lander', 'x' + p + 'lander', p[1:] + 'lander'):
            with self.subTest(kind):
                self.assertEqual(policy.role_name(ev({'agent_id': 'a', 'agent_type': kind}), table), 'subagent')
                self.assertFalse(policy.permissions(ev({'agent_id': 'a', 'agent_type': kind}), table)['edit_main_checkout'])
        self.assertEqual(policy.role_name(ev({'agent_type': p + 'lander'}), table), 'main')

    def test_a_role_with_an_unknown_permission_refuses(self):
        t = TempTree({'roles': {'lander': {'edit_main': True}}})
        self.addCleanup(t.cleanup)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(t.root)
        self.assertIn('unknown key(s) edit_main', str(cm.exception))


class KindsTableTest(unittest.TestCase):

    def write(self, data):
        fd, path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        self.addCleanup(os.remove, path)
        return path

    def test_the_default_path_is_the_lock_packages_file(self):
        self.assertEqual(os.path.normcase(policy.KINDS_FILE), os.path.normcase(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(policy.__file__))), 'lock', 'kinds.json')))

    def test_a_missing_table_refuses(self):
        with self.assertRaises(config.ConfigError) as cm:
            policy.kinds_table(os.path.join(tempfile.gettempdir(), 'kit-no-such-kinds.json'))
        self.assertIn('no kinds table', str(cm.exception))

    def test_a_flat_or_wrapped_table_reads_with_notes_dropped(self):
        flat = {'_about': 'x', 'engine-test': {'slot': 'cpu'}, 'render': {'slot': 'gpu'}}
        self.assertEqual(sorted(policy.kinds_table(self.write(flat))), ['engine-test', 'render'])
        wrapped = {'schema': 1, 'kinds': {'build': {'slot': 'cpu'}}}
        self.assertEqual(policy.kinds_table(self.write(wrapped)), {'build': {'slot': 'cpu'}})

    def test_a_bad_table_refuses(self):
        for bad in ('{', '[1]', '{"build": 3}'):
            with self.assertRaises(config.ConfigError):
                policy.kinds_table(self.write(bad))

    def test_aliases_map_a_retired_name_to_its_kind(self):
        data = {'kinds': {'graph-build': {'cpu': 'heavy'}},
                'aliases': {'_note': 'n', 'old-name': {'to': 'graph-build', 'why': 'retired'}}}
        path = self.write(data)
        self.assertEqual(policy.kinds_aliases(path), {'old-name': 'graph-build'})
        self.assertEqual(sorted(policy.kinds_table(path)), ['graph-build'])  # not a kind itself

    def test_no_aliases_or_no_file_is_none(self):
        self.assertEqual(policy.kinds_aliases(self.write({'kinds': {'b': {}}})), {})
        self.assertEqual(policy.kinds_aliases(
            os.path.join(tempfile.gettempdir(), 'kit-no-such-kinds.json')), {})

    def test_a_bad_alias_refuses(self):
        for bad in ({'a': 'b'}, {'a': {'to': 'b'}}, {'a': {'why': 'w'}}, ['a']):
            with self.assertRaises(config.ConfigError):
                policy.kinds_aliases(self.write({'kinds': {'b': {}}, 'aliases': bad}))

    def test_an_alias_that_is_a_kind_or_names_no_kind_refuses(self):
        kinds = {'a': {}, 'b': {}}
        for aliases in ({'a': {'to': 'b', 'why': 'w'}},          # the alias is a kind
                        {'x': {'to': 'nope', 'why': 'w'}},       # to is not a kind
                        {'x': {'to': 'y', 'why': 'w'}, 'y': {'to': 'a', 'why': 'w'}}):   # chain
            with self.assertRaises(config.ConfigError):
                policy.kinds_aliases(self.write({'kinds': kinds, 'aliases': aliases}))

    def test_aliases_is_never_a_kind_in_the_flat_form(self):
        flat = {'engine-test': {'cpu': 1}, 'aliases': {'old': {'to': 'engine-test', 'why': 'w'}}}
        path = self.write(flat)
        self.assertEqual(sorted(policy.kinds_table(path)), ['engine-test'])
        self.assertEqual(policy.kinds_aliases(path), {'old': 'engine-test'})

    def test_aliases_inside_the_kinds_object_refuses(self):
        path = self.write({'kinds': {'a': {}, 'aliases': {'x': {'to': 'a', 'why': 'w'}}}})
        with self.assertRaisesRegex(config.ConfigError, 'aliases belongs at the top level'):
            policy.kinds_table(path)


class HookModuleTest(TreeCase):

    def test_no_hook_named_is_none(self):
        self.assertIsNone(policy.load_hook_module(self.load({})))

    def test_the_named_module_loads_with_its_options(self):
        cfg = self.load({'repo': {'tools_dir': 'scripts'},
                         'hook': {'module': 'repo_hook', 'options': {'lanes': 2}}},
                        {'scripts/repo_hook.py': 'def gate_steps(cfg):\n    return ["x"]\n'})
        module, options = policy.load_hook_module(cfg)
        self.assertEqual(module.gate_steps(None), ['x'])
        self.assertEqual(options, {'lanes': 2})

    def test_a_named_module_that_is_missing_refuses(self):
        with self.assertRaises(config.ConfigError) as cm:
            policy.load_hook_module(self.load({'hook': {'module': 'repo_hook'}}))
        self.assertIn('does not exist', str(cm.exception))

    def test_a_module_that_fails_to_import_refuses_and_names_the_failure(self):
        cfg = self.load({'hook': {'module': 'repo_hook'}},
                        {'tools/repo_hook.py': 'raise RuntimeError("boom")\n'})
        with self.assertRaises(config.ConfigError) as cm:
            policy.load_hook_module(cfg)
        self.assertIn('RuntimeError: boom', str(cm.exception))
        self.assertFalse([n for n, m in list(sys.modules.items())
                          if n.startswith('kit_repo_hook_') and getattr(m, '__file__', '').startswith(cfg.root)])

    def loaded_from(self, cfg):
        return [n for n, m in list(sys.modules.items())
                if n.startswith('kit_repo_hook_') and getattr(m, '__file__', '').startswith(cfg.root)]

    def test_a_module_that_exits_while_it_loads_refuses_and_is_not_left_loaded(self):
        cfg = self.load({'hook': {'module': 'repo_hook'}},
                        {'tools/repo_hook.py': 'import sys\nsys.exit(3)\n'})
        with self.assertRaises(config.ConfigError) as cm:
            policy.load_hook_module(cfg)
        self.assertIn('SystemExit: 3', str(cm.exception))
        self.assertFalse(self.loaded_from(cfg))

    def test_ctrl_c_while_it_loads_is_raised_and_the_module_not_left_loaded(self):
        cfg = self.load({'hook': {'module': 'repo_hook'}},
                        {'tools/repo_hook.py': 'raise KeyboardInterrupt\n'})
        with self.assertRaises(KeyboardInterrupt):
            policy.load_hook_module(cfg)
        self.assertFalse(self.loaded_from(cfg))

    def test_the_module_is_in_sys_modules_while_it_runs(self):
        cfg = self.load({'hook': {'module': 'repo_hook'}},
                        {'tools/repo_hook.py': 'import dataclasses\n\n\n@dataclasses.dataclass\n'
                                               'class Lane:\n    name: str\n'})
        module, _ = policy.load_hook_module(cfg)
        self.addCleanup(sys.modules.pop, module.__name__, None)
        self.assertIs(sys.modules[module.__name__], module)
        self.assertEqual(module.Lane('x').name, 'x')


HOOK = ('def pin_check(*args):\n    return ("pass", args)\n\n\n'
        'GATE_CHECKS = {"pin_check": pin_check}\n')
CHECK_STEP = {'name': 'pin-check', 'always': True, 'check': 'pin_check'}


class GateChecksTest(TreeCase):
    """Repo gate checks live in the hook module's GATE_CHECKS."""

    def hooked(self, body, steps=(), options=None):
        hook = {'module': 'repo_hook'}
        if options is not None:
            hook['options'] = options
        return self.load({'hook': hook, 'gate': {'steps': list(steps)}}, {'tools/repo_hook.py': body})

    def test_no_hook_is_no_checks(self):
        self.assertEqual(policy.load_gate_checks(self.load({})), ({}, {}))

    def test_a_hook_without_gate_checks_is_no_checks_and_its_options(self):
        cfg = self.hooked('X = 1\n', options={'lanes': 2})
        self.assertEqual(policy.load_gate_checks(cfg), ({}, {'lanes': 2}))

    def test_a_step_check_is_found_and_callable(self):
        cfg = self.hooked(HOOK, [CHECK_STEP], {'gradle': 'x'})
        checks, options = policy.load_gate_checks(cfg)
        self.assertEqual(sorted(checks), ['pin_check'])
        self.assertEqual(checks['pin_check'](1, 2), ('pass', (1, 2)))
        self.assertEqual(options, {'gradle': 'x'})

    def test_a_step_naming_a_check_the_module_lacks_refuses(self):
        cfg = self.hooked(HOOK, [CHECK_STEP, {'name': 'drops', 'always': True, 'check': 'py_drops'}])
        with self.assertRaises(config.ConfigError) as cm:
            policy.load_gate_checks(cfg)
        self.assertIn('provides no gate check py_drops (step drops)', str(cm.exception))

    def test_a_malformed_gate_checks_refuses(self):
        for body, why in (('GATE_CHECKS = [1]\n', 'must be a dict'),
                          ('GATE_CHECKS = {"../x": print}\n', "key '../x' is not a check name"),
                          ('GATE_CHECKS = {3: print}\n', 'key 3 is not a check name'),
                          ('GATE_CHECKS = {"pins": 3}\n', "GATE_CHECKS['pins'] is not callable")):
            with self.assertRaises(config.ConfigError) as cm:
                policy.load_gate_checks(self.hooked(body))
            self.assertIn(why, str(cm.exception))

    def test_the_package_exports_the_loaders(self):
        self.assertIs(config.load_gate_checks, policy.load_gate_checks)
        self.assertIs(config.gate_checks, policy.gate_checks)


if __name__ == '__main__':
    unittest.main()
