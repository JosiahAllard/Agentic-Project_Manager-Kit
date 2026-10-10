"""kitlib.git.checkout.read_text / write_text and tools/worktree_paths.py (from the PM kit's
test_worktree_paths.py, classes WorktreePaths and ScriptRunInMain): a scratch script's write goes
to the worktree it names, never to its cwd or the main checkout."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.git import checkout, repo
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(KIT, 'tools', 'worktree_paths.py')
DOC = os.path.join('docs', 'a.md')
BASE = 'line one\nline two\n'


def _read(path):
    with open(path, encoding='utf-8', newline='') as fh:
        return fh.read()


class Repo(unittest.TestCase):
    """main holds a committed docs/a.md; w and v are linked worktrees of it."""

    def setUp(self):
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)
        self.main = self.r.root
        self.r.write('docs/a.md', BASE)
        self.r.commit('doc', 'docs/a.md')
        self.w = self.r.worktree('w', 'area/w')
        self.v = self.r.worktree('v', 'area/v')
        self.outside = tempfile.mkdtemp(prefix='kit-paths-out-')
        self.addCleanup(gitfixture.rmtree, self.outside)


class WorktreePaths(Repo):

    def test_a_relative_path_resolves_from_the_worktree_root_not_the_cwd(self):
        with mock.patch('os.getcwd', return_value=self.main):
            got = repo.check_write(DOC, self.w)
        self.assertTrue(repo.same_path(got, os.path.join(self.w, DOC)))

    def test_an_absolute_path_into_the_main_checkout_is_refused_and_nothing_written(self):
        target = os.path.join(self.main, DOC)
        with self.assertRaisesRegex(repo.WriteRefused, 'MAIN checkout'):
            checkout.write_text(target, 'changed\n', self.w)
        self.assertEqual(_read(target), BASE)

    def test_a_new_file_in_the_main_checkout_is_refused(self):
        target = os.path.join(self.main, 'docs', 'new', 'b.md')
        with self.assertRaises(repo.WriteRefused):
            checkout.write_text(target, 'x\n', self.w)
        self.assertFalse(os.path.exists(target))

    def test_another_linked_worktree_is_refused(self):
        target = os.path.join(self.v, DOC)
        with self.assertRaisesRegex(repo.WriteRefused, 'another worktree'):
            checkout.write_text(target, 'changed\n', self.w)
        self.assertEqual(_read(target), BASE)

    def test_a_path_outside_every_repository_is_allowed(self):
        if repo.classify(self.outside).kind != repo.NONE:
            self.skipTest('the temp folder is inside a git checkout')
        target = os.path.join(self.outside, 'scratch', 'patch.txt')
        checkout.write_text(target, 'ok\n', self.w)
        self.assertEqual(_read(target), 'ok\n')

    def test_write_text_writes_lf_into_the_worktree(self):
        checkout.write_text(DOC, checkout.read_text(DOC, self.w).replace('two', 'TWO'), self.w)
        self.assertEqual(_read(os.path.join(self.w, DOC)), 'line one\nline TWO\n')
        self.assertEqual(_read(os.path.join(self.main, DOC)), BASE)

    def test_crlf_text_is_refused(self):
        with self.assertRaisesRegex(repo.WriteRefused, 'CRLF'):
            checkout.write_text(DOC, 'a\r\nb\r\n', self.w)
        self.assertEqual(_read(os.path.join(self.w, DOC)), BASE)

    @unittest.skipUnless(os.name == 'nt', 'a directory junction is a Windows feature')
    def test_a_junction_inside_the_worktree_pointing_into_main_is_refused(self):
        # `mklink /J` needs no elevation. The junction's spelled path is inside the worktree; only
        # resolving it shows the write would land in the main checkout.
        link = os.path.join(self.w, 'into-main')
        r = subprocess.run(('cmd', '/c', 'mklink', '/J', link, os.path.join(self.main, 'docs')),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        try:
            with self.assertRaisesRegex(repo.WriteRefused, 'MAIN checkout'):
                checkout.write_text(os.path.join('into-main', 'a.md'), 'changed\n', self.w)
        finally:
            os.rmdir(link)   # removes the junction itself, never its target
        self.assertEqual(_read(os.path.join(self.main, DOC)), BASE)

    def test_a_root_in_the_main_checkout_refuses_every_path(self):
        with self.assertRaisesRegex(repo.WriteRefused, 'MAIN checkout'):
            checkout.write_text(os.path.join(self.outside, 'x.txt'), 'x\n', self.main)
        self.assertFalse(os.path.exists(os.path.join(self.outside, 'x.txt')))


class ScriptRunInMain(Repo):
    """The incident: a scratch script whose cwd is the MAIN checkout, relative paths."""

    def run_script(self, root, body):
        script = os.path.join(self.outside, 'patch.py')
        with open(script, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('import sys\nsys.path.insert(0, %r)\nfrom kitlib.git import checkout\n'
                     'ROOT = %r\n%s\n' % (KIT, root, body))
        return subprocess.run((sys.executable, script), cwd=self.main, capture_output=True,
                              text=True)

    def test_a_relative_patch_run_from_main_edits_the_worktree_not_main(self):
        r = self.run_script(self.w, "checkout.write_text(%r, checkout.read_text(%r, ROOT)"
                                    ".replace('two', 'TWO'), ROOT)" % (DOC, DOC))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_read(os.path.join(self.main, DOC)), BASE)
        self.assertEqual(_read(os.path.join(self.w, DOC)), 'line one\nline TWO\n')

    def test_naming_the_main_checkout_as_root_refuses_and_writes_nothing(self):
        r = self.run_script(self.main, "checkout.write_text(%r, 'changed\\n', ROOT)" % DOC)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('WriteRefused', r.stderr)
        self.assertEqual(_read(os.path.join(self.main, DOC)), BASE)

    def test_the_cli_exits_2_on_a_refused_path_and_0_on_a_good_one(self):
        bad = subprocess.run((sys.executable, CLI, '-C', self.w, os.path.join(self.main, DOC)),
                             cwd=self.main, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)
        self.assertIn('REFUSED', bad.stdout)
        good = subprocess.run((sys.executable, CLI, '-C', self.w, DOC), cwd=self.main,
                              capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertTrue(repo.same_path(good.stdout.strip(), os.path.join(self.w, DOC)))

    def test_the_cli_from_a_subfolder_resolves_its_worktree(self):
        good = subprocess.run((sys.executable, CLI, 'a.md'), cwd=os.path.join(self.w, 'docs'),
                              capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertTrue(repo.same_path(good.stdout.strip(), os.path.join(self.w, 'a.md')))

    def test_the_cli_in_the_main_checkout_refuses(self):
        bad = subprocess.run((sys.executable, CLI, DOC), cwd=self.main, capture_output=True,
                             text=True)
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)
        self.assertIn('MAIN checkout', bad.stdout)


if __name__ == '__main__':
    unittest.main()
