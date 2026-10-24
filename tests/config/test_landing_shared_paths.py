"""kitlib.config.schema: "landing".shared_paths, the globs whose change makes a landing need a
--full record of a tip (docs/gate.md "Shared paths"): unset by default, a non-empty list of
whole-path globs (none ending in "/") when set, and left out of the gate config a gate-OK record hashes."""
import unittest

from kitlib import config
from kitlib.config import schema
from kitlib.gate import runner
from tests.config.helpers import TempTree


class SharedPaths(unittest.TestCase):

    def load(self, kit=None):
        t = TempTree(kit)
        self.addCleanup(t.cleanup)
        return config.load(t.root)

    def refused(self, kit):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(kit)
        return str(cm.exception)

    def test_unset_by_default(self):
        self.assertIsNone(self.load().section('landing')['shared_paths'])

    def test_a_list_of_globs_loads(self):
        cfg = self.load({'landing': {'shared_paths': ['tools/*', '.kit.json']}})
        self.assertEqual(cfg.section('landing')['shared_paths'], ['tools/*', '.kit.json'])

    def test_an_empty_list_is_a_config_error(self):
        self.assertIn('"landing".shared_paths', self.refused({'landing': {'shared_paths': []}}))

    def test_a_string_is_a_config_error(self):
        self.assertIn('"landing".shared_paths', self.refused({'landing': {'shared_paths': 'tools/*'}}))

    def test_a_folder_entry_ending_in_a_slash_is_a_config_error(self):
        # "api/" would match no path under the folder: entries are whole-path globs
        for entry in ('api/', 'api\\'):
            why = self.refused({'landing': {'shared_paths': ['tools/*', entry]}})
            self.assertIn('"landing".shared_paths', why)
            self.assertIn('api/*', why)

    def test_it_does_not_change_the_gate_config_hash(self):
        gate = {'steps': [{'name': 's', 'always': True, 'cmd': ['x']}]}
        plain = runner.gate_config(self.load({'gate': gate}))
        shared = runner.gate_config(self.load({'gate': gate, 'landing': {'shared_paths': ['a/*']}}))
        self.assertEqual(plain, shared)

    def test_the_doc_line_names_it(self):
        self.assertTrue([l for l in schema.doc_lines() if l.startswith('- `landing.shared_paths` ')])


if __name__ == '__main__':
    unittest.main()
