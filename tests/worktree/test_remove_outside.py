"""kitlib.worktree.remove on a registered worktree OUTSIDE the worktree folders (an agent's red-run
tree in a scratchpad): named by its path, a DETACHED one goes through the plain verb's or
`--abandon`'s checks; a slug that only matches such a tree is refused with the path-form command
(docs/worktree-remove.md "Tools").

Every worktree here is one the test made, in fixture folders beside the repository.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib.config import load
from kitlib.worktree import remove
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'worktree_remove.py')
WINDOWS = os.name == 'nt'


class OutsideCase(unittest.TestCase):
    """A throwaway repo with a bare origin, a branch `kit/topic` one commit past main, and an
    `outside` folder that is a sibling of the repository."""

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True)
        self.addCleanup(self.fx.cleanup)
        self.repo = os.path.realpath(self.fx.root)
        self.outside = os.path.join(os.path.realpath(self.fx.base), 'outside')
        os.makedirs(self.outside)
        self.base = self.fx.head()
        topic = os.path.realpath(self.fx.worktree('topic'))
        self.fx.write('topic.txt', 'topic\n', root=topic)
        self.topic_tip = self.fx.commit('topic', 'topic.txt', cwd=topic)
        self.git('worktree', 'remove', topic)

    def git(self, *args, cwd=None, check=True):
        return self.fx.git(*args, cwd=cwd or self.repo, check=check)

    def tree(self, name='red', commit=None, root=None):
        path = os.path.join(root or self.outside, name)
        self.git('worktree', 'add', '-q', '--detach', path, commit or self.base)
        return path

    def run_remove(self, argument):
        got = remove.remove(self.repo, argument, cwd=self.repo)
        return got.code, '\n'.join(got.lines)

    def run_abandon(self, argument, why='red evidence, done'):
        got = remove.abandon(self.repo, argument, why, cwd=self.repo)
        return got.code, '\n'.join(got.lines)

    def registered(self):
        return [os.path.normcase(os.path.realpath(line[len('worktree '):]))
                for line in self.git('worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')]

    def tag(self, name):
        return self.git('rev-parse', '-q', '--verify', 'refs/tags/%s^{commit}' % name,
                        check=False).strip()

    def assertRemoved(self, path, rc, out):
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s' % path, out)
        self.assertFalse(os.path.exists(path), out)
        self.assertNotIn(os.path.normcase(path), self.registered(), out)

    def assertKept(self, path, rc, out, words):
        self.assertEqual(rc, 1, out)
        self.assertIn(words, out)
        self.assertTrue(os.path.isdir(path), out)
        self.assertIn(os.path.normcase(path), self.registered(), out)


class Accepted(OutsideCase):

    def test_a_landed_detached_tree_named_by_path_is_removed(self):
        path = self.tree()
        rc, out = self.run_remove(path)
        self.assertRemoved(path, rc, out)
        self.assertIn('branch    none: it was a detached HEAD', out)

    def test_an_unlanded_detached_tree_is_abandoned_under_a_tag(self):
        path = self.tree(commit=self.topic_tip)
        rc, out = self.run_abandon(path)
        self.assertRemoved(path, rc, out)
        self.assertEqual(self.tag('archive/red'), self.topic_tip)

    def test_a_commit_no_ref_reaches_is_kept_by_the_abandon_tag(self):
        path = self.tree()
        self.fx.write('mine.txt', 'mine\n', root=path)
        mine = self.fx.commit('mine', 'mine.txt', cwd=path)
        rc, out = self.run_abandon(path)
        self.assertRemoved(path, rc, out)
        self.assertEqual(self.tag('archive/red'), mine)

    def test_the_cli_abandons_by_path(self):
        path = self.tree(commit=self.topic_tip)
        done = subprocess.run([sys.executable, TOOL, '-C', self.repo, path, '--abandon', 'red done'],
                              cwd=self.repo, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=300, env=gitfixture.fixture_env())
        out = (done.stdout or '') + (done.stderr or '')
        self.assertRemoved(path, done.returncode, out)


class Refused(OutsideCase):

    def test_unlanded_work_with_the_plain_verb_names_the_path_not_the_slug(self):
        path = self.tree(commit=self.topic_tip)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'holds UNLANDED work')
        self.assertIn('"%s" --abandon "<why>"' % path, out)
        self.assertNotIn(' red --abandon', out)

    def test_an_untracked_file_is_refused(self):
        path = self.tree()
        self.fx.write('work.txt', 'work\n', root=path)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'uncommitted or untracked files')
        self.assertIn('"%s"' % path, out)

    def test_a_modified_tracked_file_is_refused_by_abandon(self):
        path = self.tree(commit=self.topic_tip)
        self.fx.write('topic.txt', 'changed\n', root=path)
        rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'uncommitted or untracked files')
        self.assertEqual(self.tag('archive/red'), '')

    def test_an_ignored_file_that_is_not_build_output_is_refused(self):
        self.fx.write('.gitignore', '*.log\n')
        tip = self.fx.commit('ignore logs', '.gitignore')
        self.git('push', '-q', 'origin', 'main')
        path = self.tree(commit=tip)
        self.fx.write('run.log', 'evidence\n', root=path)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'ignored files that are not build output')

    def test_a_tree_on_a_branch_is_refused(self):
        path = os.path.join(self.outside, 'onbranch')
        self.git('worktree', 'add', '-q', path, '-b', 'kit/onbranch')
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'only a detached tree')
        rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'only a detached tree')
        self.assertEqual(self.git('branch', '--list', 'kit/onbranch').strip(), 'kit/onbranch')

    def test_a_locked_tree_is_refused(self):
        path = self.tree()
        self.git('worktree', 'lock', path)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'is locked')

    def test_a_tree_holding_another_worktree_is_refused(self):
        path = self.tree()
        inner = os.path.join(path, 'inner')
        self.git('worktree', 'add', '-q', '--detach', inner, self.base)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'holds the registered worktree')
        self.assertTrue(os.path.isdir(inner))

    def test_an_unregistered_folder_is_refused_and_kept(self):
        path = os.path.join(self.outside, 'stump')
        os.makedirs(path)
        rc, out = self.run_remove(path)
        self.assertEqual(rc, 1, out)
        self.assertIn('not a registered worktree', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_missing_folder_is_nothing(self):
        rc, out = self.run_remove(os.path.join(self.outside, 'gone'))
        self.assertEqual(rc, 0, out)
        self.assertIn('nothing   ', out)

    def test_the_main_checkout_is_refused(self):
        rc, out = self.run_abandon(self.repo)
        self.assertEqual(rc, 1, out)
        self.assertIn('main checkout', out)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'README.md')))

    def test_a_registered_tree_inside_the_main_checkout_but_outside_the_folders_is_refused(self):
        path = self.tree(root=os.path.join(self.repo, 'stray'))
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'inside the main checkout')

    @unittest.skipUnless(WINDOWS, 'junctions are Windows')
    def test_a_junction_on_the_way_is_refused(self):
        real_parent = os.path.join(self.outside, 'real')
        os.makedirs(real_parent)
        path = self.tree(root=real_parent)
        link = os.path.join(self.outside, 'link')
        subprocess.run(['cmd', '/c', 'mklink', '/J', link, real_parent], check=True,
                       capture_output=True)
        self.addCleanup(os.rmdir, link)
        rc, out = self.run_remove(os.path.join(link, 'red'))
        self.assertEqual(rc, 1, out)
        self.assertIn('link or junction', out)
        self.assertTrue(os.path.isdir(path))

    def test_an_unreadable_worktree_list_refuses(self):
        path = self.tree()
        with mock.patch.object(remove, 'worktree_listing',
                               side_effect=remove.GitReadError('git worktree list failed')):
            rc, out = self.run_remove(path)
        self.assertEqual(rc, 1, out)
        self.assertIn('could not be read', out)
        self.assertTrue(os.path.isdir(path))


class SlugRedirect(OutsideCase):

    def test_a_slug_matching_only_an_outside_tree_names_the_path_form(self):
        path = self.tree()
        rc, out = self.run_abandon('red')
        self.assertKept(path, rc, out, 'is not under')
        self.assertIn('"%s" --abandon "<why>"' % path, out)

    def test_two_outside_matches_are_both_named(self):
        one = self.tree()
        other_root = os.path.join(self.outside, 'second')
        os.makedirs(other_root)
        two = self.tree(root=other_root)
        rc, out = self.run_remove('red')
        self.assertEqual(rc, 1, out)
        self.assertIn('"%s"' % one, out)
        self.assertIn('"%s"' % two, out)
        self.assertTrue(os.path.isdir(one) and os.path.isdir(two))

    def test_a_slug_with_no_match_anywhere_is_still_nothing(self):
        rc, out = self.run_remove('nowhere')
        self.assertEqual(rc, 0, out)
        self.assertIn('nothing   ', out)


class Unchanged(OutsideCase):

    def test_the_landing_cleanup_entry_still_refuses_an_outside_tree_on_a_branch(self):
        path = os.path.join(self.outside, 'landed')
        self.git('worktree', 'add', '-q', path, '-b', 'kit/landed', self.base)
        ctx = remove.Ctx(self.repo, load(self.repo), cwd=self.repo)
        code = remove._remove(ctx, path, False)
        self.assertEqual(code, remove.REFUSED, ctx.out)
        self.assertTrue(os.path.isdir(path))
        self.assertIn(os.path.normcase(path), self.registered())


if __name__ == '__main__':
    unittest.main()
