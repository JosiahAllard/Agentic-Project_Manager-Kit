"""Temp homes for tests, under the home fence (docs/test-tools.md "Home fence").

  temp_home(case)    a new empty temp folder that USERPROFILE and HOME point at for the rest of
                     the test: kitlib.config.user.home_dir() returns it, so code that looks up the
                     home reads and writes there, never in the real home.
  fence_decoy(case)  temp_home(case), recorded as the fenced home: home_dir() refuses it as it
                     refuses the real one. A test of the fence itself uses it, so that the same
                     test run without the fence reads an empty temp folder, not the real home.
  TempHomeCase       a TestCase whose every test runs under temp_home (self.home). A subclass
                     with its own setUp calls super().setUp() first.
"""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.config import user as kuser
from tests import gitfixture


def temp_home(case, prefix='kit-home-'):
    """A new temp folder, the home of the rest of the test `case` (USERPROFILE and HOME)."""
    home = tempfile.mkdtemp(prefix=prefix)
    case.addCleanup(gitfixture.rmtree, home)
    patch = mock.patch.dict(os.environ, {'USERPROFILE': home, 'HOME': home})
    patch.start()
    case.addCleanup(patch.stop)
    return home


class TempHomeCase(unittest.TestCase):
    """Every test runs under temp_home: code that reads the machine config reads an empty one."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)


def fence_decoy(case):
    """temp_home(case), fenced: home_dir() raises HomeFenced for it until the test ends."""
    home = temp_home(case, prefix='kit-fenced-home-')
    patch = mock.patch.object(kuser, '_FENCED_HOME', os.path.normcase(os.path.normpath(home)),
                              create=True)
    patch.start()
    case.addCleanup(patch.stop)
    return home
