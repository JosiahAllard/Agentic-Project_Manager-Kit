---
name: coordinator-dispatch
description: Use as a coordinator before any dispatch - writing a brief, choosing the role and tier, pulling the record, a creative (art, 3D, sound) brief, assigning shared resources or a measurement, requesting a deep-worker session, resuming agents that all died at once, and the daily prune and weekly tool-set upkeep runs. Load skill subagent-dispatch too for the dispatch traps.
---

# Coordinator: dispatch

Dispatch traps (built-in planners that cannot write, output files are no rescue path): skill `subagent-dispatch`. Roles are named bare here; your host's instructions say how a role name becomes its dispatch name.

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
- A change to `.kit.json` or any other owner-only path (`--find kit-json`, key `owner.paths`) is
  never dispatched to a builder: it goes to the owner, through the PM.

## The brief

- Holds: role; the owner's decisions where they matter, as a paraphrase plus the ruling id; verified facts; **exact files and sections
  to read**; scope + files *not* theirs + their owners; shared resources assigned; test gate (final, on the exact tip: `gate.py --full` for a strict path, else the optimized default gate once the gate config names smoke steps);
  report wanted; no land, merge or push; for code work the entry files and symbols you know
  (path:Symbol) - a brief with no paths costs the worker about 35 calls. Brief names doc topics
  to look up, never a pasted rule. Where the repo has a brief template (`--find brief`), follow it.
- Every agent starts with `python <kit>/tools/preflight.py -C <worktree>`. Briefs point at `python <kit>/tools/codemap.py -C <worktree>`
  and `python <kit>/tools/doc_index.py -C <worktree> --section "<heading>"` before any search; agents use their search
  and file-read tools, not shell grep, cat or sed. Review (analyst) and lander briefs name
  `python <kit>/tools/review_pack.py -C <repo> <branch>` as their first call.
- In a large file the brief names the sections or line ranges to read; the agent reads ranges (codemap or a search first), never the whole file, unless it is rewriting all of it.
- Every coding brief states its risk level and why (path in the risk map, or the reason it is
  higher); unlisted is not a reason - list routine paths at their real level.
- Briefs to the docs-writer role give facts as keyword lines in the entry format
  `ID | status | area | keywords | ref`, 300 chars at most, not paragraphs to transcribe. Decision
  logs split into more per-area files as they fill.

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

- Art that is code (skill `specialty-tech-art`), scripted 3D props (skill `specialty-3d-assets`)
  and scripted character rigs (skill `specialty-rigging`) go to senior-worker at least; sound
  (skill `specialty-sound`; the agent cannot hear, so its report ends in a question you put to the
  owner) to worker at least, senior-worker for design; never weak-worker. They work via the
  owner's local models, against an art direction the owner has decided or, where none exists, by
  proposing strongly different looks for the owner to pick; none hand-sculpts; rigs and their test
  animations are scripted (skill `specialty-rigging`). An asset generation cannot supply -> the
  owner, not another agent.
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

- **Prune daily.** Start each day with `python <kit>/tools/doc_index.py -C <repo> --stale`: confirm (touch it),
  rewrite or delete each line, in ONE docs-writer commit. A new `.md`, shard or section adds its
  index line in the same commit, or the doc-budget check refuses the branch.
- Durable facts -> the repo's docs, landed like any docs change, never in the host's config
  folder; session state -> your state note (`<agent temp>/<repo slug>-coordinator/state-<session>.md`). Worktrees are created and removed as the
  profile says.
- **Stale worktrees.** A B or C tree in `--classify` whose work is stale (superseded, ruled out, its
  record closed): mark it, `python <kit>/tools/worktree_remove.py -C <repo> <branch> --mark-stale
  "<why>"` (only a clean, unlanded, unclaimed tree; `--classify` offers the line for those and
  notes why the others take none). The nightly sweep then abandons it under `archive/<slug>` once it is idle 24 h, unless
  it changes first; `<branch> --unmark-stale` clears the mark. A branch with no worktree is
  abandoned by hand, `<branch> --branch --abandon "<why>"`, on the PM's word.
- **Grow the tool set.** Weekly, run the repetition report on the repo
  (`<kit>/optional/usage/repetition_report.py`): repeated shell shapes, or a scratch script
  written twice, become lines in the repo's `docs/ops/tool-requests.md`; build the top one as
  ordinary work (`tools/` is High risk).
