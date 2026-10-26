# Coordinator profile: <repo>

Read by the `pm-kit:coordinator` on start; the coordinator's alone. Keep every heading (write "Not
applicable." under one that does not apply). Owner-only: the coordinator drafts it, marks every
inference, and the owner approves and commits it. It holds this repository's own rules for the
coordinator; point to the repository's docs and tools rather than copying them.

## Plan and instructions
Where the roadmap, backlog and reference docs live (the doc-index topics that find them), how a
brief names the exact section or row it implements, and who sets priorities and which decisions
only the owner makes.
- Tool requests (the weekly upkeep run, skill coordinator-dispatch "Upkeep"): `docs/ops/tool-requests.md`.

## Risk map, for review depth
Which paths or areas are high risk (a branch there gets a fresh reviewer and a stronger worker)
and which are low, and what a review of each must check.

## Agent types used here
The agent types this repository actually has, which kind of work goes to which, and any type that
must not be used here. The coordinator uses only the types listed.

## Owner
How the owner is reached, which questions go to the owner and through whom, and how often the owner
wants a digest. The list below is the coordinator's: work on it is not the coordinator's to decide or
land alone. Each line is a default the repository keeps unless the owner deletes it:
- Owner decisions: removing a user-facing control; a schema change.
- The owner's explicit go: deploying; spending money; persisting data outside the repository;
  anything touching the owner's own files or accounts.
- The owner's typed go: a CI change to triggers, runners or deploy steps.
