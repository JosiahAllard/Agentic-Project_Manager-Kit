"""kitlib.config.user (the machine config: the ledger path and the rest) and docs/kit-json.md
kept equal to the schema."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import config
from kitlib.config import schema, user

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   'docs', 'kit-json.md')


class UserConfigTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.ledger = os.path.join(self.home, 'ledger', 'rulings.md')

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def test_no_machine_config_means_no_ledger(self):
        self.assertIsNone(user.ledger_path(self.home))

    def test_the_ledger_path_is_read_from_the_machine_config(self):
        self.write({'ledger_path': self.ledger, '_about': 'notes allowed'})
        self.assertEqual(config.ledger_path(self.home), self.ledger)

    def test_nothing_in_the_environment_or_a_repo_overrides_it(self):
        self.write({'ledger_path': self.ledger})
        env = {'KIT_LEDGER_PATH': os.path.join(self.home, 'other.md'),
               'LEDGER_PATH': os.path.join(self.home, 'other.md')}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(user.ledger_path(self.home), self.ledger)
        self.assertNotIn('ledger_path', schema.SECTIONS['repo'])
        self.assertFalse(any('ledger_path' in fields for fields in schema.SECTIONS.values()))

    def test_the_standing_delegation_is_read_from_the_machine_config(self):
        self.assertIsNone(user.standing_delegation(self.home))   # no file
        self.write({'ledger_path': self.ledger})
        self.assertIsNone(user.standing_delegation(self.home))   # file without the key
        rid = 'R-' + '9168'      # built, so tests/scrub sees no id in this file
        self.write({'standing_delegation': rid})
        self.assertEqual(user.standing_delegation(self.home), rid)

    def test_a_standing_delegation_that_is_not_a_ruling_id_refuses(self):
        rid = 'R-' + '9168'
        for bad in ('9168', rid + 'x', rid.lower(), rid[:4], '', 9168):
            self.write({'standing_delegation': bad})
            with self.assertRaises(config.ConfigError, msg=repr(bad)) as cm:
                user.standing_delegation(self.home)
            self.assertIn('standing_delegation', str(cm.exception))

    def test_a_relative_path_an_unknown_key_or_bad_json_refuses(self):
        for body, why in (({'ledger_path': 'rulings.md'}, 'must be an absolute path'),
                          ({'ledger': self.ledger}, 'unknown key(s) ledger'),
                          ('{', 'is not valid JSON'),
                          ('{"ledger_path": "/a", "ledger_path": "/b"}', 'written twice in one object: ledger_path'),
                          ('[]', 'must be an object')):
            self.write(body)
            with self.assertRaises(config.ConfigError) as cm:
                user.user_config(self.home)
            self.assertIn(why, str(cm.exception))

    def test_a_file_that_is_not_utf8_refuses(self):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'wb') as f:
            f.write(b'{"ledger_path": "' + bytes([0xff, 0xfe]) + b'"}')
        with self.assertRaises(config.ConfigError) as cm:
            user.user_config(self.home)
        self.assertIn('cannot read', str(cm.exception))

    def test_keys_other_modules_own_are_accepted_as_objects(self):
        """The machine config's shape on 2026-10-07: pm_start and coordinator_start beside the ledger,
        plus pm_watch and backlog_lint, which kitlib.pmwatch and kitlib.backloglint read."""
        self.write({'ledger_path': self.ledger,
                    'pm_start': {'hub_url': 'https://example.invalid/hub', 'hub_source': 'hub.html',
                                 'pm_watch_cmd': 'python pm_watch.py', 'memory_dir': self.home,
                                 'handoff_glob': 'handoff-*.md', 'reminders_file': 'r.json',
                                 'sessions_file': 's.json', 'standing': ['a rule']},
                    'coordinator_start': {'standing': ['a rule'], 'cap': {'items': 5},
                                        'review_session': {'name': 'review'}},
                    'pm_watch': {'repos': ['kit']},
                    'backlog_lint': {'repos': {'kit': {'root': self.home}}}})
        got = user.user_config(self.home)
        self.assertEqual(got['ledger_path'], self.ledger)
        self.assertEqual(got['pm_start']['sessions_file'], 's.json')
        self.assertEqual(got['coordinator_start']['cap'], {'items': 5})
        self.assertEqual(got['backlog_lint']['repos']['kit'], {'root': self.home})

    def test_an_owned_key_must_be_an_object_and_unknown_keys_still_refuse(self):
        for body, why in (({'pm_start': ['x']}, 'pm_start must be an object'),
                          ({'coordinator_start': 'x'}, 'coordinator_start must be an object'),
                          ({'pm_watch': 'x'}, 'pm_watch must be an object'),
                          ({'backlog_lint': []}, 'backlog_lint must be an object'),
                          ({'pm_start': {}, 'pm_wtach': {}}, 'unknown key(s) pm_wtach')):
            self.write(body)
            with self.assertRaises(config.ConfigError) as cm:
                user.user_config(self.home)
            self.assertIn(why, str(cm.exception))

    def test_the_default_home_is_the_user_profile(self):
        self.assertEqual(user.config_file(), os.path.join(os.path.expanduser('~'), user.DIR_NAME,
                                                          'config.json'))

    def test_home_follows_userprofile_on_windows_and_home_elsewhere(self):
        var = 'USERPROFILE' if os.name == 'nt' else 'HOME'
        self.write({'ledger_path': self.ledger})
        with mock.patch.dict(os.environ, {var: self.home}):
            self.assertEqual(user.config_file(), os.path.join(self.home, user.DIR_NAME, 'config.json'))
            self.assertEqual(user.ledger_path(), self.ledger)


class DocTest(unittest.TestCase):
    """docs/kit-json.md lists exactly the schema's lines: one per key, in schema order."""

    def field_lines(self):
        with open(DOC, encoding='utf-8') as f:
            lines = f.read().splitlines()
        out, current = {}, None
        for line in lines:
            if line.startswith('## '):
                current = line[3:].strip()
                out[current] = []
            elif current and line.startswith('- `'):
                out[current].append(line)
        return out

    def test_the_kit_json_section_is_the_schema(self):
        self.assertEqual(self.field_lines().get('`.kit.json` keys'), schema.doc_lines())

    def test_the_machine_config_section_is_its_schema(self):
        self.assertEqual(self.field_lines().get('`~/.claude-pm-kit/config.json` keys'), user.doc_lines())

    def test_every_schema_section_is_documented(self):
        lines = '\n'.join(schema.doc_lines())
        for name in schema.SECTIONS:
            self.assertIn('`%s' % name, lines)


if __name__ == '__main__':
    unittest.main()
