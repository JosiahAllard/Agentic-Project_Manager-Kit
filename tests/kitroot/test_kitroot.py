"""kitlib.kitroot: the kit checkout a module is in, and the lock tool under it."""
import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import kitroot, platform
from tests import gitfixture

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
JUNCTIONS = hasattr(__import__('_winapi'), 'CreateJunction')


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


class Command(unittest.TestCase):
    KIT = 'C:/some kit'

    def run_on(self, name, *args):
        with mock.patch.object(platform, 'NAME', name):
            return kitroot.command('gate', *args, kit=self.KIT)

    def test_windows_names_python(self):
        self.assertEqual(self.run_on('windows'), 'python "C:/some kit/tools/gate.py"')
        self.assertEqual(self.run_on('windows', 'D:\\wt'),
                         'python "C:/some kit/tools/gate.py" -C "D:/wt"')

    def test_linux_names_python3(self):
        self.assertEqual(self.run_on('linux'), 'python3 "C:/some kit/tools/gate.py"')
        self.assertEqual(self.run_on('linux', 'D:\\wt'),
                         'python3 "C:/some kit/tools/gate.py" -C "D:/wt"')


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


@unittest.skipUnless(JUNCTIONS, 'needs NTFS junctions (_winapi.CreateJunction)')
class LockToolThroughAJunction(unittest.TestCase):
    """A kitlib reached through a junction: a copy of kitroot.py in `real/kitlib`, loaded from its
    file under another spelling, as a module imported through the link is."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix='kit-kitroot-'))
        self.addCleanup(gitfixture.rmtree, self.base)
        self.real = os.path.join(self.base, 'real')
        os.makedirs(os.path.join(self.real, 'kitlib'))
        shutil.copyfile(kitroot.__file__, os.path.join(self.real, 'kitlib', 'kitroot.py'))

    def junction(self, target, link):
        import _winapi
        os.makedirs(os.path.dirname(link), exist_ok=True)
        _winapi.CreateJunction(target, link)
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))

    def write_tool(self):
        tool = os.path.join(self.real, 'tools', 'machine_lock.py')
        os.makedirs(os.path.dirname(tool))
        with open(tool, 'w') as f:
            f.write('# never run\n')
        return tool

    def load(self, kitlib):
        spec = importlib.util.spec_from_file_location('kitroot_through_a_link',
                                                      os.path.join(kitlib, 'kitroot.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_a_linked_kitlib_finds_the_tool_beside_the_real_one(self):
        tool = self.write_tool()
        other = os.path.join(self.base, 'other', 'kitlib')
        self.junction(os.path.join(self.real, 'kitlib'), other)
        got = self.load(other).lock_tool()
        self.assertTrue(os.path.isfile(got), got)
        self.assertTrue(os.path.samefile(got, tool), got)

    def test_a_linked_checkout_keeps_the_path_as_spelled(self):
        self.write_tool()
        link = os.path.join(self.base, 'link')
        self.junction(self.real, link)
        got = self.load(os.path.join(link, 'kitlib')).lock_tool()
        self.assertEqual(os.path.normcase(got), os.path.normcase(os.path.join(link, 'tools', 'machine_lock.py')))

    def test_no_tool_either_way_names_the_spelled_place(self):
        other = os.path.join(self.base, 'other', 'kitlib')
        self.junction(os.path.join(self.real, 'kitlib'), other)
        got = self.load(other).lock_tool()
        self.assertEqual(os.path.normcase(got),
                         os.path.normcase(os.path.join(self.base, 'other', 'tools', 'machine_lock.py')))

    def test_a_named_kit_is_not_resolved(self):
        self.write_tool()
        other = os.path.join(self.base, 'other', 'kitlib')
        self.junction(os.path.join(self.real, 'kitlib'), other)
        kit = os.path.join(self.base, 'other')
        self.assertEqual(self.load(other).lock_tool(kit), os.path.join(kit, 'tools', 'machine_lock.py'))


if __name__ == '__main__':
    unittest.main()
