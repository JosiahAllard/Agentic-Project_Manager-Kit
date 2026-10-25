"""The guard's git-worktree-remove rule (kitlib.guard.rules.git_worktree_remove) takes a stale
mark that holds (kitlib.worktree.marks, `worktree_remove.py <branch> --mark-stale`) as the PM's
word to remove an unmerged worktree, with the nightly sweep's path, tip and activity checks only,
and fails closed on a void or unreadable one, an unknown merge, a place or branch it cannot read
and a detached HEAD. The refusal never points at --mark-stale. Throwaway repositories only
(tests/gitfixture.py), judged by a real guard Ctx (filesystem places, real git refs) with a temp
home and a temp folder that holds neither tree."""
import os
import time
import unittest
from unittest import mock

from kitlib.git import refs
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

    def judge(self, payload, git=None, hide_common=False):
        """The guard's decision; `git` replaces kitlib.git.refs, `hide_common` makes the tree's
        place carry no git common dir."""
        ctx = context.Ctx(environ={}, home=self.ghome, tempdir=self.tmp, lock_held=lambda: (False, 'none'),
                          git=git)
        if hide_common:
            real = ctx.place

            def place(path):
                got = real(path)
                return got._replace(common=None, detail='the test hides it') if self.is_wt(path) else got
            ctx.place = place
        return decide(payload, ctx)

    def is_wt(self, path):
        return os.path.normcase(os.path.normpath(path or '')) == os.path.normcase(os.path.normpath(self.wt))

    def git_with(self, always=None, **for_the_tree):
        """kitlib.git.refs, but each call named in `for_the_tree` that names the tree, and each
        named in `always`, answers as given."""
        test, always = self, always or {}

        class Git(object):
            def __getattr__(self, name):
                real = getattr(refs, name)
                if name in always:
                    return lambda *a, **kw: always[name]
                if name not in for_the_tree:
                    return real
                return lambda *a, **kw: for_the_tree[name] if any(
                    isinstance(x, str) and test.is_wt(x) for x in a) else real(*a, **kw)
        return Git()

    def remove_from_main(self, path=None):
        """The remove as a coordinator's session runs it, from the main checkout (an agent's own
        write jail holds only its own tree)."""
        return W.bash('git -C %s worktree remove %s' % (W.fwd(self.repo), W.fwd(path or self.wt)), self.repo)

    def marked(self, branch='kit/old'):
        rc, out = self.mark(branch)
        self.assertEqual(rc, 0, out)

    def refused(self, payload, **kw):
        d = self.judge(payload, **kw)
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

    def test_a_lock_after_the_mark_voids_it_while_held(self):
        self.marked()
        self.git('worktree', 'lock', self.wt, cwd=self.repo)
        self.addCleanup(self.git, 'worktree', 'unlock', self.wt, cwd=self.repo)
        reason = self.refused(self.remove_from_main())
        self.assertIn('its stale mark is void: ', reason)
        self.assertIn('locked changed at ', reason)

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

    def test_a_tree_git_cannot_place_is_no_word(self):
        self.marked()
        reason = self.refused(self.remove_from_main(), hide_common=True)
        self.assertIn('A stale mark does not count: git cannot place its repository (the test hides it).', reason)

    def test_a_branch_that_cannot_be_read_is_no_word(self):
        self.marked()
        git = self.git_with(current_branch=(None, 'the test hides it'))
        reason = self.refused(self.remove_from_main(), git=git)
        self.assertIn('A stale mark does not count: its branch cannot be read (the test hides it).', reason)

    def test_a_detached_head_is_no_word(self):
        # the mark names kit/old and still matches path and tip: only the branch is gone
        self.marked()
        self.git('checkout', '-q', '--detach', cwd=self.wt)
        reason = self.refused(self.remove_from_main())
        self.assertIn('is not merged into main (not an ancestor). Merge it first;', reason)
        self.assertNotIn('stale mark', reason)

    def test_a_merge_git_cannot_tell_never_reads_the_mark(self):
        # a live mark, but git did not say the tip is unmerged: refused before any mark is read
        self.marked()
        git = self.git_with(always={'is_ancestor': (None, 'git did not answer for the test')})
        with mock.patch.object(sweep, 'last_activity', side_effect=AssertionError('the mark was read')):
            reason = self.refused(self.remove_from_main(), git=git)
        self.assertIn('is not merged into main (git did not answer for the test). Merge it first;', reason)
        self.assertNotIn('stale mark', reason)

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
