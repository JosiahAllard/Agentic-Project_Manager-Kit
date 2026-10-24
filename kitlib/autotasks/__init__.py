"""kitlib.autotasks: the automatic tasks the hub schedules (the hub's `schedules` collection,
kitlib.hubsite.api SCHEDULE_TASKS) run on this machine.

- due: when a schedule is due (daily or weekly at a local HH:MM; a missed slot runs once).
- tick: one tick of tools/auto_tasks.py: the hub's schedules, the state file and its lock, the
  fixed task table (TASKS) and the detached start.

The Windows scheduled task that runs the tick every 15 minutes is a machine-setup extra
(kitlib.machinesetup.extras, AUTO_TASK).
"""
