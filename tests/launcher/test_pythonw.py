"""The pythonw.exe a scheduled task runs (kitlib/launcher/pythonw.py) and install-autostart's use of
it: the base install's, never a WindowsApps alias. Nothing is registered here: PowerShell is a fake."""
import os
import sys
import unittest

from kitlib.launcher import autostart
from kitlib.launcher import pythonw as P

SEP = chr(92)
ALIAS = SEP.join(['C:', 'Users', 'u', 'AppData', 'Local', 'Microsoft', 'WindowsApps', 'pythonw.exe'])
STORE = SEP.join(['C:', 'Program Files', 'WindowsApps', 'PythonSoftwareFoundation.Python.3.12_x', 'python.exe'])
BASE = SEP.join(['C:', 'Python', 'pythoncore-3.14-64'])


def never(*a, **k):
    raise AssertionError('PowerShell ran')


class Helper(unittest.TestCase):

    def test_a_windows_apps_folder_is_found_in_any_case_and_either_slash(self):
        self.assertTrue(P.under_windows_apps(ALIAS))
        self.assertTrue(P.under_windows_apps(STORE.lower()))
        self.assertTrue(P.under_windows_apps('/'.join(['C:', 'Users', 'u', 'AppData', 'Local', 'Microsoft',
                                                       'WINDOWSAPPS', 'pythonw.exe'])))
        self.assertFalse(P.under_windows_apps(os.path.join(BASE, 'pythonw.exe')))
        self.assertFalse(P.under_windows_apps(SEP.join(['C:', 'MyWindowsAppsCopy', 'pythonw.exe'])))

    def test_the_base_pythonw_is_in_the_base_prefix(self):
        self.assertEqual(P.base_pythonw(BASE), os.path.join(BASE, 'pythonw.exe'))
        self.assertEqual(P.base_pythonw(), os.path.join(sys.base_prefix, 'pythonw.exe'))

    def test_refusal_names_the_store_alias_before_a_missing_file(self):
        why = P.refusal(ALIAS, isfile=lambda p: True)
        self.assertIn('WindowsApps', why)
        self.assertIn(ALIAS, why)
        self.assertIn('no pythonw.exe at', P.refusal(os.path.join(BASE, 'pythonw.exe'), isfile=lambda p: False))
        self.assertIsNone(P.refusal(os.path.join(BASE, 'pythonw.exe'), isfile=lambda p: True))


class Install(unittest.TestCase):

    def test_with_no_python_named_the_task_runs_the_base_install_pythonw(self):
        """sys.executable is not asked: the pythonw and the console python are the base install's."""
        out = autostart.install('/kit/tools/launch.py', whatif=True, run=never, isfile=lambda p: True,
                                base_prefix=BASE)
        self.assertEqual(len(out), 2, out)
        self.assertIn("-Execute '%s' -Argument '\"%s\" \"%s\" \"/kit/tools/launch.py\" after-reboot'"
                      % (os.path.join(BASE, 'pythonw.exe'), os.path.join('/kit/tools', 'no_window.py'),
                         os.path.join(BASE, 'python.exe')), out[1])

    def test_the_default_is_this_interpreters_base_prefix(self):
        out = autostart.install('/kit/tools/launch.py', whatif=True, run=never, isfile=lambda p: True)
        self.assertIn("-Execute '%s'" % os.path.join(sys.base_prefix, 'pythonw.exe').replace("'", "''"), out[1])

    def test_a_windows_apps_python_is_refused_even_when_its_pythonw_is_a_file(self):
        with self.assertRaisesRegex(autostart.LaunchError, 'WindowsApps') as cm:
            autostart.install('/kit/tools/launch.py', python=STORE, run=never, isfile=lambda p: True)
        self.assertIn('nothing registered', str(cm.exception))
        out = autostart.install('/kit/tools/launch.py', python=STORE, whatif=True, run=never,
                                isfile=lambda p: True)
        self.assertEqual(len(out), 3, out)
        self.assertTrue(out[2].startswith('install-autostart would refuse: '), out[2])
        self.assertIn('WindowsApps', out[2])
        with self.assertRaisesRegex(autostart.LaunchError, 'WindowsApps'):
            autostart.install('/kit/tools/launch.py', run=never, isfile=lambda p: True,
                              base_prefix=os.path.dirname(STORE))


if __name__ == '__main__':
    unittest.main()
