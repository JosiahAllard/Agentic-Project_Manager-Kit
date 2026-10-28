# Coordinator profile: the kit

## Plan and instructions
- Reference docs: find them by topic with `tools/doc_index.py --find <topic>` (docs/doc-tools.md).

## Risk map, for review depth
docs/agent-profile.md, Risk map: its High paths get a fresh reviewer and a stronger worker; a branch changing a strict path lands alone (docs/gate.md "Strict paths").

## Agent types used here
The kit's roster: docs/ROSTER.md. The coordinator uses only the types listed there.

## Owner
- Owner decisions: removing a user-facing control; a schema change.
- The owner's explicit go: deploying; spending money; persisting data outside the repository;
  anything touching the owner's own files or accounts.
- The owner's typed go: a CI change to triggers, runners or deploy steps.
