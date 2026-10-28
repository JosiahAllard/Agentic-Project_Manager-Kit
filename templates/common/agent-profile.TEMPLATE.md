# Agent profile: <repo>

Read by the kit's agents section by section; they find their sections by these headings, so keep
every heading (write "Not applicable." under one that does not apply). Owner-only: the coordinator
drafts it, marks every inference, and the owner approves and commits it. Point to the repository's
own docs and tools rather than copying them; keep machine values in config, not here.

## Branches and worktrees
Branch naming, where worktrees go and how they are made, how to commit, and which worktrees an
agent may remove (only its own). For a sync (skill branch-sync): the untracked generated files the
base may now track, which make git refuse the merge and may be deleted first, and only those (a
Godot repo: the `.uid` files of scripts the agent does not own); or "None.".

## Build and test
The gate command and what it covers, faster commands for one area, and how red-first is done.

### Known flakes
Tests that fail without a code cause, and how to tell a flake from a failure.

## Test runner
The tiers, the pass string to look for, and what a report should contain.

## Landing
Whether landing is standing owner-approved work, who gives the go, the landing command, its
success line and exit code, how to verify the push, what to do on each failure verdict, and
whether batches are allowed.

## Shared resources that must be serialised
Locks, ports, devices, accounts and anything else two agents must not use at once, and how each
is taken and released. Never pre-empt a holder.
- Default, kept unless the owner deletes it: pages, renders and clips an agent wrote outside the
  repository are deleted by the owner only.

## Risk map
High-risk paths (a change there gets a stronger worker and a fresh reviewer) and low-risk ones.

## Moves and renames
How to move or rename without changing behaviour.

## Records
Where the backlog lives, which agent writes which records, and what tracked files
must never contain.
- Conventions digest (the consultant reads it on start), where the repo has one: `docs/conventions-digest.md`.

## Art
What visual work exists, how a change is shown to the owner, or "Not applicable." (which drops the
defaults below). Which generator makes which asset. Each line is a default kept unless the owner
deletes it:
- Paid generation (an API key, a paid service, a credits node) needs the owner's approval for that
  specific task. Never sign up for, spend on, or reach for a shelved source. Shelved sources: <list, or none>.
- Never wire a new asset into the game, or replace an existing one, unless the brief says to:
  changing how the game looks or sounds is the owner's decision.
- Never change gameplay, physics or netcode to make something look or sound better: report the
  conflict.
- Generation sessions are recorded as keyword lines in the repo's `docs/ops/assets.md` (create it on the first session), failures included.
- Asset policy: <where the repository keeps it>; anything it does not yet allow is the owner's decision.

## Audio
What audio work exists and how it is judged, or "Not applicable." (which drops the defaults
below). Which generator makes which sound. Each line is a default kept unless the owner deletes it:
- Paid generation needs the owner's approval for that specific task. Never sign up for, spend on,
  or reach for a shelved source. Shelved sources: <list, or none>.
- Never wire a new sound into the game, or replace an existing one, unless the brief says to:
  changing how the game sounds is the owner's decision.
- Never change gameplay, physics or netcode to make something sound better: report the conflict.
- Generation sessions are recorded as keyword lines in the repo's `docs/ops/assets.md` (create it on the first session), failures included.
- Asset policy: <where the repository keeps it>; anything it does not yet allow is the owner's decision.
- Text to speech: announcer lines only.
- Local music generation: music only (menu, match and victory themes, stingers over a few seconds),
  never sound effects.
