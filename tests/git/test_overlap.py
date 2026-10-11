"""kitlib.git.overlap and tools/branch_overlap.py (docs/branch-overlap.md): which unmerged branches
change files in a set. Each case gets a copy of one built repository (tests/gitfixture.py):

  main          initial README.md, then kit/merged merged in; pushed to origin
  kit/merged    kitlib/m.py                     (merged, so skipped)
  kit/a         kitlib/a.py, docs/x.md          (also pushed: origin/kit/a)
  kit/b         tools/b.py
  kit/c         docs/x.md, tools/c.py
"""
import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from kitlib.git import overlap
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'branch_overlap.py')


def _prepare(fx):
    def branch(name, *files):
        fx.git('switch', '-q', '-c', name, 'main')
        for f in files:
            fx.write(f, '%s on %s\n' % (f, name))
        fx.commit(name, *files)

    branch('kit/merged', 'kitlib/m.py')
    fx.git('switch', '-q', 'main')
    fx.git('merge', '-q', '--no-ff', '-m', 'merge kit/merged', 'kit/merged')
    fx.git('push', '-q', 'origin', 'main')
    branch('kit/a', 'kitlib/a.py', 'docs/x.md')
    fx.git('push', '-q', 'origin', 'kit/a')
    branch('kit/b', 'tools/b.py')
    branch('kit/c', 'docs/x.md', 'tools/c.py')
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
                                      prepare_key=('test_overlap.Overlap', 1))
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

    def test_files_lists_each_unmerged_branch_that_changes_them(self):
        code, out = self.look(files=['kitlib/**', 'tools/b.py'])
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('base      refs/remotes/origin/main (', out)
        self.assertIn('kit/a (', out)
        self.assertIn('origin/kit/a (', out)
        self.assertIn('  kitlib/a.py  (matches kitlib/**)', out)
        self.assertIn('kit/b (', out)
        self.assertIn('  tools/b.py  (matches tools/b.py)', out)
        self.assertNotIn('kit/c (', out)
        self.assertNotIn('docs/x.md', out)
        self.assertIn('OVERLAP: 3 branch(es)', out)

    def test_branch_set_is_that_branchs_changes_and_leaves_it_out(self):
        code, out = self.look(branch='kit/c')
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('set       2 file(s) kit/c changed', out)
        self.assertIn('kit/a (', out)
        self.assertIn('origin/kit/a (', out)
        self.assertIn('  docs/x.md  (matches docs/x.md)', out)
        self.assertNotIn('kit/c (', out)
        self.assertNotIn('kit/b (', out)

    def test_no_overlap_is_clear(self):
        code, out = self.look(files=['README.md', 'docs/other.md'])
        self.assertEqual(code, overlap.CLEAR, out)
        self.assertIn('no overlap: no unmerged branch changes a file in the set; '
                      '4 unmerged branch(es) checked', out)

    def test_merged_branches_are_skipped(self):
        code, out = self.look(files=['kitlib/m.py'])
        self.assertEqual(code, overlap.CLEAR, out)
        self.assertNotIn('kit/merged', out)
        self.assertIn('3 merged skipped', out)      # main, origin/main, kit/merged

    def test_backslashes_and_case_match_as_the_collision_check_does(self):
        code, out = self.look(files=['KITLIB\\A.py'])
        self.assertEqual(code, overlap.OVERLAP, out)
        self.assertIn('  kitlib/a.py  (matches KITLIB/A.py)', out)

    def test_cli_exit_codes(self):
        code, out = self.cli('--files', 'README.md')
        self.assertEqual(code, 0, out)
        code, out = self.cli('--files', 'tools/**')
        self.assertEqual(code, 1, out)
        self.assertIn('kit/b (', out)

    def test_cli_files_from(self):
        listing = os.path.join(self.fx.base, 'set.txt')
        with open(listing, 'w', encoding='utf-8') as fh:
            fh.write('# sweep exclusions\n\ndocs/x.md\n')
        code, out = self.cli('--files-from', listing)
        self.assertEqual(code, 1, out)
        self.assertIn('kit/c (', out)

    def test_usage_errors_exit_2(self):
        for args in ((), ('--files', 'a.py', '--branch', 'kit/a'), ('--files', '*.py'),
                     ('--files-from', os.path.join(self.fx.base, 'missing.txt'))):
            code, out = self.cli(*args)
            self.assertEqual(code, 2, (args, out))
            self.assertIn('USAGE:', out, args)


if __name__ == '__main__':
    unittest.main()
