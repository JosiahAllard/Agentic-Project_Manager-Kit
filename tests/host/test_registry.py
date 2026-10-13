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
    with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
        return json.load(f)


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
        self.assertEqual(claude.adapter, 'claude.adapter')
        self.assertEqual(claude.shell, 'claude')
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
                lambda e: e.update(adapter='claude/adapter'),
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
                lambda e: e['tiers'].pop('deep'),
                lambda e: e['tiers'].update(light='')):
            bad = copy.deepcopy(data)
            change(bad['claude'])
            with self.subTest(entry=bad['claude']):
                self.refused(bad)

    def test_host_names_are_lowercase_words(self):
        self.refused(second(kit_hosts(), 'Other'))
        self.refused(second(kit_hosts(), 'a b'))


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
