"""kitlib.git.checkout: the write tools' main-checkout refusal, against real repositories."""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.git import checkout, repo
from tests import gitfixture


class MainRefusal(unittest.TestCase):

    def setUp(self):
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)

    def test_a_linked_worktree_may_be_written(self):
        wt = self.r.worktree('w', 'area/w')
        self.assertIsNone(checkout.main_refusal(wt, 'a commit', 'commit.py'))

    def test_the_main_checkout_is_refused_and_lists_the_worktrees(self):
        wt = self.r.worktree('w', 'area/w')
        why = checkout.main_refusal(self.r.root, 'a commit', 'commit.py', rule_pointer='CLAUDE.md')
        self.assertTrue(why.startswith('REFUSED: '), why)
        self.assertIn('so a commit was not written', why)
        self.assertIn('lander-only (`CLAUDE.md`)', why)
        self.assertIn('commit.py -C <worktree>', why)
        self.assertIn(os.path.normcase(os.path.normpath(wt)), os.path.normcase(why.replace('/', os.sep)))
        self.assertTrue(why.endswith('Nothing was written.'))

    def test_no_worktrees_says_how_to_make_one(self):
        why = checkout.main_refusal(self.r.root, 'a merge', 'sync_base.py')
        self.assertIn('none registered', why)
        self.assertNotIn('(`', why)

    def test_outside_every_checkout_is_not_refused(self):
        d = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, d)
        if repo.classify(d).kind != repo.NONE:
            self.skipTest('the temp folder is inside a git checkout')
        self.assertIsNone(checkout.main_refusal(d, 'a commit', 'commit.py'))

    def test_could_not_tell_is_refused(self):
        unknown = repo.Place(repo.UNKNOWN, None, 'git did not answer: timeout', None, None)
        with mock.patch.object(repo, 'classify', return_value=unknown):
            why = checkout.main_refusal(self.r.root, 'a commit', 'commit.py')
        self.assertIn('could not tell', why)
        self.assertIn('timeout', why)


if __name__ == '__main__':
    unittest.main()
