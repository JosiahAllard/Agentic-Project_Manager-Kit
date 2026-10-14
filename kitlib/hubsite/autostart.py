"""The opt-in scheduled task that starts the hub server at sign-in (hub.py setup --autostart).

The same shape as the launcher's after-reboot task (kitlib.launcher.autostart): it runs as the
signed-in user, not elevated (LogonType Interactive, RunLevel Limited), at logon, so it also fires
when Windows signs the user in by itself after a restart. Its program is the pythonw.exe beside the
python that installs it, running the carrier kitlib.proc.spawn.CARRIER (tools/no_window.py beside
hub.py), which starts the console python.exe with a hidden console: `hub.py serve` with no window.
Nothing else is written: no machine config key, no token file. The same task name replaces an
installed task in place. A server that is already serving the store when the task fires makes the
task's own start refuse (the store lock), and the running one goes on.
Remove it with: Unregister-ScheduledTask -TaskName '<TASK_NAME>'.
"""
import os
import sys

from kitlib.hubsite.serve import ServeError
from kitlib.launcher import window
from kitlib.launcher.autostart import in_worktree, pythonw_beside
from kitlib.proc import spawn

TASK_NAME = 'PM kit hub server'
VERB = 'serve'


class AutostartError(ServeError):
    """The task cannot be installed from here, or registering it failed."""


def task_script(python, hub_py):
    """The PowerShell that registers the task to run `python hub_py serve` at logon with no
    console window: pythonw.exe beside `python` runs the carrier beside `hub_py`, which starts the
    console python.exe. `python` may be either of the two; neither is checked to exist here
    (install refuses a pythonw.exe that is not a file)."""
    folder = os.path.dirname(python)
    pythonw = pythonw_beside(python)
    console = (os.path.join(folder, 'python.exe')
               if os.path.basename(python).lower() == 'pythonw.exe' else python)
    carrier = os.path.join(os.path.dirname(hub_py), spawn.CARRIER)
    arg = '"%s" "%s" "%s" %s' % (carrier, console, hub_py, VERB)
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
    ])


def refusals(hub_py, python=None, isfile=os.path.isfile):
    """Why the task cannot be installed for `hub_py` with `python` (default: this one), each as one
    line; empty when it can. hub.py setup asks before it writes a token file."""
    python = python or sys.executable
    out = []
    if in_worktree(hub_py):
        out.append('install it from the main kit checkout, not a worktree: the task would run a '
                   'hub.py that goes when the worktree does (%s)' % hub_py)
    pythonw = pythonw_beside(python)
    if not isfile(pythonw):
        out.append('no pythonw.exe beside %s (looked for %s): the task runs it so that no console '
                   'window opens at sign-in' % (python, pythonw))
    return out


def install(hub_py, python=None, whatif=False, run=spawn.run, isfile=os.path.isfile):
    """Register the task; the lines it says. whatif: the script and why a real install would
    refuse, registering nothing (so it runs from a worktree too). Refused with nothing
    registered from a kit worktree, or when the pythonw.exe the task would run is not a file."""
    python = python or sys.executable
    text = task_script(python, hub_py)
    why = refusals(hub_py, python, isfile)
    if whatif:
        return ['Would run in PowerShell:', '  ' + text] + [
            'setup --autostart would refuse: %s' % r for r in why]
    if why:
        raise AutostartError('; '.join(why) + '; nothing registered')
    r = run(['powershell', '-NoProfile', '-NonInteractive', '-Command', text], capture_output=True,
            text=True, timeout=120)
    if r.returncode != 0:
        raise AutostartError('registering the task failed (powershell exit %d): %s'
                             % (r.returncode, (r.stderr or '').strip()[:300]))
    return ["Installed the scheduled task '%s': it starts the hub server at sign-in. Remove it "
            "with: Unregister-ScheduledTask -TaskName '%s'" % (TASK_NAME, TASK_NAME)]
