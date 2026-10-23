---
name: session-handoff
description: Use when the PM tells you to "checkpoint and stand down" for an account hand-off (coordinator or long-running session), when a coordinator winds down a finished work set, and when you are the successor session started after one. Covers writing the state note, the exact ready reply, and the successor's verify-then-act start.
---

# Session hand-off

The PM moves long-running sessions between accounts and recycles coordinators whose context is stale or whose work set is finished. The old session saves state and goes idle; the PM's launcher starts the successor. Never mid-landing, never during a live session or an exclusive window.

Old session (told to checkpoint and stand down, or winding down a finished set):
1. Dispatch nothing new and submit no landing. A landing already submitted finishes on its own.
2. Write the state note at the path the repo profile names (coordinators: `<agent temp>/<repo slug>-coordinator/state-<session>.md`, outside the repo). Open items only, facts apart from assumptions: brief and scope; what is running; branches, worktrees and where each is; relevant hashes; active jobs and log paths; locks, ports and resources held; every LAND verdict as branch, tip, reviewer and range; pending owner items; open blockers; the next concrete step. Landed history goes in the repo's records, not the note.
3. Name each submitted landing (branch, tip) and each running agent's branch and worktree. Those agents report to you, not the successor.
4. Record the id of each detached run (machine lock runs, submitted landings, containers): they keep going.
5. Reply to the PM exactly `ready to hand off: <state file path>`. After a work-set wind-down: `ready to restart: <state file path>` while work remains (items still building); `ready to stop: <state file path>` when nothing is startable, nothing is building and the landing queue is clear.
6. Go idle. Land nothing more; your window stays open until the PM stops it after the successor checks in (after `ready to stop:`, with no successor).

Successor (started with "read <state file>"):
1. Read the state note, and your brief if one is named.
2. Before anything else, read `python <kit>/tools/land.py -C <main checkout> --queue` and the land result records of every landing the note lists (`python <kit>/tools/gate.py wait-result <branch> --tip <sha> --kind land --timeout 0 -C <main checkout>`: a final record prints its `RESULT` line; exit 7, none yet: the landing is still queued or running). Then verify each claim before acting: `git ls-remote <remote>` for branches and main; process, job and port checks for agents and runs; `python <kit>/tools/machine_lock.py status` for lock runs. A landing still queued or running: start its `wait-result` again as a background command; never resubmit it.
3. Before re-dispatching on any worktree, confirm the old session's agents there have stopped (no process with that worktree as cwd, no file change for 5 minutes); one still running: wait and tell the PM. After a wind-down never wait for their reports: re-dispatch from the branch once the agent has stopped. Then run `python <kit>/tools/worktree_remove.py -C <main checkout> --classify --apply` and take up its B and C trees; never land, reuse or remove an L tree. Each item goes to an end, as the owner ruled: landed, blocked on its record with the reason and its branch kept (`backlog.py block`), or released with its worktree removed. Never park halfway; pull nothing new while one sits stopped without a block.
4. Check in with the PM, then act on the note; queue every LAND verdict it lists that is not already submitted.

PM: run the launcher's hand-off for that session, `python <kit>/tools/launch.py handoff --session <key> --state <state file> --what-if` first, then without `--what-if` (`--recycle` for a restart). Once the successor has checked in, stop exactly that old session (`python <kit>/tools/launch.py stop --name <name>`) and tell the owner. On `ready to stop:` start no successor: stop that session the same way, and start it again (`launch.py start`) when its repo gets startable work, an owner answer or an approval.

## Never

- Assume a session record that is gone means its process ended.
- Take a saved note as permission to send or act on anything it does not authorise.

Details: `<kit>/docs/launcher.md`.
