"""Temp homes for tests, under the home fence (docs/test-tools.md "Home fence").

  temp_home(case)    a new empty temp folder that USERPROFILE and HOME point at for the rest of
                     the test: kitlib.config.user.home_dir() returns it, so code that looks up the
                     home reads and writes there, never in the real home. PSModuleAnalysisCachePath
                     points under it too (gitfixture.ps_cache_env), so a real powershell started in
                     the test never writes its module cache into the cwd.
  fence_decoy(case)  temp_home(case), recorded as the fenced home: home_dir() refuses it as it
                     refuses the real one. A test of the fence itself uses it, so that the same
                     test run without the fence reads an empty temp folder, not the real home.
  TempHomeCase       a TestCase whose every test runs under temp_home (self.home). A subclass
                     with its own setUp calls super().setUp() first.
  unmarked_env(env)  a copy of `env` (default: os.environ) without the fence's marker (FENCE_ENV):
                     the environment of a child that must run as a real run does, unfenced. Its
                     home is the caller's business: a temp folder, unless the child only computes
                     the home's path.
"""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.config import user as kuser
from tests import gitfixture

# The marker the fence's children read (kitlib.config.user.FENCE_ENV), spelled here so that a
# renamed constant fails a test instead of following it.
FENCE_ENV = 'KIT_HOME_FENCE'


def unmarked_env(env=None):
    """A copy of `env` (default: os.environ) without the home fence's marker."""
    out = dict(os.environ if env is None else env)
    out.pop(FENCE_ENV, None)
    return out


def temp_home(case, prefix='kit-home-'):
    """A new temp folder, the home of the rest of the test `case` (USERPROFILE and HOME)."""
    home = tempfile.mkdtemp(prefix=prefix)
    case.addCleanup(gitfixture.rmtree, home)
    patch = mock.patch.dict(os.environ, dict(gitfixture.ps_cache_env(home), USERPROFILE=home, HOME=home))
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
