"""kitlib.worktree.scratch (`tools/worktree_remove.py <path> --scratch`): a detached scratch
worktree under an agent temp root, removed only when its commit is reachable from a ref and every
uncommitted file is already in git at the same path (docs/worktree-remove.md "Tools", `--scratch`).

Every worktree here is one the test made: its repository and its temp root are fixture folders.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib.worktree import remove, scratch
from tests import gitfixture
from tests.realgit import real_git
from tests.platforms import windows_only

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'worktree_remove.py')
WINDOWS = os.name == 'nt'


class ScratchCase(unittest.TestCase):
    """A throwaway repo with a bare origin and a branch `kit/topic` that adds tests/new_test.py and
    changes README.md; the temp root is a sibling of the repository, never above it."""

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.repo = os.path.realpath(self.fx.root)
        self.root = os.path.join(os.path.realpath(self.fx.base), 'agenttemp')
        os.makedirs(self.root)
        self.base = self.fx.head()
        topic = os.path.realpath(self.fx.worktree('topic'))
        self.fx.write('tests/new_test.py', 'def test_new():\n    assert False\n', root=topic)
        self.fx.write('README.md', 'hello, topic\n', root=topic)
        self.topic_tip = self.fx.commit('topic', 'tests/new_test.py', 'README.md', cwd=topic)
        self.git('worktree', 'remove', topic)

    def git(self, *args, cwd=None, check=True):
        return self.fx.git(*args, cwd=cwd or self.repo, check=check)

    def environ(self, **extra):
        env = {'AGENT_TEMP': self.root}
        env.update(extra)
        return env

    def scratch_tree(self, name='red', commit=None, root=None):
        path = os.path.join(root or self.root, name)
        self.git('worktree', 'add', '-q', '--detach', path, commit or self.base)
        return path

    def run_scratch(self, path, environ=None):
        got = scratch.remove_scratch(self.repo, path, environ=environ or self.environ())
        return got.code, '\n'.join(got.lines)

    def registered(self):
        return [os.path.normcase(os.path.realpath(line[len('worktree '):]))
                for line in self.git('worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')]

    def assertRemoved(self, path, rc, out):
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s' % path, out)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(os.path.normcase(path), self.registered())

    def assertKept(self, path, rc, out, words):
        self.assertEqual(rc, 1, out)
        self.assertIn(words, out)
        self.assertTrue(os.path.isdir(path), out)
        self.assertIn(os.path.normcase(path), self.registered(), out)


class Removed(ScratchCase):

    def test_a_clean_detached_tree_at_a_branch_commit_is_removed(self):
        path = self.scratch_tree(commit=self.topic_tip)
        refs = self.git('for-each-ref')
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)
        self.assertIn('in refs/heads/kit/topic', out)
        self.assertEqual(self.git('for-each-ref'), refs)

    def test_a_copied_untracked_test_file_found_on_a_branch_is_cleared(self):
        path = self.scratch_tree()
        self.fx.write('tests/new_test.py', 'def test_new():\n    assert False\n', root=path)
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)
        self.assertIn('restored  1 file(s)', out)

    def test_a_modified_tracked_file_found_on_a_branch_is_restored(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        self.git('add', 'README.md', cwd=path)
        self.fx.write('tests/new_test.py', 'def test_new():\n    assert False\n', root=path)
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)
        self.assertIn('restored  2 file(s)', out)

    def test_a_deleted_tracked_file_is_restored(self):
        path = self.scratch_tree()
        os.remove(os.path.join(path, 'README.md'))
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)

    def test_build_output_only_is_removed(self):
        self.fx.write('.gitignore', '__pycache__/\n')
        self.fx.write('.kit.json', '{"worktree_remove": {"build_output": ["__pycache__"]}}\n')
        tip = self.fx.commit('ignore build output', '.gitignore', '.kit.json')
        path = self.scratch_tree(commit=tip)
        self.fx.write('__pycache__/x.pyc', 'bytes', root=path)
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)

    def test_the_cli_removes_with_agent_temp_from_the_environment(self):
        path = self.scratch_tree()
        done = subprocess.run([sys.executable, TOOL, '-C', self.repo, path, '--scratch'],
                              cwd=self.repo, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=300,
                              env=gitfixture.fixture_env({'AGENT_TEMP': self.root}))
        out = (done.stdout or '') + (done.stderr or '')
        self.assertRemoved(path, done.returncode, out)


class Refused(ScratchCase):

    def test_a_changed_copy_is_refused_and_kept(self):
        path = self.scratch_tree()
        self.fx.write('tests/new_test.py', 'def test_new():\n    assert True\n', root=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'not in git at that path')
        self.assertTrue(os.path.isfile(os.path.join(path, 'tests', 'new_test.py')))

    def test_a_staged_blob_that_differs_from_the_file_is_refused(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'staged only\n', root=path)
        self.git('add', 'README.md', cwd=path)
        self.fx.write('README.md', 'hello, topic\n', root=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'not in git at that path')

    def test_a_commit_made_in_the_tree_is_refused(self):
        path = self.scratch_tree()
        self.fx.write('mine.txt', 'mine\n', root=path)
        self.fx.commit('mine', 'mine.txt', cwd=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'no ref reaches')

    def test_a_tree_on_a_branch_is_refused(self):
        path = os.path.join(self.root, 'onbranch')
        self.git('worktree', 'add', '-q', path, '-b', 'kit/onbranch')
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'is on branch kit/onbranch')

    def test_a_locked_tree_is_refused(self):
        path = self.scratch_tree()
        self.git('worktree', 'lock', path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'is locked')

    def test_a_workstream_tree_is_refused(self):
        path = os.path.realpath(self.fx.worktree('ws'))
        self.git('checkout', '-q', '--detach', cwd=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'workstream worktree')

    def test_a_path_outside_every_root_is_refused(self):
        other = os.path.join(os.path.realpath(self.fx.base), 'elsewhere')
        os.makedirs(other)
        path = self.scratch_tree(root=other)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'is not under a scratch root')

    def test_a_root_holding_the_main_checkout_is_not_a_root(self):
        path = self.scratch_tree(root=os.path.realpath(self.fx.base))
        rc, out = self.run_scratch(path, environ={'AGENT_TEMP': os.path.realpath(self.fx.base)})
        self.assertKept(path, rc, out, 'no scratch root')

    def test_the_home_folder_and_a_drive_root_are_not_roots(self):
        home = os.path.realpath(self.fx.base)
        drive = os.path.splitdrive(home)[0] + os.sep
        self.assertEqual(scratch.roots({'AGENT_TEMP': home, 'TEMP': drive, 'USERPROFILE': home},
                                       self.repo)[0], [])
        under = os.path.join(home, 'agenttemp')
        self.assertEqual(scratch.roots({'AGENT_TEMP': under, 'USERPROFILE': home}, self.repo)[0],
                         [under])

    def test_an_unregistered_folder_is_refused(self):
        path = os.path.join(self.root, 'stump')
        os.makedirs(path)
        rc, out = self.run_scratch(path)
        self.assertEqual(rc, 1, out)
        self.assertIn('not a registered worktree', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_missing_folder_is_nothing(self):
        rc, out = self.run_scratch(os.path.join(self.root, 'gone'))
        self.assertEqual(rc, 0, out)
        self.assertIn('nothing   ', out)

    def test_a_tree_holding_another_worktree_is_refused(self):
        path = self.scratch_tree()
        inner = os.path.join(path, 'inner')
        self.git('worktree', 'add', '-q', '--detach', inner, self.base)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'holds the registered worktree')

    @windows_only('junctions are Windows')
    def test_a_junction_on_the_way_is_refused(self):
        real_parent = os.path.join(self.root, 'real')
        os.makedirs(real_parent)
        path = self.scratch_tree(root=real_parent)
        link = os.path.join(self.root, 'via')
        subprocess.run(['cmd', '/c', 'mklink', '/J', link, real_parent], check=True,
                       capture_output=True)
        rc, out = self.run_scratch(os.path.join(link, 'red'))
        self.assertEqual(rc, 1, out)
        self.assertIn('link or junction', out)
        self.assertTrue(os.path.isdir(path))

    def test_an_ignored_keeper_is_refused(self):
        path = self.scratch_tree()
        self.fx.write('.git/info/exclude', '*.db\n', root=self.repo)
        self.fx.write('dev.db', 'data', root=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'ignored files that are not build output')

    def test_a_file_rewritten_after_its_check_is_kept(self):
        path = self.scratch_tree()
        target = os.path.join(path, 'tests', 'new_test.py')
        self.fx.write('tests/new_test.py', 'def test_new():\n    assert False\n', root=path)
        real = scratch.preserved

        def then_rewrite(*a, **kw):
            got = real(*a, **kw)
            with open(target, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write('rewritten\n')
            return got

        with mock.patch.object(scratch, 'preserved', then_rewrite):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'changed after it was checked')
        with open(target, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'rewritten\n')

    def test_the_removal_is_only_through_git_remove(self):
        path = self.scratch_tree()
        with mock.patch.object(remove, 'git_remove', return_value=False) as gr:
            rc, out = self.run_scratch(path)
        self.assertEqual(rc, 1, out)
        gr.assert_called_once()
        self.assertTrue(os.path.isdir(path))

    def test_an_unreadable_status_refuses(self):
        path = self.scratch_tree()
        with mock.patch.object(remove, 'dirty', return_value=(None, None, None, 'boom')):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'its status could not be read (boom)')

    def test_an_unreadable_hash_refuses(self):
        path = self.scratch_tree()
        self.fx.write('tests/new_test.py', 'def test_new():\n    assert False\n', root=path)
        with mock.patch.object(scratch, 'hashes', return_value=(None, 'boom')):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'boom')


class HashBatches(ScratchCase):
    def test_many_paths_are_hashed_in_several_git_calls(self):
        path = self.scratch_tree()
        rels = ['f%02d.txt' % i for i in range(12)]
        for i, rel in enumerate(rels):
            self.fx.write(rel, 'content %d\n' % i, root=path)
        calls = []
        real = remove.git

        def spy(tree, *args):
            calls.append(args)
            return real(tree, *args)

        with mock.patch.object(scratch, 'BATCH_CHARS', 40), mock.patch.object(remove, 'git', spy):
            got, why = scratch.hashes(path, rels)
        self.assertIsNone(why)
        self.assertGreater(len(calls), 1)
        self.assertEqual(sum(len(c) - 2 for c in calls), len(rels))
        want = {rel: self.git('hash-object', '--', os.path.join(path, rel)) for rel in rels}
        self.assertEqual(got, want)

    def test_a_failing_batch_fails_the_whole_hash(self):
        with mock.patch.object(scratch, 'BATCH_PATHS', 1), \
                mock.patch.object(remove, 'git', return_value=(1, '', 'boom')):
            got, why = scratch.hashes(self.repo, ['a', 'b'])
        self.assertIsNone(got)
        self.assertIn('boom', why)


NEW_TEST = 'def test_new():\n    assert False\n'


class SamePath(ScratchCase):
    """The core rule: content counts only at its own path, and only in refs/heads or refs/remotes."""

    def test_the_same_content_at_another_path_is_refused(self):
        path = self.scratch_tree()
        self.fx.write('tests/other_test.py', NEW_TEST, root=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'tests/other_test.py: it is not in git at that path')
        self.assertTrue(os.path.isfile(os.path.join(path, 'tests', 'other_test.py')))

    def test_content_held_only_by_a_tag_is_refused(self):
        wt = os.path.realpath(self.fx.worktree('tagged'))
        self.fx.write('tests/tag_only.py', 'tagged\n', root=wt)
        self.git('tag', 'only-tag', self.fx.commit('tag only', 'tests/tag_only.py', cwd=wt))
        self.git('worktree', 'remove', wt)
        self.git('branch', '-D', 'kit/tagged')
        path = self.scratch_tree()
        self.fx.write('tests/tag_only.py', 'tagged\n', root=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'tests/tag_only.py: it is not in git at that path')

    def test_an_added_file_then_deleted_from_disk_is_restored(self):
        path = self.scratch_tree()
        self.fx.write('tests/new_test.py', NEW_TEST, root=path)
        self.git('add', 'tests/new_test.py', cwd=path)
        os.remove(os.path.join(path, 'tests', 'new_test.py'))
        self.assertIn('AD tests/new_test.py', self.git('status', '--porcelain', cwd=path))
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)

    def test_a_staged_change_then_deleted_from_disk_is_restored(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        self.git('add', 'README.md', cwd=path)
        os.remove(os.path.join(path, 'README.md'))
        self.assertIn('MD README.md', self.git('status', '--porcelain', cwd=path))
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)

    def test_a_staged_new_file_is_restored(self):
        path = self.scratch_tree()
        self.fx.write('tests/new_test.py', NEW_TEST, root=path)
        self.git('add', 'tests/new_test.py', cwd=path)
        self.assertIn('A  tests/new_test.py', self.git('status', '--porcelain', cwd=path))
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)

    def test_a_nested_repository_is_refused(self):
        path = self.scratch_tree()
        self.git('init', '-q', os.path.join(path, 'nested'))
        self.fx.write('nested/x.txt', 'x\n', root=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'nested/ is a folder git does not list into')
        self.assertTrue(os.path.isfile(os.path.join(path, 'nested', 'x.txt')))


class Failures(ScratchCase):
    """Every read that fails, and every git step that fails, keeps the tree."""

    def wrap_git(self, when, then):
        real = remove.git

        def wrapped(cwd, *args, **kw):
            hit = when(args)
            if hit == 'fail':
                return 128, '', 'boom'
            got = real(cwd, *args, **kw)
            if hit:
                then()
            return got
        return mock.patch.object(remove, 'git', wrapped)

    def test_a_failed_checkout_keeps_the_tree(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        self.fx.write('tests/new_test.py', NEW_TEST, root=path)
        with self.wrap_git(lambda a: 'fail' if a[:1] == ('checkout',) else None, None):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'git worktree remove was NOT run')
        self.assertTrue(os.path.isfile(os.path.join(path, 'tests', 'new_test.py')))

    def test_a_failed_reset_keeps_the_tree(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        self.git('add', 'README.md', cwd=path)
        with self.wrap_git(lambda a: 'fail' if a[:1] == ('reset',) else None, None):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'git worktree remove was NOT run')

    def test_a_merge_base_error_refuses(self):
        self.fx.write('one.txt', '1\n')
        first = self.fx.commit('one', 'one.txt')
        self.fx.write('two.txt', '2\n')
        self.fx.commit('two', 'two.txt')
        path = self.scratch_tree(commit=first)
        with self.wrap_git(lambda a: 'fail' if a[:1] == ('merge-base',) else None, None):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'is unknown')

    def test_an_unreadable_index_refuses(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        self.git('add', 'README.md', cwd=path)
        with mock.patch.object(scratch, 'staged', return_value=(None, 'boom')):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'the staged blobs could not be read (boom)')

    def test_an_unsearchable_ref_list_refuses(self):
        path = self.scratch_tree()
        self.fx.write('tests/new_test.py', NEW_TEST, root=path)
        with mock.patch.object(scratch, 'found', return_value=(None, 'boom')):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'the refs could not be searched (boom)')

    def test_a_commit_made_after_the_restore_is_refused(self):
        path = self.scratch_tree()
        real = remove.clear_disposable

        def commit_first(ctx, p):
            self.fx.write('late.txt', 'late\n', root=path)
            self.fx.commit('late', 'late.txt', cwd=path)
            return real(ctx, p)

        with mock.patch.object(remove, 'clear_disposable', commit_first):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'HEAD moved')
        self.assertTrue(os.path.isfile(os.path.join(path, 'late.txt')))

    def test_a_file_rewritten_after_the_checkout_is_kept(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        target = self.fx.write('tests/new_test.py', NEW_TEST, root=path)

        def rewrite():
            with open(target, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write('rewritten\n')

        with self.wrap_git(lambda a: a[:1] == ('checkout',), rewrite):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'changed after it was checked')
        with open(target, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'rewritten\n')

    @windows_only('junctions are Windows')
    def test_a_folder_made_a_junction_after_the_check_is_kept(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        self.fx.write('tests/new_test.py', NEW_TEST, root=path)
        outside = os.path.join(self.root, 'outside')

        def swap():
            os.rename(os.path.join(path, 'tests'), outside)
            subprocess.run(['cmd', '/c', 'mklink', '/J', os.path.join(path, 'tests'), outside],
                           check=True, capture_output=True)

        with self.wrap_git(lambda a: a[:1] == ('checkout',), swap):
            rc, out = self.run_scratch(path)
        self.assertEqual(rc, 1, out)
        self.assertIn('is a link', out)
        self.assertTrue(os.path.isfile(os.path.join(outside, 'new_test.py')), out)
        os.rmdir(os.path.join(path, 'tests'))

    def test_the_checkout_reads_its_paths_literally(self):
        path = self.scratch_tree()
        self.fx.write('README.md', 'hello, topic\n', root=path)
        seen = []
        real = remove.git

        def record(cwd, *args, **kw):
            if args[:1] == ('checkout',):
                seen.append((kw.get('env') or {}).get('GIT_LITERAL_PATHSPECS'))
            return real(cwd, *args, **kw)

        with mock.patch.object(remove, 'git', record):
            rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)
        self.assertEqual(seen, ['1'])


class Refs(ScratchCase):

    def test_a_tag_on_a_blob_does_not_stop_the_search(self):
        wt = os.path.realpath(self.fx.worktree('later'))
        self.fx.write('a.txt', 'a\n', root=wt)
        first = self.fx.commit('a', 'a.txt', cwd=wt)
        self.fx.write('b.txt', 'b\n', root=wt)
        self.git('tag', 'z-reach', self.fx.commit('b', 'b.txt', cwd=wt))
        self.git('worktree', 'remove', wt)
        self.git('branch', '-D', 'kit/later')
        blob = self.git('hash-object', '-w', '--', os.path.join(self.repo, 'README.md'))
        self.git('tag', 'a-blob', blob)
        path = self.scratch_tree(commit=first)
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)
        self.assertIn('in refs/tags/z-reach', out)

    @windows_only('junctions are Windows')
    def test_a_tree_in_the_worktree_folder_through_its_junction_is_refused(self):
        ctx, refused = remove.open_ctx(self.repo)
        self.assertIsNone(refused)
        home = remove.home_of(ctx)
        if os.path.isdir(home):
            os.rmdir(home)
        os.makedirs(os.path.dirname(home), exist_ok=True)
        wts = os.path.join(self.root, 'wts')
        os.makedirs(wts)
        subprocess.run(['cmd', '/c', 'mklink', '/J', home, wts], check=True, capture_output=True)
        self.addCleanup(lambda: os.path.isdir(home) and os.rmdir(home))
        path = self.scratch_tree(root=wts)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'worktree folder')


class RefFormat(ScratchCase):
    """The reflog is read from logs/HEAD, which only the files ref format writes."""

    def answer(self, rc, out):
        real = remove.git

        def wrapped(cwd, *args, **kw):
            if args == ('rev-parse', '--show-ref-format'):
                return rc, out, 'boom' if rc else ''
            return real(cwd, *args, **kw)
        return mock.patch.object(remove, 'git', wrapped)

    def test_a_reftable_repository_is_refused(self):
        path = self.scratch_tree()
        with self.answer(0, 'reftable'):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'its refs are stored as reftable')

    def test_an_unreadable_ref_format_is_refused(self):
        path = self.scratch_tree()
        with self.answer(128, ''):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'git rev-parse --show-ref-format exited 128')


class PerWorktreeRefs(ScratchCase):
    """The per-worktree refs (scratch.PER_WORKTREE) go with the tree: an unread list refuses."""

    def answer(self, rc, out):
        real = remove.git

        def wrapped(cwd, *args, **kw):
            if args[:1] == ('for-each-ref',) and 'refs/worktree' in args:
                return rc, out, 'boom' if rc else ''
            return real(cwd, *args, **kw)
        return mock.patch.object(remove, 'git', wrapped)

    def test_an_unreadable_per_worktree_ref_list_is_refused(self):
        path = self.scratch_tree()
        with self.answer(128, ''):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'git for-each-ref exited 128')

    def test_a_per_worktree_ref_that_is_not_a_commit_is_refused(self):
        path = self.scratch_tree()
        with self.answer(0, '%s tag refs/worktree/odd' % ('a' * 40)):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'its per-worktree ref refs/worktree/odd is not a commit')


@real_git('git reflog and annotated tags on a blob: the fake writes no reflog')
class Reflog(ScratchCase):

    def test_an_annotated_tag_on_a_blob_does_not_stop_the_search(self):
        wt = os.path.realpath(self.fx.worktree('later'))
        self.fx.write('a.txt', 'a\n', root=wt)
        first = self.fx.commit('a', 'a.txt', cwd=wt)
        self.fx.write('b.txt', 'b\n', root=wt)
        self.git('tag', 'z-reach', self.fx.commit('b', 'b.txt', cwd=wt))
        self.git('worktree', 'remove', wt)
        self.git('branch', '-D', 'kit/later')
        blob = self.git('hash-object', '-w', '--', os.path.join(self.repo, 'README.md'))
        self.git('tag', '-a', '-m', 'on a blob', 'a-blob', blob)
        self.git('tag', '-a', '-m', 'on a tree', 'b-tree', self.base + '^{tree}')
        path = self.scratch_tree(commit=first)
        real, failed = remove.git, {}

        def record(cwd, *args, **kw):
            got = real(cwd, *args, **kw)
            if args[:2] == ('merge-base', '--is-ancestor'):
                failed[args[-1]] = got[0]
            return got

        with mock.patch.object(remove, 'git', record):
            rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)
        self.assertIn('in refs/tags/z-reach', out)
        self.assertNotIn(failed.get('refs/tags/a-blob'), (None, 0, 1), failed)

    def test_a_reflog_changed_while_head_stayed_is_refused(self):
        path = self.scratch_tree()
        real = remove.clear_disposable

        def commit_and_return(ctx, p):
            self.fx.write('late.txt', 'late\n', root=path)
            self.fx.commit('late', 'late.txt', cwd=path)
            self.git('checkout', '-q', '--detach', self.base, cwd=path)
            return real(ctx, p)

        with mock.patch.object(remove, 'clear_disposable', commit_and_return):
            rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'its HEAD reflog or a per-worktree ref changed after it was checked')

    def test_a_commit_left_behind_in_the_reflog_is_refused(self):
        path = self.scratch_tree()
        self.fx.write('mine.txt', 'mine\n', root=path)
        self.fx.commit('mine', 'mine.txt', cwd=path)
        self.git('checkout', '-q', '--detach', self.base, cwd=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'reflog')

    def test_a_commit_only_a_per_worktree_ref_keeps_is_refused(self):
        path = self.scratch_tree()
        kept = self.git('commit-tree', self.base + '^{tree}', '-p', self.base, '-m', 'kept',
                        cwd=path).strip()
        self.git('update-ref', 'refs/bisect/bad', kept, cwd=path)
        rc, out = self.run_scratch(path)
        self.assertKept(path, rc, out, 'which no ref reaches')
        self.assertIn(kept[:10], out)

    def test_a_clean_tree_with_a_reachable_reflog_is_removed(self):
        path = self.scratch_tree(commit=self.topic_tip)
        self.git('checkout', '-q', '--detach', self.base, cwd=path)
        rc, out = self.run_scratch(path)
        self.assertRemoved(path, rc, out)


class Cli(ScratchCase):

    def cli(self, *args):
        done = subprocess.run([sys.executable, TOOL] + list(args), cwd=self.repo,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env({'AGENT_TEMP': self.root}), timeout=300)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

    def test_scratch_with_another_verb_is_usage(self):
        for args in (['--scratch'], ['p', '--scratch', '--keep-branch'],
                     ['p', '--scratch', '--abandon', 'w'], ['p', '--scratch', '--branch'],
                     ['--check', '--scratch'], ['--sweep-merged', '--scratch'],
                     ['p', 'q', '--scratch'], ['p', '--scratch', '--scratch']):
            rc, out = self.cli(*args)
            self.assertEqual(rc, 2, (args, out))
            self.assertIn('USAGE: ', out)


if __name__ == '__main__':
    unittest.main()
