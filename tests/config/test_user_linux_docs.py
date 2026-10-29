"""The machine config's schema strings name the Linux case beside the Windows one."""
import unittest

from kitlib.config import user


class LinuxDocTest(unittest.TestCase):

    def test_temp_dir_names_the_linux_system_temp(self):
        self.assertIn('on Linux /tmp', user.SCHEMA['temp_dir'].doc)
