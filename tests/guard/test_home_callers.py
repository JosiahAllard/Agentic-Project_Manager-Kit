"""The guard's callers of shell.resolve_path read `~` as the context's home (ctx.home, or the
home the line was parsed with: Command.home), never the process's own: with no home given they
meet the home fence (kitlib.config.user.HomeFenced), so none of them widens what is allowed when
the home is unknown."""
import os
import unittest
from types import SimpleNamespace

from kitlib.config import user as kuser
from kitlib.guard import commands as cm
from kitlib.guard import rolepolicy, rules
from kitlib.guard import shell as sh
from tests.home import fence_decoy

CWD = 'D:\\w'
GIVEN = 'C:\\given-home'


def same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class HomeCallers(unittest.TestCase):
    def setUp(self):
        fence_decoy(self)

    def test_a_command_carries_the_home_its_line_was_parsed_with(self):
        parsed = cm.read('git status && echo x', sh.BASH, CWD, home=GIVEN)
        self.assertTrue(parsed.commands)
        self.assertEqual({c.home for c in parsed.commands}, {GIVEN})
        self.assertEqual({c.home for c in cm.read('git status', sh.BASH, CWD).commands}, {None})

    def test_git_dash_c_tilde_is_the_parsed_home(self):
        inv = cm.invocation(cm.read('git -C ~/repo status', sh.BASH, CWD, home=GIVEN).commands[0])
        self.assertTrue(same(cm.git_call(inv).dir, GIVEN + '\\repo'))
        with self.assertRaises(kuser.HomeFenced):       # read() asks git_call too (expand)
            cm.read('git -C ~/repo status', sh.BASH, CWD)

    def test_an_edit_of_a_tilde_path_is_under_the_given_home(self):
        action = SimpleNamespace(path='~/x.txt')
        self.assertTrue(same(rolepolicy.edit_path(action, CWD, GIVEN), GIVEN + '\\x.txt'))
        with self.assertRaises(kuser.HomeFenced):
            rolepolicy.edit_path(action, CWD)

    def test_a_path_back_at_ctx_home_is_a_root(self):
        ctx = SimpleNamespace(home=GIVEN)
        self.assertTrue(rules.is_root('~/a/..', CWD, ctx))
        self.assertFalse(rules.is_root('~/a', CWD, ctx))
        with self.assertRaises(kuser.HomeFenced):
            rules.is_root('~/a', CWD, SimpleNamespace())


if __name__ == '__main__':
    unittest.main()
