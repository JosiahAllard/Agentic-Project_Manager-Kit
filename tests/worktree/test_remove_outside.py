"""kitlib.worktree.remove on a registered worktree OUTSIDE the worktree folders (an agent's red-run
tree in a scratchpad): named by its path, a DETACHED one goes through the plain verb's or
`--abandon`'s checks; a slug that only matches such a tree is refused with the path-form command
(docs/worktree-remove.md "Tools").

Every worktree here is one the test made, in fixture folders beside the repository.
"""
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from kitlib.config import load
from kitlib.worktree import remove
from tests import gitfixture
from tests.realgit import real_git
from tests.platforms import windows_only

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

    def test_a_tree_on_a_branch_is_refused_by_abandon(self):
        path = os.path.join(self.outside, 'onbranch')
        self.git('worktree', 'add', '-q', path, '-b', 'kit/onbranch')
        rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'only a detached tree')
        self.assertEqual(self.git('rev-parse', '--verify', 'refs/heads/kit/onbranch').strip(),
                         self.base)

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

    @windows_only('junctions are Windows')
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


@real_git('git reflog: the fake writes no reflog')
class ReflogOnly(OutsideCase):
    """A commit made in the tree and left by a checkout lives only in the tree's HEAD reflog
    (<admin dir>/logs/HEAD), which `git worktree remove` deletes: no verb drops it silently."""

    def real_commit(self, path, name):
        """A commit of `<name>.txt` in the tree at `path` made by the test's own git, so it is in
        the tree's reflog. Returns its oid."""
        self.fx.write(name + '.txt', name + '\n', root=path)
        self.git('add', name + '.txt', cwd=path)
        self.git('commit', '-q', '-m', name, cwd=path)
        return self.git('rev-parse', 'HEAD', cwd=path).strip()

    def test_a_commit_made_while_the_verb_ran_is_refused(self):
        path = self.tree()
        real = remove.clear_disposable

        def commit_and_return(ctx, p):
            self.fx.write('late.txt', 'late\n', root=path)
            self.fx.commit('late', 'late.txt', cwd=path)
            self.git('checkout', '-q', '--detach', self.base, cwd=path)
            return real(ctx, p)

        with mock.patch.object(remove, 'clear_disposable', commit_and_return):
            rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'its HEAD reflog or a per-worktree ref changed after it was checked')

    def left_behind(self, start):
        path = self.tree(commit=start)
        self.fx.write('left.txt', 'left\n', root=path)
        left = self.fx.commit('left behind', 'left.txt', cwd=path)
        self.git('checkout', '-q', '--detach', start, cwd=path)
        return path, left

    def test_the_plain_verb_refuses_a_reflog_only_commit(self):
        path, left = self.left_behind(self.base)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'which no ref reaches')
        self.assertIn(left[:10], out)

    def test_abandon_refuses_a_reflog_only_commit_and_tags_nothing(self):
        path, left = self.left_behind(self.topic_tip)
        rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'which no ref reaches')
        self.assertIn(left[:10], out)
        self.assertEqual(self.tag('archive/red'), '')

    def test_a_reflog_commit_a_branch_reaches_does_not_refuse(self):
        path = self.tree()
        self.git('checkout', '-q', '--detach', self.topic_tip, cwd=path)
        self.git('checkout', '-q', '--detach', self.base, cwd=path)
        rc, out = self.run_remove(path)
        self.assertRemoved(path, rc, out)

    def test_a_head_moved_after_the_merge_check_is_refused(self):
        # kit/topic reaches the new HEAD, so only the merge check's HEAD tells it is unlanded.
        path = self.tree()
        real = remove.merge_check

        def then_move(*args):
            got = real(*args)
            self.git('checkout', '-q', '--detach', self.topic_tip, cwd=path)
            return got

        with mock.patch.object(remove, 'merge_check', then_move):
            rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'HEAD moved after it was checked')

    def test_a_commit_only_a_per_worktree_ref_keeps_is_refused(self):
        path = self.tree()
        kept = self.git('commit-tree', self.base + '^{tree}', '-p', self.base, '-m', 'kept',
                        cwd=path).strip()
        self.git('update-ref', 'refs/worktree/keep', kept, cwd=path)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'which no ref reaches')
        self.assertIn(kept[:10], out)

    def test_abandon_refuses_a_commit_only_a_per_worktree_ref_keeps(self):
        path = self.tree(commit=self.topic_tip)
        kept = self.git('commit-tree', self.base + '^{tree}', '-p', self.base, '-m', 'kept',
                        cwd=path).strip()
        self.git('update-ref', 'refs/bisect/bad', kept, cwd=path)
        rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'which no ref reaches')
        self.assertIn(kept[:10], out)
        self.assertEqual(self.tag('archive/red'), '')

    def test_abandon_refuses_a_reflog_changed_before_git(self):
        path = self.tree(commit=self.topic_tip)
        real = remove.clear_disposable

        def commit_and_return(ctx, p):
            self.fx.write('late.txt', 'late\n', root=path)
            self.fx.commit('late', 'late.txt', cwd=path)
            self.git('checkout', '-q', '--detach', self.topic_tip, cwd=path)
            return real(ctx, p)

        with mock.patch.object(remove, 'clear_disposable', commit_and_return):
            rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'its HEAD reflog or a per-worktree ref changed after it was checked')
        self.assertIn('kept     tag archive/red', out)

    def test_an_unknown_reach_refuses_abandon(self):
        path = self.tree(commit=self.topic_tip)
        self.git('checkout', '-q', '--detach', self.base, cwd=path)
        self.git('checkout', '-q', '--detach', self.topic_tip, cwd=path)
        from kitlib.worktree import scratch
        with mock.patch.object(scratch, '_reaches',
                               return_value=(None, 'git merge-base exited 128')):
            rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'is unknown (git merge-base exited 128)')
        self.assertEqual(self.tag('archive/red'), '')

    def test_abandon_keeps_a_reflog_commit_its_tag_reaches(self):
        """The two commits go through the test's own (real) git: the fixture's commit runs on the
        fake, which writes no reflog, and then the reflog would never name `mine` at all."""
        path = self.tree(commit=self.topic_tip)
        mine = self.real_commit(path, 'mine')
        more = self.real_commit(path, 'more')
        rc, out = self.run_abandon(path)
        self.assertRemoved(path, rc, out)
        self.assertEqual(self.tag('archive/red'), more)
        self.git('merge-base', '--is-ancestor', mine, 'archive/red')    # raises when not


class Spelling(OutsideCase):

    def test_a_relative_path_is_taken_against_the_callers_directory(self):
        path = self.tree()
        got = remove.remove(self.repo, 'outside/red', cwd=os.path.dirname(self.outside))
        self.assertRemoved(path, got.code, '\n'.join(got.lines))

    @windows_only('8.3 short names are Windows')
    def test_a_short_name_spelling_is_the_same_tree_not_a_junction(self):
        import ctypes
        import shutil
        import tempfile

        def short_of(p):
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, len(buf))
            got = buf.value if 0 < n < len(buf) else ''
            return got if got and os.path.normcase(got) != os.path.normcase(p) else ''

        # The fixture's volume may make no 8.3 names; the system drive's temp folder usually does.
        homes = [self.outside]
        local = os.environ.get('LOCALAPPDATA')
        if local and os.path.isdir(os.path.join(local, 'Temp')):
            extra = os.path.realpath(tempfile.mkdtemp(prefix='kit-short-',
                                                      dir=os.path.join(local, 'Temp')))
            self.addCleanup(shutil.rmtree, extra, True)
            homes.append(extra)
        path = short = None
        for i, home in enumerate(homes):
            parent = os.path.join(home, 'a long folder name')
            os.makedirs(parent)
            short = short_of(parent)
            if short:
                path = self.tree(name='red%d' % i, root=parent)
                short = os.path.join(short, 'red%d' % i)
                break
        if not short:
            self.skipTest('no volume here makes 8.3 short names')
        self.assertNotEqual(os.path.normcase(os.path.realpath(short)), os.path.normcase(short))
        rc, out = self.run_remove(short)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('link or junction', out)
        self.assertFalse(os.path.exists(path), out)


class MoreRefusals(OutsideCase):

    def test_a_folder_holding_the_main_checkout_is_refused(self):
        rc, out = self.run_remove(os.path.dirname(self.repo))
        self.assertEqual(rc, 1, out)
        self.assertIn('holds the main checkout', out)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'README.md')))

    def test_an_exempt_outside_tree_is_refused(self):
        self.fx.write('.kit.json', json.dumps(
            {'worktree_remove': {'exempt': {'red': 'kept by ruling Y'}}}) + '\n')
        self.fx.commit('config', '.kit.json')
        path = self.tree()
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'is exempt (kept by ruling Y)')

    def test_no_head_is_refused(self):
        path = self.tree()
        real = remove.worktree_entries

        def headless(listing):
            got = real(listing)
            for e in got:
                if os.path.normcase(os.path.realpath(e['path'])) == os.path.normcase(path):
                    e['head'] = None
            return got
        with mock.patch.object(remove, 'worktree_entries', side_effect=headless):
            rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'git reported no HEAD for it')

    def test_an_unreadable_reflog_refuses_both_verbs(self):
        from kitlib.worktree import scratch
        landed, unlanded = self.tree(), self.tree(name='blue', commit=self.topic_tip)
        with mock.patch.object(scratch, 'reflog', return_value=(None, 'its HEAD reflog broke')):
            rc, out = self.run_remove(landed)
            self.assertKept(landed, rc, out, 'its HEAD reflog broke; nothing was removed')
            rc, out = self.run_abandon(unlanded)
            self.assertKept(unlanded, rc, out, 'its HEAD reflog broke; nothing was tagged or removed')
        self.assertEqual(self.tag('archive/blue'), '')

    def test_a_failed_ref_list_refuses_abandon(self):
        path = self.tree(commit=self.topic_tip)
        real = remove.git

        def failing(cwd, *args, **kw):
            if args[:1] == ('for-each-ref',) and 'refs/tags' in args:
                return 128, '', 'fatal: refs unreadable'
            return real(cwd, *args, **kw)

        with mock.patch.object(remove, 'git', failing):
            rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'whether a ref reaches its HEAD reflog is unknown')
        self.assertIn('refs unreadable', out)
        self.assertEqual(self.tag('archive/red'), '')

    def test_the_ignored_file_refusal_reruns_by_the_quoted_path(self):
        self.fx.write('.gitignore', '*.log\n')
        tip = self.fx.commit('ignore logs', '.gitignore')
        self.git('push', '-q', 'origin', 'main')
        path = self.tree(commit=tip)
        self.fx.write('run.log', 'evidence\n', root=path)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'ignored files that are not build output')
        self.assertRegex(out, r'(?m)^  rerun    \S.*worktree_remove.* "%s"$' % re.escape(path))

    def test_abandon_of_a_landed_tree_names_the_plain_verb_by_path(self):
        path = self.tree()
        rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'nothing to abandon')
        self.assertRegex(out, r'Remove it with `\S.*worktree_remove.* "%s"`' % re.escape(path))


class SlugRedirect(OutsideCase):

    def test_a_slug_matching_only_an_outside_tree_names_the_path_form(self):
        path = self.tree()
        rc, out = self.run_abandon('red')
        self.assertKept(path, rc, out, 'is not under')
        self.assertIn('"%s" --abandon "<why>"' % path, out)

    def test_the_redirect_names_the_scratch_verb(self):
        path = self.tree()
        rc, out = self.run_remove('red')
        self.assertKept(path, rc, out, 'is not under')
        self.assertIn('"%s" --scratch' % path, out)

    def test_an_unreadable_list_for_a_missing_slug_is_still_nothing(self):
        with mock.patch.object(remove, 'worktree_listing',
                               side_effect=remove.GitReadError('git worktree list failed')):
            rc, out = self.run_remove('nowhere')
        self.assertEqual(rc, 0, out)
        self.assertIn('nothing   ', out)
        self.assertIn('git worktree list failed', out)

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


class BranchByPath(OutsideCase):
    """The owner's rule: the plain verb, named by its PATH, removes a registered, unlocked tree
    on a branch outside the worktree folders once that branch has landed; every later check runs.
    `--abandon`, the slug form and the landing cleanup still refuse it."""

    def branch_tree(self, name='landed', commit=None):
        path = os.path.join(self.outside, name)
        self.git('worktree', 'add', '-q', path, '-b', 'kit/%s' % name, commit or self.base)
        return path

    def branch(self, name):
        return self.git('rev-parse', '-q', '--verify', 'refs/heads/kit/%s' % name,
                        check=False).strip()

    def test_a_landed_branch_tree_named_by_path_is_removed_with_its_branch(self):
        path = self.branch_tree()
        rc, out = self.run_remove(path)
        self.assertRemoved(path, rc, out)
        self.assertIn('merged    kit/landed is in', out)
        self.assertIn('deleted   branch kit/landed', out)
        self.assertEqual(self.branch('landed'), '')

    def test_an_outside_tree_on_the_main_branch_is_refused(self):
        self.git('checkout', '-q', '--detach')      # free main for the outside tree
        path = os.path.join(self.outside, 'onmain')
        self.git('worktree', 'add', '-q', path, 'main')
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'is on the main branch main')
        self.assertNotIn('merged    ', out)
        self.assertEqual(self.git('rev-parse', '-q', '--verify', 'refs/heads/main',
                                  check=False).strip(), self.base)

    def test_keep_branch_keeps_the_branch(self):
        path = self.branch_tree()
        got = remove.remove(self.repo, path, keep_branch=True, cwd=self.repo)
        out = '\n'.join(got.lines)
        self.assertRemoved(path, got.code, out)
        self.assertEqual(self.branch('landed'), self.base)

    def test_an_unlanded_branch_tree_is_refused(self):
        path = self.branch_tree('unlanded', self.topic_tip)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'holds UNLANDED work')
        self.assertIn('git branch -D kit/unlanded', out)
        self.assertEqual(self.branch('unlanded'), self.topic_tip)

    def test_a_locked_branch_tree_is_refused(self):
        path = self.branch_tree()
        self.git('worktree', 'lock', path)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'is locked')
        self.assertEqual(self.branch('landed'), self.base)

    def test_a_dirty_branch_tree_is_refused(self):
        path = self.branch_tree()
        self.fx.write('work.txt', 'work\n', root=path)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'uncommitted or untracked files')
        self.assertEqual(self.branch('landed'), self.base)

    def test_an_unlanded_detached_tree_is_still_refused(self):
        path = self.tree(commit=self.topic_tip)
        rc, out = self.run_remove(path)
        self.assertKept(path, rc, out, 'holds UNLANDED work')

    def test_abandon_still_refuses_a_landed_branch_tree(self):
        path = self.branch_tree()
        rc, out = self.run_abandon(path)
        self.assertKept(path, rc, out, 'only a detached tree')
        self.assertEqual(self.branch('landed'), self.base)
        self.assertEqual(self.tag('archive/landed'), '')

    def test_the_slug_form_still_refuses_a_landed_branch_tree(self):
        path = self.branch_tree()
        rc, out = self.run_remove('landed')
        self.assertKept(path, rc, out, 'a slug never names one')
        self.assertIn(' "%s"\n' % path, out)
        self.assertEqual(self.branch('landed'), self.base)


class Unchanged(OutsideCase):

    def test_the_landing_cleanup_entry_still_refuses_an_outside_tree_on_a_branch(self):
        path = os.path.join(self.outside, 'landed')
        self.git('worktree', 'add', '-q', path, '-b', 'kit/landed', self.base)
        ctx = remove.Ctx(self.repo, load(self.repo), cwd=self.repo)
        code = remove._remove(ctx, path, False)
        self.assertEqual(code, remove.REFUSED, ctx.out)
        self.assertIn('only a detached tree', '\n'.join(ctx.out))
        self.assertTrue(os.path.isdir(path))
        self.assertIn(os.path.normcase(path), self.registered())


if __name__ == '__main__':
    unittest.main()
