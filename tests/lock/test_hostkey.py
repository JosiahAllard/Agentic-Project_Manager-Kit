"""The lock's host facts come from hosts.json, and the actor's agent host is under "host".

The agent process images and the session variable are each registered host's profile
process_names and session_env (kitlib.host.registry), not names the lock spells. The actor key
"claude" became "host": an actor that still carries the old key (made before the rename) is read
the same, and the new key is the one written. Pure functions only: no store, no process tree."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import kitlib.platform
from kitlib.host import registry
from kitlib.lock import api
from kitlib.lock import store
from kitlib.proc import process

from .helpers import OWNER, REPO, agent


def legacy(actor):
    """`actor` as it was made before the rename: its agent host under "claude"."""
    out = dict(actor)
    out['claude'] = out.pop('host')
    return out


class LegacyActorKey(unittest.TestCase):
    def test_an_actor_with_an_agent_host_under_either_key_is_not_the_owner(self):
        self.assertTrue(api.default_owner_check(OWNER))
        self.assertTrue(api.default_owner_check(legacy(OWNER)))
        self.assertFalse(api.default_owner_check(dict(OWNER, host=[4, 1])))
        self.assertFalse(api.default_owner_check(legacy(dict(OWNER, host=[4, 1]))))

    def test_a_legacy_actor_owns_its_records_and_no_other(self):
        mine = {'ancestors': [agent('s')['host']]}
        self.assertTrue(store.owns(legacy(agent('s')), mine))
        self.assertFalse(store.owns(legacy(agent('t')), mine))

    def test_a_legacy_actor_ends_only_its_own_agent_hold(self):
        hold = {'id': 'h-1', 'owner': 'agent', 'ancestors': [agent('s')['host']]}
        self.assertIsNone(api.authorize(legacy(agent('s')), 'hold_end', hold))
        self.assertIsNotNone(api.authorize(legacy(agent('t')), 'hold_end', hold))

    def test_the_new_key_is_read_first(self):
        both = dict(agent('s'), claude=[1, 1])
        self.assertEqual(store.actor_host(both), agent('s')['host'])
        self.assertEqual(store.actor_host(legacy(agent('s'))), agent('s')['host'])
        self.assertIsNone(store.actor_host({'role': 'owner'}))
        self.assertIsNone(store.actor_host(None))


class HostRegistry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='lock-hosts-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def registry(self, **profile):
        with open(os.path.join(REPO, 'hosts.json'), encoding='utf-8') as f:
            data = json.load(f)
        next(iter(data.values()))['profile'].update(profile)
        path = os.path.join(self.dir, 'hosts.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return path

    def on(self, name):
        patch = mock.patch.object(kitlib.platform, 'NAME', name)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_kits_registry_keeps_todays_agent_images(self):
        self.on('windows')
        self.assertEqual(store.agent_hosts(), frozenset({'claude.exe', 'node.exe', 'codex.exe'}))

    def test_on_linux_the_agent_images_are_the_registrys_linux_names(self):
        self.on('linux')
        self.assertEqual(store.agent_hosts(), frozenset({'claude', 'node', 'codex'}))

    def test_no_names_for_this_platform_raises_so_each_caller_fails_closed(self):
        me = [os.getpid(), process.own_ctime()]
        self.on('unsupported')
        with self.assertRaises(registry.RegistryError):
            store.agent_hosts()
        # A caller reads that as an agent: this test process is no agent host and no WMI start,
        # so with an empty name set it would pass as the owner.
        self.assertEqual(store.tree_role([me]), 'agent')

    def test_agent_images_follow_the_registry_lower_cased(self):
        self.on('windows')
        path = self.registry(process_names={'windows': ['Other.EXE']})
        self.assertEqual(store.agent_hosts(path),
                         frozenset({'other.exe', 'codex.exe'}))     # the first host changed, Codex's kept
        chain = [(1, 11), (2, 22)]
        hosts = store.agent_hosts(path)
        self.assertEqual(store.chain_host(chain, {1: 'claude.exe', 2: 'OTHER.exe'}, hosts), [2, 22])

    def test_an_unreadable_registry_raises_so_each_caller_fails_closed(self):
        with self.assertRaises(registry.RegistryError):
            store.agent_hosts(os.path.join(self.dir, 'none.json'))

    def test_the_session_comes_from_the_registered_variable(self):
        path = self.registry(session_env='OTHER_SESSION')
        self.assertEqual(store.session_of({'OTHER_SESSION': 's-1'}, path), 's-1')
        self.assertIsNone(store.session_of({'CLAUDE_CODE_SESSION_ID': 's-2'}, path))
        self.assertIsNone(store.session_of({'OTHER_SESSION': ''}, path))
        self.assertEqual(store.session_of({'CLAUDE_CODE_SESSION_ID': 's-2'}), 's-2')
        self.assertIsNone(store.session_of({'X': 'y'}, os.path.join(self.dir, 'none.json')))


if __name__ == '__main__':
    unittest.main()
