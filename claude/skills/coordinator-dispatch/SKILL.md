---
name: coordinator-dispatch
description: Use as a coordinator before any dispatch - writing a brief, choosing the agent and model, pulling the record, a creative (art, 3D, sound) brief, assigning shared resources or a measurement, requesting a deep-worker session, resuming agents that all died at once, and the daily prune and weekly tool-set upkeep runs. Load skill subagent-dispatch too for the harness traps.
---

# Coordinator: dispatch

Harness traps (Plan cannot write, output files are no rescue path): skill `subagent-dispatch`.

## Before the agent starts

- **Pull at dispatch**, before the agent starts, never after it finishes: the offer moves, and
  finished work on an unpulled item that dropped out of the offer cannot close without a
  pre-emption. Keep the pulled record's scope to the files the branch will touch (it blocks other
  work); narrow it on main through a quick records landing right after the plan. A session-owned
  item is pulled with its session: `python tools/backlog.py -C <worktree> --session <key> pull <ID>`.
  Price before pull: an unpriced record refuses the pull, so a price set on an unlanded branch
  must land first, or the brief has the worker set the same agent price in its worktree before
  its pull.
- **Bundle related work**: pull other open records in the same area onto the same branch, keeping
  it reviewable and never mixing a high-risk change with unrelated work.
- 1 workstream = 1 branch = 1 isolated worktree, per profile. A brief that names a worktree you
  prepared is dispatched without `isolation`: the harness otherwise confines writes to its own
  worktree and denies the first edit.
- Writes in another session's worktree (a sync, a commit, a stuck run) are made by the agent that
  owns it, or by a fresh agent dispatched into that worktree; never by you with `-C` from outside.
  Only for new work: a refused command is never rerouted this way.
- Non-trivial: plan read-only -> your approval or correction -> build.

## The brief

- Holds: role; the owner's words verbatim where they matter; verified facts; **exact files and sections
  to read**; scope + files *not* theirs + their owners; shared resources assigned; test gate;
  report wanted; no land, merge or push; for code work the entry files and symbols you know
  (path:Symbol) - a brief with no paths costs the worker about 35 calls. Brief names doc topics
  to look up, never a pasted rule. Where the repo has a brief template (`--find brief`), follow it.
- Every agent starts with `python tools/preflight.py`. Briefs point at `python tools/codemap.py`
  and `doc_index.py --section "<heading>"` before any search; agents use Grep and Read, not shell
  grep, cat or sed. Reviewer and lander briefs name `python tools/review_pack.py <branch>` as
  their first call, where the repo has it.
- Every coding brief states its risk level and why (path in the risk map, or the reason it is
  higher); unlisted is not a reason - list routine paths at their real level.
- Briefs to `records` and `spec-writer` give facts as keyword lines in the entry format
  `ID | status | area | keywords | ref`, 300 chars at most, not paragraphs to transcribe. Decision
  logs split into more per-area files as they fill.

## Which agent, which model

Read `roster.md` in this skill's folder (beside this file): the agent types and what each is for,
the cheap/strong split, the coding ladder, model and effort rules, `deep-worker` requests, the
`consultant`, `researcher` and pass/fail-check routing.

## Creative dispatches

- `tech-artist` (art that is code), `asset-smith` (scripted 3D), `sound-smith` (sound; it cannot
  hear, so its report ends in a question you put to the owner): via the owner's local models,
  against an art direction the owner has decided or, where none exists, `tech-artist` proposes
  strongly different looks for the owner to pick; none hand-sculpts, rigs or animates. An asset
  generation cannot supply -> the owner, not another agent.
- Every creative brief carries this block, filled in:

  > FEEL: one sentence. REFERENCES: one to three (a game and what specifically, or an image path,
  > including any inspiration images the owner sent). AVOID: one line. SCOPE OF CHOICE: the agent may
  > propose X, may not change Y.

  Cannot fill FEEL: ask the owner once, through the PM, using the agent's options as the question.
- Anything new with a creative choice runs options first, one dispatch per step: Step 0, a cheap
  sheet of 5-10 very different designs (or designs from the owner's inspiration images) -> the
  owner narrows -> about three takes -> the owner picks, through the PM -> a polish dispatch on
  the pick only. A fix to something the owner already approved skips to the fix.
- Visual output: as the repo profile says; default a self-contained HTML (images inlined base64)
  in the agent's scratch folder, report leading with its absolute path. Never relay a bare
  worktree image path; an image nobody opened has not been reviewed.

## Shared resources and measurements

- Serialise what the profile says cannot be shared: test machine, database, ports, deploy target,
  lock file (`--find lock`). None given + collision possible -> stop and ask.
- Measurements get exclusivity: a run whose *number* will be quoted owns the resource - pause
  everyone, run, release. Design, arms and decision rule written before the first run; counts
  reported with sample size; the rule applied literally; any after-the-fact reading stated
  separately.
- GPU generation never overlaps an engine, network or render run.
- Private scratch folder per agent - shared file names get overwritten.
- Before trusting "nothing is running", look at the actual processes.

## While agents run

- Continue an agent 2-3 rounds per artefact, then reseed from a short written summary. A message
  queued to a busy agent may never be read: if a report shows something was missed, resend in full.
- A background run dies with its agent: tell its replacement to check logs first.
- Few long-running agents: each notification costs a turn over your whole context.
- **When agents die together** (an API error or usage limit stops every running agent at once):
  check shared resources first - orphaned processes, stale locks, ports, main vs remote - then
  resume each by continuing it with a short state summary: what is running, what died, what to
  rerun. Most valuable first. If a usage limit caused it, tell the PM and wait for its word
  before resuming.

## Upkeep

- **Prune daily.** Start each day with `python tools/doc_index.py --stale`: confirm (touch it),
  rewrite or delete each line, in ONE `records` commit. A new `.md`, shard or section adds its
  index line in the same commit, or the doc-budget check refuses the branch.
- Durable facts -> the repo's docs, landed like any docs change, never under `~/.claude`; session
  state -> `docs/ops/state.local.md`. `.claude/worktrees/` is unaffected: create and remove
  worktrees as the profile says.
- **Grow the tool set.** Weekly, run the repetition report on the repo (the kit's
  `optional/usage/repetition_report.py`, installed under `~/.claude/tools/usage/`): repeated shell shapes, or a scratch script written twice, become lines in the repo's
  `docs/ops/tool-requests.md`; build the top one as ordinary work (`tools/` is High risk).
