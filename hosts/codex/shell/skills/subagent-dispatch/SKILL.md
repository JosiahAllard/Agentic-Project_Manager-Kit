---
name: subagent-dispatch
description: Use as a coordinator or any agent about to dispatch a subagent (a kit role or the host's built-in planner or explorer) whose report you will depend on. Covers briefing it, the worktree it writes in, which agents cannot write files, and why a host's output file is not a rescue path for a lost report.
---

# Dispatching subagents

1. Dispatch only where the task, the repo's rules or your role allow it; name the role, scope, worktree, branch, shared resources and validation in the brief.
2. A writer gets the worktree you prepared and owns it: dispatch it into that worktree, never into a second checkout the host creates on its own (the host would confine its writes there and deny its first edit).
3. The analyst and the host's built-in explorer or planner stay read-only. A built-in planner may have no file-write tool at all: brief it to return the plan in its report body.
4. Ask for the report in the agent's result, plus a file the agent writes itself in its scratch folder when the report is long or must survive; name that path in the brief.
5. A host's own task-output file is not a rescue path for a lost report (it can come back 0 bytes).
6. Before resuming or replacing a failed agent, look at what it left running: jobs, lock runs, ports, its worktree's state.

## Bulk work to the helper

A builder may hand bulk menial work to the helper role (light tier; read and edit tools, no shell), only where `<kit>/docs/ROSTER.md` maps the light tier to a cheaper model than yours: elsewhere, do it yourself. Its start-up plus your check costs about 15-25k tokens (initial figure), so:
- Bulk read: a log or file over about 1,000 lines you need summarised, a search expected to touch 20+ files, a whole-tree listing. One section, a known path or a single grep stays with you.
- Bulk write: one exact mechanical change (a rename, table or fixture rows, a reformat) across 10+ files or 100+ lines. Anything smaller, or anything needing judgement, stays with you.
- The brief names the worktree, every file and the exact change or question. One writer per file: leave the listed files alone while it runs.
- You own the result: re-read every cited file:line a read claim you act on rests on, read the whole diff of a write, then gate and commit yourself. A test run is never handed to the helper; run it as builder step 8 says.

## Never

- Route a classifier refusal or permission denial through another tool, agent, shell or session: it ends that action; report it verbatim (your role file's Never section). A kit-guard (hook) refusal is not one: fix the form its Instead: line gives, then retry.
- Assume one host's built-in agents, tools or file names exist on another.

Details: skill coordinator-dispatch (briefs, roles, tiers).
