"""docs/test-tools.md "Home fence": pm_watch's state file falls back to the home folder only through
kitlib.config.user.home_dir(), so a test without LOCALAPPDATA meets the fence."""
import os
import unittest
from unittest import mock

from kitlib import pmwatch as W
from kitlib.config import user as kuser
from tests.home import fence_decoy


class DefaultStateFileFenceTest(unittest.TestCase):
    def test_no_localappdata_falls_back_to_the_fenced_home(self):
        fence_decoy(self)
        env = {k: v for k, v in os.environ.items() if k != 'LOCALAPPDATA'}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(kuser.HomeFenced):
                W.default_state_file()
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': r'D:\scratch\lad'}):
            self.assertEqual(W.default_state_file(),
                             os.path.join(r'D:\scratch\lad', 'pm_watch', 'state.json'))


if __name__ == '__main__':
    unittest.main()
