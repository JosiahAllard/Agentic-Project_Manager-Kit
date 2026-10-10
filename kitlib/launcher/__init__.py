"""kitlib.launcher: start, hand off and stop the PM's long-running Claude sessions, each in its own
minimized window with Remote Control (docs/launcher.md). tools/launch.py is its command line.

  settings   the `launcher` machine-config object (state folder, repos, account folders)
  accounts   which config folder an account launches with, its usage headroom, pick-account
  usage      every account's usage as lines (kitlib.pmwatch reads them)
  lookup     finding a running session by its Remote Control name
  window     the window's PowerShell, the PM_KIT_ROLE mark, opening the window
  start      a coordinator (or any agent) session, a deep-worker, a consultant
  stop       stopping one session and its window
  handoff    a successor on another account, or a recycle on the same one
  reboot     after a restart: every session in sessions.json, then the PM resumed
  autostart  the scheduled task that runs reboot at sign-in
  host       the outside world, faked by tests

Nothing here names a machine path, a drive, an account folder, an owner or a repository: each
comes from the machine config or the state files. Every launched session gets a PM_KIT_ROLE mark
(window.role_for), so no launched session counts as the owner's.
"""
