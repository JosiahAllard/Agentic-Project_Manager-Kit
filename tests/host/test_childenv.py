"""kitlib.sessionmark.child_environment: a session launched on one host inherits neither the
launching session's mark nor another host's session variables (docs/multi-host-plan.md 2.1). The
hosts come from a test copy of hosts.json with a second host, so nothing here names the kit's."""
import copy
import json
import os
import shutil
import tempfile
import unittest

from kitlib import sessionmark
from kitlib.host import registry

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ChildEnvironment(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix='childenv-')
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
            data = json.load(f)
        first = registry.default()
        other = copy.deepcopy(data[first])
        other.pop('default')
        other['profile'].update(session_env='OTHER_SESSION', env_prefixes=['OTHER_'])
        data = {first: data[first], 'other': other}
        data[first]['profile'].update(session_env='FIRST_SESSION', env_prefixes=['FIRST_', 'VENDOR_'])
        self.first = first
        self.path = os.path.join(folder, 'hosts.json')
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        self.env = {'PATH': 'p', 'PM_KIT_ROLE': 'coordinator', 'pm_kit_session_name': 'n',
                    'FIRST_SESSION': 's1', 'FIRST_CONFIG_DIR': 'c', 'VENDOR_API_KEY': 'k',
                    'OTHER_SESSION': 's2', 'OTHER_HOME': 'h', 'MACHINE_LOCK_RUN': 'r:1:2'}

    def child(self, **kw):
        return sessionmark.child_environment(self.env, registry_path=self.path, **kw)

    def test_a_child_on_the_other_host_keeps_only_its_own_variables(self):
        self.assertEqual(self.child(host='other'), {'PATH': 'p', 'OTHER_HOME': 'h'})

    def test_a_child_on_the_first_host_keeps_its_own(self):
        self.assertEqual(self.child(host=self.first), {'PATH': 'p', 'FIRST_CONFIG_DIR': 'c', 'VENDOR_API_KEY': 'k'})

    def test_no_host_drops_every_hosts_variables(self):
        self.assertEqual(self.child(), {'PATH': 'p'})

    def test_the_lock_token_stays_only_when_asked(self):
        self.assertEqual(self.child(host='other', preserve_lock=True)['MACHINE_LOCK_RUN'], 'r:1:2')

    def test_additions_come_last_and_the_input_is_untouched(self):
        before = dict(self.env)
        out = self.child(host='other', additions={'PM_KIT_ROLE': 'worker', 'PM_KIT_HOST': 'other'})
        self.assertEqual((out['PM_KIT_ROLE'], out['PM_KIT_HOST']), ('worker', 'other'))
        self.assertEqual(self.env, before)

    def test_an_unknown_host_is_refused(self):
        with self.assertRaises(registry.RegistryError):
            self.child(host='nope')

    def test_the_kit_registry_declares_prefixes_for_every_host(self):
        for h in registry.load().values():
            self.assertTrue(h.profile.env_prefixes, h.name)


if __name__ == '__main__':
    unittest.main()
