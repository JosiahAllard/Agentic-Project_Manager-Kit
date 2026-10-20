"""The PM data leak stage of githooks/pre-push: a data push to the public remote, a leak through a merge, cherry-pick or tag, and the reads git cannot answer, which refuse. The pushes to the private remote are in test_prepush_pmdata_private.py."""
import os
import subprocess
from unittest import mock

from kitlib.guard import prepush
from tests import gitfixture
from tests.realgit import real_git
from tests.guard import prepush_case as pc
from tests.guard.prepush_case import HOOKS, SECRET


@real_git('git push runs the real pre-push hook (core.hooksPath); rev-list --no-object-names')
class PmDataTest(pc.PmDataCase):
    """The leak stage (docs/guard.md "Pre-push", "PM data branch"): with kit-pm-data.json in the
    git common dir, the data branch, its history and its objects reach only the recorded private
    remote, and that remote gets nothing else. origin is the public remote. Pushes of the data branch to the public remote."""

    def test_pmdata_to_public_is_refused(self):
        for args in (('origin', 'pmdata'), ('origin', 'pmdata:refs/heads/other'),
                     ('--all', 'origin'), ('origin', 'main', 'pmdata')):
            err = self.refused(*args, why='pre-push: refused, ')
            self.assertIn('PM data', err)
            if args == ('origin', 'pmdata'):
                self.assertIn('pushes the PM data branch pmdata to origin, which is '
                              'not its private remote private', err)
        self.assertEqual(self.ls('origin', 'refs/heads/pmdata'), '')
        self.assertEqual(self.ls('origin', 'refs/heads/other'), '')
        self.assertEqual(self.remote_main(), self.repo.initial)        # main went nowhere either

    def test_a_branch_with_a_cherry_picked_data_commit_is_refused(self):
        self.new_branch('kit/x')
        self.repo.git('cherry-pick', self.data)
        # a new commit: the history check passes, so only the object check can refuse it
        self.assertFalse(prepush.refs.is_ancestor(self.root, self.repo.head(), self.repo.root)[0])
        err = self.refused('origin', 'kit/x', why='the PM data branch holds')
        self.assertIn('ledger.md', err)
        self.assertIn('identical to a PM data file', err)
        self.assertEqual(self.ls('origin', 'refs/heads/kit/x'), '')

    def test_a_branch_merging_pmdata_is_refused(self):
        self.new_branch('kit/m')
        self.repo.git('merge', '-q', '--allow-unrelated-histories', '--no-edit', 'pmdata')
        self.refused('origin', 'kit/m', why='holds the PM data root commit %s' % self.root[:12])
        self.assertEqual(self.ls('origin', 'refs/heads/kit/m'), '')

    def test_a_tag_on_a_data_commit_is_refused(self):
        self.repo.git('tag', 'light', self.data)
        self.repo.git('tag', '-a', 'annotated', '-m', 'x', self.data)
        self.repo.git('tag', 'at-root', self.root)
        for tag in ('light', 'annotated', 'at-root'):
            self.refused('origin', 'refs/tags/%s' % tag, why='holds the PM data root commit')
            self.assertEqual(self.ls('origin', 'refs/tags/%s' % tag), '')

    def test_main_to_public_passes(self):
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        # an empty file is also in the data branch: the empty blob is no PM data
        self.repo.write('empty.txt', '')
        head = self.repo.commit('an empty file', 'empty.txt')
        self.passed(head)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), head)
        self.new_branch('kit/ok')
        self.repo.write('b.txt', 'b\n')
        self.repo.commit('b', 'b.txt')
        code, err = self.push('origin', 'kit/ok')
        self.assertEqual(code, 0, err)


@real_git('git push runs the real pre-push hook (core.hooksPath); rev-list --no-object-names')
class PmDataGitTest(pc.PmDataCase):
    """The leak stage (docs/guard.md "Pre-push", "PM data branch"): with kit-pm-data.json in the
    git common dir, the data branch, its history and its objects reach only the recorded private
    remote, and that remote gets nothing else. origin is the public remote. Reads git cannot answer refuse."""

    def test_an_ancestry_git_cannot_tell_refuses(self):
        public = 'refs/heads/kit/x %s refs/heads/kit/x %s' % (self.with_kit, '0' * 40)
        private = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        with mock.patch.object(prepush.refs, 'is_ancestor', return_value=(None, 'boom')):
            why = prepush.leak_refusal(self.repo.root, [public], 'origin', self.repo.remote)
            self.assertIn('cannot be checked for (boom)', why)
            why = prepush.leak_refusal(self.repo.root, [private], 'private', self.private)
            self.assertIn('cannot be checked for (boom)', why)
        self.assertIsNone(prepush.leak_refusal(self.repo.root, [public], 'origin', self.repo.remote))
        self.assertIsNone(prepush.leak_refusal(self.repo.root, [private], 'private', self.private))

    def test_a_data_ref_git_cannot_read_refuses(self):
        # the cherry-picked copy shares only the data commit's blob: without the data branch's
        # objects the push would pass, so a ref git fails to read is a refusal, never "no ref"
        self.new_branch('kit/x')
        self.repo.git('cherry-pick', self.data)
        line = 'refs/heads/kit/x %s refs/heads/kit/x %s' % (self.repo.head(), '0' * 40)
        real = prepush.cmd.call

        def call(args, *rest, **kw):
            if args[:1] == ['rev-parse'] and any('refs/heads/pmdata' in a for a in args):
                return prepush.cmd.Result(None, b'', 'git rev-parse timed out after 30 seconds')
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=call):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn("cannot list the PM data branch's objects", why or '')
        self.assertIn('cannot read refs/heads/pmdata: git rev-parse timed out', why)

    def test_the_data_worktree_finds_the_record(self):
        # the hook's cwd is the top of the work tree pushed from: the record is in the common dir
        line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        self.assertIn('pushes the PM data branch',
                      prepush.leak_refusal(self.data_wt, [line], 'origin', self.repo.remote))
        self.assertIsNone(prepush.leak_refusal(self.data_wt, [line], 'private', self.private))

    def test_a_push_by_name_to_a_url_it_does_not_have_is_public(self):
        line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        other = os.path.join(self.repo.base, 'unrelated.git')
        self.assertIn('pushes the PM data branch', prepush.leak_refusal(self.repo.root, [line], 'private', other))
        self.assertIn('pushes the PM data branch', prepush.leak_refusal(self.repo.root, [line], 'private', None))

    def test_a_command_line_pushurl_cannot_make_public_private(self):
        # `git -c remote.private.pushurl=<public>`: git hands the hook the public URL and the
        # -c in GIT_CONFIG_PARAMETERS; the hook reads the remotes without it. The public place
        # is no configured remote, so only the private remote's own push URLs can tell.
        public = os.path.join(self.repo.base, 'public.git').replace('\\', '/')
        self.repo.git('init', '-q', '--bare', public, cwd=self.repo.base)
        r = subprocess.run(('git', '-c', 'core.hooksPath=%s' % HOOKS.replace('\\', '/'),
                            '-c', 'remote.private.pushurl=%s' % public, 'push', 'private', 'pmdata'),
                           cwd=self.repo.root, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=gitfixture.fixture_env())
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('pushes the PM data branch pmdata to private, which is not its private remote', r.stderr)
        self.assertEqual(self.repo.git('ls-remote', public), '')
        with mock.patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'remote.private.pushurl',
                                          'GIT_CONFIG_VALUE_0': public}):
            line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
            self.assertIn('pushes the PM data branch', prepush.leak_refusal(self.repo.root, [line], 'private', public))
            self.assertIsNone(prepush.leak_refusal(self.repo.root, [line], 'private', self.private))

    def test_a_push_url_rewritten_by_git_is_judged_as_git_rewrites_it(self):
        # pushInsteadOf turns the private remote's push URL into the public one: git pushes there
        public = self.repo.remote.replace('\\', '/')
        self.repo.git('config', 'url.%s.pushInsteadOf' % public, self.private.replace('\\', '/'))
        self.repo.git('config', 'remote.private.url', self.private.replace('\\', '/'))
        self.refused('private', 'pmdata', why='not its private remote')
        self.assertEqual(self.ls('origin', 'refs/heads/pmdata'), '')

    def test_a_nested_remote_name_does_not_hide_the_objects(self):
        # --remotes=origin also matches refs/remotes/origin/sub/*, the tracking refs of a remote
        # named origin/sub. One of them on a commit of the pushed branch that copies a data file
        # would mark that commit's objects as already on origin, which it never got.
        nested = os.path.join(self.repo.base, 'nested.git')
        self.repo.git('init', '-q', '--bare', nested, cwd=self.repo.base)
        # `git remote add` refuses the name (a subset of origin); config written directly holds it
        self.repo.git('config', 'remote.origin/sub.url', nested)
        self.new_branch('kit/x')
        self.repo.write('notes.md', SECRET)                     # a data file copied by hand
        copy = self.repo.commit('copy', 'notes.md')
        self.repo.git('update-ref', 'refs/remotes/origin/sub/x', copy)
        self.repo.write('b.txt', 'b\n')
        self.repo.commit('b', 'b.txt')
        self.refused('origin', 'kit/x', why='the PM data branch holds')
        self.assertEqual(self.ls('origin', 'refs/heads/kit/x'), '')

    def test_objects_git_cannot_list_refuse(self):
        line = 'refs/heads/kit/x %s refs/heads/kit/x %s' % (self.with_kit, '0' * 40)
        real = prepush.cmd.run_text

        def run_text(args, *rest, **kw):
            if args[:1] == ['rev-list'] and '--no-object-names' in args:
                return None, 'boom'
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'run_text', side_effect=run_text):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn('cannot list the objects refs/heads/kit/x', why or '')
        self.assertIn('boom', why)

    def test_a_git_directory_git_cannot_name_refuses(self):
        line = 'refs/heads/main %s refs/heads/main %s' % (self.with_kit, '0' * 40)
        with mock.patch.object(prepush.gitrepo, 'common_dir', return_value=(None, 'git timed out')):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn('cannot find the git directory to look for kit-pm-data.json (git timed out)', why or '')

    def test_a_remote_url_git_cannot_read_refuses(self):
        # another remote's get-url failing for any reason but "No such remote" is not "no URLs"
        line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        real = prepush.cmd.call

        def call(args, *rest, **kw):
            if args[:2] == ['remote', 'get-url'] and args[-1] == 'origin':
                return prepush.cmd.Result(128, b'', 'fatal: bad config line 7')
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=call):
            why = prepush.leak_refusal(self.repo.root, [line], 'private', self.private)
        self.assertIn('cannot read the remotes to tell whether private is the private remote', why or '')
        self.assertIn('git remote get-url origin: fatal: bad config line 7', why)

        def gone(args, *rest, **kw):
            if args[:2] == ['remote', 'get-url'] and args[-1] == 'origin':
                return prepush.cmd.Result(2, b'', "error: No such remote 'origin'")
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=gone):
            self.assertIsNone(prepush.leak_refusal(self.repo.root, [line], 'private', self.private))

    def test_remotes_git_cannot_read_refuse(self):
        line = 'refs/heads/main %s refs/heads/main %s' % (self.with_kit, '0' * 40)
        real = prepush.cmd.call

        def call(args, *rest, **kw):
            if args[:1] == ['config']:
                return prepush.cmd.Result(128, b'', 'fatal: bad config line 1')
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=call):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn('cannot read the remotes to tell whether origin is the private remote', why or '')
        self.assertIn('bad config line 1', why)


if __name__ == '__main__':
    unittest.main()
