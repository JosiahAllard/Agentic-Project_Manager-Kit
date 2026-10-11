"""kitlib.git.overlap and tools/branch_overlap.py (docs/branch-overlap.md): which unmerged branches
change files in a set. Each case gets a copy of one built repository (tests/gitfixture.py):

  main          initial README.md, then kit/merged merged in; pushed to origin
  origin/HEAD   a symbolic ref to origin/main (skipped)
  kit/merged    kitlib/m.py                     (merged, so skipped)
  kit/a         kitlib/a.py, docs/x.md          (also pushed: origin/kit/a, same tip)
  kit/b         tools/b.py
  kit/c         docs/x.md, then tools/c.py      (origin/kit/c pushed at the first commit)
  kit/n         a root file named `none`
"""
import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from kitlib.git import cmd, overlap
from kitlib.git import repo as gitrepo
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'branch_overlap.py')


def _prepare(fx):
    def branch(name, *files):
        fx.git('switch', '-q', '-c', name, 'main')
        add(name, *files)

    def add(name, *files):
        for f in files:
            fx.write(f, '%s on %s\n' % (f, name))
        fx.commit(name, *files)

    branch('kit/merged', 'kitlib/m.py')
    fx.git('switch', '-q', 'main')
    fx.git('merge', '-q', '--no-ff', '-m', 'merge kit/merged', 'kit/merged')
    fx.git('push', '-q', 'origin', 'main')
    fx.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    branch('kit/a', 'kitlib/a.py', 'docs/x.md')
    fx.git('push', '-q', 'origin', 'kit/a')
    branch('kit/b', 'tools/b.py')
    branch('kit/c', 'docs/x.md')
    fx.git('push', '-q', 'origin', 'kit/c')
    add('kit/c', 'tools/c.py')
    branch('kit/n', 'none')
    fx.git('switch', '-q', 'main')


def _cli():
    spec = importlib.util.spec_from_file_location('branch_overlap_under_test', TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Overlap(unittest.TestCase):

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo(bare_remote=True, prepare=_prepare,
                                      prepare_key=('test_overlap.Overlap', 2))
        self.addCleanup(self.fx.cleanup)
        self.root = os.path.realpath(self.fx.root)

    def look(self, **kw):
        got = overlap.look(self.root, **kw)
        return got.code, '\n'.join(got.lines)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = _cli().main(['-C', self.root] + list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def tip(self, rev):
        return self.fx.git('rev-parse', rev).strip()

    def test_files_lists_each_unmerged_branch_that_changes_them(self):
        code, out = self.look(files=['kitlib/**', 'tools/b.py'])
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('base      refs/remotes/origin/main (', out)
        a = self.tip('kit/a')[:10]
        self.assertIn('kit/a  [kit/a %s, origin/kit/a %s]: 1 overlapping' % (a, a), out)
        self.assertIn('  kitlib/a.py  (matches kitlib/**)', out)
        self.assertIn('kit/b  [kit/b ', out)
        self.assertIn('  tools/b.py  (matches tools/b.py)', out)
        self.assertNotIn('kit/c', out)
        self.assertNotIn('docs/x.md', out)
        self.assertIn('OVERLAP: 2 branch(es) change files in the set; 4 unmerged branch(es) '
                      'checked (6 ref(s)), 3 merged ref(s) skipped', out)

    def test_refs_of_one_branch_at_different_tips_share_a_block(self):
        code, out = self.look(files=['docs/x.md', 'tools/c.py'])
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('kit/c  [kit/c %s, origin/kit/c %s]: 2 overlapping'
                      % (self.tip('kit/c')[:10], self.tip('origin/kit/c')[:10]), out)
        self.assertIn('  tools/c.py  (matches tools/c.py)  [only kit/c]', out)
        self.assertIn('OVERLAP: 2 branch(es)', out)

    def test_branch_set_is_that_branchs_changes_and_leaves_it_out(self):
        code, out = self.look(branch='kit/c')
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('set       2 file(s) kit/c changed', out)
        self.assertIn('kit/a  [kit/a ', out)
        self.assertIn('  docs/x.md  (matches docs/x.md)', out)
        self.assertNotIn('kit/c  [', out)          # neither kit/c nor its older origin/kit/c
        self.assertNotIn('kit/b', out)
        self.assertIn('2 ref(s) of --branch itself left out', out)

    def test_branch_given_as_a_commit_or_heads_leaves_its_older_pushed_copy_out(self):
        for rev in (self.tip('kit/c'), 'heads/kit/c', 'refs/heads/kit/c'):
            code, out = self.look(branch=rev)
            self.assertEqual(code, overlap.OVERLAP, (rev, out))
            self.assertNotIn('origin/kit/c', out, rev)
            self.assertIn('2 ref(s) of --branch itself left out', out, rev)
        self.fx.git('switch', '-q', 'kit/c')
        code, out = self.look(branch='HEAD')
        self.assertNotIn('origin/kit/c', out)

    def test_no_overlap_is_clear(self):
        code, out = self.look(files=['README.md', 'docs/other.md'])
        self.assertEqual(code, overlap.CLEAR, out)
        self.assertIn('no overlap: no unmerged branch changes a file in the set; '
                      '4 unmerged branch(es) checked (6 ref(s))', out)

    def test_merged_branches_and_symbolic_refs_are_skipped(self):
        code, out = self.look(files=['kitlib/m.py'])
        self.assertEqual(code, overlap.CLEAR, out)
        self.assertNotIn('kit/merged', out)
        self.assertNotIn('HEAD', out)
        self.assertIn('3 merged ref(s) skipped', out)      # main, origin/main, kit/merged
        every, why = overlap.branches(self.root)
        self.assertIsNone(why)
        self.assertNotIn('refs/remotes/origin/HEAD', [b.ref for b in every])
        self.assertIn('refs/remotes/origin/main', [b.ref for b in every])

    def test_backslashes_and_case_match_as_the_collision_check_does(self):
        code, out = self.look(files=['KITLIB\\A.py'])
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('  kitlib/a.py  (matches KITLIB/A.py)', out)

    def test_a_root_file_named_none_matches(self):
        code, out = self.look(files=['none'])
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('  none  (matches none)', out)
        self.assertEqual(overlap.matches('none', ['NONE']), [('NONE', '')])
        self.assertEqual(overlap.matches('none', ['none/**']), [('none/**', '')])
        self.assertEqual(overlap.matches('none', ['docs/**']), [])

    def test_an_undecidable_path_counts_as_an_overlap(self):
        got = overlap.matches('odd/a,b.md', ['tools/x.py'])
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0][0], 'tools/x.py')
        self.assertIn('undecidable, so counted', got[0][1])

    def test_local_main_ahead_of_the_base_is_not_listed_as_work(self):
        self.fx.write('tools/local.py', 'unpushed\n')
        self.fx.commit('unpushed on main', 'tools/local.py')
        code, out = self.look(files=['tools/local.py'])
        self.assertEqual(code, overlap.CLEAR, out)
        self.assertIn('note      main (%s) has commits the base refs/remotes/origin/main lacks; '
                      "it is the base's own branch" % self.tip('main')[:10], out)
        self.assertNotIn('main  [', out)
        self.assertIn("1 ref(s) of the base's own branch left out", out)

    def test_a_broken_ref_cannot_answer(self):
        path = os.path.join(self.root, '.git', 'refs', 'heads', 'kit', 'broken')
        with open(path, 'w', encoding='ascii') as fh:
            fh.write('not a sha\n')
        code, out = self.look(files=['README.md'])
        self.assertEqual(code, overlap.CANNOT, out)
        self.assertIn('CANNOT ANSWER: git for-each-ref', out)
        self.assertIn('kit/broken', out)

    def test_a_malformed_ref_line_cannot_answer(self):
        real = cmd.call

        def call(args, cwd, **kw):
            if args[:2] == ['for-each-ref', '--format=%(objectname) %(symref) %(refname)']:
                return cmd.Result(0, b'garbage\n', '')
            return real(args, cwd, **kw)
        with mock.patch.object(cmd, 'call', call):
            code, out = self.look(files=['README.md'])
        self.assertEqual(code, overlap.CANNOT, out)
        self.assertIn("printed 'garbage', not a ref line", out)

    def test_git_failing_mid_run_cannot_answer(self):
        real = cmd.run

        def run(args, cwd, **kw):
            if args[0] == 'diff':
                return None, 'git was ended'
            return real(args, cwd, **kw)
        with mock.patch.object(cmd, 'run', run):
            code, out = self.look(files=['README.md'])
        self.assertEqual(code, overlap.CANNOT, out)
        self.assertIn('CANNOT READ kit/a (', out)
        self.assertIn('git was ended', out)
        self.assertIn('CANNOT ANSWER: 6 ref(s) could not be read', out)
        self.assertNotIn('no overlap', out)

    def test_unresolvable_base_or_branch_cannot_answer(self):
        code, out = self.look(files=['README.md'], base='kit/nosuch')
        self.assertEqual(code, overlap.CANNOT, out)
        self.assertIn('CANNOT ANSWER: base kit/nosuch', out)
        code, out = self.look(branch='kit/nosuch')
        self.assertEqual(code, overlap.CANNOT, out)
        self.assertIn('CANNOT ANSWER: --branch kit/nosuch', out)

    def test_no_merge_base_cannot_answer(self):
        tree = self.fx.git('rev-parse', 'main^{tree}').strip()
        orphan = self.fx.git('commit-tree', tree, '-m', 'no parent').strip()
        self.fx.git('update-ref', 'refs/heads/kit/orphan', orphan)
        code, out = self.look(files=['README.md'])
        self.assertEqual(code, overlap.CANNOT, out)
        self.assertIn('CANNOT READ kit/orphan (', out)
        self.assertIn('no merge base', out)
        code, out = self.look(branch='kit/orphan')
        self.assertEqual(code, overlap.CANNOT, out)
        self.assertIn('CANNOT ANSWER: --branch kit/orphan: no merge base', out)

    def test_cli_exit_codes(self):
        code, out = self.cli('--files', 'README.md')
        self.assertEqual(code, 0, out)
        code, out = self.cli('--files', 'tools/**')
        self.assertEqual(code, 1, out)
        self.assertIn('kit/b  [', out)

    def test_cli_files_from(self):
        listing = os.path.join(self.fx.base, 'set.txt')
        with open(listing, 'w', encoding='utf-8') as fh:
            fh.write('# sweep exclusions\n\ndocs/x.md\n')
        code, out = self.cli('--files-from', listing)
        self.assertEqual(code, 1, out)
        self.assertIn('kit/c  [', out)

    def test_usage_errors_exit_2(self):
        empty = os.path.join(self.fx.base, 'empty.txt')
        with open(empty, 'w', encoding='utf-8') as fh:
            fh.write('# nothing\n\n')
        for args in ((), ('--files', 'a.py', '--branch', 'kit/a'), ('--files', '*.py'),
                     ('--files-from', os.path.join(self.fx.base, 'missing.txt')),
                     ('--files-from', empty)):
            code, out = self.cli(*args)
            self.assertEqual(code, 2, (args, out))
            self.assertIn('USAGE:', out, args)
        code, out = self.cli('--files-from', empty)
        self.assertIn('the file set is empty', out)

    def test_cli_git_unable_to_name_the_tree_cannot_answer_not_usage(self):
        unknown = gitrepo.Place(gitrepo.UNKNOWN, None, 'git did not answer', None, None)
        with mock.patch.object(gitrepo, 'classify', return_value=unknown):
            code, out = self.cli('--files', 'README.md')
        self.assertEqual(code, 2, out)
        self.assertIn('CANNOT ANSWER: -C ', out)
        self.assertNotIn('USAGE:', out)


if __name__ == '__main__':
    unittest.main()
