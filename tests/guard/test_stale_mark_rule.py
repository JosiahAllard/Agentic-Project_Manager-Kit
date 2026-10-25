"""The guard's git-worktree-remove rule (kitlib.guard.rules.git_worktree_remove) takes a stale
mark that holds (kitlib.worktree.marks, `worktree_remove.py <branch> --mark-stale`) as the PM's
word to remove an unmerged worktree, the way the nightly sweep does, and fails closed on a void or
unreadable one. The refusal never points at --mark-stale. Throwaway repositories only
(tests/gitfixture.py), judged by a real guard Ctx (filesystem places, real git refs) with a temp
home and a temp folder that holds neither tree."""
import os
import time
import unittest
from unittest import mock

from kitlib.guard import context
from kitlib.worktree import marks, sweep
from tests.realgit import real_git
from tests.worktree import test_marks

from . import world as W
from .claudehost import decide


@real_git('judges removes of real worktrees of a throwaway repository')
class StaleMarkWord(test_marks.MarkCase):

    def setUp(self):
        super().setUp()
        self.wt = self.workstream('old')
        side = os.path.join(self.fx.base, 'side')
        self.ghome = os.path.join(side, 'home')
        self.tmp = os.path.join(side, 'tmp')
        for d in (self.ghome, self.tmp):
            os.makedirs(d)

    def judge(self, payload):
        ctx = context.Ctx(environ={}, home=self.ghome, tempdir=self.tmp, lock_held=lambda: (False, 'none'))
        return decide(payload, ctx)

    def remove_from_main(self, path=None):
        """The remove as a coordinator's session runs it, from the main checkout (an agent's own
        write jail holds only its own tree)."""
        return W.bash('git -C %s worktree remove %s' % (W.fwd(self.repo), W.fwd(path or self.wt)), self.repo)

    def marked(self, branch='kit/old'):
        rc, out = self.mark(branch)
        self.assertEqual(rc, 0, out)

    def refused(self, payload):
        d = self.judge(payload)
        self.assertIsNotNone(d, 'the remove was let through')
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-worktree-remove'), d.reason)
        self.assertNotIn('mark-stale', d.reason)
        return d.reason

    # -- a mark that holds is the PM's word

    def test_a_live_mark_lets_the_unmerged_tree_be_removed(self):
        self.marked()
        self.assertIsNone(self.judge(self.remove_from_main()))

    def test_a_live_mark_counts_from_the_trees_own_folder_in_powershell(self):
        self.marked()
        self.assertIsNone(self.judge(W.ps('git worktree remove %s' % self.wt, self.wt, sub=True)))

    # -- a void or unreadable mark is no word

    def test_a_tip_moved_after_the_mark_voids_it(self):
        self.marked()
        self.fx.write('later.txt', 'later\n', root=self.wt)
        self.fx.commit('later', 'later.txt', cwd=self.wt)
        reason = self.refused(self.remove_from_main())
        self.assertIn('A stale mark does not count: its stale mark is void: the tip moved', reason)

    def test_activity_after_the_mark_voids_it(self):
        self.marked()
        later = time.time() + 120
        os.utime(os.path.join(self.wt, 'old-0.txt'), (later, later))      # the same bytes: status stays clean
        self.assertEqual(self.git('status', '--porcelain', cwd=self.wt), '')
        reason = self.refused(self.remove_from_main())
        self.assertIn('its stale mark is void: ', reason)
        self.assertIn('old-0.txt changed at ', reason)

    def test_a_mark_naming_another_path_is_void(self):
        self.marked()
        got = self.stored()
        got[0]['path'] = self.repo
        marks.save(self.marks_path(), got)
        reason = self.refused(self.remove_from_main())
        self.assertIn('its stale mark is void: it names the worktree %s, not this one' % os.path.normpath(self.repo),
                      reason)

    def test_an_unreadable_marks_file_is_no_word(self):
        self.marked()
        with open(self.marks_path(), 'w', encoding='utf-8') as f:
            f.write('{"version": 1, "marks": [')
        reason = self.refused(self.remove_from_main())
        self.assertIn('A stale mark does not count: the stale marks cannot be read (', reason)

    def test_last_activity_that_cannot_be_read_is_no_word(self):
        self.marked()
        with mock.patch.object(sweep, 'last_activity', return_value=(None, 'the tree could not be read in full')):
            reason = self.refused(self.remove_from_main())
        self.assertIn('its stale mark cannot be checked: its last activity cannot be read '
                      '(the tree could not be read in full)', reason)

    # -- what stays as it was

    def test_no_mark_is_refused_as_before(self):
        reason = self.refused(self.remove_from_main())
        self.assertIn('is not merged into main (not an ancestor). Merge it first;', reason)
        self.assertNotIn('stale mark', reason)

    def test_a_mark_on_another_branch_does_not_count_for_this_one(self):
        other = self.workstream('other')
        self.marked('kit/other')
        self.assertNotIn('stale mark', self.refused(self.remove_from_main()))
        self.assertIsNone(self.judge(self.remove_from_main(other)))

    def test_a_dirty_tree_is_refused_whatever_its_mark(self):
        self.marked()
        self.fx.write('new.txt', 'untracked\n', root=self.wt)
        reason = self.refused(self.remove_from_main())
        self.assertIn('removes a worktree that is not clean', reason)


if __name__ == '__main__':
    unittest.main()
