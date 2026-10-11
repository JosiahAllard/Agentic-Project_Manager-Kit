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

Not ported: per-piece red mapping and the hold after two reds, a custom step list, deferred steps, and owner-reservation reads beyond the lock's own exit 11. Tests: `tests/dailyfull`.
