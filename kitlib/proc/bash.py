"""A bare `bash` in a command the kit starts names Git Bash, never WSL's bash.

Windows' CreateProcess looks in System32 before PATH, so Popen(["bash", ...]) starts
System32\\bash.exe (the WSL launcher) even when Git's bash is first on PATH; cmd.exe (shell=True)
searches PATH, where System32 usually comes first too. Measured 2026-10-08: Popen(["bash", "-c",
...]) from a Git Bash shell, Git's usr/bin first on PATH, printed WSL's
"execvpe(/bin/bash) failed".

resolve(cmd, shell) swaps a bare leading bash (bash or bash.exe, any case, no folder) for
git_bash(), or raises BareBashError (an OSError, so callers' "could not start the command" path
reports it) when none is found. A bash named by a path is left alone.
"""
import os
import re
import shutil

NAMES = ("bash", "bash.exe")
# A shell string whose first word is a bare bash, quoted or not.
_LEAD = re.compile(r'^(\s*)(?:"(bash(?:\.exe)?)"|(bash(?:\.exe)?))(?=\s|$)', re.IGNORECASE)


class BareBashError(OSError):
    """A bare bash with no Git Bash to send it to."""


def _is_system(path, env):
    """Is path a bash that starts WSL (under the Windows folder, or an app-execution alias)?"""
    p = os.path.normcase(os.path.abspath(path))
    if os.sep + "windowsapps" + os.sep in p:
        return True
    for k in ("SystemRoot", "windir"):
        for root in (env.get(k), os.environ.get(k)):
            if not root:
                continue
            r = os.path.normcase(os.path.abspath(root)).rstrip(os.sep) + os.sep
            if p.startswith(r):
                return True
    return False


def git_bash(env=None, which=shutil.which, isfile=os.path.isfile):
    """The bash.exe a bare bash should run, or None: the first on PATH that is not WSL's, else
    Git's own (bin/bash.exe, then usr/bin/bash.exe, beside the git found on PATH)."""
    env = os.environ if env is None else env
    for d in (env.get("PATH") or "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d:
            continue
        cand = os.path.join(d, "bash.exe")
        if isfile(cand) and not _is_system(cand, env):
            return cand
    git = which("git", path=env.get("PATH"))
    if git:
        d = os.path.dirname(os.path.abspath(git))
        for _ in range(3):                  # Git/cmd, Git/bin, Git/mingw64/bin
            d = os.path.dirname(d)
            for rel in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
                cand = os.path.join(d, *rel)
                if isfile(cand) and not _is_system(cand, env):
                    return cand
    return None


def refusal():
    return BareBashError(
        "a bare `bash` would start WSL's bash (Windows looks in System32 before PATH), and no "
        "Git Bash was found on PATH or beside git: name a bash.exe by its full path, or install "
        "Git for Windows")


def resolve(cmd, shell=False, env=None, finder=None):
    """cmd with a bare leading bash replaced by Git Bash (a list's first item, or a string's first
    word); any other cmd as given. Raises BareBashError when it is bare and none is found."""
    find = finder or (lambda: git_bash(env))
    if isinstance(cmd, (list, tuple)):
        if cmd and isinstance(cmd[0], str) and cmd[0].strip().lower() in NAMES:
            b = find()
            if not b:
                raise refusal()
            return [b] + list(cmd[1:])
        return cmd
    if isinstance(cmd, str):
        m = _LEAD.match(cmd)
        if m:
            b = find()
            if not b:
                raise refusal()
            return '%s"%s"%s' % (m.group(1), b, cmd[m.end():])
    return cmd
