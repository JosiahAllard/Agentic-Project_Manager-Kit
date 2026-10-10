"""tools/review_pack.py end to end over a throwaway repository (tests/reviewpack/repo.py): the
owner-only section, the owner's identity, the header, failed git reads, the base default, the
merge layer and the doc-budget sub-run (ported from the PM kit's test_review_pack.py)."""
import contextlib
import io
import json
import os
from unittest import mock

from kitlib import reviewpack
from kitlib import verdict
from tests.gitfixture import EMAIL
from tests.realgit import real_git
from tests.reviewpack.repo import AGENT, PackCase, cli, failing_call


class OwnerOnlySection(PackCase):
    def two_edits(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('edit claude\n\nOwner-Approved: CLAUDE.md')
        self.write('CLAUDE.md', 'v2')
        self.commit_all('edit claude again, no trailer this time')

    def test_a_later_unapproved_edit_is_not_covered_by_an_earlier_approval(self):
        self.two_edits()
        changed, rows = self.owner_only()
        self.assertIn('CLAUDE.md', changed)
        self.assertEqual(sorted(status for _, status, _, _ in rows), ['MISSING', 'ok'], rows)

    def test_the_pack_exits_1_on_that_same_branch(self):
        self.two_edits()
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('MISSING', out)
        self.assertIn('docs     NOT CHECKED', out)

    def test_a_stale_trailer_on_a_commit_that_touches_nothing_locked_is_reported(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', '1')
        self.commit_all('unrelated change\n\nOwner-Approved: CLAUDE.md')
        changed, rows = self.owner_only()
        self.assertNotIn('CLAUDE.md', changed)
        self.assertTrue(any(status == 'UNMATCHED' for _, status, _, _ in rows), rows)

    def test_a_workflow_edit_needs_its_own_approval(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('.github/workflows/ci.yml', 'on: push')
        self.commit_all('ci change, no trailer')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('.github/workflows/ci.yml', out)

    def test_a_kit_config_edit_needs_its_own_approval(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('.kit.json', '{}')
        self.commit_all('kit config change, no trailer')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('MISSING   .kit.json', out)


@real_git('a merge combined diff (git show --cc)')
class OwnerOnlyEvilMerge(PackCase):
    def test_an_evil_merge_that_edits_claude_md_in_neither_parent_is_caught(self):
        self.git('checkout', '-q', '-b', 'other', 'main')
        self.write('g.txt', '1')
        self.commit_all('other branch work')
        self.git('checkout', '-q', '-b', 'feature', 'main')
        self.write('f.txt', '1')
        self.commit_all('feature branch work')
        self.git('merge', '-q', '--no-commit', '--no-ff', 'other')
        self.write('CLAUDE.md', 'v-evil')
        self.git('add', 'CLAUDE.md')
        self.git('commit', '-q', '-m', 'merge other into feature')
        changed, rows = self.owner_only()
        self.assertIn('CLAUDE.md', changed)
        self.assertEqual([(r[1], r[2]) for r in rows], [('MISSING', 'CLAUDE.md')], rows)
        self.assertNotEqual(rows[0][0], '-')
        self.assertIn('the merge commit itself', rows[0][3])


class OwnerIdentity(PackCase):
    """A trailer approves only on a commit whose AUTHOR email is the base's "owner".email."""

    def test_an_agent_authored_trailer_approves_nothing(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('agent edit\n\nOwner-Approved: CLAUDE.md', author=AGENT)
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('authored by %s, not the owner' % AGENT, out)

    def test_the_owners_own_commit_approves(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('owner edit\n\nOwner-Approved: CLAUDE.md')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('Owner-Approved CLAUDE.md', out)
        self.assertNotIn('do not land', out)

    def test_each_commit_is_judged_by_its_own_author(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('owner edit\n\nOwner-Approved: CLAUDE.md')
        self.write('CLAUDE.md', 'v2')
        self.commit_all('agent edit\n\nOwner-Approved: CLAUDE.md', author=AGENT)
        _changed, rows = self.owner_only()
        self.assertEqual(sorted(r[1] for r in rows), ['MISSING', 'ok'], rows)
        self.assertIn(AGENT, next(r for r in rows if r[1] == 'MISSING')[3])


class NoOwnerConfigured(PackCase):
    CONFIG = {}

    def test_an_unconfigured_owner_makes_every_trailer_unverified(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('owner edit\n\nOwner-Approved: CLAUDE.md')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('owner identity NOT VERIFIABLE', out)
        self.assertIn('MISSING', out)

    def test_the_branch_cannot_name_its_own_author_the_owner(self):
        # The identity comes from the base: a branch whose .kit.json names its agent author as
        # the owner, approving both its edits, still proves nothing.
        self.git('checkout', '-q', '-b', 'feature')
        self.write('.kit.json', json.dumps({'owner': {'email': AGENT}}))
        self.write('CLAUDE.md', 'v1')
        self.commit_all('self-approval\n\nOwner-Approved: CLAUDE.md\nOwner-Approved: .kit.json',
                        author=AGENT)
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('NOT VERIFIABLE', out)


class RangeCommits(PackCase):
    def test_range_commits_splits_author_message_and_names(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', 'x')
        self.write('docs/a b.md', 'y')
        self.commit_all('subject\n\nbody line\n\nOwner-Approved: f.txt', author=AGENT)
        [(sha, merge, author, body, names)] = reviewpack.range_commits(self.root, 'main..feature')
        self.assertEqual(sha, self.git('rev-parse', 'feature'))
        self.assertFalse(merge)
        self.assertEqual(author, AGENT)
        self.assertEqual(body, 'subject\n\nbody line\n\nOwner-Approved: f.txt')
        self.assertEqual(sorted(names), ['docs/a b.md', 'f.txt'])


class TheHeaderLines(PackCase):
    BRANCHES = {'behind': 'NO', 'level': 'yes', 'ahead': 'yes'}

    def header(self, out):
        return [l for l in out.splitlines() if l.startswith(('branch ', 'base '))]

    def expect(self, branch, base='main'):
        short = lambda r: self.git('rev-parse', '--short', r)  # noqa: E731
        mb = self.git('merge-base', base, branch)
        tip_in = mb == self.git('rev-parse', base)
        return ['branch   %s @ %s' % (branch, short(branch)),
                'base     %s @ %s | merge-base %s | contains base tip: %s'
                % (base, short(base), mb[:7], 'yes' if tip_in else
                   'NO - author merges it in and regates')]

    def three_branches(self):
        """`behind` lacks the base tip; `level` IS it; `ahead` is one commit past it, dated later
        so a date-sorted `--no-walk` would print it first and swap the two short hashes."""
        self.git('checkout', '-q', '-b', 'behind')
        self.write('f.txt', 'behind')
        self.commit_all('behind work')
        self.git('checkout', '-q', 'main')
        self.write('g.txt', 'main moved')
        self.commit_all('main moved on')
        self.git('branch', 'level', 'main')
        self.git('checkout', '-q', '-b', 'ahead')
        self.write('h.txt', 'ahead')
        with mock.patch.dict(os.environ, {'GIT_AUTHOR_DATE': '@4000000000 +0000',
                                          'GIT_COMMITTER_DATE': '@4000000000 +0000'}):
            self.commit_all('ahead work')
        self.git('checkout', '-q', 'main')

    def recording(self, inner):
        calls = []

        def call(args, cwd, *a, **k):
            calls.append(list(args))
            return inner(args, cwd, *a, **k)
        return calls, mock.patch.object(reviewpack.gitcmd, 'call', call)

    def test_behind_level_and_ahead_read_from_one_log(self):
        # Fails if `base_full == mb` becomes False (level, ahead read NO), if --no-walk sorts by
        # date (ahead's hashes swap), or without `pairs * 2` (level falls back to per-rev reads).
        self.three_branches()
        for branch, tip_in in self.BRANCHES.items():
            calls, patcher = self.recording(reviewpack.gitcmd.call)
            with patcher:
                _code, out = self.run_pack(branch)
            want = self.expect(branch)
            self.assertIn('contains base tip: ' + tip_in, want[1])
            self.assertEqual(self.header(out), want, out)
            self.assertEqual([a for a in calls if '--short' in a or '--is-ancestor' in a], [],
                             (branch, calls))

    def test_a_failed_or_odd_log_falls_back_to_the_per_rev_reads(self):
        self.three_branches()
        for rc in (0, 128):
            for branch in self.BRANCHES:
                calls, patcher = self.recording(failing_call(('log', '--no-walk=unsorted'), rc))
                with self.subTest(branch=branch, rc=rc), patcher:
                    _code, out = self.run_pack(branch)
                self.assertEqual(self.header(out), self.expect(branch), out)
                bare = [[x for x in a if x not in reviewpack.QUOTEPATH] for a in calls]
                for read in (['rev-parse', '--verify', 'main^{commit}'],
                             ['rev-parse', '--short', 'main'], ['rev-parse', '--short', branch]):
                    self.assertIn(read, bare, calls)
                self.assertEqual([a for a in calls if '--is-ancestor' in a], [], calls)

    def test_a_fallback_read_ended_from_outside_stops_and_never_reads_as_no(self):
        # TOOLS-REVIEWPACK-PERREV-FLAKE: a git ended from outside exits 1 having printed nothing
        # (kitlib.proc.shield's GOTCHA; measured: `taskkill /PID <n> /T /F` on a git leaves
        # exit 1, empty stdout and stderr), the same shape as `merge-base --is-ancestor`'s NO. The
        # fallback read that NO, so `level` - the base tip itself - printed 'contains base tip:
        # NO'. Every fallback read so ended must stop the pack naming it, or leave the header
        # true; none may print an answer git never gave.
        self.three_branches()
        no_log = failing_call(('log', '--no-walk=unsorted'))
        for words in (('merge-base', '--is-ancestor', 'main', 'level'),
                      ('rev-parse', '--verify', 'main^{commit}'),
                      ('rev-parse', '--short', 'main'), ('rev-parse', '--short', 'level')):
            def call(args, cwd, *a, **k):
                bare = [x for x in args if x not in reviewpack.QUOTEPATH]
                if tuple(bare[:len(words)]) == words:
                    return reviewpack.gitcmd.Result(1, b'', '')
                return no_log(args, cwd, *a, **k)
            with self.subTest(read=words), mock.patch.object(reviewpack.gitcmd, 'call', call):
                code, out = self.run_pack('level')
            self.assertNotIn('contains base tip: NO', out, (words, out))
            if code == 2:
                self.assertIn('review_pack: `git %s' % ' '.join(words), out)
                self.assertIn('failed (exit 1): (nothing on stderr)', out)
            else:
                self.assertEqual(self.header(out), self.expect('level'), (words, out))

    def test_a_per_rev_short_hash_read_that_dies_exits_2_not_an_empty_hash(self):
        self.three_branches()
        no_log = failing_call(('log', '--no-walk=unsorted'))
        for rc, why in ((1, 'failed (exit 1): (nothing on stderr)'),
                        (0, 'exit 0 but printed nothing')):
            dying = failing_call(('rev-parse', '--short', 'ahead'), rc, err='')

            def call(args, cwd, *a, **k):
                bare = [x for x in args if x not in reviewpack.QUOTEPATH]
                return (dying if bare[:3] == ['rev-parse', '--short', 'ahead'] else no_log)(
                    args, cwd, *a, **k)
            with self.subTest(rc=rc), mock.patch.object(reviewpack.gitcmd, 'call', call):
                code, out = self.run_pack('ahead')
            self.assertEqual(code, 2, (rc, out))
            self.assertNotIn('branch   ahead @', out)
            self.assertIn('review_pack: `git rev-parse --short ahead` ' + why, out)

    def test_a_failed_base_read_in_the_fallback_refuses_and_behind_reads_no(self):
        self.three_branches()
        mb = self.git('merge-base', 'main', 'behind')
        no_log = failing_call(('log', '--no-walk=unsorted'))
        bad = failing_call(('rev-parse', '--verify'), 128, 'fatal: bad ref')

        def call(args, cwd, *a, **k):
            bare = [x for x in args if x not in reviewpack.QUOTEPATH]
            return (bad if bare[:2] == ['rev-parse', '--verify'] else no_log)(args, cwd, *a, **k)
        with mock.patch.object(reviewpack.gitcmd, 'call', call), \
                self.assertRaises(reviewpack.Stop) as caught:
            reviewpack.tips(self.root, 'main', 'behind', mb)
        self.assertIn('`git rev-parse --verify main^{commit}` failed (exit 128): fatal: bad ref',
                      caught.exception.line)
        with mock.patch.object(reviewpack.gitcmd, 'call', no_log):
            got = reviewpack.tips(self.root, 'main', 'behind', mb)
        self.assertEqual(got, (self.git('rev-parse', '--short', 'main'),
                               self.git('rev-parse', '--short', 'behind'), False))


class AFailedMergeBaseReadIsNamed(PackCase):
    CASES = (
        ((1, ''), 'exit 1, nothing on stderr: no common history, or a git ended from outside'),
        ((128, 'fatal: Not a valid object name level\n'),
         'exit 128: fatal: Not a valid object name level'),
        ((0, ''), 'exit 0 but printed nothing'),
    )

    def test_each_failure_exits_2_and_names_its_exit_code(self):
        self.git('branch', 'level', 'main')
        for (rc, err), why in self.CASES:
            with self.subTest(rc=rc), mock.patch.object(
                    reviewpack.gitcmd, 'call', failing_call(('merge-base', 'main', 'level'), rc, err)):
                code, out = self.run_pack('level')
            self.assertEqual(code, 2, out)
            self.assertEqual(out.strip(), 'cannot find a merge-base for main and level '
                             '(`git merge-base main level` %s)' % why)

    def test_an_answered_merge_base_still_reads_as_before(self):
        self.git('branch', 'level', 'main')
        code, out = self.run_pack('level')
        self.assertIn('merge-base %s | contains base tip: yes' % self.git('rev-parse', 'main')[:7],
                      out)
        self.assertNotIn('cannot find', out)
        self.assertEqual(code, 0, out)


class TheBaseDefault(PackCase):
    def test_the_base_defaults_to_the_configured_main_branch(self):
        self.git('branch', '-m', 'main', 'trunk')
        self.write('.kit.json', json.dumps({'repo': {'main_branch': 'trunk'},
                                            'owner': {'email': EMAIL}}))
        self.commit_all('kit config on trunk')
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', '1')
        self.commit_all('work')
        code, out = self.run_pack('feature')
        self.assertIn('base     trunk @ ', out)
        self.assertEqual(code, 0, out)

    def test_an_unreadable_working_tree_kit_config_refuses(self):
        self.write('.kit.json', '{not json')
        code, out = self.run_pack('main')
        self.assertEqual(code, 2, out)
        self.assertIn('review_pack: refused: kit configuration', out)

    def test_an_unreadable_branch_kit_config_refuses(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('.kit.json', '{not json')
        self.commit_all('break the config')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 2, out)
        self.assertIn('review_pack: refused: kit configuration', out)

    def test_a_directory_outside_any_repository_refuses(self):
        empty = os.path.join(self.r.base, 'empty')
        os.makedirs(empty)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': self.r.base}), \
                contextlib.redirect_stdout(out):
            code = cli.main(['-C', empty, 'main', '--base', 'main'])
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn('review_pack: refused:', out.getvalue())
        self.assertNotIn('branch   ', out.getvalue())


class AnEditTheBranchTakesBack(PackCase):
    def round_trip(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('edit claude, no trailer')
        self.write('CLAUDE.md', 'v0')
        self.commit_all('take it back')

    def test_an_edit_that_is_reverted_is_reported_but_does_not_block(self):
        self.round_trip()
        changed, rows = self.owner_only()
        self.assertEqual(changed, [])
        self.assertEqual([r[1] for r in rows], ['reverted', 'reverted'], rows)
        self.assertEqual({r[2] for r in rows}, {'CLAUDE.md'})
        self.assertNotIn('-', [r[0] for r in rows])

    def test_the_pack_exits_0_on_that_same_branch(self):
        self.round_trip()
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('REVERTED', out)
        self.assertNotIn('MISSING', out)

    def test_a_partial_revert_that_leaves_the_path_changed_still_blocks(self):
        self.round_trip()
        self.write('CLAUDE.md', 'v2')
        self.commit_all('edit it again, still no trailer')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('MISSING', out)
        self.assertNotIn('REVERTED', out)

    def test_moving_the_locked_file_away_is_not_a_revert(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.git('mv', 'CLAUDE.md', 'notes.md')
        self.commit_all('move the owner-only file away, no trailer')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('MISSING', out)
        self.assertNotIn('REVERTED', out)

    def test_deleting_the_locked_file_still_blocks(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.git('rm', '-q', 'CLAUDE.md')
        self.commit_all('delete the owner-only file, no trailer')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('MISSING', out)

    def test_a_plain_unreverted_edit_still_blocks(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('edit claude, no trailer')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('MISSING', out)


@real_git('a merge combined diff (git show --cc)')
class EvilMergeReEditingAnAccountedPath(PackCase):
    BASE = 'one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n'

    def build(self, merge_message='merge other into feature'):
        self.git('checkout', '-q', '-b', 'other', 'main')
        self.write('g.txt', '1')
        self.commit_all('other branch work')
        self.git('checkout', '-q', '-b', 'feature', 'main')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('edit claude, approved\n\nOwner-Approved: CLAUDE.md')
        self.git('merge', '-q', '--no-commit', '--no-ff', 'other')
        self.write('CLAUDE.md', 'v2-evil')
        self.git('add', 'CLAUDE.md')
        self.git('commit', '-q', '-m', merge_message)

    def two_sides(self):
        self.write('CLAUDE.md', self.BASE)
        self.commit_all('lines to edit apart\n\nOwner-Approved: CLAUDE.md')
        self.git('checkout', '-q', '-b', 'other', 'main')
        self.write('CLAUDE.md', self.BASE.replace('ten', 'TEN-other'))
        self.commit_all('owner edits the bottom\n\nOwner-Approved: CLAUDE.md')
        self.git('checkout', '-q', '-b', 'feature', 'main')
        self.write('CLAUDE.md', self.BASE.replace('one\n', 'ONE-feature\n', 1))
        self.commit_all('owner edits the top\n\nOwner-Approved: CLAUDE.md')

    def test_the_merges_own_second_edit_is_reported_against_the_merge(self):
        self.build()
        changed, rows = self.owner_only()
        self.assertIn('CLAUDE.md', changed)
        self.assertEqual(sorted(status for _, status, _, _ in rows), ['MISSING', 'ok'], rows)
        missing = next(r for r in rows if r[1] == 'MISSING')
        self.assertEqual(missing[2], 'CLAUDE.md')
        self.assertNotEqual(missing[0], '-')
        self.assertIn('the merge commit itself', missing[3])

    def test_the_pack_exits_1_on_that_same_branch(self):
        self.build()
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('MISSING', out)

    def test_an_approval_on_the_merge_commit_itself_covers_it(self):
        self.build('merge other into feature\n\nOwner-Approved: CLAUDE.md')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertNotIn('do not land', out)

    def test_an_approval_on_a_merge_that_is_the_only_toucher_clears_the_branch(self):
        self.git('checkout', '-q', '-b', 'other', 'main')
        self.write('g.txt', '1')
        self.commit_all('other branch work')
        self.git('checkout', '-q', '-b', 'feature', 'main')
        self.write('f.txt', '1')
        self.commit_all('feature branch work')
        self.git('merge', '-q', '--no-commit', '--no-ff', 'other')
        self.write('CLAUDE.md', 'v-merge')
        self.git('add', 'CLAUDE.md')
        self.git('commit', '-q', '-m', 'merge other into feature\n\nOwner-Approved: CLAUDE.md')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertNotIn('do not land', out)

    def test_a_merge_whose_sides_touched_different_files_is_not_a_finding(self):
        self.git('checkout', '-q', '-b', 'other', 'main')
        self.write('CLAUDE.md', 'v-main\n\n')
        self.commit_all('owner edits claude on the other branch\n\nOwner-Approved: CLAUDE.md')
        self.git('checkout', '-q', '-b', 'feature', 'main')
        self.write('f.txt', '1')
        self.commit_all('feature work')
        self.git('merge', '-q', '--no-ff', '-m', 'merge other into feature', 'other')
        rows = self.owner_only()[1]
        self.assertEqual([r for r in rows if r[1] != 'ok'], [], rows)

    def test_a_merge_reconciling_two_sides_that_both_edited_the_locked_file_is_not_a_finding(self):
        self.two_sides()
        self.git('merge', '-q', '--no-ff', '-m', 'merge other into feature', 'other')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertNotIn('MISSING', out)

    def test_a_merge_that_reconciles_and_adds_its_own_edit_is_still_caught(self):
        self.two_sides()
        self.git('merge', '-q', '--no-commit', '--no-ff', 'other')
        self.write('CLAUDE.md', self.BASE.replace('one\n', 'ONE-feature\n', 1)
                   .replace('ten', 'TEN-other').replace('three', 'SMUGGLED'))
        self.git('add', 'CLAUDE.md')
        self.git('commit', '-q', '-m', 'merge other into feature')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('the merge commit itself', out)


class MixedCaseLockedPath(PackCase):
    def add_mixed_case(self, message):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('.CLAUDE/notes.md', 'x')
        self.commit_all(message)

    def test_a_mixed_case_locked_path_with_no_trailer_is_caught(self):
        self.add_mixed_case('mixed-case owner-only file, no trailer')
        changed, rows = self.owner_only()
        self.assertEqual(changed, ['.CLAUDE/notes.md'])
        self.assertIn(('MISSING', '.CLAUDE/notes.md'), [(r[1], r[2]) for r in rows])
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('.CLAUDE/notes.md', out)

    def test_a_mixed_case_blanket_trailer_approves_it(self):
        self.add_mixed_case('mixed-case owner-only file\n\nOwner-Approved: .Claude/')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertNotIn('do not land', out)


class ProseIsNotATrailer(PackCase):
    def test_a_prose_owner_approved_line_in_the_body_is_not_a_trailer(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', '1')
        self.commit_all('tools: explain the rule\n\n'
                        'This makes an Owner-Approved: CLAUDE.md trailer name a changed path.\n\n'
                        'Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertNotIn('UNMATCHED', out)
        self.assertNotIn('do not land', out)


class TheConfiguredTrailerKey(PackCase):
    CONFIG = {'owner': {'email': EMAIL, 'trailer': 'Owner-OK'}}

    def test_only_the_configured_key_approves(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('edit\n\nOwner-Approved: CLAUDE.md')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.write('CLAUDE.md', 'v2')
        self.commit_all('edit\n\nOwner-OK: CLAUDE.md')
        _changed, rows = self.owner_only()
        self.assertEqual(sorted(r[1] for r in rows), ['MISSING', 'ok'], rows)


class TheDiffOption(PackCase):
    def test_it_prints_only_that_files_merge_base_diff(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', 'changed')
        self.write('g.txt', 'new')
        self.commit_all('work')
        code, out = self.run_pack('feature', '--base', 'main', '--diff', 'f.txt')
        self.assertEqual(code, 0, out)
        self.assertIn('+changed', out)
        self.assertNotIn('g.txt', out)
        self.assertNotIn('branch   ', out)


class FailedGitReads(PackCase):
    def failed_pack(self, words):
        with mock.patch.object(reviewpack.gitcmd, 'call', failing_call(words)):
            code, out = self.run_pack('main', '--base', 'main')
        self.assertEqual(code, 2, out)
        self.assertIn('review_pack: `git %s' % ' '.join(words), out)
        self.assertIn('failed (exit 128): fatal: injected read failure', out)

    def test_a_failed_commit_listing_refuses(self):
        self.failed_pack(('log', '--oneline'))

    def test_a_failed_file_listing_refuses(self):
        self.failed_pack(('diff', '--numstat'))

    def test_a_git_that_did_not_run_refuses(self):
        def call(args, cwd, *a, **k):
            return reviewpack.gitcmd.Result(None, b'', 'could not run git: git missing')
        with mock.patch.object(reviewpack.gitcmd, 'call', call), \
                self.assertRaises(reviewpack.Stop) as caught:
            reviewpack.git(self.root, 'log', '--oneline')
        self.assertIn('did not run: could not run git: git missing', caught.exception.line)

    def test_a_successful_empty_read_is_an_answer(self):
        self.assertEqual(reviewpack.git(self.root, 'diff', '--numstat', 'main', 'main'), '')

    def test_a_bad_ref_refuses_instead_of_reading_empty(self):
        with self.assertRaises(reviewpack.Stop) as caught:
            reviewpack.git(self.root, 'rev-parse', '--verify', 'missing-branch')
        self.assertIn('exit 128', caught.exception.line)

    def test_an_unknown_branch_has_no_merge_base(self):
        code, out = self.run_pack('missing-branch', '--base', 'main')
        self.assertEqual(code, 2, out)
        self.assertIn('cannot find a merge-base for main and missing-branch', out)


DOC_WARNING_STUB = (
    'import sys\n'
    'print("DOC BUDGET FAILED: over budget and grew.")\n'
    'for i in range(3):\n'
    '    print("  docs/f%d.md: over budget on the branch by 1 bytes." % i)\n'
    '    print("      budget 1 | branch 2 | merge-base 1 | main 1 | merged = 2")\n'
    'print("doc_budget: WARNING: cannot read the doc budget at main; using the branch\'s.")\n'
    'sys.exit(1)\n')

DOC_OK_STUB = (
    'import json, sys\n'
    'with open(%r, "w", encoding="utf-8") as fh:\n'
    '    json.dump(sys.argv[1:], fh)\n'
    'print(%r)\n')


class TheDocBudgetSubRun(PackCase):
    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.r.base, 'kit', 'tools', 'doc_budget.py')
        os.makedirs(os.path.dirname(self.stub))
        self.enter(mock.patch.object(reviewpack, 'DOC_BUDGET', self.stub))
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', '1')
        self.commit_all('work')

    def install(self, text):
        with open(self.stub, 'w', encoding='utf-8') as fh:
            fh.write(text)

    def test_a_warning_past_the_quoted_lines_is_still_quoted(self):
        self.install(DOC_WARNING_STUB)
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        [docs] = [l for l in out.splitlines() if l.startswith('docs ')]
        self.assertIn('WARNING: cannot read the doc budget at main', docs)
        self.assertIn('FAILED (exit 1', docs)

    def test_a_passing_run_is_green_and_names_the_checkout_base_and_head(self):
        argv_file = os.path.join(self.r.base, 'argv.json')
        self.install(DOC_OK_STUB % (argv_file, verdict.DOC_BUDGET_OK + ': within budget'))
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        [docs] = [l for l in out.splitlines() if l.startswith('docs ')]
        self.assertNotIn('FAILED', docs)
        self.assertIn(verdict.DOC_BUDGET_OK, docs)
        with open(argv_file, encoding='utf-8') as fh:
            argv = json.load(fh)
        self.assertEqual(argv[0], '-C')
        self.assertEqual(os.path.normcase(os.path.realpath(argv[1])),
                         os.path.normcase(os.path.realpath(self.root)))
        self.assertEqual(argv[2:], ['--base', 'main', '--head', 'feature'])

    def test_no_checker_reads_not_checked_and_leaves_the_exit_status_alone(self):
        code, out = self.run_pack('feature', '--base', 'main')   # the stub was never written
        self.assertEqual(code, 0, out)
        self.assertIn('docs     NOT CHECKED - the kit has no tools/doc_budget.py', out)


if __name__ == '__main__':
    import unittest
    unittest.main()
