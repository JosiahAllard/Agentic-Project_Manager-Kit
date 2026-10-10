"""review_pack's merge-base over phase-style branches (a worker's branch cut from a stale local
main, from a freshly fetched origin/main, from nothing shared, in a shallow clone): what each
case prints and which exit it takes."""
import os
import pathlib
import subprocess
from unittest import mock

from tests.gitfixture import fixture_env
from kitlib.reviewpack import trailers
from tests.reviewpack.repo import PackCase
from tests.realgit import real_git


class WithOrigin(PackCase):
    """No tests of its own: a repository with a bare `origin`."""

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.r.base, 'remote.git')
        self.r.git('init', '-q', '--bare', self.remote, cwd=self.r.base)
        self.git('remote', 'add', 'origin', self.remote)
        self.git('push', '-q', 'origin', 'main')
        self.stale = self.git('rev-parse', 'HEAD')

    def advance_origin(self, n=2):
        """Push n commits to origin/main, then put the local main back at the stale commit."""
        for i in range(n):
            self.write('up%d.txt' % i, 'x')
            self.commit_all('upstream %d' % i)
        self.git('push', '-q', 'origin', 'main')
        self.fresh = self.git('rev-parse', 'HEAD')
        self.git('reset', '-q', '--hard', self.stale)


class PhaseBranches(WithOrigin):
    """origin/main is two commits past the local main."""

    def test_a_branch_cut_from_a_stale_local_main(self):
        self.advance_origin()
        self.git('checkout', '-q', '-b', 'phase/a')
        self.write('f.txt', '1')
        self.commit_all('phase work')
        for argv in (('phase/a',), ('phase/a', '--base', 'main')):
            with self.subTest(argv=argv):
                code, out = self.run_pack(*argv)
                self.assertEqual(code, 0, out)
                self.assertNotIn('cannot find', out)
                self.assertIn('merge-base %s' % self.stale[:7], out)

    def test_a_branch_cut_from_a_fetched_origin_main_with_local_main_not_updated(self):
        self.advance_origin()
        self.git('checkout', '-q', '-b', 'phase/b', 'origin/main')
        self.write('f.txt', '1')
        self.commit_all('phase work')
        code, out = self.run_pack('phase/b')
        self.assertEqual(code, 0, out)
        self.assertIn('base: local main %s is 2 behind origin/main' % self.stale[:7], out)
        self.assertIn('base     refs/remotes/origin/main @ ', out)
        self.assertIn('merge-base %s | contains base tip: yes' % self.fresh[:7], out)
        self.assertIn('commits  1 (no merges)', out)

    def test_the_same_branch_against_the_stale_local_main_still_has_a_merge_base(self):
        self.advance_origin()
        self.git('checkout', '-q', '-b', 'phase/b', 'origin/main')
        code, out = self.run_pack('phase/b', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('merge-base %s' % self.stale[:7], out)

    def test_a_branch_from_a_local_main_ahead_of_origin(self):
        self.write('local.txt', 'x')
        self.commit_all('unpushed local main work')
        self.git('checkout', '-q', '-b', 'phase/c')
        self.write('f.txt', '1')
        self.commit_all('phase work')
        code, out = self.run_pack('phase/c')
        self.assertEqual(code, 0, out)
        self.assertIn('merge-base', out)

    def test_a_branch_with_no_history_in_common_names_it(self):
        self.git('checkout', '-q', '--orphan', 'phase/d')
        self.git('rm', '-q', '-rf', '.')
        self.write('g.txt', '1')
        self.commit_all('unrelated root')
        code, out = self.run_pack('phase/d')
        self.assertEqual(code, 2, out)
        self.assertIn('cannot find a merge-base for', out)
        self.assertIn('phase/d', out)
        self.assertIn('no common history', out)
        self.assertNotIn('shallow', out)


@real_git('a shallow clone (git clone --depth)')
class ShallowClone(PackCase):
    """A clone cut off at different depths for main and the branch, so no common ancestor is
    in it: git exits 1 with nothing on stderr, as it does for unrelated histories."""

    def clone(self):
        self.remote = os.path.join(self.r.base, 'remote.git')
        self.r.git('init', '-q', '--bare', self.remote, cwd=self.r.base)
        self.git('remote', 'add', 'origin', self.remote)
        self.git('branch', '-q', 'phase/e')
        self.git('checkout', '-q', 'phase/e')
        for i in range(3):
            self.write('p%d.txt' % i, 'x')
            self.commit_all('phase %d' % i)
        self.git('push', '-q', 'origin', 'main', 'phase/e')
        self.shallow = os.path.join(self.r.base, 'shallow')
        self.r.git('clone', '-q', '--depth=1', '--branch', 'main', '--no-single-branch',
                 pathlib.Path(self.remote).as_uri(), self.shallow, cwd=self.r.base)
        self.r.git('fetch', '-q', '--depth=1', 'origin', 'phase/e:refs/remotes/origin/phase/e',
                 cwd=self.shallow)

    def test_a_shallow_clone_with_no_shared_commit_says_it_is_shallow(self):
        self.clone()
        out = subprocess.run(
            ('git', 'merge-base', 'origin/main', 'origin/phase/e'), cwd=self.shallow,
            capture_output=True, text=True, env=fixture_env())
        self.assertEqual(out.returncode, 1, out)
        from kitlib import reviewpack
        mb, why = reviewpack.merge_base(self.shallow, 'origin/main', 'origin/phase/e')
        self.assertIsNone(mb)
        self.assertIn('shallow', why)

    def test_refs_merge_base_in_a_shallow_clone_is_no_answer_not_none(self):
        # The cut-off commits read as roots there, so disjoint "roots" prove nothing.
        self.clone()
        from kitlib.git import refs
        mb, why = refs.merge_base('origin/main', 'origin/phase/e', self.shallow)
        self.assertIsNone(mb, 'a shallow clone was read as "no common history"')
        self.assertIn('shallow clone', why)


class TheBaseNoteAndTheDiffOption(WithOrigin):
    NOTE = 'review_pack: base: local main '

    def phase(self):
        self.advance_origin()
        self.git('checkout', '-q', '-b', 'phase/f', 'origin/main')
        self.write('f.txt', '1')
        self.commit_all('phase work')

    def test_the_pack_prints_the_base_note(self):
        self.phase()
        code, out = self.run_pack('phase/f')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count(self.NOTE), 1, out)

    def test_diff_without_a_base_prints_only_the_files_diff(self):
        self.phase()
        code, out = self.run_pack('phase/f', '--diff', 'f.txt')
        self.assertEqual(code, 0, out)
        self.assertNotIn('base:', out)
        self.assertTrue(out.lstrip().startswith('diff --git a/f.txt b/f.txt'), out)
        self.assertIn('+1', out)
        self.assertNotIn('up0.txt', out)


class BranchCommitsWithAFullRefBase(WithOrigin):
    """trailers.branch_commits probes `refs/remotes/<remote>/<base>` for a short base only."""

    def probes(self, base):
        seen = []
        real = trailers.gitcmd.call

        def call(args, cwd, *a, **k):
            seen.append(list(args))
            return real(args, cwd, *a, **k)
        with mock.patch.object(trailers.gitcmd, 'call', call):
            commits, why = trailers.branch_commits(base, self.root, head='phase/g')
        self.assertIsNone(why)
        return commits, [a[-1] for a in seen if a[:1] == ['rev-parse']]

    def phase(self):
        self.advance_origin()
        self.git('checkout', '-q', '-b', 'phase/g', 'origin/main')
        self.write('f.txt', '1')
        self.commit_all('phase work')

    def test_a_full_ref_base_makes_no_tracking_probe(self):
        self.phase()
        commits, probes = self.probes('refs/remotes/origin/main')
        self.assertEqual([c.subject for c in commits], ['phase work'])
        self.assertEqual(probes, [])

    def test_a_short_base_still_probes_its_tracking_ref(self):
        self.phase()
        commits, probes = self.probes('main')
        self.assertEqual(probes, ['refs/remotes/origin/main'])
        self.assertEqual([c.subject for c in commits], ['phase work'])


class ADiffRunThatStops(WithOrigin):
    def test_the_error_names_the_base_note_when_the_base_was_defaulted(self):
        self.advance_origin()
        code, out = self.run_pack('no-such-branch', '--diff', 'f.txt')
        self.assertEqual(code, 2, out)
        self.assertIn('cannot find a merge-base for refs/remotes/origin/main and no-such-branch', out)
        self.assertIn('[review_pack: base: local main %s is 2 behind origin/main' % self.stale[:7], out)
