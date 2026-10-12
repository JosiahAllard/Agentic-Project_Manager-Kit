# daily_full

`tools/daily_full.py [-C DIR] [--plan | --summary] [--force] [--out DIR]` runs every non-manual gate step in full (each step's `daily_cmd` when it has one, else its `full_cmd`, else its `cmd`; docs/gate.md "Steps"), on `"daily_full".ref` (default `<remote>/<main branch>`), in a detached worktree of its own (default `<worktrees_dir>/daily-full`). Logic: `kitlib/dailyfull`.

- Opt-in: it refuses (exit 2) unless `.kit.json` "daily_full".enabled is true. The kit never schedules it. A repository that wants it daily registers the schedule itself.
- It moves only a clean linked worktree of its own. A worktree of `.` or the main checkout, a folder that is not a registered linked worktree, or a tree with uncommitted files is recorded red `infra` with the reason (for a dirty tree, the file names). Nothing is cleaned, reset or switched.
- One run at a time per tree. Before it fetches, a run takes the tree's lock (`kitlib/dailyfull/treelock.py`), an OS byte-range lock on `<git common dir>/kit-tree-<name>-<hash>.lock`, and it holds the lock until its result is written, on every exit. A second run finds the lock held and exits 5 with `daily: busy: held by pid N since T`. It does not touch the tree and records nothing. The system drops the lock of a holder that exits or is killed, so the next run takes over and says `took over`.
- The fetch and switch come before the machine-lock booking, not inside it. `runner.gated` takes the planned steps and loads the hook checks before it books, and both come from the tree at the ref. Only a run holding the tree's lock moves the tree, so a run waiting for its slot never moves a tree another run is gating.
- Each run fetches, puts the worktree at the ref, and loads the gate from the ref's own `.kit.json`. All steps then run in one slot of `"daily_full".kind` (default `daily-full`) through `kitlib.gate.runner.gated`, waiting up to `"daily_full".max_wait` seconds. A failing step does not stop the run.
- Every run writes `<out>/<stamp>/result.json`, where `<out>` defaults to `<gate log dir>/daily`. The fields are `verdict`, which is green, red or skipped; `sha`; `steps`; `red`, which lists the failing step names plus `infra` when the run could not start; `skipped`, the steps that did not run; `error`; and, for a skip, `reason` and `cause`. A run in which no step ran (every one SKIP) is red `infra`, never green.
- Unchanged commit: after the fetch, if the latest run that ran (skips do not count) is green at the ref's commit, the run is a recorded skip with cause `unchanged`, prints `daily: SKIPPED (unchanged) - ...` and exits 0. The recorded results under `<out>` are the state; there is no other file. A red latest run, or a green one at another commit, runs. `--force` runs regardless. `--summary` shows these skips as `skipped (unchanged since the last green run)`, not as a problem.
- A lock refusal is a recorded skip, not a red. The cause is one of busy, timeout, lock-unavailable, reserved or hold.
- `--summary` prints the status lines from the recorded runs:
  - the last verdict, with its failing steps;
  - the skips in a row;
  - STALE, when the newest attempt is over `"daily_full".stale_hours` old.

  As a preflight report hook it is `daily_full:summary_lines`.
- Exit codes: 0 green or skipped, 1 a step red, 2 usage or off, 4 the tree or its gate could not be readied (recorded red), 5 another run holds the tree (nothing recorded). A lock usage refusal, such as an unknown kind, is exit 2 and recorded red.

## The schedule

The kit registers no task for it (above); the repository's owner does. A task whose program is `python.exe` is a console program started by a process that has no console, and Windows gives such a program a new console and shows its window (`kitlib/proc/job.py`, "No console window from a parent without a console"); for a task that runs as the signed-in user, that window is on the desktop. For no window, the task's program is `pythonw.exe` and its first argument the carrier, `tools/no_window.py`:

```powershell
$py = (Get-Command pythonw).Source
Register-ScheduledTask -TaskName '<task name>' -Trigger (New-ScheduledTaskTrigger -Daily -At <HH:mm>) -Action (New-ScheduledTaskAction -Execute $py -Argument '"<kit>\tools\no_window.py" python "<kit>\tools\daily_full.py" -C "<main checkout>"' -WorkingDirectory '<main checkout>') -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) -User $env:USERNAME -RunLevel Limited
```

A task that is already registered keeps its trigger and settings and gets only the new action:

```powershell
$py = (Get-Command pythonw).Source
Set-ScheduledTask -TaskName '<task name>' -Action (New-ScheduledTaskAction -Execute $py -Argument '"<kit>\tools\no_window.py" python "<kit>\tools\daily_full.py" -C "<main checkout>"' -WorkingDirectory '<main checkout>')
```

- `tools/no_window.py PROGRAM [ARGS...]` starts PROGRAM, waits for it and exits with its exit code, so the task's last result is still `daily_full.py`'s (`kitlib.proc.spawn.carry`). It never prints. Its own exit codes: 2 when no PROGRAM is named, 127 when PROGRAM cannot be started.
- `python` as PROGRAM is the `python.exe` in the folder of the `pythonw.exe` that runs the carrier; any other PROGRAM is started as named.
- `pythonw.exe` has no console, so the carrier starts PROGRAM with `CREATE_NO_WINDOW`: a console with no window. Everything PROGRAM starts in turn shares that console, so the whole run opens none. Run from a console (`python tools/no_window.py ...`) it adds nothing and PROGRAM shares that console.
- `$py` must not be the Microsoft Store alias (a path under `WindowsApps`). The kit's own task lines refuse it (`kitlib/worktree/task.py` `NOT_THE_STORE_ALIAS`); these two lines do not check.
- The kit's own scheduled tasks use the same carrier: the worktree sweep (docs/worktree-remove.md "The scheduled task"), the agent temp sweep (docs/machine-setup.md "Extras") and the launcher's sign-in task (docs/launcher.md "Autostart").

Every start in the kit's own code follows one rule, so a kit program run with no console (a hook under `pythonw.exe`, a task without the carrier) opens no window for what it starts: it goes through `kitlib.proc.spawn` (`run`, `call`, `popen`) or `kitlib.proc.job` `Job.start`, which add `CREATE_NO_WINDOW` only when the process has no console and the caller chose no console of its own, or it passes `creationflags=` itself. `tests/proc/test_spawn_sites.py` reads the source and fails on a start that does neither and has no row, with its reason, in that test's `EXEMPT` table.

Tests: `tests/proc/test_spawn.py` (the flags and the carrier's logic), `tests/proc/test_no_window.py` (`SpawnWindowTest` and `CarrierTest` start a real `pythonw.exe` and read the child's `GetConsoleWindow()`), `tests/proc/test_spawn_sites.py`.

Not ported: per-piece red mapping and the hold after two reds, a custom step list, deferred steps, and owner-reservation reads beyond the lock's own exit 11. Tests: `tests/dailyfull`.
