"""tools/trailers.py over kitlib.trailerscheck (docs/repo-checks.md "trailers"): the gate step that
fails a branch commit with no agent Co-Authored-By trailer, judged by the rule review_pack uses
(kitlib.reviewpack.trailers). The fixture repository is the review-pack one: `main` names the
fixture identity as the owner, an agent commit passes `-c user.email=agent@example.invalid`."""
import contextlib
import io
import os
import sys
from unittest import mock

from kitlib.reviewpack import trailers
from tests.reviewpack.repo import AGENT, KIT, TRAILERED_MSG, PackCase

sys.path.insert(0, os.path.join(KIT, 'tools'))
import trailers as cli  # noqa: E402


class TrailersCase(PackCase):
    def setUp(self):
        super().setUp()
        self.enter(mock.patch.object(trailers, 'TRAILER_FAIL_SINCE', 0))    # every commit is new
        self.git('checkout', '-q', '-b', 'feature')

    def run_tool(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['-C', self.root] + list(argv))
        return code, out.getvalue()

    def commit(self, text, message, author=None):
        self.write('f.txt', text)
        self.commit_all(message, author=author)


class Branch(TrailersCase):
    def test_a_trailerless_commit_fails_and_names_it(self):
        self.commit('1', 'with\n\n' + TRAILERED_MSG)
        self.commit('2', 'without')
        code, out = self.run_tool('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('trailers: 2 commit(s) on this branch and not on main, 1 without a '
                      'Co-Authored-By trailer (1 failing)', out)
        self.assertIn('FAILED: ', out)
        self.assertIn('"without" has no Co-Authored-By trailer', out)
        self.assertNotIn('"with" has', out)

    def test_a_trailered_branch_is_clean(self):
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        code, out = self.run_tool('--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('0 without a Co-Authored-By trailer (0 failing)', out)

    def test_a_commit_older_than_the_cutoff_warns_and_passes(self):
        self.enter(mock.patch.object(trailers, 'TRAILER_FAIL_SINCE', 2 ** 62))
        self.commit('1', 'old work')
        code, out = self.run_tool('--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: ', out)
        self.assertIn('(0 failing)', out)

    def test_an_owner_approved_commit_by_the_owner_warns_and_passes(self):
        self.commit('1', 'owner work\n\nOwner-Approved: CLAUDE.md')
        code, out = self.run_tool('--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('carries Owner-Approved from the owner', out)

    def test_an_owner_claim_on_an_agent_commit_fails(self):
        self.commit('1', 'agent work\n\nOwner-Approved: CLAUDE.md', author=AGENT)
        code, out = self.run_tool('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('an owner claim that proves nothing', out)
        self.assertIn('FAILED: ', out)

    def test_a_sync_merge_without_a_trailer_fails(self):
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        self.git('checkout', '-q', 'main')
        self.write('g.txt', 'g')
        self.commit_all('main moves')
        self.git('checkout', '-q', 'feature')
        self.git('merge', '--no-ff', '-q', '-m', 'sync main', 'main')
        code, out = self.run_tool('--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('merge "sync main"', out)

    def test_the_default_base_is_main_here(self):
        self.commit('1', 'work')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('not on main', out)

    def test_a_base_that_is_not_a_rev_exits_2(self):
        code, out = self.run_tool('--base=--evil')
        self.assertEqual(code, 2, out)

    def test_a_base_that_does_not_resolve_exits_2(self):
        code, out = self.run_tool('--base', 'no-such-branch')
        self.assertEqual(code, 2, out)
        self.assertIn('no-such-branch', out)

    def test_a_bad_option_is_usage(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            cli.main(['--nope'])
        self.assertEqual(e.exception.code, 2)


class Merged(TrailersCase):
    def land(self):
        """feature (one trailered commit) merged into main by a trailerless lander merge; returns
        main's hash before the merge."""
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        self.git('checkout', '-q', 'main')
        before = self.git('rev-parse', 'HEAD').strip()
        self.git('merge', '--no-ff', '-q', '-m', 'Merge feature', 'feature')
        return before

    def test_the_landers_merge_on_main_is_exempt(self):
        before = self.land()
        code, out = self.run_tool('--base', before, '--merged')
        self.assertEqual(code, 0, out)
        self.assertIn('1 lander merge(s) exempt', out)
        code, out = self.run_tool('--base', before)
        self.assertEqual(code, 1, out)         # without --merged the same merge is a finding

    def test_a_trailerless_branch_commit_still_fails_when_merged(self):
        self.commit('1', 'bare')
        self.git('checkout', '-q', 'main')
        before = self.git('rev-parse', 'HEAD').strip()
        self.git('merge', '--no-ff', '-q', '-m', 'Merge feature', 'feature')
        code, out = self.run_tool('--base', before, '--merged')
        self.assertEqual(code, 1, out)
        self.assertIn('"bare" has no Co-Authored-By trailer', out)

    def test_merged_off_main_is_refused(self):
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        code, out = self.run_tool('--base', 'main', '--merged')
        self.assertEqual(code, 2, out)
        self.assertIn('REFUSED', out)


class LandingTree(TrailersCase):
    """KIT-LAND-TREE-TRAILERS-DETACHED: a landing merges and gates in a detached landing tree
    (kitlib.gate.land, step 5b), main never checked out there. `--merged` takes a detached HEAD
    whose first-parent chain from the base is merges only (the landing's), and refuses any other
    detached HEAD and every branch but main."""

    def merge_detached(self, *branches):
        """HEAD detached at main, then each branch merged --no-ff by a trailerless lander merge,
        main itself never moved; main's hash."""
        base = self.git('rev-parse', 'main').strip()
        self.git('checkout', '-q', '--detach', base)
        for b in branches:
            self.git('merge', '--no-ff', '-q', '-m', 'Merge %s' % b, b)
        return base

    def test_the_landers_merge_in_a_detached_landing_tree_is_exempt(self):
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        base = self.merge_detached('feature')
        self.assertEqual(self.git('rev-parse', 'main').strip(), base)
        code, out = self.run_tool('--base', base, '--merged')
        self.assertEqual(code, 0, out)
        self.assertIn('1 lander merge(s) exempt', out)
        code, out = self.run_tool('--base', base)
        self.assertEqual(code, 1, out)         # without --merged the same merge is a finding

    def test_a_batch_of_landing_merges_is_exempt(self):
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        self.git('checkout', '-q', '-b', 'second', 'main')
        self.write('s.txt', '2')
        self.commit_all('more\n\n' + TRAILERED_MSG)
        base = self.merge_detached('feature', 'second')
        code, out = self.run_tool('--base', base, '--merged')
        self.assertEqual(code, 0, out)
        self.assertIn('2 lander merge(s) exempt', out)

    def test_a_trailerless_branch_commit_still_fails_in_the_landing_tree(self):
        self.commit('1', 'bare')
        base = self.merge_detached('feature')
        code, out = self.run_tool('--base', base, '--merged')
        self.assertEqual(code, 1, out)
        self.assertIn('"bare" has no Co-Authored-By trailer', out)

    def test_a_detached_branch_commit_is_refused(self):
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        self.git('checkout', '-q', '--detach')
        code, out = self.run_tool('--base', 'main', '--merged')
        self.assertEqual(code, 2, out)
        self.assertIn('REFUSED', out)
        self.assertIn('detached', out)

    def test_a_detached_sync_merge_is_refused(self):
        # the branch's own merge of main: its first parent is the branch commit, not the base
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        self.git('checkout', '-q', 'main')
        self.write('g.txt', 'g')
        self.commit_all('main moves\n\n' + TRAILERED_MSG)
        self.git('checkout', '-q', 'feature')
        self.git('merge', '--no-ff', '-q', '-m', 'sync main', 'main')
        self.git('checkout', '-q', '--detach')
        code, out = self.run_tool('--base', 'main', '--merged')
        self.assertEqual(code, 2, out)
        self.assertIn('REFUSED', out)

    def test_a_merge_on_top_of_a_branch_commit_is_refused(self):
        # a landing-shaped merge whose first-parent chain from the base holds a plain commit
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        base = self.git('rev-parse', 'main').strip()
        self.git('checkout', '-q', '-b', 'side', 'main')
        self.write('s.txt', '2')
        self.commit_all('side\n\n' + TRAILERED_MSG)
        self.git('checkout', '-q', '--detach')
        self.git('merge', '--no-ff', '-q', '-m', 'Merge feature', 'feature')
        code, out = self.run_tool('--base', base, '--merged')
        self.assertEqual(code, 2, out)
        self.assertIn('REFUSED', out)

    def test_detached_at_the_base_is_refused(self):
        base = self.git('rev-parse', 'main').strip()
        self.git('checkout', '-q', '--detach', base)
        code, out = self.run_tool('--base', base, '--merged')
        self.assertEqual(code, 2, out)
        self.assertIn('REFUSED', out)

    def test_a_landing_shaped_merge_on_a_feature_branch_is_refused(self):
        # the same shape on a named branch is a branch's merge, never the lander's
        self.commit('1', 'work\n\n' + TRAILERED_MSG)
        base = self.git('rev-parse', 'main').strip()
        self.git('checkout', '-q', '-b', 'other', base)
        self.git('merge', '--no-ff', '-q', '-m', 'Merge feature', 'feature')
        code, out = self.run_tool('--base', base, '--merged')
        self.assertEqual(code, 2, out)
        self.assertIn('REFUSED', out)
        self.assertIn('refs/heads/other', out)
