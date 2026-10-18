"""A -C path Git Bash mangled (unquoted backslashes stripped) gets a hint on the refusal."""
import os
import unittest

from kitlib import git
from kitlib.git import overlap

STRIPPED = 'Z:GitHubAgentic-Project_Manager-Kit'
HINT = 'an unquoted backslash path in Git Bash loses its backslashes'


class StrippedPathHintTest(unittest.TestCase):
    def test_toplevel_hints_on_the_stripped_form(self):
        top, why = git.toplevel(STRIPPED)
        self.assertIsNone(top)
        self.assertIn('%s is not a directory' % STRIPPED, why)
        self.assertIn(HINT, why)
        self.assertIn('quote the path or use forward slashes', why)

    def test_overlap_work_tree_hints_on_the_stripped_form(self):
        top, kind, why = overlap.work_tree(STRIPPED)
        self.assertEqual((top, kind), (None, 'usage'))
        self.assertIn(HINT, why)

    def test_a_normal_missing_path_has_no_hint(self):
        for path in ('Z:/missing/place', 'Z:\\missing\\place', 'missing-place-x',
                     '/missing/place', 'Z:missing/place'):
            self.assertNotIn('backslash', git.toplevel(path)[1], path)
            self.assertNotIn('backslash', overlap.work_tree(path)[2], path)
            self.assertIn('is not a directory', git.toplevel(path)[1])

    def test_a_real_directory_works(self):
        here = os.path.dirname(os.path.abspath(__file__))
        top, why = git.toplevel(here)
        self.assertIsNone(why)
        self.assertTrue(os.path.isdir(top))
        self.assertIsNone(overlap.work_tree(here)[2])


if __name__ == '__main__':
    unittest.main()
