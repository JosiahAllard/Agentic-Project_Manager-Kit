"""The PM-repo pointer: the machine-wide registry value that names the PM repository's work tree
(docs/kit-json.md "The PM repo pointer").

It is `HKLM\\SOFTWARE\\pm-kit`, value `pm_repo`, a REG_SZ, read in the 64-bit registry view
(KEY_WOW64_64KEY) and straight from the registry, never from the environment. Only an
administrator can write it (tools/machine_setup.py --pm-repo, docs/machine-setup.md "PM repo
pointer"), so an agent cannot point the kit at another PM repository.

On Linux (docs/linux-support-plan.md slice 9) there is no registry: the value is one line of UTF-8
text in the file /etc/pm-kit/pointer, which only an administrator can write, else in the user's
<profile>/.config/pm-kit/pointer, the profile folder from the password database and never from
$HOME or $XDG_CONFIG_HOME (kitlib.platform.linux.paths.read_pointer). A machine file that is there
but bad is the answer: the user's file is then not read. There the value is an absolute path with
no drive.

The value is the absolute path of the PM repository's main or data work tree, never a pm/<slug>
worktree: an existing folder that is the top of a git work tree; when its repository's common dir
holds kit-pm-data.json (kitlib.pmdata), the work tree must be on the recorded data branch, and with
no record it must be the main checkout.

Under the kit's tests (kitlib.config.user.fenced) the registry is never read: the value is the
environment's FENCE_ENV, and with none there is no pointer.

The machine config is read from the PM repository it names (kitlib.config.user.locate), and
preflight's `pointer` line reports it.
"""
import collections
import os

import kitlib.platform
from kitlib import pmdata
from kitlib.config import user
from kitlib.git import refs, repo
from kitlib.platform.linux import paths as linux_paths

KEY_PATH = r'SOFTWARE\pm-kit'
KEY_NAME = r'HKLM' + '\\' + KEY_PATH
VALUE = 'pm_repo'
FENCE_ENV = 'KIT_PM_REPO_FENCE'

OK, NONE, BAD = 'OK', 'NONE', 'BAD'

# `state` OK, NONE or BAD; `path` the value as stored (None when there is none or it cannot be
# read); `error` why it is BAD, else None; `record` the kitlib.pmdata Record in its repository's
# common dir for OK, None when it has none.
Pointer = collections.namedtuple('Pointer', 'state path error record', defaults=(None,))


def _winreg():
    import winreg
    return winreg


def read_raw():
    """`(value, None)`; `(None, None)` when there is no value; `(None, why)` when it cannot be
    read or is not a REG_SZ. Under the tests: FENCE_ENV's value, and the registry is not read."""
    if user.fenced():
        return os.environ.get(FENCE_ENV) or None, None
    return registry()


def registry():
    """read_raw's answer from the registry (on Linux, from the pointer files), fenced or not:
    only for the test fence's one exception (tests.real_install_config, which reads it before the
    fence); everything else calls read_raw."""
    if kitlib.platform.NAME == 'linux':
        return linux_paths.read_pointer()
    try:
        winreg = _winreg()
    except ImportError:
        return None, 'no registry on this platform'
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY_PATH, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value, kind = winreg.QueryValueEx(k, VALUE)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, 'cannot read %s %s: %s' % (KEY_NAME, VALUE, e)
    if kind != winreg.REG_SZ:
        return None, '%s %s is not a REG_SZ (type %s)' % (KEY_NAME, VALUE, kind)
    if not isinstance(value, str) or not value.strip():
        return None, '%s %s is empty' % (KEY_NAME, VALUE)
    return value, None


def _absolute(path):
    """Whether `path` is absolute as the seam's platform spells one: with a drive on Windows, a
    leading / on Linux (so a path with a drive is not), else as this host's os.path says."""
    if kitlib.platform.NAME == 'windows':
        return os.path.isabs(path) and bool(os.path.splitdrive(path)[0])
    if kitlib.platform.NAME == 'linux':
        return linux_paths.is_absolute(path)
    return os.path.isabs(path)


def check(path, classify=None, branch=None):
    """None when `path` can be the pointer's value; else why not, in words. `classify` and
    `branch` replace repo.classify and refs.current_branch (the guard reads both from files)."""
    return _check(path, classify, branch)[0]


def _check(path, classify=None, branch=None):
    """(check's answer, the kitlib.pmdata Record of path's repository or None)."""
    if not _absolute(path):
        return '%s is not an absolute path' % path, None
    if not os.path.isdir(path):
        return 'no folder at %s' % path, None
    place = (classify or repo.classify)(path)
    if place.kind not in (repo.MAIN, repo.LINKED):
        return '%s is not a git work tree (%s)' % (path, place.detail or place.kind), None
    if not repo.same_path(place.top, path):
        return '%s is not the top of its work tree (that is %s)' % (path, place.top), None
    try:
        record = pmdata.read(place.common)
    except pmdata.PmDataError as e:
        return str(e), None
    if record is None:
        if place.kind != repo.MAIN:
            return ('%s is a linked worktree and its repository has no PM data record (%s): name '
                    'the main checkout' % (path, pmdata.FILE_NAME)), None
        return None, None
    name, why = (branch or refs.current_branch)(path)
    if name is None:
        return 'cannot read the branch of %s: %s' % (path, why), None
    if name != record.branch:
        return ('%s is on %s, not the PM data branch %s: name the PM repository\'s data work tree'
                % (path, "branch '%s'" % name if name else 'a detached HEAD', record.branch)), None
    return None, record


def read(raw=None, classify=None, branch=None):
    """The Pointer on this machine. `raw` is read_raw's answer already taken; `classify` and
    `branch` are check's."""
    value, why = raw if raw is not None else read_raw()
    if why:
        return Pointer(BAD, None, why)
    if value is None:
        return Pointer(NONE, None, None)
    bad, record = _check(value, classify, branch)
    if bad:
        return Pointer(BAD, value, bad)
    return Pointer(OK, value, None, record)


def quoted(path):
    # A trailing backslash would escape reg's closing quote (a drive root would lose
    # its quote): double it.
    return '"%s"' % (path + '\\' if path.endswith('\\') else path)


def command(path):
    """The elevated `reg add` that sets the pointer to `path`."""
    return 'reg add "%s" /v %s /t REG_SZ /d %s /f /reg:64' % (KEY_NAME, VALUE, quoted(path))


def delete_command():
    """The elevated `reg delete` that removes the pointer."""
    return 'reg delete "%s" /v %s /f /reg:64' % (KEY_NAME, VALUE)
