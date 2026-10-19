---
name: subagent-dispatch
description: Use as a coordinator or any agent about to dispatch a subagent (a kit role or the host's built-in planner or explorer) whose report you will depend on. Covers briefing it, the worktree it writes in, which agents cannot write files, and why a host's output file is not a rescue path for a lost report.
---

# Dispatching subagents

1. Dispatch only where the task, the repo's rules or your role allow it; name the role, scope, worktree, branch, shared resources and validation in the brief.
2. A writer gets the worktree you prepared and owns it: dispatch it into that worktree, never into a second checkout the host creates on its own (the host would confine its writes there and deny its first edit).
3. Reviewer, researcher and the host's built-in explorer or planner stay read-only. A built-in planner may have no file-write tool at all: brief it to return the plan in its report body.
4. Ask for the report in the agent's result, plus a file the agent writes itself in its scratch folder when the report is long or must survive; name that path in the brief.
5. A host's own task-output file is not a rescue path for a lost report (it can come back 0 bytes).
6. Before resuming or replacing a failed agent, look at what it left running: jobs, lock runs, ports, its worktree's state.

## Never

- Route a classifier refusal or permission denial through another tool, agent, shell or session: it is stop-and-report. A kit-guard (hook) refusal: follow its `Instead:` line and retry.
- Assume one host's built-in agents, tools or file names exist on another.

Details: skill coordinator-dispatch (briefs, roles, tiers).
