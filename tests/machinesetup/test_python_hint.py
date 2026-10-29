"""The interpreter machine setup's printed hints name: python3 on Linux, python on Windows."""
import unittest
from unittest import mock

from kitlib.machinesetup import common, managed, pointer


class PythonHintTest(unittest.TestCase):

    def on(self, name):
        return mock.patch('kitlib.platform.NAME', name)

    def test_managed_command_per_platform(self):
        with self.on('linux'):
            self.assertTrue(managed.command('/opt/kit').startswith('python3 /opt/kit/tools/'))
        with self.on('windows'):
            self.assertTrue(managed.command('C:\\kit').startswith('python C:/kit/tools/'))

    def test_pointer_command_per_platform(self):
        with self.on('linux'):
            self.assertTrue(pointer.command('/opt/kit', '/srv/pm').startswith('python3 /opt/kit/tools/'))
        with self.on('windows'):
            self.assertTrue(pointer.command('C:\\kit', 'C:\\pm').startswith('python C:/kit/tools/'))

    def test_python_name(self):
        with self.on('linux'):
            self.assertEqual(common.python_name(), 'python3')
        with self.on('windows'):
            self.assertEqual(common.python_name(), 'python')


if __name__ == '__main__':
    unittest.main()
