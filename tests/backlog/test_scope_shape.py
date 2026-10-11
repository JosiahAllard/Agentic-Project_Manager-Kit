"""A scope path holds no control character (kitlib/backlog/scope.py `path_shape_error`): a line
break in `add --scope` (or `retarget`'s OLD/NEW) would split the record's `scope:` line into two
header lines, so it is refused with USAGE and nothing is written. `set` checks no field shape
itself, so kitlib/backlog/record.py `render` refuses a control character in any header key or
value (never in the body) for every verb that writes a record."""
import unittest

from kitlib.backlog import record as rmod
from kitlib.backlog import scope as smod
from kitlib.backlog import store as stmod
from kitlib.backlog.errors import Usage

from tests.backlog import test_cli

BROKEN = ('a/x\npin: 1', 'a/x\rb', 'a\tb', 'a/\x7fb')


class Shape(unittest.TestCase):

    def test_a_control_character_is_a_shape_error(self):
        for p in BROKEN:
            self.assertIn('control character', smod.path_shape_error(p) or '', repr(p))
            self.assertIn('control character', smod.scope_member_error(p) or '', repr(p))

    def test_a_plain_path_is_still_fine(self):
        self.assertIsNone(smod.path_shape_error('docs/a b.md'))

    def test_render_refuses_a_control_character_in_the_header_only(self):
        for pairs in ([('summary', 'x\npin: 1')], [('scope', 'a\tb')], [('pin\n', '1')]):
            with self.assertRaises(Usage, msg=pairs):
                rmod.render(pairs, '')
        text = rmod.render([('id', 'FOO')], 'line one\n\tindented\r\nline two\n')
        self.assertTrue(text.startswith('id: FOO\n\nline one\n\tindented'), text)


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

    def test_set_refuses_a_line_break_in_a_header_value(self):
        # record.render's guard: `set` does not run the field validators itself.
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'a/x')
        before = self.tip()
        for flag, value in (('--scope', 'a/x\npin: 1'), ('--summary', 'a thing\npin: 1'),
                            ('--summary', 'a thing\rto do'), ('--scope', 'a\tb')):
            rc, out = self.run_tool('set', 'FOO', flag, value)
            self.assertEqual(rc, 2, (flag, value, out))
            self.assertRegex(out, r'^USAGE: ')
        self.assertEqual(self.tip(), before)
        self.assertNotIn('pin:', self.ok('show', 'FOO'))

    def test_a_plain_set_still_writes(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'a/x')
        before = self.tip()
        self.ok('set', 'FOO', '--summary', 'a thing to do, reworded', '--scope', 'a/y')
        self.assertNotEqual(self.tip(), before)
        shown = self.ok('show', 'FOO')
        self.assertIn('summary: a thing to do, reworded', shown)
        self.assertIn('scope: a/y', shown)


if __name__ == '__main__':
    unittest.main()
