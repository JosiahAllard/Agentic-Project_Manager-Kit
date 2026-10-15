"""What the launcher touches outside its own files, in one object so tests can fake it: the home
folder, the environment (process first, then the user's registry values), the process list, the
clock, opening a window, git check-ignore, a folder's git place, the home worktree (home.py), and
stopping a process by pid and creation time.
"""
import datetime
import os
import subprocess
import sys
import time

from kitlib.config import user as kuser
from kitlib.launcher import lookup, window


def _user_env(name):
    """A user environment variable from the registry (HKCU\\Environment), or None."""
    if sys.platform != 'win32':
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
            v, kind = winreg.QueryValueEx(k, name)
        v = expand_value(v, kind == winreg.REG_EXPAND_SZ, winreg.ExpandEnvironmentStrings)
    except OSError:
        return None
    return v if isinstance(v, str) and v else None


def expand_value(v, is_expand_sz, expand):
    """A REG_EXPAND_SZ value with its %VAR% references expanded, as .NET's
    GetEnvironmentVariable(name, 'User') returned it; any other value as it is."""
    return expand(v) if is_expand_sz and isinstance(v, str) else v


class Host(object):
    def __init__(self, home=None, environ=None):
        self.home = home or kuser.home_dir()
        self.environ = os.environ if environ is None else environ

    def env(self, name):
        """The process value, then the user's registry value: a process started before the owner
        set a user variable still holds the old environment."""
        return self.environ.get(name) or _user_env(name)

    def processes(self):
        return lookup.list_processes()

    def now(self):
        return datetime.datetime.now().astimezone()

    def open_window(self, text):
        window.open_window(text)

    def git_ignored(self, repo, path):
        """True (ignored), False (not), or None (git could not say). Run console-less (a scheduled
        task), git gets no console window (kitlib.proc.job.no_window_flags)."""
        from kitlib.proc import job
        try:
            r = subprocess.run(['git', '-C', repo, 'check-ignore', '-q', path],
                               capture_output=True, text=True, timeout=30,
                               creationflags=job.no_window_flags(0))
        except (OSError, subprocess.SubprocessError):
            return None
        return {0: True, 1: False}.get(r.returncode)

    def place(self, folder):
        """kitlib.git.repo.classify(folder): MAIN, LINKED, NONE or UNKNOWN, and the work tree's top."""
        from kitlib.git import repo
        return repo.classify(folder)

    def home_worktree(self, main, path, whatif=False):
        """kitlib.launcher.home.ensure: the lines it says, or a LaunchError."""
        from kitlib.launcher import home
        return home.ensure(main, path, whatif)

    def own_pid(self):
        return os.getpid()

    def ctime(self, pid):
        from kitlib.proc import process
        return process.proc_ctime(pid)

    def command_line(self, pid, ctime):
        """The command line of the process (pid, ctime), or None when it is gone or reused."""
        from kitlib.proc import process
        return process.command_line(pid, ctime)

    def terminate(self, pid, ctime):
        """'killed', 'gone', 'reused' or 'denied' (kitlib.proc.process.terminate_verified)."""
        from kitlib.proc import process
        return process.terminate_verified(pid, ctime)

    def uptime_minutes(self):
        if sys.platform != 'win32':
            return float('inf')
        import ctypes
        f = ctypes.windll.kernel32.GetTickCount64
        f.restype = ctypes.c_ulonglong
        return f() / 60000.0

    def sleep(self, seconds):
        time.sleep(seconds)
