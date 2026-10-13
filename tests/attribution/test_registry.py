"""Attribution comes from hosts.json: each host's profile "attribution" names its trailer, email,
model variable and model form, and a provider is a host name. Claude's trailers are exactly what
they were; Codex's stays available, unregistered, until step 9 registers that host."""
import copy
import json
import os
import shutil
import tempfile
import unittest

from kitlib import attribution
from kitlib.host import Attribution, registry

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class KitHosts(unittest.TestCase):
    def test_claudes_attribution_is_data(self):
        a = registry.host('claude').profile.attribution
        self.assertEqual(a, Attribution('Claude', 'noreply@anthropic.com', 'CLAUDE_AGENT_MODEL', None))
        self.assertEqual(attribution.model_environment(), 'CLAUDE_AGENT_MODEL')
        self.assertEqual(attribution.model_environment('claude'), 'CLAUDE_AGENT_MODEL')
        self.assertFalse(attribution.requires_model_id())

    def test_trailers_are_exactly_as_before(self):
        self.assertEqual(attribution.coauthor('Opus 5.5'),
                         'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')
        self.assertEqual(attribution.coauthor('Claude  Opus 5.5', 'claude'),
                         'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')
        self.assertEqual(attribution.coauthor('Codex-mini', 'claude', ['Codex']),
                         'Co-Authored-By: Codex-mini <noreply@anthropic.com>')
        self.assertEqual(attribution.coauthor('gpt-6.1-sol', 'codex'),
                         'Co-Authored-By: Codex gpt-6.1-sol <noreply@openai.com>')
        self.assertTrue(attribution.requires_model_id('codex'))
        self.assertIsNone(attribution.model_environment('codex'))

    def test_the_trailer_classifier_is_the_one_it_was(self):
        self.assertEqual(attribution.AGENT_TRAILER.pattern,
                         r'^(?:\S.*\s<noreply@anthropic\.com>|'
                         r'Codex (?:[A-Za-z0-9][A-Za-z0-9._:/-]*) <noreply@openai\.com>)$')

    def test_a_registered_host_wins_over_the_unregistered_entry(self):
        self.assertEqual(list(attribution.providers())[:1], [registry.default()])
        self.assertIn('codex', attribution.providers())


class OtherRegistry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='attr-hosts-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
            data = json.load(f)
        other = copy.deepcopy(data['claude'])
        other.pop('default')
        other['profile']['attribution'] = {'name': 'Other', 'email': 'bot@example.invalid',
                                           'model_env': 'OTHER_MODEL', 'model_pattern': None}
        data['other'] = other
        self.path = os.path.join(self.dir, 'hosts.json')
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_a_new_host_is_attributed_from_its_entry_alone(self):
        self.assertEqual(attribution.coauthor('M 1', 'other', path=self.path),
                         'Co-Authored-By: Other M 1 <bot@example.invalid>')
        self.assertEqual(attribution.model_environment('other', path=self.path), 'OTHER_MODEL')
        pattern = attribution.agent_trailer(self.path)
        self.assertIsNotNone(pattern.fullmatch('Other M 1 <bot@example.invalid>'))
        self.assertIsNotNone(pattern.fullmatch('Claude Opus <noreply@anthropic.com>'))
        self.assertIsNone(pattern.fullmatch('Other <elsewhere@example.invalid>'))

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            attribution.coauthor('m', 'nobody', path=self.path)


if __name__ == '__main__':
    unittest.main()
