"""The retired scheduled task that ran after-reboot at sign-in (docs/launcher.md "Autostart").

The hub server is the only thing that restarts agents now: its coord-restart job restarts the
coordinators and, once after a boot, runs launch.py after-reboot for the plain sessions and the PM
(kitlib.hubjobs.coord_restart). The hub's own sign-in task (kitlib.hubsite.autostart) starts the
hub. install-autostart registers nothing and says so; a task installed before stays until the
owner removes it: Unregister-ScheduledTask -TaskName '<TASK_NAME>' (Windows), or the Linux job
LINUX_JOB (kitlib.platform.linux.schedule). TASK_NAME and LINUX_JOB stay so such a leftover is
still known as the kit's (kitlib.pmstart.consoletasks).
"""
import os

from kitlib.host import registry
from kitlib.launcher.settings import LaunchError

TASK_NAME = 'Claude coordinators after reboot'
LINUX_JOB = 'coordinators-after-reboot'
RETIRED = ("install-autostart is retired: the hub restarts agents (docs/launcher.md \"Hub restart\"), "
           "started at sign-in by its own task (hub.py setup --autostart); nothing registered. Remove a "
           "task installed before with: Unregister-ScheduledTask -TaskName '%s' -Confirm:$false" % TASK_NAME)


def in_worktree(path):
    """True when path lies under a host's agent worktrees folder (hosts.json; .claude/worktrees on
    Claude): a worktree that is removed after it lands."""
    parts = [p.lower() for p in os.path.normpath(os.path.abspath(path)).replace('\\', '/').split('/')]
    for folder in registry.union('worktrees_directory'):
        want = folder.lower().split('/')
        if any(parts[i:i + len(want)] == want for i in range(len(parts) - len(want) + 1)):
            return True
    return False


def install(launch_py=None, whatif=False):
    """Retired: always refuses (LaunchError RETIRED), registers nothing and runs nothing."""
    raise LaunchError(RETIRED)
