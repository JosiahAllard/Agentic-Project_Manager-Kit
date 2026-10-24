"""kitlib.gate.gitstatus: on Windows a git reading .git/config while
another git in the same repository rewrites it by rename dies with `fatal: unable to access
'<path>config': Permission denied`. The landing's and the gate's `git status` try again through
that one failure, and through no other.

The unit cases answer git from a stub of kitlib.git.cmd._run; the landing case runs a real landing
in a scratch repository (tests.gate.fixtures) with every `git status` failing that way once."""
import importlib
import os
import threading
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land
from kitlib.git import cmd
from tests.gate import fixtures

BRANCH = 'kit/feature'
RACE = "fatal: unable to access '%s': Permission denied"
CONFIGS = ('Q:\\GitHub\\repo\\.git\\config', 'Q:/GitHub/repo/.git/config', '.git/config',
           'Q:/GitHub/repo/.git/worktrees/wt/config.worktree')


def gitstatus():
    """kitlib.gate.gitstatus, imported at the call (each test fails on its own without it)."""
    return importlib.import_module('kitlib.gate.gitstatus')


def answers(*replies):
    """A patch of cmd._run answering each git with the next of `replies` ((rc, stdout, stderr
    text)), the last one again once they run out; and the list of argvs it was asked."""
    asked = []

    def run(argv, cwd=None, input=None, timeout=None, env=None):
        asked.append(argv)
        rc, out, err = replies[min(len(asked), len(replies)) - 1]
        return rc, out, err.encode('utf-8')
    return mock.patch.object(cmd, '_run', side_effect=run), asked


class Retry(unittest.TestCase):
    def setUp(self):
        gs = gitstatus()
        no_wait = mock.patch.object(gs, 'PAUSE', 0)
        no_wait.start()
        self.addCleanup(no_wait.stop)
        self.gs = gs

    def test_a_config_race_once_is_tried_again(self):
        for path in CONFIGS:
            with self.subTest(path=path):
                patch, asked = answers((128, b'', RACE % path), (0, b'?? new.txt\0', ''))
                with patch:
                    entries, why = self.gs.status('.')
                self.assertEqual((entries, why), ([('??', 'new.txt')], None))
                self.assertEqual(len(asked), 2, asked)
                self.assertTrue(all(argv[1] == 'status' for argv in asked), asked)

    def test_another_failure_is_not_tried_again(self):
        for err in ('fatal: not a git repository (or any of the parent directories): .git',
                    RACE % 'Q:/home/.config/git/attributes',
                    "fatal: unable to access 'Q:/repo/.git/config': Invalid argument",
                    'fatal: unable to read Q:/repo/.git/index'):
            with self.subTest(err=err):
                patch, asked = answers((128, b'', err), (0, b'', ''))
                with patch:
                    entries, why = self.gs.status('.')
                self.assertEqual((entries, why), (None, err))
                self.assertEqual(len(asked), 1, asked)

    def test_a_race_that_lasts_gives_up_after_five_tries(self):
        race = RACE % CONFIGS[0]
        patch, asked = answers((128, b'', race))
        with patch:
            entries, why = self.gs.status('.')
        self.assertEqual((entries, why), (None, race))
        self.assertEqual(len(asked), 5, asked)


class Landing(fixtures.GateRepoCase):
    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        env = mock.patch.dict(os.environ, fixtures.test_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_a_landing_whose_every_git_status_races_once_lands(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.repo.git('push', '-q', 'origin', BRANCH)
        fixtures.gate_full(self.repo.root, tip)
        real = cmd.spawner()
        state = {'checks': False, 'fail': True}
        raced, lock = [], threading.Lock()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] == 'status':
                with lock:
                    fail, state['fail'] = state['fail'], not state['fail']
                    if fail:
                        raced.append(state['checks'])
                if fail:
                    return 128, b'', (RACE % os.path.join(self.repo.root, '.git', 'config')).encode()
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        real_checks = land.Lander.locked_checks

        def checks(lander):
            state['checks'] = True
            try:
                return real_checks(lander)
            finally:
                state['checks'] = False
        fixtures.queue_review(self.repo.root, BRANCH, tip)
        lander = land.Lander(self.repo.root, BRANCH, self.main, tip, git_env=self.env,
                             book_kw=self.book_kw, log_dir=self.logs, keep_branch=True)
        with mock.patch.object(cmd, '_run', side_effect=run), \
                mock.patch.object(land.Lander, 'locked_checks', checks):
            out = lander.run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(len([l for l in out.lines if l.startswith('LANDED ')]), 1, out.lines)
        self.assertIn(True, raced, 'no git status of the locked checks raced')
        self.assertGreater(len(raced), 1, raced)
        self.assertEqual(self.remote_main(), lander.merge)


if __name__ == '__main__':
    unittest.main()
