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
  pm_repo(case, path=None, record=True)  a fenced PM repository for the rest of the test: `path`
                     (default a new temp folder) made a git main checkout on main, with a
                     kit-pm-data.json record (branch main, remote origin) when `record`, that the
                     PM-repo pointer names (kitlib.config.pointer FENCE_ENV, patched for the
                     test). It returns the path.
  machine_config(home, body, case=None)  write the machine config of a temp home: into the PM
                     repository the fenced pointer names (pm_repo; a new one on the first call of
                     the test), a dict split between its .pm-kit/config.json and local.json
                     (kitlib.config.user POLICY_KEYS and LOCAL_KEYS), or config.json's text as it
                     is. `pm_data` is derived from the pointer now: a body's pm_data.path becomes
                     the PM repository itself (pm_repo of that folder). `case` defaults to the
                     test temp_home made `home` for. After result_drive, a dict body without
                     `dev_drive` gets the test's.
  result_drive(case)  a new temp folder, the machine config's dev_drive for the rest of the test
                     (machine_config, into this test's PM repository or a new one): the gate's
                     result records (kitlib.gate.result) go under it, never under the real Dev
                     Drive. tests.gate.fixtures.GateRepoCase calls it before its test_env(), so
                     the gate and land tools a test starts write there too.
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
# The PM-repo pointer's value under the fence (kitlib.config.pointer.FENCE_ENV), spelled here too.
PM_REPO_FENCE_ENV = 'KIT_PM_REPO_FENCE'
# A record's root: any full commit id passes the record's check (kitlib.pmdata.check_fields).
RECORD_ROOT = '0' * 40

# {normcase home: the test case temp_home made it for}: machine_config's default case.
_CASES = {}


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
    key = os.path.normcase(os.path.normpath(home))
    _CASES[key] = case
    case.addCleanup(_CASES.pop, key, None)
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


def pm_repo(case, path=None, record=True):
    """`path` (default a new temp folder, removed after the test `case`) made a PM repository
    for the rest of the test: a git main checkout on main (git init, the fake git on the unit
    tier), with a kit-pm-data.json record (branch main, remote origin) when `record`, that the
    PM-repo pointer names (PM_REPO_FENCE_ENV, patched until the test ends). The remembered pointer
    answers are dropped now and when the test ends (kitlib.config.user.forget). Returns the path."""
    from kitlib import pmdata
    if path is None:
        path = tempfile.mkdtemp(prefix='kit-pm-repo-')
        case.addCleanup(gitfixture.rmtree, path)
    os.makedirs(path, exist_ok=True)
    if not os.path.isdir(os.path.join(path, '.git')):
        r = gitfixture.own_git(['init', '-q', '-b', 'main'], path)
        if r.returncode != 0:
            raise AssertionError('git init %s: %s' % (path, r.stderr))
    if record:
        pmdata.write(os.path.join(path, '.git'), 'main', 'origin', RECORD_ROOT)
    patch = mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: path})
    patch.start()
    case.addCleanup(patch.stop)
    kuser.forget()
    case.addCleanup(kuser.forget)
    case._kit_pm_repo = path
    return path


def machine_config(home, body, case=None):
    """Write the machine config of the temp home `home` into the PM repository the fenced pointer
    names: the one this test's pm_repo made, else a new one (pm_repo). `body` is a dict, split
    between <PM repo>/.pm-kit/config.json (every key but LOCAL_KEYS) and local.json (LOCAL_KEYS,
    removed when there are none), or config.json's text as it is (to plant one the kit cannot
    read; local.json removed). A dict's `pm_data` is not written: its `path` becomes the PM
    repository (pm_repo of that folder), since the kit derives pm_data from the pointer. `case`
    (default the test temp_home made `home` for) holds the pointer's patch. Returns the PM
    repository's .pm-kit folder."""
    import json
    if case is None:
        case = _CASES.get(os.path.normcase(os.path.normpath(home)))
        if case is None:
            raise ValueError('machine_config(%r): pass case=, this home is not from temp_home' % home)
    drive = getattr(case, '_kit_dev_drive', None)
    if isinstance(body, dict) and drive and 'dev_drive' not in body:
        body = dict(body, dev_drive=drive)
    if isinstance(body, dict) and isinstance(body.get('pm_data'), dict) and body['pm_data'].get('path'):
        body = dict(body)
        pm = pm_repo(case, body.pop('pm_data')['path'])
    else:
        pm = getattr(case, '_kit_pm_repo', None) or pm_repo(case)
    folder = os.path.join(pm, kuser.DIR_NAME)
    os.makedirs(folder, exist_ok=True)
    local = os.path.join(folder, kuser.LOCAL_FILE_NAME)
    if isinstance(body, str):
        policy, private = body, None
    else:
        policy = json.dumps({k: v for k, v in body.items() if k not in kuser.LOCAL_KEYS})
        private = {k: v for k, v in body.items() if k in kuser.LOCAL_KEYS} or None
    with open(os.path.join(folder, kuser.FILE_NAME), 'w', encoding='utf-8') as fh:
        fh.write(policy)
    if private is None:
        if os.path.exists(local):
            os.remove(local)
    else:
        with open(local, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(private))
    return folder


def result_drive(case):
    """A new temp folder (removed after the test `case`), the machine config's dev_drive for the
    rest of the test: written now (machine_config, an empty config otherwise) and kept by every
    dict body machine_config writes for the test later. A test with no temp_home yet gets one
    first: reading the machine config in the test's own process looks up the home, which the
    fence refuses when it is the real one. Returns the folder."""
    if os.path.normcase(os.path.normpath(os.environ.get('USERPROFILE', ''))) not in _CASES:
        temp_home(case)
    drive = tempfile.mkdtemp(prefix='kit-dev-drive-')
    case.addCleanup(gitfixture.rmtree, drive)
    case._kit_dev_drive = drive
    machine_config(None, {}, case=case)
    return drive


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
