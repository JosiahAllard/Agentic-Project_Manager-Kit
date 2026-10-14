"""kitlib.kitroot: the kit checkout a module is in, and the lock tool under it."""
import os
import unittest

from kitlib import kitroot

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


class Root(unittest.TestCase):
    def test_root_is_the_checkout_this_kitlib_is_in(self):
        self.assertTrue(os.path.samefile(kitroot.root(), REPO), kitroot.root())
        self.assertTrue(os.path.isabs(kitroot.root()))
        self.assertTrue(os.path.isfile(os.path.join(kitroot.root(), 'kitlib', 'kitroot.py')))

    def test_root_does_not_follow_the_working_directory(self):
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(os.path.dirname(REPO))
        self.assertTrue(os.path.samefile(kitroot.root(), REPO), kitroot.root())


class LockTool(unittest.TestCase):
    def test_default_is_the_real_lock_tool_of_this_checkout(self):
        tool = kitroot.lock_tool()
        self.assertTrue(os.path.isfile(tool), tool)
        self.assertTrue(os.path.samefile(tool, os.path.join(REPO, 'tools', 'machine_lock.py')), tool)
        self.assertEqual(kitroot.lock_tool(None), tool)

    def test_a_named_kit_is_used_as_given(self):
        kit = os.path.join('C:' + os.sep, 'some kit', 'checkout')
        self.assertEqual(kitroot.lock_tool(kit), os.path.join(kit, 'tools', 'machine_lock.py'))

    def test_it_never_names_a_home_folder_link(self):
        for tool in (kitroot.lock_tool(), kitroot.lock_tool(os.path.join('C:' + os.sep, 'k'))):
            self.assertNotIn('.claude', tool.replace(os.sep, '/').split('/')[-3:], tool)
            self.assertEqual(tool.replace(os.sep, '/').split('/')[-2:], ['tools', 'machine_lock.py'])


if __name__ == '__main__':
    unittest.main()
