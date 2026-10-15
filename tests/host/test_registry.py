"""kitlib.host.registry: hosts.json checked whole, PM_KIT_HOST selection, --host precedence."""
import copy
import json
import os
import shutil
import tempfile
import unittest

from kitlib.host import HostCapabilities, HostProfile, registry

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def kit_hosts():
    """The kit's hosts.json with its first (default) host alone: the base the cases below vary."""
    with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
        first, entry = next(iter(json.load(f).items()))
    return {first: entry}


def second(data, name='other', **changes):
    """`data` plus a non-default copy of its first host named `name`."""
    entry = copy.deepcopy(next(iter(data.values())))
    entry.pop('default', None)
    entry.update(changes)
    data = copy.deepcopy(data)
    data[name] = entry
    return data


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='hosts-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'hosts.json')

    def put(self, data, raw=None):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(raw if raw is not None else json.dumps(data))
        return self.path


class KitRegistry(unittest.TestCase):
    def test_the_kit_hosts_json_loads_and_its_default_is_the_back_compat_host(self):
        hosts = registry.load()
        self.assertEqual(registry.PATH, os.path.join(ROOT, 'hosts.json'))
        self.assertEqual(len([h for h in hosts.values() if h.default]), 1)
        self.assertEqual(registry.default(), 'claude')
        claude = hosts['claude']
        self.assertEqual(claude.adapter, 'hosts.claude.adapter')
        self.assertEqual(claude.shell, 'hosts/claude/shell')
        self.assertEqual(claude.platforms, ('windows',))
        self.assertIsInstance(claude.capabilities, HostCapabilities)
        self.assertIsInstance(claude.profile, HostProfile)
        self.assertEqual(claude.profile.worktrees_directory, '.claude/worktrees')
        self.assertEqual(dict(claude.tiers), {'light': 'haiku', 'standard': 'sonnet',
                                              'strong': 'opus', 'deep': 'opus'})

    def test_every_host_shell_is_a_folder_of_the_kit(self):
        for h in registry.load().values():
            with self.subTest(host=h.name):
                self.assertTrue(os.path.isdir(os.path.join(ROOT, h.shell)), h.shell)


class KitPath(Case):
    """hosts.json is found beside the real kitlib when the spelled one has none: a machine's
    kitlib reached through a link runs from the link's spelling."""

    def test_a_linked_kitlib_finds_the_kits_hosts_json(self):
        tools = os.path.join(self.dir, 'tools')
        os.makedirs(tools)
        link = os.path.join(tools, 'kitlib')
        try:
            os.symlink(os.path.join(ROOT, 'kitlib'), link, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest('no directory link here: %s' % e)
        here = os.path.join(link, 'host', 'registry.py')
        self.assertEqual(os.path.normcase(registry.kit_path(here)),
                         os.path.normcase(os.path.join(os.path.realpath(ROOT), 'hosts.json')))
        self.put(kit_hosts())                             # one beside the link wins
        os.replace(self.path, os.path.join(tools, 'hosts.json'))
        self.assertEqual(registry.kit_path(here), os.path.join(tools, 'hosts.json'))

    def test_none_anywhere_is_the_spelled_path(self):
        here = os.path.join(self.dir, 'kitlib', 'host', 'registry.py')
        self.assertEqual(registry.kit_path(here), os.path.join(self.dir, 'hosts.json'))


class Validation(Case):
    def refused(self, data=None, raw=None):
        with self.assertRaises(registry.RegistryError):
            registry.load(self.put(data, raw))

    def test_a_good_file_with_two_hosts_loads_in_order(self):
        path = self.put(second(kit_hosts()))
        self.assertEqual(registry.names(path), ('claude', 'other'))
        self.assertFalse(registry.host('other', path).default)

    def test_missing_unreadable_or_empty_files_are_refused(self):
        with self.assertRaises(registry.RegistryError):
            registry.load(os.path.join(self.dir, 'none.json'))
        self.refused(raw='{not json')
        self.refused(raw='{"claude": {}, "claude": {}}')
        self.refused({})
        self.refused([])

    def test_exactly_one_default(self):
        data = kit_hosts()
        none = copy.deepcopy(data)
        del none['claude']['default']
        self.refused(none)
        two = second(data)
        two['other']['default'] = True
        self.refused(two)
        bad = copy.deepcopy(data)
        bad['claude']['default'] = 'yes'
        self.refused(bad)

    def test_entry_shape_is_exact(self):
        data = kit_hosts()
        for change in (
                lambda e: e.pop('adapter'),
                lambda e: e.update(extra=1),
                lambda e: e.update(adapter='hosts/claude/adapter'),
                lambda e: e.update(adapter=''),
                lambda e: e.update(shell='../outside'),
                lambda e: e.update(shell='C:/abs'),
                lambda e: e.pop('platforms'),
                lambda e: e.update(platforms=[]),
                lambda e: e.update(platforms=['windows', 'windows']),
                lambda e: e.update(platforms=['amiga']),
                lambda e: e['capabilities'].pop('mcp'),
                lambda e: e['capabilities'].update(supports_ask='true'),
                lambda e: e['capabilities'].update(hook_events=[]),
                lambda e: e['profile'].update(role_environment='PM_KIT_ROLE'),
                lambda e: e['profile'].update(worktrees_directory='/abs'),
                lambda e: e['profile'].update(process_names='claude.exe'),
                lambda e: e['profile'].pop('session_env'),
                lambda e: e['profile'].update(session_env='NOT A NAME'),
                lambda e: e['profile'].update(session_env=''),
                lambda e: e['profile'].pop('skills_directory'),
                lambda e: e['profile'].update(skills_directory='../skills'),
                lambda e: e['profile'].pop('attribution'),
                lambda e: e['profile']['attribution'].pop('model_pattern'),
                lambda e: e['profile']['attribution'].update(email='a b@c'),
                lambda e: e['profile']['attribution'].update(name='Two  spaces'),
                lambda e: e['profile']['attribution'].update(model_env='NOT A NAME'),
                lambda e: e['profile']['attribution'].update(model_pattern='('),
                lambda e: e['profile']['attribution'].update(model_pattern=''),
                lambda e: e['profile']['attribution'].update(session_trailer={}),
                lambda e: e['profile']['attribution'].update(session_trailer='Claude-Session'),
                lambda e: e['profile']['attribution']['session_trailer'].pop('env'),
                lambda e: e['profile']['attribution']['session_trailer'].update(extra=1),
                lambda e: e['profile']['attribution']['session_trailer'].update(key='Two Words'),
                lambda e: e['profile']['attribution']['session_trailer'].update(key='Ends:'),
                lambda e: e['profile']['attribution']['session_trailer'].update(key=''),
                lambda e: e['profile']['attribution']['session_trailer'].update(env='NOT A NAME'),
                lambda e: e['profile']['attribution']['session_trailer'].update(env=None),
                lambda e: e['profile']['attribution']['session_trailer'].update(url='https://x/'),
                lambda e: e['profile']['attribution']['session_trailer'].update(url='{id}{id}'),
                lambda e: e['profile']['attribution']['session_trailer'].update(url='{id}/{other}'),
                lambda e: e['profile']['attribution']['session_trailer'].update(url='a {id}'),
                lambda e: e['profile'].pop('agent_name_format'),
                lambda e: e['profile'].update(agent_name_format='{plugin}:'),
                lambda e: e['profile'].update(agent_name_format='{name}:{plugin}'),
                lambda e: e['profile'].update(agent_name_format='{other}:{name}'),
                lambda e: e['profile'].update(agent_name_format='{name}{name}'),
                lambda e: e['profile'].pop('session_process'),
                lambda e: e['profile'].update(session_process=''),
                lambda e: e['profile'].pop('env_prefixes'),
                lambda e: e['profile'].update(env_prefixes=[]),
                lambda e: e['profile'].update(env_prefixes=['claude_']),
                lambda e: e['profile'].update(env_prefixes=['CLAUDE']),
                lambda e: e.pop('hook'),
                lambda e: e['hook'].update(extra=1),
                lambda e: e['hook'].update(tool_event='Nope'),
                lambda e: e['hook'].update(tool_field='a..b'),
                lambda e: e['hook'].update(tool_field=''),
                lambda e: e['hook'].update(guarded_tools=[]),
                lambda e: e['hook'].update(guarded_tools=['*']),
                lambda e: e['hook'].update(guarded_tools=['a*b']),
                lambda e: e['hook'].update(guarded_tools=['Bash', 'Bash']),
                lambda e: e['hook'].update(fast_path='sometimes'),
                lambda e: e['tiers'].pop('deep'),
                lambda e: e['tiers'].update(light='')):
            bad = copy.deepcopy(data)
            change(bad['claude'])
            with self.subTest(entry=bad['claude']):
                self.refused(bad)

    def test_the_hook_data_is_read_and_matches_names_and_prefixes(self):
        hook = registry.host('claude', self.put(kit_hosts())).hook
        self.assertIsInstance(hook, registry.HookSpec)
        self.assertEqual((hook.tool_event, hook.tool_field, hook.fast_path), ('PreToolUse', 'tool_name', 'all'))
        self.assertTrue(hook.guarded('Bash'))
        self.assertTrue(hook.guarded('mcp__x__y'))
        self.assertFalse(hook.guarded('Read'))
        self.assertFalse(hook.guarded('Bash2'))
        self.assertFalse(hook.guarded('mcp_x'))

    def test_host_names_are_lowercase_words(self):
        self.refused(second(kit_hosts(), 'Other'))
        self.refused(second(kit_hosts(), 'a b'))


class Profiles(Case):
    def test_profiles_and_union_cover_every_host_in_file_order(self):
        data = second(kit_hosts())
        data['other']['profile']['process_names'] = ['other.exe', 'node.exe']
        data['other']['profile']['session_env'] = 'OTHER_SESSION'
        path = self.put(data)
        self.assertEqual(len(registry.profiles(path)), 2)
        self.assertEqual(registry.union('process_names', path), ('claude.exe', 'node.exe', 'other.exe'))
        self.assertEqual(registry.union('session_env', path), ('CLAUDE_CODE_SESSION_ID', 'OTHER_SESSION'))

    def test_the_kit_registry_names_claudes_session_trailer_and_codex_has_none(self):
        hosts = registry.load()
        st = hosts['claude'].profile.attribution.session_trailer
        self.assertEqual((st.key, st.env, st.url), ('Claude-Session', 'CLAUDE_CODE_BRIDGE_SESSION_ID',
                                                    'https://claude.ai/code/{id}'))
        self.assertIsNone(hosts['codex'].profile.attribution.session_trailer)

    def test_the_session_trailer_is_optional_and_null_is_none(self):
        for change in (lambda a: a.pop('session_trailer', None),
                       lambda a: a.update(session_trailer=None)):
            data = kit_hosts()
            change(data['claude']['profile']['attribution'])
            with self.subTest(attribution=data['claude']['profile']['attribution']):
                self.assertIsNone(registry.host('claude', self.put(data)).profile
                                  .attribution.session_trailer)

    def test_the_kit_registry_names_claudes_session_variable(self):
        self.assertEqual(registry.union('session_env'), ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'))

    def test_the_default_profile_and_the_host_folders(self):
        data = second(kit_hosts())
        data['other']['profile']['owner_paths'] = ['OTHER.md', '.other/', '.other/x/', '.claude/']
        path = self.put(data)
        self.assertEqual(registry.default_profile(path), registry.host('claude', path).profile)
        self.assertEqual(registry.host_folders(path), ('.claude', '.other'))
        self.assertEqual(registry.host_folders(), ('.claude', '.codex'))
        self.assertEqual(registry.default_profile().worktrees_directory, '.claude/worktrees')

    def test_the_kit_state_folder_is_the_default_hosts(self):
        data = second(kit_hosts())
        data['other']['profile']['state_directory'] = '.other'
        path = self.put(data)
        home = os.path.join(self.dir, 'home')
        self.assertEqual(registry.state_path(home, 'machine-lock', path=path),
                         os.path.join(home, '.claude', 'machine-lock'))
        self.assertEqual(registry.state_path(home, 'tools', 'x.py'),
                         os.path.join(home, '.claude', 'tools', 'x.py'))
        self.assertEqual(registry.default_profile().skills_directory, '.claude/skills')


class Selection(Case):
    def setUp(self):
        super().setUp()
        self.put(second(kit_hosts()))

    def test_unset_or_blank_is_the_default_host(self):
        self.assertEqual(registry.selected({}, self.path), 'claude')
        self.assertEqual(registry.selected({registry.ENV: '  '}, self.path), 'claude')

    def test_pm_kit_host_names_a_registered_host(self):
        self.assertEqual(registry.selected({registry.ENV: 'other'}, self.path), 'other')
        self.assertEqual(registry.selected({registry.ENV: ' other '}, self.path), 'other')
        with self.assertRaises(registry.RegistryError):
            registry.selected({registry.ENV: 'nobody'}, self.path)

    def test_a_flag_wins_over_the_environment(self):
        env = {registry.ENV: 'other'}
        self.assertEqual(registry.choose('claude', env, self.path), 'claude')
        self.assertEqual(registry.choose(None, env, self.path), 'other')
        with self.assertRaises(registry.RegistryError):
            registry.choose('nobody', env, self.path)

    def test_the_role_marker_is_not_the_host_selector(self):
        self.assertEqual(registry.selected({'PM_KIT_ROLE': 'other'}, self.path), 'claude')


if __name__ == '__main__':
    unittest.main()
