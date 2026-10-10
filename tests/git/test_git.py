"""kitlib.git against real throwaway repositories (tests/gitfixture.py)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib import git
from kitlib.git import batch, cmd, refs, repo
from tests import gitfixture
from tests.realgit import real_git

ZERO = '0' * 40


def _outside_any_repo(path):
    """True when git places `path` in no repository (a temp folder normally is not)."""
    return git.classify(path).kind == git.NONE


class CmdTest(unittest.TestCase):

    def test_scrubbed_env_drops_git_location_variables_in_any_case(self):
        env = git.scrubbed_env({'GIT_DIR': 'x', 'git_work_tree': 'y', 'GIT_COMMON_DIR': 'z',
                                'GIT_CEILING_DIRECTORIES': 'c', 'PATH': 'p', 'GIT_AUTHOR_NAME': 'a'})
        self.assertEqual(env, {'PATH': 'p', 'GIT_AUTHOR_NAME': 'a'})

    def test_text_reads_crlf_and_lone_cr_as_lf_and_replaces_bad_utf8(self):
        self.assertEqual(git.text(b'a\r\nb\rc\n\xff'), 'a\nb\nc\n\ufffd')

    def test_a_timeout_is_a_failed_call_not_a_hang(self):
        boom = subprocess.TimeoutExpired(['git'], 1)
        with mock.patch.object(cmd, '_run', side_effect=boom):
            r = git.call(['status'], '.', timeout=1)
            out, why = git.run(['status'], '.')
        self.assertIsNone(r.rc)
        self.assertIn('timed out', r.err)
        self.assertIsNone(out)
        self.assertIn('timed out', why)

    def test_git_that_cannot_start_is_a_failed_call(self):
        with mock.patch.object(cmd, '_run', side_effect=FileNotFoundError('no git')):
            r = git.call(['status'], '.')
        self.assertIsNone(r.rc)
        self.assertIn('could not run git', r.err)

    def test_every_call_has_a_timeout_by_default(self):
        seen = {}

        def fake(*a, **kw):
            seen.update(kw)
            return 0, b'', b''
        with mock.patch.object(cmd, '_run', side_effect=fake):
            git.call(['status'], '.')
        self.assertEqual(seen['timeout'], git.GIT_TIMEOUT)
        self.assertNotIn('GIT_DIR', {k.upper() for k in seen['env']})

    def test_every_git_child_runs_in_the_c_locale_whatever_env_is_passed(self):
        seen = []

        def fake(*a, **kw):
            seen.append(kw['env'])
            return 0, b'', b''
        with mock.patch.dict(os.environ, {'LC_ALL': 'de_DE.UTF-8', 'LANGUAGE': 'de'}), \
                mock.patch.object(cmd, '_run', side_effect=fake):
            git.call(['status'], '.')
            git.call(['status'], '.', env={'PATH': 'p', 'LC_ALL': 'fr_FR'})
        for env in seen:
            self.assertEqual((env['LC_ALL'], env['LANGUAGE']), ('C', 'C'))
        self.assertEqual(seen[1]['PATH'], 'p')
        self.assertEqual(cmd.child_env({'GIT_DIR': 'x'})['GIT_DIR'], 'x')    # a caller's env is its own


class RepoFixture(unittest.TestCase):
    """One repository with one linked worktree, shared by read-only tests."""

    @classmethod
    def setUpClass(cls):
        cls.r = gitfixture.TempRepo()
        cls.wt = cls.r.worktree('feature')
        cls.r.write('feature.txt', 'f\n', root=cls.wt)
        cls.feature = cls.r.commit('feature', 'feature.txt', cwd=cls.wt)
        cls.other = gitfixture.TempRepo()

    @classmethod
    def tearDownClass(cls):
        cls.r.cleanup()
        cls.other.cleanup()


class ClassifyTest(RepoFixture):

    def test_main_checkout_is_main(self):
        p = git.classify(self.r.root)
        self.assertEqual(p.kind, git.MAIN)
        self.assertTrue(git.same_path(p.top, self.r.root))
        self.assertEqual(p.common, git.real(self.r.common_dir()))
        self.assertEqual(p.git_dir, p.common)
        self.assertTrue(git.is_main_checkout(self.r.root))

    def test_linked_worktree_is_linked_and_shares_the_common_dir(self):
        p = git.classify(self.wt)
        self.assertEqual(p.kind, git.LINKED)
        self.assertTrue(git.same_path(p.top, self.wt))
        self.assertEqual(p.common, git.real(self.r.common_dir()))
        self.assertNotEqual(p.git_dir, p.common)
        self.assertFalse(git.is_main_checkout(self.wt))

    def test_a_path_that_does_not_exist_yet_is_judged_by_its_nearest_folder(self):
        self.assertEqual(git.classify(os.path.join(self.wt, 'new', 'deep', 'x.py')).kind, git.LINKED)
        self.assertEqual(git.classify(self.r.path('new', 'x.py')).kind, git.MAIN)

    def test_no_repository_is_none(self):
        d = tempfile.mkdtemp(prefix='kit-norepo-')
        try:
            if not _outside_any_repo(os.path.dirname(d)):
                self.skipTest('the temp folder is itself inside a git repository')
            p = git.classify(d)
            self.assertEqual(p.kind, git.NONE)
            self.assertIn('not a git repository', p.detail.lower())
        finally:
            gitfixture.rmtree(d)

    def test_any_other_failure_is_unknown_never_a_pass(self):
        with mock.patch.object(cmd, '_run', side_effect=subprocess.TimeoutExpired(['git'], 1)):
            self.assertEqual(git.classify(self.r.root).kind, git.UNKNOWN)
        bad = (128, b'', b'fatal: something else broke')
        with mock.patch.object(cmd, '_run', return_value=bad):
            p = git.classify(self.r.root)
        self.assertEqual(p.kind, git.UNKNOWN)
        self.assertIn('something else broke', p.detail)
        short = (0, b'/a\n/b\n', b'')
        with mock.patch.object(cmd, '_run', return_value=short):
            self.assertEqual(git.classify(self.r.root).kind, git.UNKNOWN)

    def test_an_inherited_git_dir_does_not_redirect_the_answer(self):
        # Without the scrub, GIT_DIR naming the other repository makes git describe it instead.
        with mock.patch.dict(os.environ, {'GIT_DIR': self.other.common_dir()}):
            p = git.classify(self.wt)
            common, _ = git.common_dir(self.wt)
        self.assertEqual(p.kind, git.LINKED)
        self.assertEqual(common, git.real(self.r.common_dir()))

    def test_a_ceiling_naming_the_main_checkout_is_ignored(self):
        # Unscrubbed, git run in <root>/ceiling-sub with the ceiling at <root> says "not a git
        # repository" (checked by hand with git 2.55), so this would read NONE.
        sub = self.r.path('ceiling-sub')
        os.makedirs(sub, exist_ok=True)
        with mock.patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': self.r.root}):
            self.assertEqual(git.classify(sub).kind, git.MAIN)

    def test_common_dir_and_git_path(self):
        a, _ = git.common_dir(self.r.root)
        b, _ = git.common_dir(self.wt)
        self.assertEqual(a, b)
        merge_head, why = git.git_path(self.wt, 'MERGE_HEAD')
        self.assertIsNone(why)
        self.assertTrue(git.inside(merge_head, os.path.join(self.r.common_dir(), 'worktrees')))
        # A name git does not know is per work tree: a shared kit file belongs under common_dir().
        own, _ = git.git_path(self.wt, 'kit-landing.lock')
        self.assertFalse(git.same_path(os.path.dirname(own), self.r.common_dir()))
        config, _ = git.git_path(self.wt, 'config')
        self.assertTrue(git.same_path(os.path.dirname(config), self.r.common_dir()))

    def test_worktrees_lists_main_first(self):
        wts, why = git.worktrees(self.wt)
        self.assertIsNone(why)
        self.assertEqual(len(wts), 2)
        self.assertTrue(git.same_path(wts[0].path, self.r.root))
        self.assertEqual(wts[0].branch, 'main')
        self.assertTrue(git.same_path(wts[1].path, self.wt))
        self.assertEqual(wts[1].branch, 'kit/feature')
        self.assertEqual(wts[1].head, self.feature)
        self.assertEqual([git.norm(p) for p in git.linked_worktrees(self.r.root)], [git.norm(self.wt)])

    def test_main_checkout_toplevel_from_either_side(self):
        for here in (self.r.root, self.wt, os.path.join(self.wt, 'missing')):
            self.assertTrue(git.same_path(git.main_checkout_toplevel(here), self.r.root), here)

    def test_same_repository(self):
        self.assertEqual(git.same_repository(self.r.root, self.wt), (True, None))
        self.assertEqual(git.same_repository(self.r.root, self.other.root), (False, None))


class ParseWorktreesTest(unittest.TestCase):

    def test_every_field_git_prints(self):
        data = (b'worktree C:/r\0HEAD ' + b'a' * 40 + b'\0branch refs/heads/main\0\0'
                b'worktree C:/r/.claude/worktrees/x\0HEAD ' + b'b' * 40 + b'\0detached\0locked why\0\0'
                b'worktree C:/gone\0HEAD ' + b'c' * 40 + b'\0branch refs/heads/kit/gone\0prunable gitdir file points to non-existent location\0\0')
        wts = repo.parse_worktrees(data)
        self.assertEqual([w.branch for w in wts], ['main', None, 'kit/gone'])
        self.assertTrue(wts[1].detached)
        self.assertEqual(wts[1].locked, 'why')
        self.assertIn('non-existent', wts[2].prunable)
        self.assertEqual(wts[0].head, 'a' * 40)

    def test_bare(self):
        wts = repo.parse_worktrees(b'worktree C:/b.git\0bare\0\0')
        self.assertTrue(wts[0].bare)


class TopLevelTest(RepoFixture):
    """-C is a plain path: any work tree of any repository names its own top level."""

    def test_a_path_names_the_top_level_of_its_own_work_tree(self):
        for path, top in ((self.wt, self.wt), (self.r.root, self.r.root),
                          (self.other.root, self.other.root)):
            got, why = git.toplevel(path)
            self.assertIsNone(why)
            self.assertTrue(git.same_path(got, top), (path, got))
        os.makedirs(os.path.join(self.wt, 'sub'), exist_ok=True)
        self.assertTrue(git.same_path(git.toplevel(os.path.join(self.wt, 'sub'))[0], self.wt))

    def test_a_missing_folder_or_no_work_tree_is_refused(self):
        self.assertIn('not a directory', git.toplevel(os.path.join(self.wt, 'nope'))[1])
        d = tempfile.mkdtemp(prefix='kit-norepo-')
        try:
            if not _outside_any_repo(d):
                self.skipTest('the temp folder is itself inside a git repository')
            top, why = git.toplevel(d)
            self.assertIsNone(top)
            self.assertIn('in no git work tree', why)
        finally:
            gitfixture.rmtree(d)

    def test_no_hand_off_helpers_remain(self):
        for gone in ('check_same_repo', 'resolve_worktree'):
            self.assertFalse(hasattr(git, gone), gone)


class CheckWriteTest(RepoFixture):

    def test_inside_the_worktree_is_allowed(self):
        self.assertTrue(git.same_path(git.check_write('a/b.txt', self.wt), os.path.join(self.wt, 'a', 'b.txt')))
        self.assertTrue(git.same_path(git.check_write(os.path.join(self.wt, 'c.txt'), self.wt),
                                      os.path.join(self.wt, 'c.txt')))

    def test_the_main_checkout_is_refused(self):
        with self.assertRaisesRegex(git.WriteRefused, 'MAIN checkout'):
            git.check_write(self.r.path('README.md'), self.wt)

    def test_a_relative_climb_out_is_refused(self):
        with self.assertRaisesRegex(git.WriteRefused, 'climbs out'):
            git.check_write(os.path.join('..', 'x.txt'), self.wt)

    def test_another_worktree_is_refused(self):
        other_wt = self.r.worktree('second')
        with self.assertRaisesRegex(git.WriteRefused, 'another worktree'):
            git.check_write(os.path.join(other_wt, 'x.txt'), self.wt)

    def test_outside_every_repository_is_allowed(self):
        d = tempfile.mkdtemp(prefix='kit-norepo-')
        try:
            if not _outside_any_repo(d):
                self.skipTest('the temp folder is itself inside a git repository')
            self.assertTrue(git.same_path(git.check_write(os.path.join(d, 'x.txt'), self.wt),
                                          os.path.join(os.path.realpath(d), 'x.txt')))
        finally:
            gitfixture.rmtree(d)

    def test_the_root_must_be_a_linked_worktree(self):
        with self.assertRaisesRegex(git.WriteRefused, 'MAIN checkout, not a linked worktree'):
            git.check_write('x.txt', self.r.root)
        with self.assertRaisesRegex(git.WriteRefused, 'not a linked worktree root'):
            git.check_write('x.txt', os.path.join(self.wt, 'sub'))

    def test_a_path_git_cannot_place_is_refused(self):
        real_classify = repo.classify

        def flaky(path):
            if git.same_path(path, self.wt):
                return real_classify(path)
            return repo.Place(git.UNKNOWN, None, 'boom', None, None)
        with mock.patch.object(repo, 'classify', side_effect=flaky):
            with self.assertRaisesRegex(git.WriteRefused, 'could not tell'):
                git.check_write(os.path.join(self.other.root, 'x'), self.wt)


class RefsTest(RepoFixture):

    def test_bad_revs_never_reach_git(self):
        for rev in ('', '-n', '--all', 'a\nb', 'a\0b', None):
            self.assertIsNotNone(refs.bad_rev(rev), rev)
            self.assertIsNone(git.resolve(rev, self.r.root)[0])
            self.assertIsNone(git.is_ancestor(rev, 'HEAD', self.r.root)[0])
        self.assertIsNone(refs.bad_rev('HEAD~1'))

    def test_resolve_head_tree_and_tips(self):
        head, _ = git.head(self.wt)
        self.assertEqual(head, self.feature)
        self.assertTrue(git.is_sha(head))
        for not_sha in (head + chr(10), head[:39], head.upper(), head + "0", None):
            self.assertFalse(git.is_sha(not_sha), repr(not_sha))
        tree, _ = git.tree_of('HEAD', self.wt)
        self.assertEqual(tree, self.r.git('rev-parse', 'HEAD^{tree}', cwd=self.wt))
        self.assertEqual(git.branch_tip('kit/feature', self.r.root), (self.feature, None))
        self.assertEqual(git.branch_tip('main', self.wt), (self.r.initial, None))
        missing, why = git.branch_tip('no-such', self.r.root)
        self.assertIsNone(missing)
        self.assertIn('no-such', why)

    def test_current_branch(self):
        self.assertEqual(git.current_branch(self.r.root), ('main', None))
        self.assertEqual(git.current_branch(self.wt), ('kit/feature', None))

    def test_ancestry(self):
        self.assertEqual(git.is_ancestor(self.r.initial, self.feature, self.r.root), (True, None))
        self.assertEqual(git.is_ancestor(self.feature, self.r.initial, self.r.root), (False, None))
        self.assertEqual(git.is_ancestor(self.feature, self.feature, self.r.root), (True, None))
        unknown, why = git.is_ancestor('f' * 40, self.feature, self.r.root)
        self.assertIsNone(unknown)
        self.assertTrue(why)

    def test_fast_forward(self):
        self.assertEqual(git.is_fast_forward(self.r.initial, self.feature, self.r.root), (True, None))
        self.assertEqual(git.is_fast_forward(self.feature, self.r.initial, self.r.root), (False, None))
        self.assertEqual(git.is_fast_forward(ZERO, self.feature, self.r.root), (True, None))
        self.assertEqual(git.is_fast_forward(self.feature, ZERO, self.r.root), (False, None))

    def test_merge_base(self):
        self.assertEqual(git.merge_base('main', 'kit/feature', self.r.root), (self.r.initial, None))

    def test_a_tree_or_blob_is_no_ancestor_answer(self):
        # rev-list alone answers YES for a tree; the ^{commit} peel refuses it, as merge-base did.
        for rev in ('%s^{tree}' % self.feature, '%s:' % self.feature):
            self.assertIsNone(git.is_ancestor(rev, self.feature, self.r.root)[0], rev)
            self.assertIsNone(git.is_ancestor(self.feature, rev, self.r.root)[0], rev)


class AKilledGitIsNoAncestryAnswer(RepoFixture):
    """KIT-TEST-GRANDCHILD-SHIELD: a git ended from outside (a stale `taskkill /PID <n> /T /F`
    that reached a reused pid, kitlib.proc.shield's GOTCHA) exits 1 having printed nothing, the
    shape `merge-base --is-ancestor` gives for NO. refs.is_ancestor must give no answer for it,
    never a NO git did not say. The pair asked is a real NO (feature is not in initial), so an
    answer of False could only come from reading the kill as one."""

    def test_exit_1_with_no_output_is_no_answer(self):
        calls = []

        def killed(argv, cwd=None, input=None, timeout=None, env=None):
            calls.append(argv)
            return 1, b'', b''
        with mock.patch.object(cmd, '_run', side_effect=killed):
            got, why = refs.is_ancestor(self.feature, self.r.initial, self.r.root)
        self.assertEqual(len(calls), 1, calls)
        self.assertIsNone(got, 'a killed git was read as an answer')
        self.assertIn('printed nothing', why)


class ANoIsConfirmed(RepoFixture):
    """rev-list's NO can be wrong (its walk stops early under skewed commit dates, refs.is_ancestor
    GOTCHA), so refs.is_ancestor confirms every NO with `merge-base --independent`; a confirmation
    that fails is no answer, never a NO. git is a stub here, by subcommand; rev-list always says NO."""

    def ask(self, independent, rev_parse=None):
        a, d = self.feature, self.r.initial
        calls = []

        def stub(argv, cwd=None, input=None, timeout=None, env=None):
            calls.append(argv[1])
            if argv[1] == 'rev-list':
                return 0, (a + '\n').encode(), b''
            if argv[1] == 'rev-parse':
                return rev_parse or (0, (a + '\n').encode(), b'')
            if argv[1] == 'merge-base':
                self.assertEqual(argv[2:], ['--independent', a, d + '^{commit}'])
                return independent
            raise AssertionError(argv)
        with mock.patch.object(cmd, '_run', side_effect=stub):
            got = refs.is_ancestor(a, d, self.r.root)
        return got, calls

    def test_a_confirmed_no_is_a_no(self):
        both = (0, ('%s\n%s\n' % (self.feature, self.r.initial)).encode(), b'')
        self.assertEqual(self.ask(both), ((False, None), ['rev-list', 'rev-parse', 'merge-base']))

    def test_a_no_the_exact_walk_overturns_is_a_yes(self):
        # Only the descendant is independent: it reaches the ancestor, whatever rev-list printed.
        self.assertEqual(self.ask((0, (self.r.initial + '\n').encode(), b''))[0], (True, None))

    def test_a_confirmation_that_fails_is_no_answer(self):
        for independent, rev_parse, words in (
                ((1, b'', b''), None, 'printed nothing'),                   # killed
                ((128, b'', b'fatal: boom'), None, 'fatal: boom'),
                ((0, b'', b''), None, 'not one or two commits'),
                ((0, b'abc\n', b''), None, 'not one or two commits'),
                (None, (1, b'', b''), 'does not name a commit')):          # rev-parse killed
            with self.subTest(words=words):
                (got, why), _ = self.ask(independent, rev_parse)
                self.assertIsNone(got)
                self.assertIn('confirming a NO', why)
                self.assertIn(words, why)

    def test_a_yes_needs_no_confirmation(self):
        calls = []

        def stub(argv, cwd=None, input=None, timeout=None, env=None):
            calls.append(argv[1])
            return 0, b'', b''
        with mock.patch.object(cmd, '_run', side_effect=stub):
            self.assertEqual(refs.is_ancestor(self.r.initial, self.feature, self.r.root),
                             (True, None))
        self.assertEqual(calls, ['rev-list'])


@real_git('rev-list\'s early stop under skewed commit dates is real git\'s walk; the fake is exact')
class SkewedDates(unittest.TestCase):
    """A root dated AFTER the 8 commits built on it: rev-list's limited walk stops before it
    reaches the root and prints it - a NO that is wrong (measured on real git 2026-10-10, no
    commit-graph). refs.is_ancestor's confirmation turns it into the right YES."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, self.dir)
        self.git('init', '-q')
        self.root = self.commit('root', '2000000000 +0000')
        for n in range(8):
            self.tip = self.commit('c%d' % n, '1000000000 +0000')

    def git(self, *args, date=None):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        if date:
            env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = date
        r = subprocess.run(['git'] + list(args), cwd=self.dir, env=env, capture_output=True,
                           text=True, check=True)
        return r.stdout.strip()

    def commit(self, message, date):
        self.git('-c', 'commit.gpgSign=false', 'commit', '-q', '--allow-empty', '-m', message,
                 date=date)
        return self.git('rev-parse', 'HEAD')

    def test_rev_lists_wrong_no_is_confirmed_away(self):
        # The premise: rev-list alone says NO here (else this test proves nothing).
        self.assertEqual(self.git('rev-list', '-n', '1', '%s..%s' % (self.tip, self.root)),
                         self.root)
        self.assertEqual(refs.is_ancestor(self.root, self.tip, self.dir), (True, None))
        self.assertEqual(refs.is_ancestor(self.tip, self.root, self.dir), (False, None))

    def test_unrelated_histories_are_a_confirmed_no(self):
        # pmdata's case: a data branch's root against main shares no commit. `merge-base` would
        # exit 1 with no output there (a kill's shape); `--independent` prints both: a NO.
        orphan = self.git('-c', 'commit.gpgSign=false', 'commit-tree', '-m', 'orphan',
                          '4b825dc642cb6eb9a060e54bf8d69288fbee4904')
        self.assertEqual(refs.is_ancestor(orphan, self.tip, self.dir), (False, None))
        self.assertEqual(refs.is_ancestor(self.tip, orphan, self.dir), (False, None))


@real_git('the git process itself: a real git ended by taskkill, and the rev-list form on real git')
class AKilledRealGitIsNoAncestryAnswer(RepoFixture):
    """AKilledGitIsNoAncestryAnswer with the real killer on a real git, and the same answers from
    real git as RefsTest gets from the fake."""

    def test_real_git_answers(self):
        self.assertEqual(refs.is_ancestor(self.r.initial, self.feature, self.r.root), (True, None))
        self.assertEqual(refs.is_ancestor(self.feature, self.r.initial, self.r.root), (False, None))
        self.assertEqual(refs.is_ancestor(self.feature, self.feature, self.r.root), (True, None))
        for rev in ('f' * 40, '%s^{tree}' % self.feature, '%s:' % self.feature):
            self.assertIsNone(refs.is_ancestor(rev, self.feature, self.r.root)[0], rev)
            self.assertIsNone(refs.is_ancestor(self.feature, rev, self.r.root)[0], rev)

    @unittest.skipUnless(os.name == 'nt', 'taskkill is Windows-only')
    def test_a_git_ended_by_taskkill_is_no_answer(self):
        # The real killer on a real git: started suspended (it runs nothing), ended by taskkill /F
        # by its pid while this process holds its handle (so the pid cannot be anyone else's).
        seen = []

        def killed(argv, cwd=None, input=None, timeout=None, env=None):
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=0x00000004)              # CREATE_SUSPENDED
            try:
                tk = subprocess.run(['taskkill', '/PID', str(p.pid), '/F'], capture_output=True)
                out, err = p.communicate(timeout=60)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
            seen.append((tk.returncode, p.returncode, out, err))
            return p.returncode, out, err
        with mock.patch.object(cmd, '_run', side_effect=killed):
            got, why = refs.is_ancestor(self.feature, self.r.initial, self.r.root)
        self.assertEqual(seen, [(0, 1, b'', b'')])          # killed: exit 1, nothing printed
        self.assertIsNone(got, 'a git ended by taskkill was read as an answer')
        self.assertIn('printed nothing', why)


def kill_git(match):
    """A patch of cmd._run under which each git argv `match(argv)` picks exits 1 having printed
    nothing (a git ended from outside, refs.KILLED_SHAPE) and every other one runs as before (the
    spawner captured before the patch: the fake, or real git)."""
    real = cmd.spawner()

    def run(argv, cwd=None, input=None, timeout=None, env=None):
        if match(argv):
            return 1, b'', b''
        return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
    return mock.patch.object(cmd, '_run', side_effect=run)


class AKilledGitIsNoBranchOrBaseAnswer(RepoFixture):
    """KIT-GIT-EXIT1-ANSWERS: `symbolic-ref --quiet` (detached) and `merge-base` (no common
    commit) answer with exit 1 and no output, the shape a git ended from outside leaves. Each read
    must give no answer for a kill, never "detached" or "no merge base"."""

    def test_a_killed_branch_read_is_no_answer_not_a_detached_head(self):
        with kill_git(lambda argv: True):
            got, why = refs.current_branch(self.r.root)
        self.assertIsNone(got, 'a killed git was read as a detached HEAD')
        self.assertIn('printed nothing', why)

    def test_a_killed_merge_base_is_no_answer_when_the_histories_share_a_root(self):
        with kill_git(lambda argv: argv[1] == 'merge-base'):
            got, why = refs.merge_base('main', 'kit/feature', self.r.root)
        self.assertIsNone(got, 'a killed merge-base was read as "no common history"')
        self.assertIn('confirming no merge base', why)
        self.assertIn(self.r.initial[:10], why)

    def test_a_merge_base_none_whose_confirmation_is_killed_is_no_answer(self):
        with kill_git(lambda argv: argv[1] in ('merge-base', 'rev-list')):
            got, why = refs.merge_base('main', 'kit/feature', self.r.root)
        self.assertIsNone(got)
        self.assertIn('confirming no merge base', why)
        self.assertIn('printed nothing', why)


class PositiveForms(RepoFixture):
    """The answers the positive forms give where git really answers (KIT-GIT-EXIT1-ANSWERS)."""

    def test_unrelated_histories_have_no_merge_base(self):
        orphan = self.r.git('commit-tree', self.r.git('rev-parse', 'HEAD^{tree}'), '-m', 'orphan')
        self.assertEqual(refs.merge_base(orphan, 'main', self.r.root), ('', None))
        self.assertEqual(refs.merge_base('main', orphan, self.r.root), ('', None))

    def test_ref_commit(self):
        self.assertEqual(refs.ref_commit('refs/heads/kit/feature', self.r.root), (self.feature, None))
        self.assertEqual(refs.ref_commit('refs/heads/no-such', self.r.root), ('', None))
        # for-each-ref's pattern also matches the refs below it: only the exact name answers.
        self.assertEqual(refs.ref_commit('refs/heads/kit', self.r.root), ('', None))
        for bad in ('main', '-x', 'refs/heads/a\nb'):
            got, why = refs.ref_commit(bad, self.r.root)
            self.assertIsNone(got, bad)
            self.assertTrue(why)

    def test_a_killed_ref_read_is_no_answer_not_no_ref(self):
        with kill_git(lambda argv: True):
            got, why = refs.ref_commit('refs/heads/kit/feature', self.r.root)
        self.assertIsNone(got, 'a killed git was read as "no such ref"')
        self.assertIn('printed nothing', why)


class _Parity(object):
    """What the fake prints for the positive forms is what real git prints (each check runs on
    the fake in FakeParity and on real git in RealGitParity)."""

    def setUp(self):
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)

    def test_config_list_z_puts_a_newline_between_key_and_value(self):
        self.r.git('config', 'kit.probe', 'a b')
        with open(os.path.join(self.r.common_dir(), 'config'), 'a', encoding='utf-8',
                  newline='\n') as fh:
            fh.write('[kit "Sub"]\n\tflag\n')
        out, why = cmd.run(['config', '--local', '--list', '-z'], self.r.root)
        self.assertIsNone(why)
        items = out.split(b'\0')
        self.assertIn(b'kit.probe\na b', items)
        self.assertIn(b'kit.Sub.flag', items)          # no value: the key alone

    def test_branch_show_current(self):
        self.assertEqual(refs.current_branch(self.r.root), ('main', None))
        self.r.git('checkout', '-q', '--detach')
        self.assertEqual(refs.current_branch(self.r.root), ('', None))

    def test_a_head_outside_refs_heads_is_no_branch_answer(self):
        self.r.git('symbolic-ref', 'HEAD', 'refs/remotes/origin/main')
        got, why = refs.current_branch(self.r.root)
        self.assertIsNone(got)
        self.assertIn('refs/heads', why)            # git's wording varies by version

    def test_an_annotated_tag_ref_peels_to_its_commit(self):
        self.r.git('tag', '-a', '-m', 'v', 'v1')
        self.assertEqual(refs.ref_commit('refs/tags/v1', self.r.root), (self.r.initial, None))


class FakeParity(_Parity, unittest.TestCase):
    pass


@real_git('parity: the fake\'s `config --list -z` and `branch --show-current` against real git')
class RealGitParity(_Parity, unittest.TestCase):
    pass


class WorkTreeStateTest(unittest.TestCase):

    def setUp(self):
        self.r = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.r.cleanup)

    def test_detached_head_has_no_branch(self):
        self.r.git('checkout', '-q', '--detach')
        self.assertEqual(git.current_branch(self.r.root), ('', None))

    def test_status_and_is_clean(self):
        self.assertEqual(git.is_clean(self.r.root), (True, None))
        self.r.write('new dir/a b.txt', 'x\n')
        self.r.write('README.md', 'changed\n')
        self.r.write('.gitignore', 'ignored.txt\n')
        self.r.write('ignored.txt', 'i\n')
        entries, why = git.status(self.r.root)
        self.assertIsNone(why)
        self.assertEqual(sorted(entries), [(' M', 'README.md'), ('??', '.gitignore'), ('??', 'new dir/a b.txt')])
        self.assertEqual(git.is_clean(self.r.root), (False, None))

    def test_a_rename_is_one_entry(self):
        self.r.git('mv', 'README.md', 'READ ME.md')
        self.r.write('z.txt', 'z\n')
        entries, _ = git.status(self.r.root)
        self.assertEqual(entries, [('R ', 'READ ME.md'), ('??', 'z.txt')])

    def test_merge_in_progress_is_per_work_tree(self):
        wt = self.r.worktree('side')
        self.r.write('README.md', 'side\n', root=wt)
        self.r.commit('side', 'README.md', cwd=wt)
        self.r.write('README.md', 'main\n')
        self.r.commit('main', 'README.md')
        self.assertEqual(git.merge_in_progress(self.r.root), (False, None))
        self.r.git('merge', '-q', '--no-edit', 'kit/side', check=False)
        self.assertEqual(git.merge_in_progress(self.r.root), (True, None))
        self.assertEqual(git.merge_in_progress(wt), (False, None))
        self.r.git('merge', '--abort')
        self.assertEqual(git.merge_in_progress(self.r.root), (False, None))

    def test_remote_tips(self):
        sha, why = git.ls_remote('origin', 'main', self.r.root)
        self.assertEqual((sha, why), (self.r.initial, None))
        self.assertEqual(git.ls_remote('origin', 'refs/heads/none', self.r.root), ('', None))
        self.assertIsNotNone(git.ls_remote('--upload-pack=x', 'main', self.r.root)[1])
        self.r.git('fetch', '-q', 'origin')
        self.assertEqual(git.remote_tip('origin', 'main', self.r.root), (self.r.initial, None))


class BatchTest(RepoFixture):

    def test_cat_file(self):
        got, why = batch.cat_file(['HEAD:feature.txt', 'HEAD:missing.txt', 'main:README.md'], self.wt)
        self.assertIsNone(why)
        self.assertEqual(got['HEAD:feature.txt'], ('blob', b'f\n'))
        self.assertIsNone(got['HEAD:missing.txt'])
        self.assertEqual(got['main:README.md'], ('blob', b'hello\n'))
        self.assertEqual(batch.cat_file([], self.wt), ({}, None))
        self.assertIsNone(batch.cat_file(['a\0b'], self.wt)[0])

    def test_commits(self):
        got, why = batch.commits(['main', 'kit/feature', 'nope', 'HEAD:README.md'], self.r.root)
        self.assertIsNone(why)
        self.assertEqual(got, {'main': self.r.initial, 'kit/feature': self.feature, 'nope': None,
                               'HEAD:README.md': None})
        self.assertIsNone(batch.commits(['a\nb'], self.r.root)[0])

    def test_ancestry(self):
        (chain, ancestors), why = batch.ancestry('kit/feature', self.r.root)
        self.assertIsNone(why)
        self.assertEqual(chain, {self.feature, self.r.initial})
        self.assertEqual(ancestors, chain)
        self.assertIsNone(batch.ancestry('-x', self.r.root)[0])

    def test_catfile_lazy_reader(self):
        with batch.CatFile(self.wt) as cat:
            self.assertEqual(cat.read('HEAD', 'feature.txt'), b'f\n')
            self.assertIsNone(cat.read('HEAD', 'missing'))
            self.assertIsNone(cat.read('HEAD', ''))          # a tree, not a blob
            self.assertEqual(cat.text('main', 'README.md'), 'hello\n')
            self.assertFalse(cat.dead)
        with batch.CatFile(self.wt, check=True) as cat:
            self.assertEqual(cat.size('HEAD', 'feature.txt'), 2)
            self.assertIsNone(cat.size('HEAD', 'missing'))

    def test_catfile_dead_git_is_reported(self):
        cat = batch.CatFile(os.path.join(self.r.base, 'no-such-dir'))
        self.assertIsNone(cat.read('HEAD', 'x'))
        self.assertTrue(cat.dead)
        self.assertIn('died', cat.dead_reason)
        cat.close()

    def hung_catfile(self, script):
        """A CatFile whose `git` is a Python fake running `script`, with a 1-second deadline."""
        started = []
        real = subprocess.Popen

        def fake(args, **kw):
            started.append(real([sys.executable, '-c', script], **kw))
            return started[-1]
        patcher = mock.patch.object(batch.subprocess, 'Popen', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return batch.CatFile(self.wt, timeout=1), started

    def assert_killed_in_time(self, script, check=False):
        cat, started = self.hung_catfile(script)
        cat.check = check
        t0 = time.monotonic()
        got = cat.size('HEAD', 'feature.txt') if check else cat.read('HEAD', 'feature.txt')
        self.assertLess(time.monotonic() - t0, 10)          # the fake sleeps 60
        self.assertIsNone(got)
        self.assertTrue(cat.dead)
        self.assertIn('within 1 seconds; git was killed', cat.dead_reason)
        self.assertIsNotNone(started[0].poll())             # close() waited for the killed fake
        self.assertIsNone(cat.read('HEAD', 'x') if not check else cat.size('HEAD', 'x'))
        self.assertEqual(len(started), 1)                    # dead stays dead: no second git
        cat.close()

    def test_a_catfile_git_that_never_answers_is_killed_and_dead(self):
        self.assert_killed_in_time('import sys, time\nsys.stdin.readline()\ntime.sleep(60)')

    def test_a_catfile_git_that_hangs_inside_a_body_is_killed_and_dead(self):
        self.assert_killed_in_time(
            'import sys, time\nsys.stdin.readline()\n'
            'sys.stdout.buffer.write(b"%s blob 10\\nabc")\nsys.stdout.flush()\ntime.sleep(60)' % ('a' * 40))

    def test_a_catfile_check_git_that_never_answers_is_killed_and_dead(self):
        self.assert_killed_in_time('import sys, time\nsys.stdin.readline()\ntime.sleep(60)', check=True)

    def test_a_catfile_reader_that_breaks_is_dead_not_a_raise(self):
        cat = batch.CatFile(self.wt)
        with mock.patch.object(cat, '_exchange', side_effect=MemoryError('huge body')):
            self.assertIsNone(cat.read('HEAD', 'feature.txt'))
        self.assertTrue(cat.dead)
        self.assertIn('huge body', cat.dead_reason)
        cat.close()

    def test_helpers(self):
        self.assertEqual(list(batch.chunks(['aaaa', 'bbbb', 'cc'], budget=10)), [['aaaa', 'bbbb'], ['cc']])
        self.assertEqual(batch.log_records('\x00h1\na\nb\n\n\x00h2\n'), [('h1', ['a', 'b']), ('h2', [])])
        self.assertEqual(batch.unquote_path('"a\\tb\\303\\251"'), 'a\tb\u00e9')
        self.assertEqual(batch.unquote_path('plain'), 'plain')
        env = batch.fixture_env({})
        self.assertEqual(env['GIT_CONFIG_COUNT'], '2')
        self.assertEqual(batch.fixture_env(dict(os.environ, **env)), {})


@real_git('the git process itself: the environment cat-file is started with')
class BatchProcessTest(RepoFixture):

    def test_catfile_runs_git_in_the_c_locale(self):
        seen = {}
        real = subprocess.Popen

        def spy(args, **kw):
            seen.update(kw['env'])
            return real(args, **kw)
        with mock.patch.object(batch.subprocess, 'Popen', spy), batch.CatFile(self.wt) as cat:
            self.assertEqual(cat.read('HEAD', 'feature.txt'), b'f\n')
        self.assertEqual((seen['LC_ALL'], seen['LANGUAGE']), ('C', 'C'))


if __name__ == '__main__':
    unittest.main()
