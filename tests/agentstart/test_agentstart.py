"""kitlib.agentstart: the short block a dispatched kit agent gets at its start hook (docs/preflight.md
"Agent start block"). Every case builds its own repository (tests.gitfixture); nothing fetches, and
the stray check is recorded, not run, except where a case says so (it has its own tests in
tests/git/test_stray.py)."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib import agentstart
from kitlib.git import cmd
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NETWORK = ('fetch', 'pull', 'push', 'ls-remote', 'remote', 'clone', 'submodule')


def slash(path):
    return path.replace('\\', '/')


class Fixture(unittest.TestCase):
    """A repository on main, one commit, origin/main at it; the kit is this checkout."""

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.git = self.repo.git
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.head = self.git('rev-parse', '--short', 'HEAD')
        p = mock.patch.object(agentstart.stray, 'check', return_value=(0, ['STRAY-LINE']))
        self.stray = p.start()
        self.addCleanup(p.stop)

    def row(self, lines, key):
        got = [line for line in lines if line.startswith(key)]
        self.assertEqual(len(got), 1, lines)
        return got[0]


class MainCheckout(Fixture):
    def test_it_says_main_and_how_to_make_a_worktree(self):
        lines = agentstart.lines(self.repo.root, KIT)
        row = self.row(lines, 'checkout ')
        self.assertIn('MAIN', row)
        self.assertIn('branch main @ %s' % self.head, row)
        nxt = self.row(lines, 'next ')
        self.assertIn('new_worktree.py', nxt)
        self.assertIn('-C "%s"' % slash(self.repo.root), nxt)
        self.assertIn('<area>/<slug>', nxt)
        self.assertNotIn('vs base', '\n'.join(lines))

    def test_the_base_pair_reads_equal_only_when_both_were_read(self):
        row = self.row(agentstart.lines(self.repo.root, KIT), 'base ')
        self.assertIn('main @ %s | origin/main @ %s (equal)' % (self.head, self.head), row)

    def test_a_missing_remote_ref_is_never_equal(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        row = self.row(agentstart.lines(self.repo.root, KIT), 'base ')
        self.assertIn('origin/main @ ?', row)
        self.assertNotIn('(equal)', row)
        self.assertNotIn('DIFFERENT', row)

    def test_a_base_that_moved_is_different(self):
        self.repo.write('a.txt', 'a\n')
        self.repo.commit('more', 'a.txt')
        self.assertIn('(DIFFERENT)', self.row(agentstart.lines(self.repo.root, KIT), 'base '))

    def test_the_fetch_age_comes_from_fetch_head(self):
        self.assertIn('never fetched here', self.row(agentstart.lines(self.repo.root, KIT), 'base '))
        fh = os.path.join(self.repo.root, '.git', 'FETCH_HEAD')
        with open(fh, 'w') as f:
            f.write('')
        now = time.time()
        os.utime(fh, (now - 7 * 60, now - 7 * 60))
        row = self.row(agentstart.lines(self.repo.root, KIT, now=now), 'base ')
        self.assertIn('origin as of the last fetch 7 min ago', row)

    def test_the_stray_check_is_the_last_line(self):
        lines = agentstart.lines(self.repo.root, KIT)
        self.assertEqual(lines[-1], 'STRAY-LINE')
        self.assertEqual(os.path.normcase(os.path.abspath(self.stray.call_args[0][0])),
                         os.path.normcase(os.path.abspath(self.repo.root)))


class Worktree(Fixture):
    def setUp(self):
        super().setUp()
        self.wt = self.repo.worktree('y', 'x/y')

    def test_it_names_the_worktree_branch_and_counts(self):
        self.repo.write('b.txt', 'b\n', root=self.wt)
        self.repo.commit('work', 'b.txt', cwd=self.wt)
        self.repo.write('c.txt', 'c\n', root=self.wt)
        lines = agentstart.lines(self.wt, KIT)
        row = self.row(lines, 'checkout ')
        self.assertIn('worktree', row)
        self.assertIn('branch x/y @ %s' % self.repo.git('rev-parse', '--short', 'HEAD', cwd=self.wt), row)
        self.assertIn('behind 0, ahead 1', self.row(lines, 'vs base '))
        self.assertIn('1 file(s)', self.row(lines, 'dirty '))
        self.assertNotIn('new_worktree', '\n'.join(lines))

    def test_a_stale_local_main_is_measured_against_origin(self):
        self.repo.write('m.txt', 'm\n')
        self.repo.commit('landed', 'm.txt')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('update-ref', 'refs/heads/main', self.head)
        row = self.row(agentstart.lines(self.wt, KIT), 'vs base ')
        self.assertIn('behind 1, ahead 0 (against origin/main)', row)

    def test_a_failed_count_is_a_question_mark_not_zero(self):
        real = cmd.run_text

        def broken(args, cwd, **kw):
            if args[:1] in (['rev-list'], ['status']):
                return None, 'git broke'
            return real(args, cwd, **kw)
        with mock.patch.object(agentstart.cmd, 'run_text', broken):
            lines = agentstart.lines(self.wt, KIT)
        self.assertIn('behind ?, ahead ?', self.row(lines, 'vs base '))
        self.assertIn('UNKNOWN', self.row(lines, 'dirty '))


class Never(unittest.TestCase):
    def test_a_folder_in_no_work_tree_is_not_checked(self):
        d = tempfile.mkdtemp(prefix='kit-agentstart-')
        self.addCleanup(os.rmdir, d)
        lines = agentstart.lines(d, KIT)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('checkout  NOT CHECKED - '), lines)

    def test_no_cwd_is_not_checked(self):
        for cwd in (None, '', 'relative/path', 5):
            lines = agentstart.lines(cwd, KIT)
            self.assertEqual(len(lines), 1, lines)
            self.assertIn('NOT CHECKED', lines[0])

    def test_it_never_raises(self):
        with mock.patch.object(agentstart.repo, 'classify', side_effect=RuntimeError('boom')):
            lines = agentstart.lines('C:/r', KIT)
        self.assertEqual(lines, ['could not check: RuntimeError: boom'])
        with mock.patch.object(agentstart.threading, 'Thread', side_effect=RuntimeError('no threads')):
            self.assertEqual(agentstart.lines('C:/r', KIT), ['could not check: RuntimeError: no threads'])

    def test_the_budget_cuts_what_is_left(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(path):
            release.wait(5)
            raise RuntimeError('too late')
        start = time.monotonic()
        with mock.patch.object(agentstart.repo, 'classify', slow):
            lines = agentstart.lines('C:/r', KIT, budget=0.2)
        took = time.monotonic() - start
        self.assertLess(took, 1.5)
        self.assertEqual(lines, ['NOT CHECKED - the rest: over the 0.2 s budget'])


class NoNetwork(Fixture):
    def test_no_git_it_runs_reaches_a_remote(self):
        seen = []
        real = cmd.run_text

        def spy(args, cwd, **kw):
            seen.append(list(args))
            self.assertIn('timeout', kw)
            return real(args, cwd, **kw)
        wt = self.repo.worktree('n', 'x/n')
        with mock.patch.object(agentstart.cmd, 'run_text', spy):
            agentstart.lines(self.repo.root, KIT)
            agentstart.lines(wt, KIT)
        self.assertTrue(seen)
        for argv in seen:
            self.assertNotIn(argv[0], NETWORK, argv)


class Text(unittest.TestCase):
    def test_the_block_names_the_role_the_time_and_no_fetch(self):
        with mock.patch.object(agentstart, 'lines', return_value=['a', 'b']):
            got = agentstart.text('C:/r', KIT, 'worker')
        first, rest = got.split('\n', 1)
        self.assertTrue(first.startswith('Agent start (worker):'), first)
        self.assertIn('no fetch', first)
        self.assertRegex(first, r'read at \d\d:\d\d')
        self.assertRegex(first, r'\d+ ms')
        self.assertIn('preflight', first)
        self.assertEqual(rest, '- a\n- b')

    def test_no_role_is_a_subagent(self):
        with mock.patch.object(agentstart, 'lines', return_value=['a']):
            self.assertTrue(agentstart.text('C:/r', KIT).startswith('Agent start (subagent):'))


class Dispatched(unittest.TestCase):
    def test_a_dispatched_kit_role_is_one(self):
        for role in ('worker', 'pm-kit:worker', 'reviewer', 'senior-worker', 'opus-worker', 'test-runner'):
            self.assertTrue(agentstart.is_dispatched(role, KIT), role)

    def test_a_session_role_a_host_agent_or_nothing_is_not(self):
        for role in (None, '', 'Explore', 'general-purpose', 'pm', 'coordinator', 'consultant',
                     'deep-worker', 'pm-kit:coordinator', 'nobody'):
            self.assertFalse(agentstart.is_dispatched(role, KIT), role)

    def test_a_kit_with_no_roles_has_none(self):
        d = tempfile.mkdtemp(prefix='kit-agentstart-')
        self.addCleanup(os.rmdir, d)
        self.assertFalse(agentstart.is_dispatched('worker', d))


if __name__ == '__main__':
    unittest.main()
