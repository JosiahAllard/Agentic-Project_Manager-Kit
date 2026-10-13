---
name: session-handoff
description: Use when the PM tells you to "checkpoint and stand down" for an account hand-off (coordinator or long-running session), when a coordinator winds down a finished work set, and when you are the successor session started after one. Covers writing the state note, the exact ready reply, and the successor's verify-then-act start.
---

# Account hand-off

The PM moves long-running sessions between Claude accounts and recycles coordinators whose context is stale or whose work set is finished. The old session saves state and goes idle; the PM's launcher starts the successor.

## Old session (told to checkpoint and stand down)

1. Dispatch nothing new. A landing already running finishes; start no other.
2. Write your state file (the path the repo's profile names; coordinators usually `docs/ops/state.local.md` in the repo). Open items, holds and the next step only; landed history goes in the repo's records (landing notes), not the note. Include: what is running; branches; worktrees and where each is; every LAND verdict as branch, tip, reviewer and range (the successor lands them all at once); pending owner items; sequencing notes.
3. For every running agent, name its branch and worktree. Running agents report to you, the old session, not to the successor. After an account hand-off the successor may wait for a report only if you stay up for it; after a coordinator wind-down (the PM stops you once the successor has checked in) the successor never waits for the old session's agents: it re-dispatches from each branch once the agent there has stopped.
4. Detached background runs (machine lock runs, containers) keep going. Record each id so the successor can find them.
5. Reply to the PM exactly `ready to hand off: <state file path>` (after a work-set wind-down: `ready to restart: <state file path>`).
6. Then go idle and start nothing. Your window stays open until the PM stops it after your successor checks in, or the owner closes it.

## Successor (started with "read <state file>")

1. Read the state file (and your brief, if one is named).
2. Verify its claims before acting: `git ls-remote` for branches and main, process checks for agents and runs, `python ${CLAUDE_PLUGIN_ROOT}/tools/machine_lock.py status` for lock runs.
3. Before re-dispatching work on any worktree, confirm the old session's agents there have stopped (no process with that worktree as cwd, no file changes for 5 minutes). If one is still running, wait and tell the PM.
4. Check in with the PM, then act on the note.

## Recycle

Never mid-landing, never during a live session or an exclusive window.

1. The PM sends "checkpoint and stand down", or the coordinator winds down its finished work set unasked; either way it writes its state note and replies `ready to hand off: <state file>` or `ready to restart: <state file>` (steps above).
2. The PM runs its launcher's hand-off for that session (`launch.py handoff`, `--what-if` first; `--recycle` for a restart). The successor is named by the launcher.
3. The successor reads the state note, verifies it, and confirms to the PM that it did.
4. The old session lands nothing after it replies ready. Once the successor has checked in, the PM stops the old session and closes its window, and tells the owner; the successor lands the LAND verdicts the note lists. It stops only that exact session.
