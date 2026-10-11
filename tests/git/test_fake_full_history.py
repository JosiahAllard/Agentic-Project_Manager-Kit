"""The fake git's `log --full-history` (kitlib/git/fake/history.py `Walker.run`), with a twin on
real git so the fake is checked against git itself (docs/test-tools.md "Fake git").

The history: a side branch adds `x.md` and deletes it again, and adds `y.md`; main moves on; the
side branch is merged with --no-ff. The merge is TREESAME to both parents for `x.md` and to the
side parent only for `y.md`."""
import unittest

from tests.gitfixture import TempRepo
from tests.realgit import real_git


class FullHistory(unittest.TestCase):

    def setUp(self):
        r = self.r = TempRepo()
        self.addCleanup(r.cleanup)
        r.git('checkout', '-q', '-b', 'side')
        r.write('x.md', 'x\n')
        self.add_x = r.commit('add x', 'x.md')
        r.git('rm', '-q', '--', 'x.md')
        r.git('commit', '-q', '-m', 'delete x')
        self.del_x = r.head()
        r.write('y.md', 'y\n')
        self.add_y = r.commit('add y', 'y.md')
        r.git('checkout', '-q', 'main')
        r.write('z.txt', 'z\n')
        r.commit('main moves', 'z.txt')
        r.git('merge', '-q', '--no-ff', '-m', 'merge side', 'side')
        self.merge = r.head()

    def log(self, *args):
        out = self.r.git('log', '--format=%H', *args)
        return set(out.split()) if out else set()

    def test_default_simplification_prunes_a_side_branch_the_merge_is_treesame_to(self):
        self.assertEqual(self.log('--diff-filter=D', '--', 'x.md'), set())
        self.assertEqual(self.log('--', 'x.md'), set())

    def test_full_history_walks_every_parent_of_a_merge(self):
        self.assertEqual(self.log('--full-history', '--no-renames', '--diff-filter=D', '--',
                                  'x.md'), {self.del_x})
        self.assertEqual(self.log('--full-history', '--', 'x.md'), {self.add_x, self.del_x})

    def test_full_history_shows_a_merge_unless_it_is_treesame_to_every_parent(self):
        self.assertEqual(self.log('--', 'y.md'), {self.add_y})
        self.assertEqual(self.log('--full-history', '--', 'y.md'), {self.add_y, self.merge})

    def test_a_diff_filter_never_shows_a_merge(self):
        # w.md is deleted on main (C) while a branch cut before C still has it; the --no-ff merge
        # differs from that branch's parent for w.md, yet git names C, not the merge.
        r = self.r
        r.write('w.md', 'w\n')
        r.commit('add w', 'w.md')
        r.git('branch', 'stale')
        r.git('rm', '-q', '--', 'w.md')
        r.git('commit', '-q', '-m', 'delete w on main')
        deleted = r.head()
        r.git('checkout', '-q', 'stale')
        r.write('v.txt', 'v\n')
        r.commit('stale moves', 'v.txt')
        r.git('checkout', '-q', 'main')
        r.git('merge', '-q', '--no-ff', '-m', 'merge stale', 'stale')
        self.assertEqual(self.log('--full-history', '--no-renames', '--diff-filter=D', '--',
                                  'w.md'), {deleted})
        self.assertEqual(self.r.git('log', '-1', '--format=%H', '--full-history', '--no-renames',
                                    '--diff-filter=D', '--', 'w.md'), deleted)


@real_git('fidelity twin: the same --full-history walk answered by git itself')
class FullHistoryRealGit(FullHistory):
    pass


if __name__ == '__main__':
    unittest.main()
