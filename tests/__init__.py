"""The kit's tests. Importing this package puts the run on the fake git (docs/test-tools.md
"Fake git"): KIT_FAKE_GIT=1 unless the environment already says otherwise (kit_tests.py
--real-git sets 0), so kitlib.git.cmd answers git in process and the tool processes a test starts
inherit the same. Classes marked tests.realgit.real_git (the integration tier) run on real git.

The guard below makes the unit tier's promise checkable: while KIT_FAKE_GIT is 1, a process
this package is imported into that starts git raises instead of starting it.

The home fence (docs/test-tools.md "Home fence"): kitlib.config.user.fence_home() records the home
folder this process starts with, and from then on kitlib.config.user.home_dir() refuses it
(HomeFenced), so a test that forgets to give a home or account folders fails instead of reading the
real ~/.claude* and machine config folders.

The fence has one exception, real_scrub_patterns(): the shipped-text scrub tests check the kit's
files against this install's own names, which only the real machine config's `scrub_patterns`
holds. It reads that one key from the home recorded below, before the fence, and nothing else."""
import os
import sys

os.environ.setdefault('KIT_FAKE_GIT', '1')

from kitlib.config import user as _kuser  # noqa: E402  (after KIT_FAKE_GIT is set)

_REAL_HOME = os.path.expanduser('~')    # read by real_scrub_patterns() only
_kuser.fence_home()


def real_scrub_patterns():
    """The home fence's one exception: (patterns, None) with the real machine config's
    `scrub_patterns` list, else (None, why) when the config file is absent or sets no patterns.
    It reads the machine config under the home recorded before the fence, explicitly and
    read-only, and returns that one key; a malformed config raises ConfigError."""
    loc = _kuser.locate(_REAL_HOME)
    if loc.state == _kuser.NONE:
        return None, 'no machine config file (%s)' % loc.display()
    pats = _kuser.user_config(location=loc)['scrub_patterns']
    if not pats:
        return None, 'the machine config (%s) sets no scrub_patterns' % loc.display()
    return list(pats), None


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
