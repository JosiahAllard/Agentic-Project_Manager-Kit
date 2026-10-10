"""The one default-base rule (kitlib.git.refs.default_base, docs/gate.md "Default base") and the
environment of a kit network read (kitlib.git.cmd NETWORK_ENV), against throwaway repositories
(tests/gitfixture.py), and the callers that use the rule: each measures against origin/main when
the local main is stale, says so, and keeps a base the caller names."""
import unittest
from unittest import mock

from kitlib.docs import budget
from kitlib.git import cmd, refs
from kitlib.preflight import settings as pfsettings
from kitlib.reviewpack import _pack
from kitlib.rulingcites import check as cites_check
from kitlib.worktree import new as newwt
from tests import gitfixture

TRACKING = 'refs/remotes/origin/main'


def _stale(repo):
    """origin/main one commit ahead of local main (nobody pulled into the main checkout)."""
    repo.write('later.txt', 'later\n')
    later = repo.commit('later', 'later.txt')
    repo.git('push', '-q', 'origin', 'main')
    repo.git('update-ref', 'refs/heads/main', repo.initial)
    return later


class DefaultBase(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)

    def test_local_main_behind_origin_measures_origin_and_says_so(self):
        later = _stale(self.repo)
        rev, note = refs.default_base(self.repo.root, 'origin', 'main')
        self.assertEqual(rev, TRACKING)
        self.assertEqual(refs.resolve(rev, self.repo.root)[0], later)
        self.assertEqual(note, 'base: local main %s is 1 behind origin/main %s; measuring against %s'
                         % (self.repo.initial[:7], later[:7], TRACKING))

    def test_equal_measures_origin_quietly(self):
        self.assertEqual(refs.default_base(self.repo.root, 'origin', 'main'), (TRACKING, None))

    def test_local_main_ahead_is_named(self):
        self.repo.write('mine.txt', 'mine\n')
        self.repo.commit('mine', 'mine.txt')
        rev, note = refs.default_base(self.repo.root, 'origin', 'main')
        self.assertEqual(rev, TRACKING)
        self.assertIn('is 1 ahead of origin/main', note)

    def test_no_remote_measures_local_main(self):
        plain = gitfixture.TempRepo()
        self.addCleanup(plain.cleanup)
        self.assertEqual(refs.default_base(plain.root, 'origin', 'main'), ('main', None))

    def test_another_remote_name_is_used(self):
        self.assertEqual(refs.default_base(self.repo.root, 'upstream', 'main'), ('main', None))

    def test_no_local_main_measures_origin(self):
        self.repo.git('checkout', '-q', '-b', 'kit/x')
        self.repo.git('branch', '-q', '-D', 'main')
        self.assertEqual(refs.default_base(self.repo.root, 'origin', 'main'), (TRACKING, None))


class Callers(unittest.TestCase):
    """Each caller measures against origin/main when local main is stale, names it, and keeps an
    explicit base. One repository, read only, shared by the class."""

    @classmethod
    def setUpClass(cls):
        cls.repo = gitfixture.TempRepo(bare_remote=True)
        cls.later = _stale(cls.repo)
        cls.repo.git('checkout', '-q', '-b', 'kit/x', cls.later)
        cls.repo.write('x.txt', 'x\n')
        cls.repo.commit('x', 'x.txt')

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    def test_doc_budget_default_and_explicit(self):
        out = []
        self.assertEqual(budget.run(self.repo.root, out.append), 0, out)
        self.assertIn('doc_budget: base: local main', '\n'.join(out))
        out = []
        self.assertEqual(budget.run(self.repo.root, out.append, base='main'), 0, out)
        self.assertNotIn('measuring against', '\n'.join(out))

    def test_review_pack_default_and_explicit(self):
        lines = []
        _pack(lines, self.repo.root, 'kit/x', None, None)
        text = '\n'.join(lines)
        self.assertIn('measuring against %s' % TRACKING, text)
        self.assertIn('base     %s @ %s' % (TRACKING, self.later[:7]), text)
        lines = []
        _pack(lines, self.repo.root, 'kit/x', 'main', None)
        text = '\n'.join(lines)
        self.assertNotIn('measuring against', text)
        self.assertIn('base     main @ %s' % self.repo.initial[:7], text)

    def test_ruling_cites_names_the_base(self):
        with mock.patch('kitlib.rulingcites.ledgermod.read_entries', return_value=([], None)), \
                mock.patch('kitlib.rulingcites.added_lines', return_value={}) as added:
            outcome = cites_check(self.repo.root, ledger='ledger.md')
            explicit = cites_check(self.repo.root, base='main', ledger='ledger.md')
        self.assertEqual(added.call_args_list, [mock.call(self.repo.root, TRACKING),
                                                mock.call(self.repo.root, 'main')])
        self.assertIn('measuring against', outcome.lines[0])
        self.assertNotIn('measuring against', '\n'.join(explicit.lines))


class PreflightMeasure(unittest.TestCase):
    class Cfg:
        remote = 'origin'

    def test_rows_measure_origin_only_when_it_differs(self):
        m = pfsettings.measure_rev
        self.assertEqual(m(self.Cfg, 'main', 'aaa', 'bbb'), TRACKING)
        self.assertEqual(m(self.Cfg, 'main', 'aaa', 'aaa'), 'main')
        self.assertEqual(m(self.Cfg, 'main', 'aaa', ''), 'main')
        self.assertEqual(m(self.Cfg, 'main', 'aaa', 'bbb', explicit='main'), 'main')


class NewWorktreeBase(unittest.TestCase):
    def test_a_remote_qualified_base_names_the_branch(self):
        for given in ('origin/foo', 'refs/remotes/origin/foo', 'refs/heads/foo', 'foo'):
            self.assertEqual(newwt.branch_name(given, 'origin'), 'foo', given)
        self.assertEqual(newwt.branch_name('upstream/foo', 'origin'), 'upstream/foo')
        self.assertEqual(newwt.branch_name('origin/', 'origin'), 'origin/')


class NetworkEnv(unittest.TestCase):
    def test_subcommand_skips_global_options(self):
        self.assertEqual(cmd.subcommand(['-C', 'x', '-c', 'a=b', '--no-pager', 'fetch', 'o']),
                         'fetch')
        self.assertEqual(cmd.subcommand(['--git-dir', 'g', 'ls-remote']), 'ls-remote')
        self.assertEqual(cmd.subcommand(['-c', 'a=b']), '')

    def test_fetch_and_ls_remote_get_the_network_env(self):
        for args in (['fetch', 'origin'], ['-C', 'x', 'fetch'], ['ls-remote', 'origin', 'main']):
            env = cmd.child_env({'PATH': 'p'}, args)
            for k, v in cmd.NETWORK_ENV.items():
                self.assertEqual(env[k], v, args)
        self.assertEqual(cmd.NETWORK_ENV['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(cmd.NETWORK_ENV['GCM_INTERACTIVE'], 'never')
        self.assertEqual(cmd.network_env(['fetch'], {})['GCM_INTERACTIVE'], 'never')
        self.assertEqual(cmd.NETWORK_ENV['GIT_HTTP_LOW_SPEED_TIME'], '60')

    def test_other_commands_and_push_do_not(self):
        for args in (['status'], ['push', 'origin', 'main'], ['log', 'fetch']):
            env = cmd.child_env({'PATH': 'p'}, args)
            self.assertNotIn('GIT_TERMINAL_PROMPT', env, args)
            self.assertNotIn('GCM_INTERACTIVE', env, args)

    def test_ssh_is_left_to_the_user(self):
        for args in (['fetch', 'origin'], ['ls-remote', 'origin']):
            env = cmd.child_env({'PATH': 'p', 'GIT_SSH_COMMAND': 'plink -batch'}, args)
            self.assertEqual(env['GIT_SSH_COMMAND'], 'plink -batch')
            self.assertNotIn('GIT_SSH_COMMAND', cmd.child_env({'PATH': 'p'}, args))

    def test_network_env_leaves_the_callers_mapping_alone(self):
        mine = {'PATH': 'p'}
        out = cmd.network_env(['fetch'], mine)
        self.assertEqual(mine, {'PATH': 'p'})
        self.assertEqual(out['GIT_TERMINAL_PROMPT'], '0')

    def test_call_hands_a_fetch_the_env(self):
        seen = []

        def fake(argv, **kw):
            seen.append((argv, kw['env']))
            return 0, b'', b''
        with mock.patch.object(cmd, '_run', side_effect=fake):
            cmd.call(['fetch', 'origin', '--quiet'], '.', env={'PATH': 'p'})
            cmd.call(['status'], '.', env={'PATH': 'p'})
        self.assertEqual(seen[0][1]['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(seen[0][1]['LC_ALL'], 'C')
        self.assertNotIn('GIT_TERMINAL_PROMPT', seen[1][1])

    def test_a_real_fetch_runs_with_it(self):
        """A fetch through call() against the fixture's bare remote: the env does not break it."""
        repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(repo.cleanup)
        r = cmd.call(['fetch', 'origin', '--quiet'], repo.root)
        self.assertEqual(r.rc, 0, r.err)


if __name__ == '__main__':
    unittest.main()
