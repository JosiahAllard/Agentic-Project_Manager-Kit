"""The guard's refusal log (kitlib/guard/refusal_log.py; docs/guard-v2.md "Refusal log"): one JSON
line per deny in `<pm_data>/audit/refusals.jsonl`, the folder made on the first write: FIELDS in
order and nothing else, never the reason, the command, the path, the working directory or the
Instead text. Appends hold the write lock, so concurrent hooks never interleave a line. record()
never raises: no pm_data, an unreadable config, a busy lock or a failed write is no line."""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from kitlib import kitroot, sessionmark
from kitlib.fs import writelock
from kitlib.guard import refusal_log
from kitlib.host import Action, Decision
from tests.gitfixture import TempRepo, rmtree
from tests.home import machine_config, temp_home

NOW = datetime.datetime(2026, 10, 20, 9, 8, 7, tzinfo=datetime.timezone.utc)
SECRET_PATH = 'D:\\private\\notes\\journal.txt'
SECRET_COMMAND = 'git push --force origin main'
DENY = Decision('deny', 'some-rule', 'You ran %s on %s. Instead: do the other thing' % (SECRET_COMMAND, SECRET_PATH),
                'do the other thing')
ACTION = Action('shell', tool='PowerShell', path=SECRET_PATH, command=SECRET_COMMAND, cwd=None,
                agent='agent-7', agent_role='worker')


class Entry(unittest.TestCase):
    def test_the_fields_in_order_and_nothing_the_call_touched(self):
        env = {sessionmark.SESSION_NAME_ENV: 'kit-coordinator'}
        fields = refusal_log.entry(DENY, ACTION, 'claude', 'subagent', env, NOW)
        self.assertEqual(tuple(fields), refusal_log.FIELDS)
        self.assertEqual(dict(fields, repo=None), {
            'utc': '2026-10-20T09:08:07Z', 'host': 'claude', 'rule': 'some-rule', 'role': 'subagent',
            'agent_role': 'worker', 'agent': 'agent-7', 'session': 'kit-coordinator', 'tool': 'PowerShell',
            'kind': 'shell', 'repo': None, 'instead_given': True})
        text = json.dumps(fields)
        for secret in ('push', 'private', 'journal', 'other thing', 'You ran'):
            self.assertNotIn(secret, text)

    def test_absent_values_are_missing_and_no_rule_is_unknown(self):
        with mock.patch('kitlib.guard.place.classify', return_value=None):
            fields = refusal_log.entry(Decision('deny'), None, None, 'main', {}, NOW)
        self.assertEqual([fields[k] for k in refusal_log.FIELDS[1:10]],
                         ['-', 'unknown', 'main', '-', '-', '-', '-', '-', '-'])
        self.assertIs(fields['instead_given'], False)

    def test_a_main_sessions_action_has_no_agent_role(self):
        fields = refusal_log.entry(DENY, Action('edit', agent_role='worker'), 'claude', 'main', {}, NOW)
        self.assertEqual((fields['agent'], fields['agent_role']), ('-', '-'))

    def test_the_repo_is_the_checkouts_folder_name(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        sub = os.path.join(repo.root, 'deep')
        os.makedirs(sub)
        self.assertEqual(refusal_log.repo_of(Action('shell', cwd=sub)), 'repo')
        self.assertEqual(refusal_log.repo_of(Action('shell', cwd='C:/elsewhere', workdir=sub)), 'repo')
        with mock.patch('kitlib.guard.place.classify', return_value=None):
            self.assertEqual(refusal_log.repo_of(Action('shell', cwd=sub)), '-')


class Record(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        self.data = tempfile.mkdtemp(prefix='kit-pmdata-')
        self.addCleanup(rmtree, self.data)
        machine_config(self.home, {'pm_data': {'path': self.data}})
        self.log = os.path.join(self.data, 'audit', 'refusals.jsonl')

    def lines(self):
        with open(self.log, 'rb') as fh:
            return fh.read().decode('utf-8').split('\n')

    def test_a_deny_appends_a_line_and_keeps_the_earlier_ones(self):
        self.assertEqual(refusal_log.record(DENY, ACTION, 'claude', 'subagent', {}, now=NOW), self.log)
        self.assertEqual(refusal_log.record(DENY, None, 'codex', 'main', {}, now=NOW), self.log)
        lines = self.lines()
        self.assertEqual(lines[-1], '', 'every line ends with a newline')
        rows = [json.loads(line) for line in lines[:-1]]
        self.assertEqual([(r['host'], r['role']) for r in rows], [('claude', 'subagent'), ('codex', 'main')])
        with open(self.log, 'rb') as fh:
            self.assertNotIn(b'\r', fh.read())

    def test_only_a_deny_is_logged(self):
        for d in (Decision('allow'), Decision('ask', 'r', 'why'), Decision('allow', 'w', 'warned'), None):
            with self.subTest(d=d):
                self.assertIsNone(refusal_log.record(d, ACTION, 'claude', 'main', {}))
        self.assertFalse(os.path.exists(self.log))

    def test_no_pm_data_or_an_unreadable_config_is_no_line(self):
        machine_config(self.home, {})
        self.assertIsNone(refusal_log.record(DENY, ACTION, 'claude', 'main', {}))
        machine_config(self.home, '{nope')
        self.assertIsNone(refusal_log.record(DENY, ACTION, 'claude', 'main', {}))
        self.assertFalse(os.path.exists(os.path.join(self.data, 'audit')))

    def test_a_write_that_fails_is_no_line_and_no_raise(self):
        with mock.patch.object(refusal_log, 'append', side_effect=OSError('disk full')):
            self.assertIsNone(refusal_log.record(DENY, ACTION, 'claude', 'main', {}))
        with mock.patch.object(refusal_log, 'entry', side_effect=ValueError('bad')):
            self.assertIsNone(refusal_log.record(DENY, ACTION, 'claude', 'main', {}))

    def test_a_fenced_home_is_no_line_and_no_raise(self):
        from kitlib.config import user
        with mock.patch('kitlib.pmdata.machine_settings', side_effect=user.HomeFenced('fenced')):
            self.assertIsNone(refusal_log.record(DENY, ACTION, 'claude', 'main', {}))

    def test_a_busy_lock_is_no_line(self):
        os.makedirs(os.path.dirname(self.log))
        held, done = threading.Event(), threading.Event()

        def hold():
            with writelock.locked(self.log, refusal_log.LOCK_PREFIX, 5):
                held.set()
                done.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(done.set)
        self.assertTrue(held.wait(10))
        with mock.patch.object(refusal_log, 'WAIT', 0.1):
            self.assertIsNone(refusal_log.record(DENY, ACTION, 'claude', 'main', {}))
        self.assertFalse(os.path.exists(self.log))

    def test_concurrent_writers_never_interleave_a_line(self):
        code = ('import sys; sys.path.insert(0, sys.argv[1])\n'
                'from kitlib.guard import refusal_log\n'
                'for i in range(50):\n'
                '    refusal_log.append(sys.argv[2], {"n": i, "who": sys.argv[3], "pad": "x" * 8000}, wait=30)\n')
        root = kitroot.root()
        kids = [subprocess.Popen([sys.executable, '-c', code, root, self.data, str(k)], cwd=root,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for k in range(4)]
        for kid in kids:
            out, err = kid.communicate(timeout=120)
            self.assertEqual(kid.returncode, 0, err.decode('utf-8', 'replace'))
        lines = self.lines()
        self.assertEqual(lines[-1], '')
        rows = [json.loads(line) for line in lines[:-1]]
        self.assertEqual(len(rows), 200)
        for k in range(4):
            self.assertEqual(sorted(r['n'] for r in rows if r['who'] == str(k)), list(range(50)))


if __name__ == '__main__':
    unittest.main()
