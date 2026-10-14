"""A pull that would record the main branch as `pulled_by` is refused:
a landing closes only what the landed branch holds pulled, and the main branch never lands, so such
a record would stay pulled for ever. kitlib.backlog.pull_branch, end to end through Store.write on
a ref store whose repository has `main` checked out (the main checkout)."""
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import pmdata
from kitlib.backlog import plan as pmod
from kitlib.backlog import pull_branch
from kitlib.backlog.errors import Refusal

from tests.backlog.helpers import BRANCH, TODAY, StoreCase, priced, rec


class PullFromTheMainBranch(StoreCase):

    def pull(self, ctx, rid, **kw):
        change, _tip = self.store.write(lambda s: pull_branch.plan_pull(s, ctx, rid, **kw))
        return change

    def assert_refused_and_unwritten(self, ctx):
        before = self.remote_tip()
        with self.assertRaises(Refusal) as cm:
            self.pull(ctx, 'FOO')
        text = str(cm.exception)
        self.assertIn('pulled_by: main', text)
        self.assertIn('--as <branch>', text)
        self.assertIn("branch's worktree", text)
        self.assertEqual(self.remote_tip(), before)
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_pull_in_the_main_checkout_is_refused_and_writes_nothing(self):
        self.seed(priced('FOO'))
        self.assertEqual(self.repo.git('symbolic-ref', '--short', 'HEAD'), 'main')
        ctx = pmod.Context(self.store, self.repo.root, main_branch='main', today=TODAY)
        self.assert_refused_and_unwritten(ctx)

    def test_as_the_main_branch_is_refused_too(self):
        self.seed(priced('FOO'))
        self.repo.git('checkout', '-q', BRANCH)
        ctx = pmod.Context(self.store, self.repo.root, main_branch='main', branch='main',
                           today=TODAY)
        self.assert_refused_and_unwritten(ctx)

    def test_as_a_branch_pulls_from_the_main_checkout(self):
        self.seed(priced('FOO'))
        ctx = pmod.Context(self.store, self.repo.root, main_branch='main', branch=BRANCH,
                           today=TODAY)
        self.pull(ctx, 'FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('pulled_by')), ('pulled', BRANCH))

    def test_a_pull_on_a_branch_checkout_records_that_branch(self):
        self.seed(priced('FOO'))
        self.repo.git('checkout', '-q', BRANCH)
        ctx = pmod.Context(self.store, self.repo.root, main_branch='main', today=TODAY)
        self.pull(ctx, 'FOO')
        self.assertEqual(self.get('FOO').get('pulled_by'), BRANCH)

    def test_the_records_own_refusal_comes_first(self):
        # An unpriced record says why it cannot be pulled at all before where it is pulled from.
        self.seed(rec('FOO'))
        ctx = pmod.Context(self.store, self.repo.root, main_branch='main', today=TODAY)
        with self.assertRaises(Refusal) as cm:
            self.pull(ctx, 'FOO')
        self.assertNotIn('pulled_by: main', str(cm.exception))


class PullFromThePmDataWorktree(StoreCase):
    """In the PM data worktree the configured main is the data branch; the code repository's
    main (pull_branch.code_mains: the main checkout's configuration) is refused there too."""

    def setUp(self):
        StoreCase.setUp(self)
        tree = self.repo.git('rev-parse', 'main^{tree}')
        root = self.repo.git('commit-tree', tree, '-m', 'PM data root')
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root)
        self.pm = self.repo.worktree('pm', branch='pmdata')

    pull = PullFromTheMainBranch.pull
    assert_refused_and_unwritten = PullFromTheMainBranch.assert_refused_and_unwritten

    def ctx_as(self, branch, root=None):
        return pmod.Context(self.store, root or self.pm, main_branch='pmdata', branch=branch,
                            today=TODAY)

    def test_as_the_code_repositorys_main_is_refused(self):
        self.seed(priced('FOO'))
        self.assertEqual(pull_branch.code_mains(self.pm), ('main',))
        self.assert_refused_and_unwritten(self.ctx_as('main'))

    def test_as_a_branch_pulls_from_the_pm_data_worktree(self):
        self.seed(priced('FOO'))
        self.pull(self.ctx_as(BRANCH), 'FOO')
        self.assertEqual(self.get('FOO').get('pulled_by'), BRANCH)

    def test_an_unreadable_main_checkout_configuration_refuses_main_and_master(self):
        with mock.patch.object(kconfig, 'load', side_effect=kconfig.ConfigError('bad')):
            self.assertEqual(pull_branch.code_mains(self.pm), pull_branch.FALLBACK_MAINS)
            self.assertEqual(pull_branch.main_branches(self.ctx_as('master')),
                             ['pmdata', 'main', 'master'])

    def test_another_worktree_has_only_its_own_main(self):
        wt = self.repo.worktree('other')
        self.assertEqual(pull_branch.code_mains(wt), ())
        self.assertEqual(pull_branch.code_mains(self.repo.root), ())


class RefusalText(unittest.TestCase):

    def test_one_name_or_several(self):
        self.assertIsNone(pull_branch.refusal('FOO', BRANCH, 'main'))
        self.assertIn('pulled_by: main', pull_branch.refusal('FOO', 'main', 'main'))
        self.assertIn('pulled_by: master',
                      pull_branch.refusal('FOO', 'master', ['pmdata', 'main', 'master']))
        self.assertIsNone(pull_branch.refusal('FOO', BRANCH, ('pmdata', 'main')))


if __name__ == '__main__':
    unittest.main()
