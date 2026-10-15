"""tools/pending.py: arguments, exit codes, and --commit/--push committing the pending file alone
(the logic is tests/pending/test_pending.py's). Real git in a throwaway repository
(tests/gitfixture.py) with a bare remote."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.gitfixture import TempRepo, fixture_env
from tests.realgit import real_git
from kitlib.config import user as kuser

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'pending.py')
MODEL = 'Test Model 1'
TEXT = '# Pending\n\n1. First step\n2. Second step\n'
HOLD = 0.4           # seconds a racing child sleeps between its read of the file and its write
RACE_TIMEOUT = 60.0  # the hard bound on a child
# A racing child: kitlib.pending.write_atomic sleeps HOLD first, so two children that read the file
# together both read it before either writes. It says it is ready, waits for the go file, then runs
# tools/pending.py with the rest of its arguments.
RACE_CHILD = r'''
import os, runpy, sys, time
tool, ready, go, hold = sys.argv[1:5]
sys.path.insert(0, os.path.dirname(os.path.dirname(tool)))
from kitlib import pending
real = pending.write_atomic
def slow(p, text):
    time.sleep(float(hold))
    real(p, text)
pending.write_atomic = slow
open(ready, 'w').close()
deadline = time.monotonic() + 30
while not os.path.exists(go):
    if time.monotonic() > deadline:
        sys.exit('no go file')
    time.sleep(0.01)
sys.argv = [tool] + sys.argv[5:]
runpy.run_path(tool, run_name='__main__')
'''
# A child that holds the pending file's write lock until its stdin closes.
HOLDER = ('import os, sys; sys.path.insert(0, os.path.dirname(os.path.dirname(sys.argv[1]))); '
          'from kitlib import pending\n'
          'with pending.locked(sys.argv[2]):\n'
          '    print("held", flush=True); sys.stdin.read()\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.repo.write('owner/PENDING.md', TEXT)
        self.repo.commit('pending', 'owner/PENDING.md')
        self.path = self.repo.path('owner', 'PENDING.md')
        self.home = os.path.join(self.repo.base, 'home')
        self.config({'pending_path': self.path})

    def config(self, data):
        os.makedirs(kuser.config_dir(self.home), exist_ok=True)
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def run_tool(self, *args, model=MODEL):
        env = fixture_env({'USERPROFILE': self.home, 'HOME': self.home, 'CLAUDE_AGENT_MODEL': model})
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60, cwd=self.repo.root)

    def read(self):
        with open(self.path, encoding='utf-8', newline='') as f:
            return f.read()


class CliTest(Base):
    def test_list_add_done_edit_without_commit(self):
        r = self.run_tool('add', '--text', 'Third step')
        self.assertEqual((r.returncode, r.stdout), (0, 'added 3\n'), r.stderr)
        r = self.run_tool('done', '2', '--when', '10-10', '--note', 'ok')
        self.assertEqual((r.returncode, r.stdout), (0, 'done 2\n'), r.stderr)
        r = self.run_tool('edit', '1', '--text', 'First, changed')
        self.assertEqual((r.returncode, r.stdout), (0, 'edited 1\n'), r.stderr)
        r = self.run_tool('list')
        self.assertEqual((r.returncode, r.stdout), (0, '1. First, changed\n3. Third step\n'), r.stderr)
        self.assertIn('2. DONE 10-10 (ok). Was: Second step\n', self.read())
        self.assertEqual(self.repo.git('log', '-1', '--format=%s'), 'pending')

    def test_push_commits_the_file_alone_and_sends_the_branch(self):
        self.repo.write('other.txt', 'not mine\n')
        self.repo.git('add', '--', 'other.txt')
        r = self.run_tool('add', '--text', 'Third step', '--push')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], 'added 3')
        self.assertTrue(lines[1].startswith('commit '), lines)
        self.assertEqual(lines[2], 'pushed origin/main')
        self.assertEqual(self.repo.git('show', '--name-only', '--format=', 'HEAD'), 'owner/PENDING.md')
        msg = self.repo.git('log', '-1', '--format=%B')
        self.assertTrue(msg.startswith('pending: added 3'), msg)
        self.assertIn('Co-Authored-By: Claude %s <noreply@anthropic.com>' % MODEL, msg)
        self.assertEqual(self.repo.git('rev-parse', 'HEAD'),
                         self.repo.git('rev-parse', 'main', cwd=self.repo.remote))
        self.assertIn('other.txt', self.repo.git('diff', '--cached', '--name-only'))

    def test_commit_refuses_before_writing_a_dirty_file_or_another_branch(self):
        with open(self.path, 'a', encoding='utf-8', newline='') as f:
            f.write('stray\n')
        before = self.read()
        r = self.run_tool('add', '--text', 'x', '--commit')
        self.assertEqual(r.returncode, 1)
        self.assertIn('uncommitted', r.stderr)
        self.assertEqual(self.read(), before)
        self.repo.git('checkout', '-q', '--', 'owner/PENDING.md')
        self.repo.git('checkout', '-q', '-b', 'side')
        r = self.run_tool('done', '1', '--commit')
        self.assertEqual(r.returncode, 1)
        self.assertIn('--branch-ok', r.stderr)
        self.assertEqual(self.read(), TEXT)
        r = self.run_tool('done', '1', '--commit', '--branch-ok')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.repo.git('log', '-1', '--format=%s').startswith('pending: done 1'))

    def test_commit_needs_a_model(self):
        r = self.run_tool('add', '--text', 'x', '--commit', model='')
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.read(), TEXT)

    def test_refusals_and_usage(self):
        r = self.run_tool('done', '9')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no item 9', r.stderr)
        self.assertEqual(self.run_tool('done', '1', '--when', 'today').returncode, 2)
        self.assertEqual(self.run_tool('add', '--text', ' ').returncode, 2)
        self.assertEqual(self.run_tool('add').returncode, 2)
        self.assertEqual(self.read(), TEXT)

    def test_a_file_that_cannot_be_written_is_a_clean_refusal(self):
        spec = importlib.util.spec_from_file_location('pending_tool_under_test', TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home}), \
                mock.patch.object(tool.pending, 'write_atomic', side_effect=PermissionError(13, 'Access is denied')), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = tool.main(['add', '--text', 'x'])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), 'pending.py: refused: cannot write %s: Access is denied\n' % self.path)
        self.assertEqual(self.read(), TEXT)

    def test_no_pending_path_exits_1(self):
        self.config({})
        r = self.run_tool('list')
        self.assertEqual(r.returncode, 1)
        self.assertIn('pending_path', r.stderr)


class WriteLockTest(Base):
    """Two changes at once: the write lock keeps both (without it the second replace drops the first)."""

    def env(self):
        return fixture_env({'USERPROFILE': self.home, 'HOME': self.home, 'CLAUDE_AGENT_MODEL': MODEL})

    def race(self, *runs):
        procs, readies = [], []
        go = os.path.join(self.repo.base, 'go')
        for i, args in enumerate(runs):
            ready = os.path.join(self.repo.base, 'ready-%d' % i)
            readies.append(ready)
            p = subprocess.Popen([sys.executable, '-c', RACE_CHILD, TOOL, ready, go, str(HOLD)] + list(args),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=self.env(), cwd=self.repo.root)
            self.addCleanup(lambda p=p: p.poll() is None and p.kill())
            procs.append(p)
        deadline = time.monotonic() + 30
        while not all(os.path.exists(r) for r in readies):
            for p in procs:
                if p.poll() is not None:
                    self.fail('a child exited before it was ready: %s' % (p.communicate(),))
            self.assertLess(time.monotonic(), deadline, 'the children were not ready')
            time.sleep(0.01)
        open(go, 'w').close()
        return [p.communicate(timeout=RACE_TIMEOUT) + (p.returncode,) for p in procs]

    def test_two_adds_at_once_get_two_numbers_and_keep_both_items(self):
        got = self.race(['add', '--text', 'From A'], ['add', '--text', 'From B'])
        for out, err, rc in got:
            self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(out for out, _, _ in got), ['added 3\n', 'added 4\n'])
        text = self.read()
        self.assertIn('. From A\n', text)
        self.assertIn('. From B\n', text)
        self.assertEqual(sorted(line[:2] for line in text.splitlines() if line[:1].isdigit()),
                         ['1.', '2.', '3.', '4.'])

    def test_two_adds_with_commit_at_once_give_one_commit_per_item(self):
        got = self.race(['add', '--text', 'From A', '--commit'], ['add', '--text', 'From B', '--commit'])
        for out, err, rc in got:
            self.assertEqual(rc, 0, err)
        subjects = self.repo.git('log', '-2', '--format=%s').splitlines()
        self.assertEqual(sorted(subjects), ['pending: added 3', 'pending: added 4'])
        self.assertEqual(self.repo.git('status', '--porcelain', '--', 'owner/PENDING.md'), '')
        self.assertIn('. From A', self.read())
        self.assertIn('. From B', self.read())

    def test_a_held_lock_is_refused_after_the_wait_and_nothing_is_written(self):
        holder = subprocess.Popen([sys.executable, '-c', HOLDER, TOOL, self.path], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(holder.communicate, timeout=RACE_TIMEOUT)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        spec = importlib.util.spec_from_file_location('pending_tool_under_test', TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home}), \
                mock.patch.object(tool.pending, 'LOCK_WAIT', 0.3), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = tool.main(['add', '--text', 'x'])
        self.assertEqual(code, 1)
        self.assertIn('pending.py: refused: ', err.getvalue())
        self.assertIn('held by pid %d' % holder.pid, err.getvalue())
        self.assertIn('nothing was written', err.getvalue())
        self.assertEqual(self.read(), TEXT)


@real_git('hooks: the fake runs no hooks')
class FailingHookTest(Base):
    def test_a_commit_that_fails_after_the_write_is_not_called_a_refusal(self):
        hooks = os.path.join(self.repo.base, 'failhooks')
        os.makedirs(hooks)
        with open(os.path.join(hooks, 'pre-commit'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(os.path.join(hooks, 'pre-commit'), 0o755)
        self.repo.git('config', 'core.hooksPath', hooks.replace(os.sep, '/'))
        r = self.run_tool('add', '--text', 'Third step', '--commit')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout, 'added 3\n')
        self.assertIn('written but NOT committed', r.stderr)
        self.assertNotIn('refused', r.stderr)
        self.assertIn('3. Third step', self.read())
        self.assertEqual(self.repo.git('log', '-1', '--format=%s'), 'pending')


if __name__ == '__main__':
    unittest.main()
