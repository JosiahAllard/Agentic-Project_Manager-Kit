"""The backlog's audit log (kitlib/backlog/audit.py; docs/backlog.md "Audit log"): after a write
whose rows carry an AUDITED risk (kitlib/backlog/roles.py) is published, tools/backlog.py appends
one JSON line to `<pm_data>/audit/backlog.jsonl`, making the folder on its first write: the verb,
the record id, the session's role and name, the UTC time and the store commit the write published,
never the command's text. With no pm_data the line is skipped with a note; a log that cannot be
written is a warning after the write, never a refusal."""
import datetime
import json
import os
import re
import tempfile
import unittest

from kitlib import sessionmark
from kitlib.backlog import audit
from kitlib.backlog import store as smod
from tests import inproc
from tests.backlog.helpers import ADD_FLAGS, kit_json_ref
from tests.gitfixture import TempRepo, fixture_env, rmtree
from tests.home import machine_config

BACKLOG = inproc.load_tool('backlog')
UTC = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z')
WHEN = datetime.datetime(2026, 10, 20, 13, 5, 6, tzinfo=datetime.timezone.utc)


class Pieces(unittest.TestCase):
    def test_the_audited_rows(self):
        for rows in (['close'], ['set-hold'], ['set-other'], ['set-text', 'set-other'],
                     ['pull-collision'], ['retarget']):
            with self.subTest(rows):
                self.assertTrue(audit.audited(rows))
        for rows in ([], ['read'], ['add'], ['pull'], ['set-text', 'set-price'], ['release']):
            with self.subTest(rows):
                self.assertFalse(audit.audited(rows))

    def test_the_entry_has_the_six_fields_in_order(self):
        e = audit.entry('close', 'X-1', 'owner', 's1', 'abc123', WHEN)
        self.assertEqual(list(e), list(audit.FIELDS))
        self.assertEqual(e, {'verb': 'close', 'id': 'X-1', 'role': 'owner', 'session': 's1',
                             'utc': '2026-10-20T13:05:06Z', 'store_sha': 'abc123'})

    def test_a_missing_value_is_a_dash(self):
        e = audit.entry('retarget', None, None, '', None, WHEN)
        self.assertEqual((e['id'], e['role'], e['session'], e['store_sha']), ('-', '-', '-', '-'))

    def test_the_role_is_the_tools_own(self):
        self.assertEqual(audit.role_of({}), 'owner')
        self.assertEqual(audit.role_of({sessionmark.ENV: 'coordinator'}), 'coordinator')
        self.assertEqual(audit.role_of({sessionmark.ENV: 'no-such-role'}), '-')

    def test_the_session(self):
        named = {sessionmark.SESSION_NAME_ENV: 'kit-x'}
        self.assertEqual(audit.session_of('s9', named), 's9')
        self.assertEqual(audit.session_of(None, named), 'kit-x')
        self.assertEqual(audit.session_of(None, {}), '-')

    def test_append_makes_the_folder_and_adds_a_line(self):
        data = tempfile.mkdtemp(prefix='kit-pmdata-')
        self.addCleanup(rmtree, data)
        path = audit.append(data, audit.entry('close', 'X-1', 'owner', '-', 'a1', WHEN))
        self.assertEqual(path, os.path.join(data, 'audit', 'backlog.jsonl'))
        audit.append(data, audit.entry('delete', 'X-2', 'pm', '-', 'a2', WHEN))
        with open(path, 'rb') as fh:
            raw = fh.read()
        self.assertNotIn(b'\r', raw)
        lines = raw.decode('utf-8').splitlines()
        self.assertEqual([json.loads(x)['id'] for x in lines], ['X-1', 'X-2'])
        self.assertEqual(os.listdir(data), ['audit'])          # no .gitignore, nothing else


class Tool(unittest.TestCase):
    """tools/backlog.py's writes against a temp repository, with pm_data in the machine config of
    a temp home."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True, prepare=kit_json_ref,
                             prepare_key='test_cli.Cli.kit_json_ref')
        self.addCleanup(self.repo.cleanup)
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, self.home)
        self.data = tempfile.mkdtemp(prefix='kit-pmdata-')
        self.addCleanup(rmtree, self.data)
        machine_config(self.home, {'pm_data': {'path': self.data}})
        self.env = fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        for name in (sessionmark.ENV, sessionmark.SESSION_NAME_ENV):
            self.env.pop(name, None)
        self.log = os.path.join(self.data, 'audit', 'backlog.jsonl')

    def run_tool(self, *args, env=None):
        """(exit code, stdout, stderr) of backlog.py's main(argv) in this process."""
        e = dict(self.env)
        e.update(env or {})
        return inproc.run_main(BACKLOG.main, ['-C', self.repo.root] + list(args), e, self.repo.root)

    def ok(self, *args, env=None):
        rc, out, err = self.run_tool(*args, env=env)
        self.assertEqual(rc, 0, out + err)
        return out, err

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as fh:
            return [json.loads(x) for x in fh.read().splitlines()]

    def tip(self):
        return self.repo.git('ls-remote', 'origin', smod.STORE_REF).split('\t')[0]

    def add(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)

    def test_a_low_write_writes_no_line(self):
        self.add()
        self.ok('pull', 'FOO', '--as', 'tools/wk')
        self.assertFalse(os.path.exists(os.path.join(self.data, 'audit')))

    def test_a_medium_write_makes_the_folder_and_its_line(self):
        self.add()
        self.ok('close', 'FOO', '--reason', 'the secret reason words')
        self.assertEqual(len(self.lines()), 1)
        line = self.lines()[0]
        self.assertEqual(list(line), list(audit.FIELDS))
        self.assertEqual((line['verb'], line['id'], line['role'], line['session'], line['store_sha']),
                         ('close', 'FOO', 'owner', '-', self.tip()))
        self.assertRegex(line['utc'], UTC)
        with open(self.log, encoding='utf-8') as fh:
            self.assertNotIn('secret reason', fh.read())

    def test_each_audited_write_adds_one_line(self):
        self.add()
        self.ok('set', 'FOO', '--priority', '2', '--priority-by', 'agent', '--effort', 's')
        first = self.tip()
        self.ok('close', 'FOO', '--reason', 'done')
        self.assertEqual([(x['verb'], x['store_sha']) for x in self.lines()],
                         [('set', first), ('close', self.tip())])

    def test_a_marked_session_and_its_name(self):
        self.add()
        marked = {sessionmark.ENV: 'coordinator', sessionmark.SESSION_NAME_ENV: 'kit-x'}
        self.ok('set', 'FOO', '--priority-by', 'agent', env=marked)
        self.ok('close', 'FOO', '--reason', 'done', '--session', 's9')   # unmarked: the owner
        self.assertEqual([(x['role'], x['session']) for x in self.lines()],
                         [('coordinator', 'kit-x'), ('owner', 's9')])

    def test_a_write_that_changes_nothing_writes_no_line(self):
        self.add()
        self.ok('set', 'FOO', '--priority-by', 'agent')
        out, _err = self.ok('set', 'FOO', '--priority-by', 'agent')
        self.assertIn('nothing was written', out)
        self.assertEqual(len(self.lines()), 1)

    def test_no_pm_data_is_a_note_and_no_line(self):
        machine_config(self.home, {})
        self.add()
        out, err = self.ok('close', 'FOO', '--reason', 'done')
        self.assertIn('WROTE', out)
        self.assertIn('NOTE:', err)
        self.assertIn('pm_data', err)
        self.assertFalse(os.path.exists(os.path.join(self.data, 'audit')))

    def test_a_log_that_cannot_be_written_is_a_warning_after_the_write(self):
        with open(os.path.join(self.data, 'audit'), 'w', encoding='utf-8') as fh:
            fh.write('a file where the folder goes\n')
        self.add()
        before = self.tip()
        out, err = self.ok('close', 'FOO', '--reason', 'done')
        self.assertIn('WROTE', out)
        self.assertNotEqual(self.tip(), before)                # the write stands
        self.assertIn('WARNING:', err)
        self.assertIn('audit', err)

    def test_migrate_is_audited(self):
        self.repo.write('docs/backlog/FOO.md', 'id: FOO\narea: xx\npriority: 2\npriority_by: agent\n'
                        'effort: s\nstate: open\nblocked_on: -\nscope: tools/x.py\n'
                        'summary: a thing to do\n')
        self.repo.commit('records', 'docs/backlog/FOO.md')
        self.repo.git('push', '-q', 'origin', 'main')
        self.ok('migrate', '--dry-run')
        self.assertEqual(self.lines(), [])
        self.ok('migrate')
        self.assertEqual([(x['verb'], x['id'], x['store_sha']) for x in self.lines()],
                         [('migrate', '-', self.tip())])


if __name__ == '__main__':
    unittest.main()
