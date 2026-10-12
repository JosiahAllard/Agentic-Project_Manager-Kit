"""The scheduled task that runs after-reboot at sign-in (docs/launcher.md "Autostart").

It runs as the signed-in user, not elevated (LogonType Interactive, RunLevel Limited): Windows
signs the user in by itself after a restart, so it still fires right after a reboot, and the
windows open in the user's own desktop session. A task at startup (S4U) would get the full admin
token, so every session and agent would run elevated. The same task name replaces an installed
task in place, and the task under its old name, OLD_TASK_NAME, is unregistered in the same script
(else both run after a reboot and start every session twice; one release, removed after the
switch, W2b). Remove it with: Unregister-ScheduledTask -TaskName '<TASK_NAME>'.
"""
import os
import sys

from kitlib.launcher import window
from kitlib.launcher.settings import LaunchError
from kitlib.proc import spawn

TASK_NAME = 'Claude coordinators after reboot'
OLD_TASK_NAME = 'Claude conductors after reboot'   # unregistered by install (removed after the switch, W2b)


def task_script(python, launch_py):
    """The PowerShell that registers the task to run `python launch_py after-reboot` with no
    console window of its own: the task's program is the pythonw.exe beside `python`, running the
    carrier kitlib.proc.spawn.CARRIER (tools/no_window.py, beside `launch_py`), which starts the
    console python.exe with a hidden console (kitlib.proc.spawn.carry). The windows after-reboot
    opens are its own, as before. `python` may be either of the two; neither is checked to exist."""
    folder = os.path.dirname(python)
    pythonw = os.path.join(folder, 'pythonw.exe')
    console = (os.path.join(folder, 'python.exe')
               if os.path.basename(python).lower() == 'pythonw.exe' else python)
    carrier = os.path.join(os.path.dirname(launch_py), spawn.CARRIER)
    arg = '"%s" "%s" "%s" after-reboot' % (carrier, console, launch_py)
    return '; '.join([
        "$ErrorActionPreference = 'Stop'",
        "$user = \"$env:USERDOMAIN\\$env:USERNAME\"",
        '$action = New-ScheduledTaskAction -Execute %s -Argument %s' % (window.q(pythonw),
                                                                        window.q(arg)),
        '$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user',
        '$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited',
        '$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries '
        '-StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)',
        'Register-ScheduledTask -TaskName %s -Action $action -Trigger $trigger -Principal $principal '
        '-Settings $settings -Force | Out-Null' % window.q(TASK_NAME),
        'if (Get-ScheduledTask -TaskName %s -ErrorAction SilentlyContinue) { Unregister-ScheduledTask '
        '-TaskName %s -Confirm:$false }' % (window.q(OLD_TASK_NAME), window.q(OLD_TASK_NAME)),
    ])


def in_worktree(path):
    """True when path lies under a .claude/worktrees folder: a worktree that is removed after it lands."""
    parts = [p.lower() for p in os.path.normpath(os.path.abspath(path)).replace('\\', '/').split('/')]
    return any(parts[i] == '.claude' and parts[i + 1] == 'worktrees' for i in range(len(parts) - 1))


def install(launch_py, python=None, whatif=False, run=spawn.run):
    """Register the task; the lines it says. whatif: print the script, register nothing. Refused
    from a kit worktree: the task would point at a launch.py that goes when the worktree does."""
    if in_worktree(launch_py):
        raise LaunchError('install-autostart from the main kit checkout, not a worktree: %s' % launch_py)
    text = task_script(python or sys.executable, launch_py)
    test = 'python "%s" after-reboot --test' % launch_py
    if whatif:
        return ['Would run in PowerShell:', '  ' + text]
    r = run(['powershell', '-NoProfile', '-NonInteractive', '-Command', text], capture_output=True, text=True,
            timeout=120)
    if r.returncode != 0:
        raise RuntimeError('registering the task failed (powershell exit %d): %s'
                           % (r.returncode, (r.stderr or '').strip()[:300]))
    return ["Installed '%s' and unregistered '%s' if it was there. Test it without rebooting (starts only a "
            "throwaway 'autostart-test' session):" % (TASK_NAME, OLD_TASK_NAME), '  ' + test]
