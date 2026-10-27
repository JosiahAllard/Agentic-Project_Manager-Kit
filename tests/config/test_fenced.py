"""kitlib.config.user.fenced(): whether this process is under the kit's tests (docs/test-tools.md
"Home fence"). It is true when fence_home() was called or the environment carries the fence's
marker, and it asks Windows nothing: a real run pays one global read and one environment read."""
import os
import unittest
from unittest import mock

from kitlib.config import user

# The marker, spelled here so that a renamed constant fails a test instead of following it.
MARK = 'KIT_HOME_FENCE'


class Fenced(unittest.TestCase):
    def process(self, recorded, marker):
        """For the rest of the test: fence_home()'s record is `recorded` (None: never called) and
        the marker is `marker` (None: unset). Asking Windows for the profile folder fails the
        test."""
        for patch in (mock.patch.object(user, '_FENCED_HOME', recorded),
                      mock.patch.object(user, '_PROFILE', None),
                      mock.patch.object(user, '_system_profile',
                                        side_effect=AssertionError('Windows was asked')),
                      mock.patch.dict(os.environ)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(MARK, None)
        if marker is not None:
            os.environ[MARK] = marker

    def test_fence_home_alone_fences(self):
        self.process(None, None)
        self.assertIs(user.fenced(), False)
        user.fence_home()               # records a path; reads nothing
        self.assertIsNotNone(user._FENCED_HOME)
        self.assertIs(user.fenced(), True)

    def test_the_marker_alone_fences(self):
        self.process(None, os.path.join(os.path.abspath(os.sep), 'no-such-home'))
        self.assertIs(user.fenced(), True)

    def test_an_empty_marker_is_no_marker(self):
        self.process(None, '')
        self.assertIs(user.fenced(), False)

    def test_neither_is_unfenced(self):
        self.process(None, None)
        self.assertIs(user.fenced(), False)

    def test_both_fence(self):
        home = os.path.join(os.path.abspath(os.sep), 'no-such-home')
        self.process(user._home_key(home), home)
        self.assertIs(user.fenced(), True)


if __name__ == '__main__':
    unittest.main()
