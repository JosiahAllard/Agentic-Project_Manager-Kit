"""The kit's tests. Importing this package puts the run on the fake git (docs/test-tools.md
"Fake git"): KIT_FAKE_GIT=1 unless the environment already says otherwise (kit_tests.py
--real-git sets 0), so kitlib.git.cmd answers git in process and the tool processes a test starts
inherit the same. Classes marked tests.realgit.real_git (the integration tier) run on real git.

The guard below makes the unit tier's promise checkable: while KIT_FAKE_GIT is 1, a process
this package is imported into that starts git raises instead of starting it.

The home fence (docs/test-tools.md "Home fence"): kitlib.config.user.fence_home() records the home
folder this process starts with, and from then on kitlib.config.user.home_dir() refuses it
(HomeFenced), so a test that forgets to give a home or account folders fails instead of reading the
real ~/.claude* and machine config folders. The tools a test starts do not import this package, so
kitlib.config.user.fence_env() also puts the fence's marker (KIT_HOME_FENCE, that home folder) in
the environment unless it is already there (kit_tests.py marks its units; a nested run keeps the
outer run's): a kit process that inherits it refuses that folder too, and the profile folder
Windows reports. A child given a hand-built environment without the marker is not fenced.

The time fence (docs/flaky-tests.md): KIT_TIME_FENCE=1 unless the environment already says
otherwise, so kitlib.clock.budget gives its ceiling for a degrade budget (kitlib.agentstart's) and a
loaded machine makes a test slower, not different. The tool processes a test starts inherit it.

The home fence has one exception, real_install_config(): the scrub tests check the kit's files against
this install's own names, which only the real machine config holds. It reads the machine config
under the home recorded below, before the fence, and returns only the install-name keys
(install_config).

The console hook (kitlib.proc.console, docs/daily-full.md "The schedule"): `import kitlib` comes
first, so a test process that has no console gets one with no window before its first start, and
no test opens a console window whatever it starts and however (tests/proc/test_console.py)."""
import os
import sys

import kitlib  # noqa: F401  (installs the CreateProcess hook before anything starts)

os.environ.setdefault('KIT_FAKE_GIT', '1')

from kitlib import clock as _kclock  # noqa: E402
from kitlib.config import user as _kuser  # noqa: E402  (after KIT_FAKE_GIT is set)

os.environ.setdefault(_kclock.FENCE_ENV, '1')   # the time fence: degrade budgets are the ceiling

_REAL_HOME = os.path.expanduser('~')    # read by real_install_config() only
_kuser.fence_env(os.environ)            # the marker the children of this process inherit
_kuser.fence_home()


INSTALL_PM_START_KEYS = ('sessions_file', 'repos', 'hub_url')


def install_config(cfg):
    """The install-name keys of a machine config `cfg` (user_config's dict) and nothing else:
    {'scrub_patterns', 'machine_path_folders', 'pm_start': the INSTALL_PM_START_KEYS it sets
    (kitlib.pmstart known_repos reads sessions_file and repos), 'cap': coordinator_start's cap,
    else conductor_start's}. These are what tests/scrub/test_generic.py install_names,
    tests/shipped.py install_patterns and tests/scrub/test_builtin.py configured_folders read."""
    pm = cfg['pm_start'] or {}
    start = cfg['coordinator_start'] or cfg['conductor_start'] or {}
    return {'scrub_patterns': cfg['scrub_patterns'],
            'machine_path_folders': cfg['machine_path_folders'],
            'pm_start': {k: pm[k] for k in INSTALL_PM_START_KEYS if k in pm},
            'cap': start.get('cap')}


def real_install_config():
    """The home fence's one exception: (install_config of the real machine config, None), or
    (None, why) when there is no machine config file. It reads the machine config file under the
    home recorded before the fence, explicitly and read-only, and returns only the install-name
    keys; a malformed config raises ConfigError."""
    loc = _kuser.locate(_REAL_HOME)
    if loc.state == _kuser.NONE:
        return None, 'no machine config file (%s)' % loc.display()
    return install_config(_kuser.user_config(location=loc)), None


def _is_git(argv):
    if isinstance(argv, (str, bytes)):
        argv = os.fsdecode(argv).split()
    if not argv:
        return False
    return os.path.basename(os.fsdecode(os.fspath(argv[0]))).lower() in ('git', 'git.exe')


def _no_git(event, args):
    if event != 'subprocess.Popen' or os.environ.get('KIT_FAKE_GIT') != '1':
        return
    exe, argv = args[0], args[1]
    if _is_git(argv) or (exe is not None and _is_git([exe])):
        shown = argv if isinstance(argv, (str, bytes)) else list(argv)
        raise RuntimeError('unit test started git on the fake git tier (KIT_FAKE_GIT=1): %r; '
                           'route it through kitlib.git.cmd, or mark the class '
                           'tests.realgit.real_git' % (shown,))


sys.addaudithook(_no_git)
