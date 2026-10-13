---
name: subagent-dispatch
description: Use as a coordinator or any agent about to dispatch a Plan or other subagent whose report you will depend on. Covers which built-in agents cannot write files and why a subagent's output file is not a rescue path for a lost report.
---

# Dispatching subagents

- A `Plan` agent has no Write tool. Brief it to return the plan in its report body.
- A subagent's `tasks\<id>.output` is not a rescue path for a lost report (it can come back 0 bytes). Have the agent write its own file in its scratch folder, and name the path in the brief.
- A tool refusal or permission denial is stop-and-report (see the Never section of your agent file); do not route it through another tool or agent.
