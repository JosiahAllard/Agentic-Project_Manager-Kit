"""The PM-repo pointer: the machine-wide registry value that names the PM repository's work tree
(docs/kit-json.md "The PM repo pointer").

It is `HKLM\\SOFTWARE\\pm-kit`, value `pm_repo`, a REG_SZ, read in the 64-bit registry view
(KEY_WOW64_64KEY) and straight from the registry, never from the environment. Only an
administrator can write it (tools/machine_setup.py --pm-repo, docs/machine-setup.md "PM repo
pointer"), so an agent cannot point the kit at another PM repository.

The value is the absolute path of the PM repository's main or data work tree, never a pm/<slug>
worktree: an existing folder that is the top of a git work tree; when its repository's common dir
holds kit-pm-data.json (kitlib.pmdata), the work tree must be on the recorded data branch, and with
no record it must be the main checkout.

Under the kit's tests (kitlib.config.user.fenced) the registry is never read: the value is the
environment's FENCE_ENV, and with none there is no pointer.

Nothing reads the pointer for config yet: preflight's `pointer` line reports it.
"""
import collections
import os

from kitlib import pmdata
from kitlib.config import user
from kitlib.git import refs, repo

KEY_PATH = r'SOFTWARE\pm-kit'
KEY_NAME = r'HKLM' + '\\' + KEY_PATH
VALUE = 'pm_repo'
FENCE_ENV = 'KIT_PM_REPO_FENCE'

OK, NONE, BAD = 'OK', 'NONE', 'BAD'

# `state` OK, NONE or BAD; `path` the value as stored (None when there is none or it cannot be
# read); `error` why it is BAD, else None.
Pointer = collections.namedtuple('Pointer', 'state path error')


def _winreg():
    import winreg
    return winreg


def read_raw():
    """`(value, None)`; `(None, None)` when there is no value; `(None, why)` when it cannot be
    read or is not a REG_SZ. Under the tests: FENCE_ENV's value, and the registry is not read."""
    if user.fenced():
        return os.environ.get(FENCE_ENV) or None, None
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


def check(path):
    """None when `path` can be the pointer's value; else why not, in words."""
    if not os.path.isabs(path) or not os.path.splitdrive(path)[0]:
        return '%s is not an absolute path' % path
    if not os.path.isdir(path):
        return 'no folder at %s' % path
    place = repo.classify(path)
    if place.kind not in (repo.MAIN, repo.LINKED):
        return '%s is not a git work tree (%s)' % (path, place.detail or place.kind)
    if not repo.same_path(place.top, path):
        return '%s is not the top of its work tree (that is %s)' % (path, place.top)
    try:
        record = pmdata.read(place.common)
    except pmdata.PmDataError as e:
        return str(e)
    if record is None:
        if place.kind != repo.MAIN:
            return ('%s is a linked worktree and its repository has no PM data record (%s): name '
                    'the main checkout' % (path, pmdata.FILE_NAME))
        return None
    branch, why = refs.current_branch(path)
    if branch is None:
        return 'cannot read the branch of %s: %s' % (path, why)
    if branch != record.branch:
        return ('%s is on %s, not the PM data branch %s: name the PM repository\'s data work tree'
                % (path, "branch '%s'" % branch if branch else 'a detached HEAD', record.branch))
    return None


def read():
    """The Pointer on this machine."""
    value, why = read_raw()
    if why:
        return Pointer(BAD, None, why)
    if value is None:
        return Pointer(NONE, None, None)
    bad = check(value)
    if bad:
        return Pointer(BAD, value, bad)
    return Pointer(OK, value, None)


def _quoted(path):
    # A trailing backslash would escape reg's closing quote (a drive root would lose
    # its quote): double it.
    return '"%s"' % (path + '\\' if path.endswith('\\') else path)


def command(path):
    """The elevated `reg add` that sets the pointer to `path`."""
    return 'reg add "%s" /v %s /t REG_SZ /d %s /f /reg:64' % (KEY_NAME, VALUE, _quoted(path))


def delete_command():
    """The elevated `reg delete` that removes the pointer."""
    return 'reg delete "%s" /v %s /f /reg:64' % (KEY_NAME, VALUE)
