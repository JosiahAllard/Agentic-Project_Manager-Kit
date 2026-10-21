"""The guard's refusal log (kitlib/guard/refusal_log.py; docs/guard-v2.md "Refusal log"): one JSON
line per deny in `<pm_data>/audit/refusals.jsonl`, the folder made on the first write: FIELDS in
order and nothing else, never the reason, the command, the path, the working directory or the
Instead text, and never a folder name that could be the account's. Appends hold an OS lock
beside the log, so concurrent hooks never interleave a line, and nothing on the way starts a
process, so a hung git cannot hold a deny up. record() never raises: no pm_data, an unreadable
config, a busy lock or a failed write is no line."""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib import kitroot, sessionmark
from kitlib.fs import treelock
from kitlib.guard import refusal_log
from kitlib.host import Action, Decision
from tests.gitfixture import TempRepo, fixture_parent, rmtree
from tests.home import machine_config, pm_repo, temp_home

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


def git_dir(path):
    """A folder git (and kitlib.guard.place) takes for a git directory: objects/, refs/, HEAD."""
    os.makedirs(os.path.join(path, 'objects'))
    os.makedirs(os.path.join(path, 'refs'))
    with open(os.path.join(path, 'HEAD'), 'w') as fh:
        fh.write('ref: refs/heads/main\n')


def dot_git_file(checkout, target):
    os.makedirs(checkout, exist_ok=True)
    with open(os.path.join(checkout, '.git'), 'w') as fh:
        fh.write('gitdir: %s\n' % target)


class RepoName(unittest.TestCase):
    """repo_of names only a checkout whose common dir is `<checkout>/.git`, and never a folder that
    could be the account's name."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-rl-', dir=fixture_parent())
        self.addCleanup(rmtree, self.base)

    def name(self, where, env=None):
        return refusal_log.repo_of(Action('shell', cwd=where), env or {})

    def test_a_linked_worktree_names_its_main_checkout(self):
        main = os.path.join(self.base, 'mainrepo')
        git_dir(os.path.join(main, '.git'))
        wt = os.path.join(main, '.git', 'worktrees', 'wt')
        os.makedirs(wt)
        with open(os.path.join(wt, 'HEAD'), 'w') as fh:
            fh.write('ref: refs/heads/side\n')
        with open(os.path.join(wt, 'commondir'), 'w') as fh:
            fh.write('../..\n')
        dot_git_file(os.path.join(self.base, 'tree'), wt)
        self.assertEqual(self.name(os.path.join(self.base, 'tree')), 'mainrepo')

    def test_a_submodule_and_a_separate_git_dir_are_missing(self):
        sup = os.path.join(self.base, 'super')
        git_dir(os.path.join(sup, '.git'))
        git_dir(os.path.join(sup, '.git', 'modules', 'sub'))
        dot_git_file(os.path.join(sup, 'sub'), '../.git/modules/sub')
        self.assertEqual(self.name(os.path.join(sup, 'sub')), '-', 'not "modules"')
        git_dir(os.path.join(self.base, 'store', '.git'))
        dot_git_file(os.path.join(self.base, 'work'), os.path.join(self.base, 'store', '.git'))
        self.assertEqual(self.name(os.path.join(self.base, 'work')), '-', 'not "store"')
        git_dir(os.path.join(self.base, 'elsewhere.git'))
        dot_git_file(os.path.join(self.base, 'plain'), os.path.join(self.base, 'elsewhere.git'))
        self.assertEqual(self.name(os.path.join(self.base, 'plain')), '-')

    def test_the_profile_folder_and_the_accounts_name_are_missing(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        self.assertEqual(self.name(repo.root, {'USERPROFILE': repo.root + os.sep}), '-')
        self.assertEqual(self.name(repo.root, {'USERPROFILE': repo.root.upper()}), '-')
        self.assertEqual(self.name(repo.root, {'USERNAME': 'Repo'}), '-')
        self.assertEqual(self.name(repo.root, {'USERPROFILE': repo.base, 'USERNAME': 'someone'}), 'repo',
                         "a checkout inside the profile folder keeps its own name")


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
        pm_repo(self, record=False)              # a PM repository with no PM data record
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
            with treelock.TreeLock(os.path.join(os.path.dirname(self.log), refusal_log.LOCK_FILE)):
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
        start = time.monotonic()
        self.assertIsNone(refusal_log.record(DENY, ACTION, 'claude', 'main', {}))
        self.assertLess(time.monotonic() - start, refusal_log.BOUND + 0.5, 'the wait is bounded')

    def test_no_process_is_started_so_a_hung_git_cannot_hold_a_deny_up(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        started = []

        def hung(*a, **k):
            started.append(a)
            time.sleep(5)
            raise OSError('hung')
        with mock.patch('kitlib.git.cmd.call', side_effect=hung), \
                mock.patch('subprocess.Popen', side_effect=hung), mock.patch('subprocess.run', side_effect=hung):
            began = time.monotonic()
            got = refusal_log.record(DENY, Action('shell', cwd=repo.root), 'claude', 'main', {}, now=NOW)
            took = time.monotonic() - began
        self.assertEqual((got, started), (self.log, []))
        self.assertLess(took, refusal_log.BOUND)
        self.assertEqual(json.loads(self.lines()[0])['repo'], 'repo')

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
