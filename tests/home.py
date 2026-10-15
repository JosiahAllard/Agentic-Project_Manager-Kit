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
  machine_config(home, body)  write the machine config under a temp home (a dict as JSON, or
                     the file's text as it is).
  child_home()       a temp folder to be the home of the processes tests start, for a helper that
                     builds a child's environment and has no test case to clean up with: one per
                     process, removed at exit.
"""
import atexit
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


_CHILD_HOME = []


def child_home():
    """A temp folder for a child's USERPROFILE and HOME: made on first use, and again when a
    runner has removed it with the unit's temp folder; removed at exit. Every test of this
    process shares it, so a test whose child writes under its home gives it one of its own
    (temp_home)."""
    if not (_CHILD_HOME and os.path.isdir(_CHILD_HOME[0])):
        home = tempfile.mkdtemp(prefix='kit-child-home-')
        atexit.register(gitfixture.rmtree, home)
        _CHILD_HOME[:] = [home]
    return _CHILD_HOME[0]


class TempHomeCase(unittest.TestCase):
    """Every test runs under temp_home: code that reads the machine config reads an empty one."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)


def machine_config(home, body):
    """Write the machine config under `home` (a temp home): `body` is a dict, written as JSON, or
    the file's text as it is (to plant one the kit cannot read)."""
    import json
    os.makedirs(os.path.join(home, kuser.DIR_NAME), exist_ok=True)
    with open(kuser.config_file(home), 'w', encoding='utf-8') as fh:
        fh.write(body if isinstance(body, str) else json.dumps(body))


def fence_decoy(case):
    """temp_home(case), fenced: home_dir() raises HomeFenced for it until the test ends."""
    home = temp_home(case, prefix='kit-fenced-home-')
    patch = mock.patch.object(kuser, '_FENCED_HOME', os.path.normcase(os.path.normpath(home)),
                              create=True)
    patch.start()
    case.addCleanup(patch.stop)
    return home


def no_machine_owner(case, email=None):
    """The machine config's owner_email is `email` (default: none) for the rest of the test
    `case`, with no home looked up: kitlib.config.user.owner_email is patched, so a Config whose
    .kit.json names no "owner".email reads it there (docs/kit-json.md "With no file"). A temp
    home would not do where the fence cannot prove a home temp (Windows names no profile)."""
    patch = mock.patch.object(kuser, 'owner_email', return_value=email)
    patch.start()
    case.addCleanup(patch.stop)
