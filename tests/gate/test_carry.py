"""kitlib.gate.carry (docs/gate.md "Carrying a verdict forward"): a reviewed tip's verdict holds
for a later tip only when every commit after the review is a conflict-free merge of main, made as
git makes it, and the branch's own diff against main is the one reviewed (-U3, so main's edits
within three lines of a branch hunk count). Anything else, or git giving no answer, refuses. On
the scratch repository of tests/gate/fixtures.py GateRepoCase."""
import re
import unittest

from kitlib.gate import carry
from kitlib.git import cmd
from tests.gate.fixtures import GateRepoCase
from tests.realgit import real_git

LINES = ''.join('line %d\n' % i for i in range(1, 21))


def edit(text, n, new):
    """`text` with its line `n` (1-based) replaced by `new`."""
    lines = text.splitlines(True)
    lines[n - 1] = new + '\n'
    return ''.join(lines)


class CarryCase(GateRepoCase):
    def on_main(self, files, message='main edit'):
        """Commit `files` on main and push it: the new main."""
        for rel, text in files.items():
            self.repo.write(rel, text)
        self.main = self.repo.commit(message, *files)
        self.repo.git('push', '-q', 'origin', 'main')
        return self.main

    def chmod_on_main(self, rel):
        """Make `rel` executable on main (its mode only) and push it: the new main."""
        self.repo.git('update-index', '--chmod=+x', '--', rel)
        self.repo.git('commit', '-q', '-m', 'main makes %s executable' % rel)
        self.main = self.repo.head()
        self.repo.git('push', '-q', 'origin', 'main')
        return self.main

    def sync(self, wt):
        """Merge main into the worktree `wt` as git merges it: the merge commit."""
        self.repo.git('merge', '-q', '--no-edit', 'main', cwd=wt)
        return self.repo.head(wt)

    def more(self, wt, files, message='more work'):
        for rel, text in files.items():
            self.repo.write(rel, text, root=wt)
        return self.repo.commit(message, *files, cwd=wt)

    def check(self, reviewed, tip, base=None, git=None):
        return carry.check(self.repo.root, reviewed, tip, base or self.main, git=git)

    def assert_carries(self, reviewed, tip, merges):
        facts, why = self.check(reviewed, tip)
        self.assertIsNone(why)
        self.assertEqual(facts['from'], reviewed)
        self.assertEqual(facts['merges'], merges)
        self.assertEqual(facts['main'], self.main)
        self.assertRegex(facts['diff'], r'^[0-9a-f]{64}$')
        return facts

    def assert_refused(self, reviewed, tip, *words, base=None):
        facts, why = self.check(reviewed, tip, base=base)
        self.assertIsNone(facts)
        self.assertIsInstance(why, str)
        for w in words:
            self.assertIn(w, why)
        return why


class Carries(CarryCase):
    def test_a_clean_main_merge_carries(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        tip = self.sync(wt)
        self.assert_carries(reviewed, tip, [tip])

    def test_two_main_merges_carry_oldest_first(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        first = self.sync(wt)
        self.on_main({'n.py': 'n\n'})
        tip = self.sync(wt)
        self.assert_carries(reviewed, tip, [first, tip])

    def test_main_editing_a_branch_file_far_from_its_hunk_carries(self):
        self.on_main({'f.txt': LINES})
        wt, reviewed = self.branch('feature', {'f.txt': edit(LINES, 2, 'branch')})
        self.on_main({'f.txt': edit(LINES, 18, 'main')})
        tip = self.sync(wt)
        self.assert_carries(reviewed, tip, [tip])

    def test_a_binary_file_main_left_alone_carries(self):
        wt, reviewed = self.branch('feature', {'b.bin': 'bin\0ary\n', 'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        tip = self.sync(wt)
        self.assert_carries(reviewed, tip, [tip])

    def test_the_diff_digest_is_the_same_for_the_same_branch_diff(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        first = self.sync(wt)
        one = self.assert_carries(reviewed, first, [first])
        self.on_main({'n.py': 'n\n'})
        tip = self.sync(wt)
        two = self.assert_carries(reviewed, tip, [first, tip])
        self.assertEqual(one['diff'], two['diff'])

    def test_a_hunk_main_moved_with_its_context_intact_carries(self):
        # A known limit, pinned: hunk positions and the enclosing name on each `@@` line are left
        # out of the comparison (kitlib.gate.carry._patch), so a hunk main moved compares equal
        # while its three context lines are intact, even when main put a new definition above it.
        self.on_main({'f.txt': LINES})
        wt, reviewed = self.branch('feature', {'f.txt': edit(LINES, 15, 'branch')})
        self.on_main({'f.txt': edit(LINES, 8, 'line 8\ndef other():\n    pass')})
        tip = self.sync(wt)
        moved = re.search(r'^@@ -(\d+)', self.repo.git('diff', '-U3', self.main, tip), re.M)
        first = re.search(r'^@@ -(\d+)', self.repo.git('diff', '-U3', '%s~1' % reviewed, reviewed),
                          re.M)
        self.assertEqual(int(moved.group(1)), int(first.group(1)) + 2)
        self.assert_carries(reviewed, tip, [tip])


class Refuses(CarryCase):
    def test_nothing_after_the_review_is_nothing_to_carry(self):
        _, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.assert_refused(reviewed, reviewed, 'nothing to carry')

    def test_a_branch_commit_after_the_merge_refuses(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        self.sync(wt)
        tip = self.more(wt, {'b.py': 'b\n'})
        self.assert_refused(reviewed, tip, tip[:10], 'not a merge')

    def test_a_branch_commit_before_the_merge_refuses(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        own = self.more(wt, {'b.py': 'b\n'})
        self.on_main({'m.py': 'm\n'})
        tip = self.sync(wt)
        self.assert_refused(reviewed, tip, own[:10], 'not a merge')

    def test_a_reviewed_commit_the_tip_does_not_hold_refuses(self):
        _, reviewed = self.branch('other', {'o.py': 'o\n'})
        wt, _ = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        tip = self.sync(wt)
        self.assert_refused(reviewed, tip, 'does not hold the reviewed %s' % reviewed[:10])

    def test_a_merge_of_a_commit_not_on_main_refuses(self):
        _, other = self.branch('other', {'o.py': 'o\n'})
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.repo.git('merge', '-q', '--no-edit', 'kit/other', cwd=wt)
        tip = self.repo.head(wt)
        self.assert_refused(reviewed, tip, other[:10], 'not in main')

    def test_a_conflict_resolved_by_hand_refuses(self):
        self.on_main({'f.txt': LINES})
        wt, reviewed = self.branch('feature', {'f.txt': edit(LINES, 5, 'branch')})
        self.on_main({'f.txt': edit(LINES, 5, 'main')})
        self.repo.git('merge', '-q', '--no-edit', 'main', cwd=wt, check=False)
        tip = self.more(wt, {'f.txt': edit(LINES, 5, 'both')}, message='resolve')
        self.assertEqual(len(self.repo.git('rev-list', '--parents', '-n', '1', tip).split()), 3)
        self.assert_refused(reviewed, tip, tip[:10], 'conflict')

    def test_a_clean_merge_edited_before_its_commit_refuses(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        self.repo.git('merge', '-q', '--no-ff', '--no-commit', 'main', cwd=wt)
        tip = self.more(wt, {'a.py': 'changed in the merge\n'}, message='merge main')
        self.assertEqual(len(self.repo.git('rev-list', '--parents', '-n', '1', tip).split()), 3)
        self.assert_refused(reviewed, tip, tip[:10], 'not the tree git merges')

    def test_main_editing_within_three_lines_of_a_branch_hunk_refuses(self):
        self.on_main({'f.txt': LINES})
        wt, reviewed = self.branch('feature', {'f.txt': edit(LINES, 5, 'branch')})
        self.on_main({'f.txt': edit(LINES, 8, 'main')})
        tip = self.sync(wt)
        self.assertEqual(self.repo.git('show', '%s:f.txt' % tip),
                         edit(edit(LINES, 5, 'branch'), 8, 'main').strip())
        self.assert_refused(reviewed, tip, "branch's own diff", 'f.txt')

    def test_an_octopus_merge_refuses(self):
        _, other = self.branch('other', {'o.py': 'o\n'})
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        tree = self.repo.git('rev-parse', '%s^{tree}' % reviewed)
        tip = self.repo.git('commit-tree', tree, '-p', reviewed, '-p', self.main, '-p', other,
                            '-m', 'octopus')
        self.assert_refused(reviewed, tip, tip[:10], '3 parents')

    def test_a_base_that_does_not_hold_the_merged_main_refuses(self):
        old_main = self.main
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        merged = self.on_main({'m.py': 'm\n'})
        tip = self.sync(wt)
        self.assert_refused(reviewed, tip, merged[:10], 'not in main', base=old_main)

    def test_a_git_that_gives_no_answer_refuses(self):
        wt, reviewed = self.branch('feature', {'a.py': 'a\n'})
        self.on_main({'m.py': 'm\n'})
        tip = self.sync(wt)
        for verb in ('rev-list', 'merge-tree', 'merge-base', 'ls-tree', 'diff'):
            def git(args, verb=verb):
                if args[0] == verb:
                    return cmd.Result(None, b'', 'git timed out')
                return cmd.call(args, self.repo.root)
            facts, why = self.check(reviewed, tip, git=git)
            self.assertIsNone(facts, verb)
            self.assertIn('git timed out', why, verb)

    def test_revs_that_are_not_full_hashes_refuse_before_git(self):
        def git(args):
            raise AssertionError('git ran: %s' % (args,))
        for bad in ('HEAD', '-x', 'a' * 39):
            facts, why = carry.check(self.repo.root, bad, 'b' * 40, 'c' * 40, git=git)
            self.assertIsNone(facts)
            self.assertTrue(re.search('full commit hash', why), why)


@real_git("update-index --chmod: the fake has none, and what is pinned is git's own merge of a "
          'mode change')
class MainChangedTheModeOfABranchFile(CarryCase):
    """A path the branch changes whose mode main changed, its patch still the reviewed one
    (kitlib.gate.carry._same_diff past the patch comparison)."""

    def test_main_changing_the_mode_of_a_binary_the_branch_edits_refuses(self):
        # Main can change a binary the branch edits without a conflict only by its mode: two
        # content edits to a binary always conflict. Its patch says only "Binary files differ".
        self.on_main({'b.bin': 'bin\0one\n'})
        wt, reviewed = self.branch('feature', {'b.bin': 'bin\0two\n'})
        self.chmod_on_main('b.bin')
        tip = self.sync(wt)
        self.assert_refused(reviewed, tip, 'binary file', 'b.bin')

    def test_a_symlink_or_submodule_entry_main_changed_refuses(self):
        # git merges a file main turned into a symlink or submodule with the branch's edit only
        # with a conflict ("distinct types"), so the entry is made here: the mode main gave the
        # file is reported as each kind in turn.
        self.on_main({'f.txt': LINES})
        wt, reviewed = self.branch('feature', {'f.txt': edit(LINES, 2, 'branch')})
        self.chmod_on_main('f.txt')
        tip = self.sync(wt)
        self.assert_carries(reviewed, tip, [tip])
        for mode in (b'120000', b'160000'):
            def git(args, mode=mode):
                r = cmd.call(args, self.repo.root)
                if args[0] == 'ls-tree' and r.out:
                    r = cmd.Result(r.rc, r.out.replace(b'100755 blob', mode + b' blob'), r.err)
                return r
            facts, why = self.check(reviewed, tip, git=git)
            self.assertIsNone(facts, mode)
            self.assertIn('f.txt is not a plain file on both sides', why, mode)

    def test_main_changing_the_mode_of_a_file_the_branch_edits_carries(self):
        # Pinned as it is: the merge is clean (main's mode, the branch's lines), and the tip's
        # patch against the new merge base is the reviewed one, since both of its sides hold the
        # new mode and the one line that names a mode (`index`) is left out of the comparison.
        # The mode change is main's, so there is nothing of the branch's own to review again.
        self.on_main({'f.txt': LINES})
        wt, reviewed = self.branch('feature', {'f.txt': edit(LINES, 2, 'branch')})
        self.chmod_on_main('f.txt')
        tip = self.sync(wt)
        self.assertTrue(self.repo.git('ls-tree', tip, '--', 'f.txt').startswith('100755 '))
        self.assert_carries(reviewed, tip, [tip])


if __name__ == '__main__':
    unittest.main()
