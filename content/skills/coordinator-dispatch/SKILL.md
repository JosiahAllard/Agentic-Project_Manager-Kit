---
name: coordinator-dispatch
description: Use as a coordinator before any dispatch - writing a brief, choosing the role and tier, pulling the record, a creative (art, 3D, sound) brief, assigning shared resources or a measurement, requesting a deep-worker session, resuming agents that all died at once, and the daily prune and weekly tool-set upkeep runs. Covers the dispatch traps: briefing, the worktree, agents that cannot write files, lost reports.
---

# Coordinator: dispatch

Roles are named bare here; your host's instructions say how a role name becomes its dispatch name.

## Before the agent starts

- **Pull at dispatch**, before the agent starts, never after it finishes: the offer moves, and
  finished work on an unpulled item that dropped out of the offer cannot close without a
  pre-emption. Keep the pulled record's scope to the files the branch will touch (it blocks other
  work); narrow it on main through a quick records landing right after the plan. A session-owned
  item is pulled with its session: `python <kit>/tools/backlog.py -C <worktree> --session <key> pull <ID>`.
  Price before pull: an unpriced record refuses the pull, so a price set on an unlanded branch
  must land first. A worker never sets a price; if its pull is refused as unpriced it stops and
  reports to you.
- **Bundle related work**: pull other open records in the same area onto the same branch, keeping
  it reviewable and never mixing a high-risk change with unrelated work.
- 1 workstream = 1 branch = 1 worktree, per profile (`python <kit>/tools/new_worktree.py -C <repo> <branch>`
  where the profile names no other way; it picks the host's worktree folder). A brief that names a
  worktree you prepared is dispatched into it, without asking the host for an isolated worktree of
  its own: the host would otherwise confine writes to that one and deny the first edit.
- Writes in another session's worktree (a sync, a commit, a stuck run) are made by the agent that
  owns it, or by a fresh agent dispatched into that worktree; never by you with `-C` from outside.
  Only for new work: a refused command is never rerouted this way.
- Non-trivial: plan read-only -> your approval or correction -> build.
- Slicing a redesign or mockup-built record: walk the mockups into an element checklist on the umbrella (one line per sidebar entry, view, control, state, each owned by a slice); it closes when every element is ticked against a landed slice, not when every slice has landed (`<kit>/docs/backlog.md` "Umbrella records").
- A change to `.kit.json` or any other owner-only path (`--find kit-json`, key `owner.paths`) is
  never dispatched to a builder: it goes to the owner, through the PM.

## The brief

- Holds: role; the owner's decisions where they matter, as a paraphrase, no ruling id; verified facts; **exact files and sections
  to read**; scope + files *not* theirs + their owners; shared resources assigned; test gate (final: the gate `<kit>/docs/gate.md` "The final gate" names, on the exact tip);
  report wanted; no land, merge or push; for code work the entry files and symbols you know
  (path:Symbol) - a brief with no paths costs the worker about 35 calls. Brief names doc topics
  to look up, never a pasted rule. Where the repo has a brief template (`--find brief`), follow it.
- Every agent starts with `python <kit>/tools/preflight.py -C <worktree>`. Briefs point at `python <kit>/tools/codemap.py -C <worktree>`
  and `python <kit>/tools/doc_index.py -C <worktree> --section "<heading>"` before any search; agents use their search
  and file-read tools, not shell grep, cat or sed. Review (analyst) briefs name
  `python <kit>/tools/review_pack.py -C <repo> <branch>` as their first call.
- In a large file the brief names the sections or line ranges to read; the agent reads ranges (codemap or a search first), never the whole file, unless it is rewriting all of it.
- Every coding brief states its risk level and why (path in the risk map, or the reason it is
  higher); unlisted is not a reason - list routine paths at their real level.
- Briefs to the docs-writer role give facts as keyword lines in the entry format
  `ID | status | area | keywords | ref`, 300 chars at most, not paragraphs to transcribe. Decision
  logs split into more per-area files as they fill. The evidence and deletion rules for a brief or
  report that records findings: `specialty-docs` `doc-writing.md`, "Findings and doc edits".
- A brief for an owner-approved change tells the author to commit with `commit.py -m "<subject>" --model "<model name>" --owner-approval hub:A-n <path> ...` and leave the work tree at the approval's base; never to copy the approved content in first.

## Dispatch traps

- Dispatch only where the task, the repo's rules or your role allow it. The analyst and the host's built-in explorer or planner stay read-only; a built-in planner may have no file-write tool at all: brief it to return the plan in its report body.
- Ask for the report in the agent's result: a text report of about 40 lines at most, never a report file (the host refuses subagent report files). A host's own task-output file is not a rescue path for a lost report (it can come back 0 bytes).
- Before resuming or replacing a failed agent, look at what it left running: jobs, lock runs, ports, its worktree's state.
- Never hand a refusal to another agent to get round it (skill backlog-ops, "Refusals"); never assume one host's built-in agents, tools or file names exist on another.

## Which role, which tier

Read `roster.md` in this skill's folder (beside this file): the roles and what each is for, the
cheap/strong split, the coding ladder, tier and effort rules, deep-worker requests, the
consultant, research and pass/fail-check routing. The kit's roster table (each role's tier, and
each host's tier-to-model map) is `<kit>/docs/ROSTER.md`.

A task matching a specialty skill (a `specialty-*` folder in `<kit>/content/skills/`): the brief names the skill
("Load skill `specialty-sound` first") and the role, at least the minimum tier the skill's
`Dispatch:` line gives. The skill carries the know-how; the role still sets model, effort and
permissions.

## Creative dispatches

Briefing art, 3D, rig or sound work: read `creative.md` in this skill's folder (beside this file) first.

## Shared resources and measurements

- Serialise what the profile says cannot be shared: test machine, database, ports, deploy target,
  lock file (`--find lock`). None given + collision possible -> stop and ask.
- Measurements get exclusivity: a run whose *number* will be quoted owns the resource - pause
  everyone, run, release. Design, arms and decision rule written before the first run; counts
  reported with sample size; the rule applied literally; any after-the-fact reading stated
  separately.
- Private scratch folder per agent - shared file names get overwritten.
- Before trusting "nothing is running", look at the actual processes.

## While agents run

- Continue an agent a few rounds per artefact (the profile may set the count), then reseed from a short written summary. A message
  queued to a busy agent may never be read: if a report shows something was missed, resend in full.
- Review a hand-back at once and continue the same agent while its prompt cache is warm (the
  host's cache window; `<kit>/docs/ROSTER.md` names the role that asks for a longer one); later,
  continue anyway unless its context is long (the profile may set the size; strong- and deep-tier coders) or
  it is an analyst on a review, then start fresh from a short written summary; never park a large-context
  agent to resume later for a small fix.
- A background run dies with its agent: tell its replacement to check logs first.
- Few long-running agents: each notification costs a turn over your whole context.
- **When agents die together** (an API error or usage limit stops every running agent at once):
  check shared resources first - orphaned processes, stale locks, ports, main vs remote - then
  resume each by continuing it with a short state summary: what is running, what died, what to
  rerun. Most valuable first. If a usage limit caused it, tell the PM and wait for its word
  before resuming.

## Upkeep

The daily prune, stale worktrees and the weekly tool-set run: read `upkeep.md` in this skill's folder (beside this file).
