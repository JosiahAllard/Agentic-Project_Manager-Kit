"""tests/pathfree.py: a word assert on text that carries a path whose folder holds the word."""
import os
import unittest

from tests import pathfree

SLUG = 'Z:/x/private-slug'
BACK = 'Z:\\x\\private-slug'
TEXT = 'python "%s/tools/gate.py" -C "%s" ok' % (SLUG, SLUG)


class WithoutPaths(unittest.TestCase):
    def test_the_plain_assert_trips_on_the_path_and_the_helper_clears_it(self):
        with self.assertRaises(AssertionError):
            self.assertNotIn('private', TEXT)
        self.assertNotIn('private', pathfree.without_paths(TEXT, SLUG))

    def test_the_word_is_still_found_outside_the_path(self):
        text = TEXT + ' a private note'
        with self.assertRaises(AssertionError):
            self.assertNotIn('private', pathfree.without_paths(text, SLUG))

    def test_both_slash_styles_and_any_drive_letter_case(self):
        text = 'at %s\\tools and %s/tools' % (BACK, SLUG)
        for root in (SLUG, BACK, 'z:\\x\\private-slug', 'z:/X/PRIVATE-slug'):
            self.assertEqual(pathfree.without_paths(text, root), 'at \\tools and /tools', root)

    def test_the_longer_root_goes_first(self):
        got = pathfree.without_paths('a Z:/x/private-slug/sub b', 'Z:/x', 'Z:/x/private-slug/sub')
        self.assertEqual(got, 'a  b')

    def test_the_kit_root_and_cwd_are_always_removed(self):
        text = 'in %s and %s' % (pathfree.KIT_ROOT.replace(os.sep, '/'), os.getcwd())
        self.assertEqual(pathfree.without_paths(text), 'in  and ')

    def test_an_empty_root_changes_nothing(self):
        self.assertEqual(pathfree.without_paths('abc', '', '/'), 'abc')


if __name__ == '__main__':
    unittest.main()
