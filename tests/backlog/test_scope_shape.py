"""A scope path holds no control character (kitlib/backlog/scope.py `path_shape_error`): a line
break in `add --scope` (or `retarget`'s OLD/NEW) would split the record's `scope:` line into two
header lines, so it is refused with USAGE and nothing is written."""
import unittest

from kitlib.backlog import scope as smod
from kitlib.backlog import store as stmod

from tests.backlog import test_cli

BROKEN = ('a/x\npin: 1', 'a/x\rb', 'a\tb', 'a/\x7fb')


class Shape(unittest.TestCase):

    def test_a_control_character_is_a_shape_error(self):
        for p in BROKEN:
            self.assertIn('control character', smod.path_shape_error(p) or '', repr(p))
            self.assertIn('control character', smod.scope_member_error(p) or '', repr(p))

    def test_a_plain_path_is_still_fine(self):
        self.assertIsNone(smod.path_shape_error('docs/a b.md'))


class ScopeShapeCli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def tip(self):
        return self.repo.git('ls-remote', 'origin', stmod.STORE_REF)

    def test_add_refuses_a_line_break_in_a_scope(self):
        before = self.tip()
        for p in BROKEN:
            rc, out = self.run_tool('add', 'FOO', '--summary', 'a thing to do', '--scope', p)
            self.assertEqual(rc, 2, (p, out))
            self.assertRegex(out, r'^USAGE: ')
        self.assertEqual(self.tip(), before)


if __name__ == '__main__':
    unittest.main()
