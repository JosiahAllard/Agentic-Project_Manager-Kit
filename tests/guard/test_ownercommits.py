"""kitlib.guard.ownercommits on a scratch repository: a commit that changes an owner-only path is a
violation unless its AUTHOR email is the owner's, for the paths that landing the tip on main would
change (NetChange); a merge is judged by what it changes beyond the clean merge of its parents;
commits already on main are not judged."""
import unittest
from unittest import mock

from kitlib.config import owner
from kitlib.guard import ownercommits as oc
from tests import gitfixture

OWNER = 'owner@example.invalid'
AGENT = 'agent@example.invalid'


class OwnerCommitsCase(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.main = self.repo.initial
        self.rules = owner.Rules(owner_email=OWNER)

    def commit_as(self, email, message, files, cwd=None):
        for rel, text in files.items():
            self.repo.write(rel, text, root=cwd)
        self.repo.git('add', '--', *files, cwd=cwd)
        self.repo.git('-c', 'user.email=%s' % email, 'commit', '-q', '-m', message, cwd=cwd)
        return self.repo.git('rev-parse', 'HEAD', cwd=cwd)

    def bad(self, tip, rules=None, exclude=None):
        found, why = oc.violations(self.repo.root, rules or self.rules, tip,
                                   [self.main] if exclude is None else exclude)
        self.assertIsNone(why)
        return found


class Commits(OwnerCommitsCase):
    def test_an_agent_commit_to_an_owner_only_path_is_a_violation(self):
        sha = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'x\n', 'a.py': 'a\n'})
        self.assertEqual(self.bad(sha), [oc.Violation(sha, AGENT, ['CLAUDE.md'])])

    def test_the_owners_commit_is_not(self):
        sha = self.commit_as(OWNER, 'owner edits CLAUDE.md', {'CLAUDE.md': 'x\n'})
        self.assertEqual(self.bad(sha), [])

    def test_owner_email_compares_case_insensitively(self):
        sha = self.commit_as(OWNER.upper(), 'owner edits CLAUDE.md', {'CLAUDE.md': 'x\n'})
        self.assertEqual(self.bad(sha), [])

    def test_ordinary_paths_and_excepted_paths_are_not(self):
        sha = self.commit_as(AGENT, 'agent work', {'a.py': 'a\n', '.claude/worktrees/x/y.txt': 'y\n'})
        self.assertEqual(self.bad(sha), [])

    def test_a_directory_entry_locks_what_is_under_it(self):
        sha = self.commit_as(AGENT, 'agent edits a workflow', {'.github/workflows/ci.yml': 'x\n'})
        self.assertEqual(self.bad(sha), [oc.Violation(sha, AGENT, ['.github/workflows/ci.yml'])])

    def test_a_deletion_counts(self):
        self.commit_as(OWNER, 'owner adds CLAUDE.md', {'CLAUDE.md': 'x\n'})
        self.main = self.repo.git('rev-parse', 'HEAD')
        self.repo.git('rm', '-q', 'CLAUDE.md')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'agent deletes it')
        sha = self.repo.git('rev-parse', 'HEAD')
        self.assertEqual(self.bad(sha), [oc.Violation(sha, AGENT, ['CLAUDE.md'])])

    def test_commits_in_exclude_are_not_judged(self):
        sha = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'x\n'})
        later = self.commit_as(AGENT, 'agent work', {'a.py': 'a\n'})
        self.assertEqual(self.bad(later, exclude=[sha]), [])
        self.assertEqual([v.sha for v in self.bad(later)], [sha])

    def test_every_offending_commit_is_named(self):
        one = self.commit_as(AGENT, 'one', {'CLAUDE.md': '1\n'})
        two = self.commit_as(AGENT, 'two', {'.kit.json': '{}\n'})
        self.assertEqual(sorted(v.sha for v in self.bad(two)), sorted([one, two]))

    def test_no_owner_email_fails_closed(self):
        sha = self.commit_as(OWNER, 'owner edits CLAUDE.md', {'CLAUDE.md': 'x\n'})
        rules = owner.Rules()
        found = self.bad(sha, rules=rules)
        self.assertEqual([v.sha for v in found], [sha])
        self.assertIn('identity is not configured', oc.describe(rules, found))

    def test_an_owner_email_also_listed_as_an_agents_proves_nothing(self):
        sha = self.commit_as(OWNER, 'owner edits CLAUDE.md', {'CLAUDE.md': 'x\n'})
        rules = owner.Rules(owner_email=OWNER, agent_emails=[OWNER])
        self.assertEqual([v.sha for v in self.bad(sha, rules=rules)], [sha])

    def test_a_mailmap_does_not_make_an_agent_the_owner(self):
        self.commit_as(OWNER, 'owner maps the agent', {'.mailmap': 'Owner <%s> <%s>\n' % (OWNER, AGENT)})
        self.main = self.repo.git('rev-parse', 'HEAD')
        sha = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'x\n'})
        self.assertEqual([v.author for v in self.bad(sha)], [AGENT])

    def test_empty_and_root_commits_in_one_batch(self):
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '--allow-empty', '-m', 'empty')
        empty = self.repo.git('rev-parse', 'HEAD')
        sha = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'x', 'a.py': 'a'})
        found, why = oc.plain_changes(self.repo.root, [sha, empty, self.repo.initial])
        self.assertIsNone(why)
        self.assertEqual(found, {sha: ['CLAUDE.md', 'a.py'], empty: [], self.repo.initial: ['README.md']})
        for one in (sha, empty, self.repo.initial):           # the batch agrees with one at a time
            parents = self.repo.git('log', '-1', '--format=%P', one).split()
            self.assertEqual(oc.changed(self.repo.root, one, parents), (found[one], None))
        self.assertEqual(self.bad(sha, exclude=[]), [oc.Violation(sha, AGENT, ['CLAUDE.md'])])

    def test_a_bad_revision_is_an_error_not_a_pass(self):
        found, why = oc.violations(self.repo.root, self.rules, '1' * 40, [self.main])
        self.assertIsNone(found)
        self.assertTrue(why)
        found, why = oc.violations(self.repo.root, self.rules, '--all')
        self.assertIsNone(found)
        self.assertTrue(why)

    def test_describe_names_sha_author_and_paths(self):
        sha = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'x\n'})
        line = oc.describe(self.rules, self.bad(sha))
        self.assertIn('%s by %s changes CLAUDE.md' % (sha[:10], AGENT), line)
        self.assertNotIn('not configured', line)


class NetChange(OwnerCommitsCase):
    """An owner-only path counts only when landing the tip on the target main would change it;
    when it would, every commit in the range that touches it must be the owner's."""

    def setUp(self):
        OwnerCommitsCase.setUp(self)
        self.main = self.commit_as(OWNER, 'owner adds CLAUDE.md', {'CLAUDE.md': 'owner\n'})

    def revert(self, sha):
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', sha)
        return self.repo.git('rev-parse', 'HEAD')

    def test_an_agent_change_then_revert_is_allowed(self):
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n', 'a.py': 'a\n'})
        tip = self.revert(edit)
        self.assertEqual(self.bad(tip), [])

    def test_a_kept_agent_change_is_refused_naming_every_commit_that_touched_it(self):
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n'})
        undo = self.revert(edit)
        again = self.commit_as(AGENT, 'agent edits it again', {'CLAUDE.md': 'kept\n'})
        tip = self.commit_as(AGENT, 'agent work', {'a.py': 'a\n'})
        self.assertEqual(sorted(v.sha for v in self.bad(tip)), sorted([edit, undo, again]))

    def test_a_rename_away_and_back_is_allowed(self):
        self.repo.git('mv', 'CLAUDE.md', 'notes.md')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'rename away')
        self.repo.git('mv', 'notes.md', 'CLAUDE.md')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'rename back')
        self.assertEqual(self.bad(self.repo.git('rev-parse', 'HEAD')), [])

    def test_a_kept_rename_away_is_refused(self):
        self.repo.git('mv', 'CLAUDE.md', 'notes.md')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'rename away')
        sha = self.repo.git('rev-parse', 'HEAD')
        self.assertEqual(self.bad(sha), [oc.Violation(sha, AGENT, ['CLAUDE.md'])])

    def test_with_no_target_every_touching_commit_is_judged(self):
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n'})
        tip = self.revert(edit)
        self.assertEqual(sorted(v.sha for v in self.bad(tip, exclude=[])), sorted([edit, tip]))


class NetChangeGitFailures(OwnerCommitsCase):
    """Every git failure inside net_change is `(None, why)`, never an empty set (which would pass):
    the real git on a revision it does not have, and a git that answers wrongly (a fake for one
    subcommand; every other call goes to the real git)."""

    def setUp(self):
        OwnerCommitsCase.setUp(self)
        self.tip = self.commit_as(AGENT, 'agent work', {'a.py': 'a\n'})
        self.real_call = oc.cmd.call

    def fake_git(self, subcommand, rc, out='', err=''):
        real = self.real_call
        calls = []

        def call(args, *a, **kw):
            if subcommand in args:
                calls.append(list(args))
                return oc.cmd.Result(rc, out.encode(), err)       # out bytes, err text, as cmd.call
            return real(args, *a, **kw)
        patcher = mock.patch.object(oc.cmd, 'call', call)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def net(self, targets=None):
        return oc.net_change(self.repo.root, self.tip, [self.main] if targets is None else targets)

    def test_a_target_git_does_not_have_is_an_error_not_an_empty_net_change(self):
        found, why = self.net(['1' * 40])
        self.assertIsNone(found)
        self.assertIn('git merge-tree failed', why)

    def test_merge_tree_exiting_with_an_error_status_is_an_error(self):
        self.fake_git('merge-tree', 128, err='fatal: boom')
        found, why = self.net()
        self.assertIsNone(found)
        self.assertIn('git merge-tree failed', why)
        self.assertIn('rc 128', why)
        self.assertIn('fatal: boom', why)

    def test_git_that_cannot_run_or_times_out_is_an_error(self):
        for sub in ('merge-tree', 'diff'):
            with self.subTest(sub=sub):
                self.fake_git(sub, None, err='git %s timed out after 120 seconds' % sub)
                found, why = self.net()
                self.assertIsNone(found)
                self.assertIn('timed out', why)

    def test_merge_tree_printing_no_tree_is_an_error(self):
        for rc in (0, 1):
            with self.subTest(rc=rc):
                self.fake_git('merge-tree', rc, out='')
                found, why = self.net()
                self.assertIsNone(found)
                self.assertIn('no output', why)

    def test_merge_tree_printing_something_that_is_not_a_tree_id_is_an_error(self):
        self.fake_git('merge-tree', 0, out='not-a-tree\0a.py\0')
        found, why = self.net()
        self.assertIsNone(found)
        self.assertIn('git merge-tree failed', why)

    def test_a_failing_diff_against_the_merged_tree_is_an_error(self):
        calls = self.fake_git('diff', 128, err='fatal: bad object')
        found, why = self.net()
        self.assertEqual(calls[0][1], 'diff')                 # reached, after merge-tree succeeded
        self.assertIsNone(found)
        self.assertEqual(why, 'git diff failed: fatal: bad object')

    def test_a_failing_diff_without_stderr_names_its_exit_status(self):
        self.fake_git('diff', 3)
        self.assertEqual(self.net(), (None, 'git diff failed: exit 3'))

    def test_one_failing_target_among_several_fails_the_whole(self):
        found, why = self.net([self.main, '1' * 40])
        self.assertIsNone(found)
        self.assertIn('git merge-tree failed', why)

    def test_violations_refuses_when_the_net_change_cannot_be_read(self):
        self.fake_git('merge-tree', 128, err='fatal: boom')
        found, why = oc.violations(self.repo.root, self.rules, self.tip, [self.main])
        self.assertIsNone(found)
        self.assertIn('git merge-tree failed', why)


class PushWithOwnerMainCommits(OwnerCommitsCase):
    """docs/guard.md "Owner-only commits", Known limits, "One net change per push"."""

    def test_known_over_refusal_an_undone_agent_edit_counts_when_an_owner_commit_shares_the_push(self):
        # Pins a documented limit: if the over-refusal is ever fixed, update this test; it is not a regression.
        remote_main = self.main
        wt = self.repo.worktree('x')                          # the branch, forked at the remote main
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n'}, cwd=wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', edit, cwd=wt)
        branch_tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        self.assertEqual(self.bad(branch_tip, exclude=[remote_main]), [])     # alone: allowed
        self.commit_as(OWNER, 'owner edits CLAUDE.md', {'CLAUDE.md': 'owner\n'})   # unpushed, local main
        self.repo.git('-c', 'user.email=%s' % AGENT, 'merge', '-q', '--no-ff', '--no-edit', branch_tip)
        push = self.repo.git('rev-parse', 'HEAD')
        self.assertEqual(sorted(self.bad(push, exclude=[remote_main])),
                         sorted([oc.Violation(edit, AGENT, ['CLAUDE.md']),
                                 oc.Violation(branch_tip, AGENT, ['CLAUDE.md'])]))

    def test_pushing_the_owners_commits_first_avoids_the_over_refusal(self):
        wt = self.repo.worktree('x')
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n'}, cwd=wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', edit, cwd=wt)
        branch_tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        owner_edit = self.commit_as(OWNER, 'owner edits CLAUDE.md', {'CLAUDE.md': 'owner\n'})
        self.repo.git('-c', 'user.email=%s' % AGENT, 'merge', '-q', '--no-ff', '--no-edit', branch_tip)
        push = self.repo.git('rev-parse', 'HEAD')
        self.assertEqual(self.bad(push, exclude=[owner_edit]), [])


class Merges(OwnerCommitsCase):
    """A branch at self.main; main gains the owner's CLAUDE.md edit; the agent merges main in."""

    def setUp(self):
        OwnerCommitsCase.setUp(self)
        self.wt = self.repo.worktree('x')
        self.commit_as(AGENT, 'agent work', {'a.py': 'a\n'}, cwd=self.wt)
        self.owner_edit = self.commit_as(OWNER, 'owner edits CLAUDE.md', {'CLAUDE.md': 'owner\n'})

    def merge_main(self, *extra):
        self.repo.git('-c', 'user.email=%s' % AGENT, 'merge', '-q', '--no-ff', '--no-edit', 'main',
                      *extra, cwd=self.wt, check=not extra)
        return self.repo.git('rev-parse', 'HEAD', cwd=self.wt)

    def test_a_clean_merge_changes_nothing_of_its_own(self):
        merge = self.merge_main()
        self.assertEqual(self.bad(merge), [])

    def test_an_evil_merge_is_judged_by_its_author(self):
        self.repo.git('-c', 'user.email=%s' % AGENT, 'merge', '-q', '--no-ff', '--no-commit', 'main',
                      cwd=self.wt)
        self.repo.write('CLAUDE.md', 'agent rewrote it in the merge\n', root=self.wt)
        self.repo.git('add', 'CLAUDE.md', cwd=self.wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '--no-edit', cwd=self.wt)
        merge = self.repo.git('rev-parse', 'HEAD', cwd=self.wt)
        self.assertEqual(self.bad(merge), [oc.Violation(merge, AGENT, ['CLAUDE.md'])])

    def test_a_conflict_resolved_in_an_owner_only_path_is_judged_by_its_author(self):
        self.commit_as(AGENT, 'agent edits CLAUDE.md too', {'CLAUDE.md': 'agent\n'}, cwd=self.wt)
        branch_edit = self.repo.git('rev-parse', 'HEAD', cwd=self.wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'merge', '-q', '--no-ff', '--no-edit', 'main',
                      cwd=self.wt, check=False)
        self.repo.write('CLAUDE.md', 'owner\n', root=self.wt)       # resolved to the owner's text
        self.repo.git('add', 'CLAUDE.md', cwd=self.wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '--no-edit', cwd=self.wt)
        merge = self.repo.git('rev-parse', 'HEAD', cwd=self.wt)
        found = {v.sha: v.paths for v in self.bad(merge)}
        self.assertEqual(found, {merge: ['CLAUDE.md'], branch_edit: ['CLAUDE.md']})

    def test_an_owner_change_through_a_main_merge_does_not_count_against_the_branch(self):
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n'}, cwd=self.wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', edit, cwd=self.wt)
        merge = self.merge_main()
        self.assertEqual(self.bad(merge, exclude=[self.owner_edit]), [])

    def test_a_conflict_resolved_to_mains_text_changes_nothing_on_that_main(self):
        self.commit_as(AGENT, 'agent edits CLAUDE.md too', {'CLAUDE.md': 'agent\n'}, cwd=self.wt)
        self.merge_main('--no-commit')
        self.repo.write('CLAUDE.md', 'owner\n', root=self.wt)
        self.repo.git('add', 'CLAUDE.md', cwd=self.wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '--no-edit', cwd=self.wt)
        merge = self.repo.git('rev-parse', 'HEAD', cwd=self.wt)
        self.assertEqual(self.bad(merge, exclude=[self.owner_edit]), [])

    def test_a_branch_behind_an_owner_change_is_judged_by_what_it_would_land(self):
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n'}, cwd=self.wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', edit, cwd=self.wt)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=self.wt)
        self.assertEqual(self.bad(tip, exclude=[self.owner_edit]), [])

    def test_a_kept_change_that_conflicts_with_main_is_refused(self):
        edit = self.commit_as(AGENT, 'agent edits CLAUDE.md', {'CLAUDE.md': 'agent\n'}, cwd=self.wt)
        self.assertEqual(self.bad(edit, exclude=[self.owner_edit]),
                         [oc.Violation(edit, AGENT, ['CLAUDE.md'])])

    def test_an_owner_merge_is_not(self):
        self.repo.git('-c', 'user.email=%s' % OWNER, 'merge', '-q', '--no-ff', '--no-commit', 'main',
                      cwd=self.wt)
        self.repo.write('CLAUDE.md', 'owner again\n', root=self.wt)
        self.repo.git('add', 'CLAUDE.md', cwd=self.wt)
        self.repo.git('-c', 'user.email=%s' % OWNER, 'commit', '-q', '--no-edit', cwd=self.wt)
        merge = self.repo.git('rev-parse', 'HEAD', cwd=self.wt)
        self.assertEqual(self.bad(merge), [])


if __name__ == '__main__':
    unittest.main()
