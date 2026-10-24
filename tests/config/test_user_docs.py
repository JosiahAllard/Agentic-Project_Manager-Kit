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
from tests.home import PM_REPO_FENCE_ENV, machine_config, pm_repo

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   'docs', 'kit-json.md')


class UserConfigTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.pm = pm_repo(self)
        self.ledger = os.path.join(self.home, 'ledger', 'rulings.md')

    def write(self, body):
        # the PM repository's two files: a str is config.json's text as it is
        machine_config(self.home, body, case=self)

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
        for body, why in (({'ledger': self.ledger}, 'unknown key(s) ledger'),
                          ('{', 'is not valid JSON'),
                          ('{"ledger_path": "/a", "ledger_path": "/b"}', 'written twice in one object: ledger_path'),
                          ('[]', 'must be an object')):
            self.write(body)
            with self.assertRaises(config.ConfigError) as cm:
                user.user_config(self.home)
            self.assertIn(why, str(cm.exception))

    def test_a_relative_ledger_path_is_read_against_the_pm_repository(self):
        self.write({'ledger_path': 'rulings.md'})
        self.assertEqual(user.ledger_path(self.home), os.path.join(self.pm, 'rulings.md'))

    def test_a_file_that_is_not_utf8_refuses(self):
        self.write({})
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
                                 'handoff_file': 'HANDOFF.md', 'reminders_file': 'r.json',
                                 'sessions_file': 's.json', 'standing': ['a rule']},
                    'coordinator_start': {'standing': ['a rule'], 'cap': {'items': 5}},
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

    def test_guard_mark_roots_takes_marks_to_absolute_or_home_paths(self):
        roots = {'pm': [{'path': self.home, 'mode': 'open', 'why': 'test'},
                        {'path': '~/notes', 'mode': 'add-only'}, {'path': '~\\memo', 'mode': 'read-only'}],
                 'deep-worker': [], '_about': 'notes allowed'}
        self.write({'guard_mark_roots': roots})
        self.assertEqual(user.user_config(self.home)['guard_mark_roots'], roots)
        self.assertEqual(user.expand_home('~/notes', self.home), os.path.join(self.home, 'notes'))
        self.assertEqual(user.expand_home(self.home, 'elsewhere'), self.home)

    def test_the_reserved_marks_are_the_session_marks_reserved_values(self):
        from kitlib import sessionmark
        self.assertEqual(user.RESERVED_MARKS, sessionmark.RESERVED)

    def test_guard_mark_roots_refuses_a_reserved_mark_a_relative_path_or_an_unknown_mode(self):
        ok = {'path': self.home, 'mode': 'open'}
        for body, why in (({'owner': [ok]}, "'owner' is not a session mark"),
                          ({'Subagent': [ok]}, "'Subagent' is not a session mark"),
                          ({'pm': [{'path': 'memory', 'mode': 'open'}]}, 'must be an absolute path or ~/<path>'),
                          ({'pm': [{'path': '~memory', 'mode': 'open'}]}, 'must be an absolute path or ~/<path>'),
                          ({'pm': [{'path': self.home, 'mode': 'write'}]}, 'must be one of'),
                          ({'pm': [{'path': self.home}]}, 'needs "mode"'),
                          ({'pm': [dict(ok, roles=['pm'])]}, 'unknown key(s) roles'),
                          ({'pm': ok}, 'must be a list'),
                          ([ok], 'guard_mark_roots must be an object')):
            with self.subTest(why):
                self.write({'guard_mark_roots': body})
                with self.assertRaises(config.ConfigError) as cm:
                    user.user_config(self.home)
                self.assertIn(why, str(cm.exception))

    def test_with_no_pointer_the_file_named_is_in_the_user_profile(self):
        # a scratch profile and no PM-repo pointer: NONE names the known-absent home file
        with mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home,
                                          PM_REPO_FENCE_ENV: ''}):
            user.forget()
            self.assertEqual(user.config_file(), os.path.join(os.path.expanduser('~'), user.DIR_NAME,
                                                              'config.json'))
            self.assertEqual(user.locate().state, user.NONE)
        user.forget()

    def test_with_a_pointer_the_file_is_the_pm_repositorys(self):
        var = 'USERPROFILE' if os.name == 'nt' else 'HOME'
        self.write({'ledger_path': self.ledger})
        with mock.patch.dict(os.environ, {var: self.home}):
            self.assertEqual(user.config_file(), os.path.join(self.pm, user.DIR_NAME, 'config.json'))
            self.assertEqual(user.ledger_path(), self.ledger)


class RetiredKeyTest(unittest.TestCase):
    """A home-folder machine config written before a key was removed still loads as the move's
    source (user.RETIRED, user.home_location): the key is dropped whatever it holds, and never
    returned. The PM repository's config refuses it (user.DROPPED_KEYS)."""

    setUp = UserConfigTest.setUp

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(os.path.join(self.home, user.DIR_NAME, user.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write(json.dumps(body))

    def read(self):
        return user.user_config(location=user.home_location(self.home))

    def test_a_retired_key_loads_and_is_not_returned(self):
        self.assertIn('ledger_refs', user.RETIRED)
        self.write({'ledger_path': self.ledger,
                    'ledger_refs': {'repos': {'alpha': {'path': self.home, 'ref': 'origin/main'}},
                                    'folders': {'notes': self.home}}})
        got = self.read()
        self.assertEqual(got['ledger_path'], self.ledger)
        for key in user.RETIRED:
            self.assertNotIn(key, got)

    def test_a_retired_key_loads_whatever_it_holds(self):
        for value in ('not an object', 7, [], None, {'no_such_key': 1}):
            self.write({'ledger_path': self.ledger, 'ledger_refs': value})
            self.assertEqual(self.read()['ledger_path'], self.ledger, repr(value))

    def test_the_pm_repositorys_config_refuses_a_retired_key(self):
        machine_config(self.home, {'ledger_path': self.ledger, 'ledger_refs': {}}, case=self)
        with self.assertRaises(config.ConfigError) as cm:
            user.user_config(self.home)
        self.assertIn('ledger_refs is a key the move drops', str(cm.exception))

    def test_no_schema_key_is_retired_and_no_doc_line_names_one(self):
        for key in user.RETIRED:
            self.assertNotIn(key, user.SCHEMA)
            self.assertFalse([line for line in user.doc_lines() if line.startswith('- `%s`' % key)])

    def test_failing_keys_never_names_a_retired_key(self):
        self.assertEqual(user.failing_keys({'ledger_refs': 'x', 'ledger_ref': 1}), ['ledger_ref'])


class RetiredKeyUnchangedTest(unittest.TestCase):
    """What the retirement leaves as it was: an unknown key still refuses."""

    setUp = UserConfigTest.setUp
    write = RetiredKeyTest.write

    def test_an_unknown_key_beside_a_retired_one_still_refuses(self):
        self.write({'ledger_refs': {}, 'ledger_ref': {}})
        with self.assertRaises(config.ConfigError) as cm:
            user.user_config(location=user.home_location(self.home))
        self.assertIn('unknown key(s) ledger_ref (allowed:', str(cm.exception))


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
        self.assertEqual(self.field_lines().get('Machine config keys'), user.doc_lines())

    def test_every_schema_section_is_documented(self):
        lines = '\n'.join(schema.doc_lines())
        for name in schema.SECTIONS:
            self.assertIn('`%s' % name, lines)



class MoveDocTest(unittest.TestCase):
    """docs/kit-json.md "Moving the machine config folder" and "The PM repo pointer" say what the
    kit does now: the PM repository is read, the session start names a home leftover with its
    remove command and the integrity line, and a copy that still reads the home folder is listed."""

    def sections(self):
        with open(DOC, encoding='utf-8') as f:
            text = f.read()
        out, current = {}, None
        for line in text.splitlines():
            if line.startswith('## '):
                current = line[3:].strip()
                out[current] = []
            elif current:
                out[current].append(line)
        return {k: '\n'.join(v) for k, v in out.items()}

    def test_the_move_section_names_the_session_start_lines(self):
        from kitlib.config import copies
        from kitlib.pmstart import configcheck
        move = self.sections()['Moving the machine config folder']
        self.assertIn('`kitlib.pmstart.configcheck`', move)
        self.assertIn(configcheck.LEFTOVER.split(' (')[0], move)
        self.assertIn("Remove-Item -LiteralPath '<file>'", move)
        for line in (configcheck.NEVER, configcheck.DIRTY, configcheck.NOT_OWNER):
            self.assertIn(line.split('%s ', 1)[1].split(':')[0], move)
        self.assertIn('never names an address', move)
        self.assertIn(copies.HOME_READER.split('): ', 1)[1], move)
        self.assertNotIn('FALLBACK', move)

    def test_the_pointer_section_says_the_kit_reads_through_it(self):
        pointer = self.sections()['The PM repo pointer']
        self.assertNotIn('Nothing reads it for config yet', pointer)
        self.assertIn('The kit reads the machine config through it', pointer)


if __name__ == '__main__':
    unittest.main()
