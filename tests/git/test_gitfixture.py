"""tests/gitfixture.py itself: a copied TempRepo is the repository the template is, and the HEAD it
reads from git's files is what `git rev-parse HEAD` prints."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import sessionmark
from tests import gitfixture
from tests.home import temp_home
from tests.realgit import real_git as on_real_git
from tests.platforms import windows_only


def real_git(cwd, *args):
    """git with no shortcut: what the fixture's own answers are checked against."""
    r = subprocess.run(('git',) + args, cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', env=gitfixture.fixture_env())
    if r.returncode != 0:
        raise AssertionError('git %s failed: %s' % (' '.join(args), r.stderr.strip()))
    return r.stdout.strip()


@on_real_git('checks the HEAD the fixture reads from files against git itself')
class HeadFromFiles(unittest.TestCase):

    def setUp(self):
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)

    def assert_head(self, top):
        self.assertEqual(gitfixture.head_from_files(top), real_git(top, 'rev-parse', 'HEAD'))

    def test_a_branch_with_a_loose_ref(self):
        self.r.write('a.txt', 'a\n')
        sha = self.r.commit('a', 'a.txt')
        self.assertEqual(sha, real_git(self.r.root, 'rev-parse', 'HEAD'))
        self.assert_head(self.r.root)

    def test_a_linked_worktree(self):
        wt = self.r.worktree('w')
        self.r.write('b.txt', 'b\n', root=wt)
        sha = self.r.commit('b', 'b.txt', cwd=wt)
        self.assertEqual(sha, real_git(wt, 'rev-parse', 'HEAD'))
        self.assertNotEqual(sha, real_git(self.r.root, 'rev-parse', 'HEAD'))
        self.assert_head(wt)
        self.assert_head(self.r.root)

    def test_a_detached_head(self):
        self.r.write('c.txt', 'c\n')
        self.r.commit('c', 'c.txt')
        real_git(self.r.root, 'checkout', '-q', '--detach', self.r.initial)
        self.assertEqual(gitfixture.head_from_files(self.r.root), self.r.initial)
        self.assert_head(self.r.root)

    def test_a_packed_ref_is_left_to_git(self):
        real_git(self.r.root, 'pack-refs', '--all')
        self.assertIsNone(gitfixture.head_from_files(self.r.root))
        self.assertEqual(self.r.head(), real_git(self.r.root, 'rev-parse', 'HEAD'))
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), self.r.initial)

    def test_an_unborn_branch_and_a_folder_with_no_repository_are_left_to_git(self):
        real_git(self.r.root, 'checkout', '-q', '--orphan', 'fresh')
        self.assertIsNone(gitfixture.head_from_files(self.r.root))
        self.assertIsNone(gitfixture.head_from_files(self.r.base))


@on_real_git('checks a copied template repository against git itself')
class Copies(unittest.TestCase):

    def make(self, **kw):
        r = gitfixture.TempRepo(**kw)
        self.addCleanup(r.cleanup)
        return r

    def test_a_copy_has_a_clean_index_and_the_built_config(self):
        self.make()
        copy = self.make()
        self.assertEqual(real_git(copy.root, 'diff-files', '--name-only'), '')
        self.assertEqual(real_git(copy.root, 'diff-index', '--name-only', 'HEAD'), '')
        self.assertEqual(real_git(copy.root, 'status', '--porcelain'), '')
        self.assertEqual(real_git(copy.root, 'config', 'user.name'), gitfixture.NAME)
        self.assertEqual(real_git(copy.root, 'config', 'user.email'), gitfixture.EMAIL)
        self.assertEqual(real_git(copy.root, 'config', 'core.trustctime'), 'false')
        self.assertEqual(real_git(copy.root, 'rev-parse', 'HEAD'), copy.initial)

    def test_a_copy_sees_an_edit_to_a_tracked_file(self):
        self.make()
        copy = self.make()
        copy.write('README.md', 'changed\n')
        self.assertEqual(real_git(copy.root, 'diff-files', '--name-only'), 'README.md')

    def test_a_copy_with_a_bare_remote_pushes_to_its_own_remote(self):
        first = self.make(bare_remote=True)
        copy = self.make(bare_remote=True)
        self.assertEqual(real_git(copy.root, 'config', 'remote.origin.fetch'),
                         '+refs/heads/*:refs/remotes/origin/*')
        url = real_git(copy.root, 'remote', 'get-url', 'origin')
        self.assertEqual(os.path.normcase(os.path.normpath(url)),
                         os.path.normcase(os.path.normpath(copy.remote)))
        copy.write('d.txt', 'd\n')
        sha = copy.commit('d', 'd.txt')
        copy.git('push', '-q', 'origin', 'main')
        self.assertEqual(real_git(copy.remote, 'rev-parse', 'refs/heads/main'), sha)
        self.assertEqual(real_git(first.remote, 'rev-parse', 'refs/heads/main'), first.initial)
        self.assertEqual(real_git(copy.root, 'rev-parse', 'refs/remotes/origin/main'), sha)

    def test_a_prepare_that_leaves_a_tracked_file_modified_is_refused_every_time(self):
        built = []

        def dirty(repo):
            built.append(repo.base)
            repo.write('README.md', 'modified, not staged\n')
        key = ('a modified tracked file', id(self))
        for attempt in (1, 2):
            with self.assertRaisesRegex(AssertionError, 'update-index --refresh failed'):
                gitfixture.TempRepo(prepare=dirty, prepare_key=key)
            self.assertEqual(len(built), attempt, 'the refused template was copied, not rebuilt')
        self.assertFalse(any(k[3] == key for k in gitfixture._TEMPLATES))
        self.assertEqual([b for b in built if os.path.exists(b)], [],
                         'a refused template was left behind')


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A child on real git (KIT_FAKE_GIT=0) that builds a TempRepo, copies it, commits a file and
# commits in a linked worktree: the fixture's own steps, each of which was a git process.
FIXTURE_STEPS = '\n'.join([
    'from tests import gitfixture',
    'gitfixture.TempRepo().cleanup()',
    'r = gitfixture.TempRepo()',
    'r.write("a.txt", "a\\n")',
    'r.commit("a", "a.txt")',
    'wt = r.worktree("w")',
    'r.write("b.txt", "b\\n", root=wt)',
    'r.commit("b", "b.txt", cwd=wt)',
    'r.cleanup()',
])


@windows_only('Windows only')
class InProcess(unittest.TestCase):
    """The fixture's own git (building a repository, commit, worktree) runs in this process on
    the fake git even on real git, so a @real_git class pays processes only for what it tests."""

    def starts(self, extra_env=None):
        """Processes started in a job by a child that runs FIXTURE_STEPS, the child (and the
        conhost a child with no console of its parent's gets) not counted."""
        from kitlib.proc import console, job
        env = dict(os.environ, PYTHONPATH=ROOT, KIT_FAKE_GIT='0')
        env.pop('KIT_FIXTURE_REAL_GIT', None)
        env.update(extra_env or {})
        j = job.Job()
        try:
            p = j.start([sys.executable, '-c', FIXTURE_STEPS], cwd=ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode('utf-8', 'replace'))
            return j.processes() - (1 if console.has_console() else 2)
        finally:
            j.end(kill=True)

    def test_a_fixture_on_real_git_starts_no_process(self):
        self.assertEqual(self.starts(), 0)

    def test_a_real_fixture_class_s_fixture_starts_real_git(self):
        self.assertGreater(self.starts({gitfixture.REAL_FIXTURE_ENV: '1'}), 0)


class RealFixtureMark(unittest.TestCase):

    def test_the_mark_holds_from_setupclass_to_the_last_class_cleanup(self):
        seen = []

        @gitfixture.real_fixture('checks the mark')
        class Marked(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                seen.append(gitfixture.in_process())

        with mock.patch.dict(os.environ):
            os.environ.pop(gitfixture.REAL_FIXTURE_ENV, None)
            self.assertTrue(gitfixture.in_process())
            Marked.setUpClass()
            self.assertEqual(seen, [False])
            self.assertFalse(gitfixture.in_process())
            Marked.doClassCleanups()
            self.assertTrue(gitfixture.in_process())
            self.assertNotIn(gitfixture.REAL_FIXTURE_ENV, os.environ)
        self.assertIn('%s.%s' % (Marked.__module__, Marked.__qualname__), gitfixture.REASONS)

    def test_a_template_built_in_process_is_not_copied_for_a_real_fixture(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(gitfixture.REAL_FIXTURE_ENV, None)
            fake_key = gitfixture._template_key('main', False, None)
            os.environ[gitfixture.REAL_FIXTURE_ENV] = '1'
            self.assertNotEqual(gitfixture._template_key('main', False, None), fake_key)


HOME_X =os.path.join(os.sep, 'nohome')


class PowerShellCacheEnv(unittest.TestCase):
    """A real powershell given a temp USERPROFILE must not fall back to the cwd for its module
    analysis cache, which would land as an untracked file in a worktree."""

    def under(self, path, home):
        return os.path.normcase(os.path.dirname(path)) == os.path.normcase(home)

    def test_fixture_env_with_a_userprofile_carries_the_cache_path_under_it(self):
        env = gitfixture.fixture_env({'USERPROFILE': HOME_X, 'HOME': HOME_X})
        self.assertTrue(self.under(env['PSModuleAnalysisCachePath'], HOME_X))

    def test_fixture_env_without_a_userprofile_adds_none(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('PSModuleAnalysisCachePath', None)
            self.assertNotIn('PSModuleAnalysisCachePath', gitfixture.fixture_env())

    def test_temp_home_sets_the_cache_path_under_the_home(self):
        home = temp_home(self)
        self.assertTrue(self.under(os.environ['PSModuleAnalysisCachePath'], home))


class SessionMarkEnv(unittest.TestCase):
    """A test run from a marked session (a coordinator's shell) must not hand its mark to the
    fixture's tools: tools/backlog.py reads its role from it (policy.cli_role). A test that wants a
    mark sets it through `extra`."""

    MARK = {sessionmark.ENV: 'coordinator', sessionmark.SESSION_NAME_ENV: 'kit-x'}

    def test_fixture_env_drops_the_callers_session_mark(self):
        with mock.patch.dict(os.environ, self.MARK):
            env = gitfixture.fixture_env()
        self.assertEqual([n for n in env if n.upper() in self.MARK], [])

    def test_a_mark_given_in_extra_stands(self):
        with mock.patch.dict(os.environ, self.MARK):
            env = gitfixture.fixture_env({sessionmark.ENV: 'pm'})
        # names only, never the environment itself: a failure must not print the caller's variables
        self.assertEqual(env.get(sessionmark.ENV), 'pm')
        self.assertEqual([n for n in env if n.upper() == sessionmark.SESSION_NAME_ENV], [])


if __name__ == '__main__':
    unittest.main()
