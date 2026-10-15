"""The pythonw.exe a scheduled task runs (docs/launcher.md "Autostart", docs/worktree-remove.md
"The scheduled task", docs/daily-full.md "The schedule").

It is the one in the running interpreter's base install (`sys.base_prefix`), never the one a PATH
lookup finds: with the Microsoft Store or install-manager alias first on PATH, `Get-Command pythonw`
gives `...\\Microsoft\\WindowsApps\\pythonw.exe`, an app execution alias, not the real install.
A venv's `sys.base_prefix` is the install it was made from. A path under a `WindowsApps` folder is
refused all the same (`refusal`): a Python that lives there (a Store install) has no pythonw a
task should run.
"""
import os
import sys

NAME = 'pythonw.exe'
STORE_FOLDER = 'windowsapps'


def under_windows_apps(path):
    """True when a folder of `path` is named WindowsApps (any case)."""
    parts = os.path.normpath(path).replace('/', '\\').lower().split('\\')
    return STORE_FOLDER in parts


def base_pythonw(base_prefix=None):
    """The pythonw.exe in `base_prefix` (default: this interpreter's sys.base_prefix)."""
    return os.path.join(base_prefix or sys.base_prefix, NAME)


def refusal(pythonw, isfile=os.path.isfile):
    """Why a task must not run `pythonw`, or None when it may."""
    if under_windows_apps(pythonw):
        return ('%s is under WindowsApps (the Microsoft Store alias or a Store install), which a '
                'scheduled task must not run: run this with a python from python.org or the Python '
                'install manager' % pythonw)
    if not isfile(pythonw):
        return 'no pythonw.exe at %s' % pythonw
    return None
