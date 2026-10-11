"""The home fence over the plugin refresh's account folders (docs/test-tools.md "Home fence"): with
no home or folders given, a lookup refuses the fenced home instead of listing the real ~/.claude*
folders and reading the machine config."""
import unittest

from kitlib import pluginsource as P
from kitlib.config import user
from kitlib.pluginsource import claude
from tests.home import fence_decoy
from tests.pluginsource import FakeProc, Kit


class AccountFoldersFenceTest(unittest.TestCase):
    def test_no_home_is_refused_an_explicit_home_passes(self):
        k = Kit(self)
        a, b = k.folders('.claude', '.claude-two')
        fence_decoy(self)
        with self.assertRaises(user.HomeFenced):
            claude.account_folders()
        with self.assertRaises(user.HomeFenced):
            claude.is_default(a)
        with self.assertRaises(user.HomeFenced):
            claude.Runner(claude='claude.cmd', run_proc=FakeProc()).run(['plugin', 'list'], a)
        self.assertEqual(claude.account_folders(k.home), [a, b])
        self.assertTrue(claude.is_default(a, k.home))


class PollFenceTest(unittest.TestCase):
    """The incident: pm_watch's and pm_start's plugin checks with folders left None."""

    def setUp(self):
        self.k = Kit(self)
        self.s = P.Settings(self.k.root, False, 'origin')
        (self.two,) = self.k.folders('.claude-two')
        self.k.seed_migrated([self.two])
        fence_decoy(self)

    def test_poll_without_folders_is_refused(self):
        with self.assertRaises(user.HomeFenced):
            P.poll(self.k.kit, self.s, None)
        self.assertEqual(P.poll(self.k.kit, self.s, None, folders=[self.two])[0][0][:22],
                         'pm-kit plugin behind k')

    def test_current_lines_without_folders_is_refused(self):
        with self.assertRaises(user.HomeFenced):
            P.current_lines(self.k.kit, self.s)
        self.assertIsInstance(P.current_lines(self.k.kit, self.s, folders=[self.two]), list)


if __name__ == '__main__':
    unittest.main()
