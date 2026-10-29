# Agent roster

The kit's plugin ships these agent definitions in `hosts/claude/shell/agents/`, rendered from `content/roles/`
(docs/content.md). Repo specifics live in the repo's `.claude/agent-profile.md` (every agent) and
`.claude/coordinator-profile.md` (coordinator only). The plugin registers each one under its prefix:
dispatch a row as `pm-kit:<row>` (`subagent_type`, `--agent`). A repo's own `.claude/agents/<row>.md`
is a separate agent named `<row>`; it does not replace `pm-kit:<row>`. A repo that ships its own
file lists that agent type in its `.claude/coordinator-profile.md` and is dispatched by the bare
name; every other row is dispatched as `pm-kit:<row>` (tests/agents/test_override.py,
tests/agents/test_dispatch_names.py). That bare-name dispatch is for the Agent tool only: the launcher
qualifies every bare kit agent name, so it always starts the kit's version, and a repo's own file for a
launched row (coordinator, consultant, deep-worker) cannot be started by the launcher under that name
(docs/launcher.md "The window").

The role list lives only in `content/roles/` (docs/multi-host-plan.md section 7, decision 17): a
role is added or removed with one file and a render. The tables between the `rendered` markers
below are written by `python tools/render_content.py --write` from the role files and each host's
tier map; never edit them by hand. To change a role's tier, effort or "use for" line, edit its
`tier:`, `effort:` or `summary:` front matter (docs/content.md "Role front matter") and render; the
render also rewrites that role's agent file on every host. `python tools/render_content.py` (and
`python tools/agent_roster.py`, which checks this file alone) lists any disagreement. `-` = no
effort key. Never `fable` unless the owner asks; override a dispatch only upward, and never the
lander's. The per-dispatch rules have one home, content/skills/coordinator-dispatch/roster.md,
"Choosing", written in tiers for every host: which reviews are raised to the strong tier (from the
review pack's printed risk groups and the strict landing paths), and the host's built-in agent
types (on Claude `general-purpose`, `Explore`, `Plan`), which this table cannot pin, on the
standard tier (Claude: `model: sonnet`).

<!-- rendered: roles -->
| role | tier | effort | use for |
|---|---|---|---|
| analyst | light | high | The read type - branch review (skill specialty-review) and research (skill specialty-research); builds and edits nothing. Raised per dispatch only by the "Reviewer model" rule (strong tier) and the research rule (standard tier) in the dispatch skill's roster file. |
| consultant | deep | high | Own long-lived launched session per repository (never dispatched; 1M via launcher). Answers engineering questions from coordinators and workers; never edits, lands or dispatches builds; idle until asked. |
| coordinator | strong | medium | Main session, never a subagent. Splits work, dispatches agents, reviews branches, queues every LAND verdict for landing at once, brings decisions to the owner. Writes no code. |
| deep-worker | deep | xhigh | Own launched session (never dispatched; 1M via launcher). Root cause or concurrency work senior-worker failed at or plainly cannot do; never builds or plans. |
| docs-writer | standard | medium | The docs type - records and specs (skill specialty-docs). Docs only. |
| helper | light | medium | Bulk menial reads and mechanical bulk writes, dispatched by a builder into its own worktree; read and edit work only, runs no shell command. Never dispatched by a coordinator for a story. |
| lander | light | medium | Optional wrapper: submits one reviewed branch (or a batch) for landing, waits for the result, reports. Never edits code. Dispatched with no model override, and never overridden. |
| pm | strong | low | The project manager: the owner's one main session (the hub resumes it so after a boot), never a subagent. Keeps every repository's coordinator running and briefed, records owner decisions in the ledger, schedules the machine, runs the Action Hub. Builds and lands nothing itself. |
| senior-worker | strong | high | High-risk changes: networking, concurrency, simulation or ordering, landing, gate or lock tools, build-cache and codegen design, migrations, CI config, known-area crash fixes. |
| strong-worker | strong | medium | High-risk build and fix work on standard paths: public API changes, live-behaviour fixes, ordinary tool features, generated-data wiring. Not plan-only or sync/prep work (worker). |
| test-runner | light | - | Runs test tiers in a given worktree and reports only PASS/FAIL plus failing lines. |
| weak-worker | light | high | Small, well-defined, low-risk changes the brief sizes by listing them (named files, named edits, a few files): doc lines, small additive changes with their test. Escalates to worker when the task is bigger or less clear than briefed. |
| worker | standard | medium | Default coder for routine low and medium risk work: additive content, UI, pure logic with tests, small fixes with a known cause, behaviour-neutral moves and renames; plan-only and sync or prep work (merging main into a branch, rebasing, conflict-free syncs, preparation). |
<!-- end rendered -->

## Tier to model, per host

Each host maps a tier to a model in `hosts.json` `tiers`, which its renderer's `tier_models()` returns;
the table below is rendered from it. A role file may name a different model for one
host (`model.<host>:`, docs/content.md); none does.

<!-- rendered: tiers -->
| tier | claude | codex |
|---|---|---|
| light | haiku | account-default |
| standard | sonnet | account-default |
| strong | opus | account-default |
| deep | opus | account-default |
<!-- end rendered -->

`account-default` writes no `model` line: the role runs on the model the Codex account is set to. On Codex a tier differs by reasoning effort: a role's `effort`, else
light `low`, standard `medium`, strong `high`, deep `xhigh` (hosts/codex/adapter/content.py
`TIER_EFFORT`). The owner names Codex models per tier in hosts.json when they are chosen, and the table follows on the next render.
With no light-tier model mapped for Codex, weak-worker there runs the account default at its own effort, high
(content/roles/weak-worker.md), until the owner maps one.

Model choices rest on measured A/B runs: docs/model-ab.md.

## The one-hour prompt cache

content/roles/strong-worker.md, content/roles/worker.md, content/roles/docs-writer.md and
content/roles/weak-worker.md say `cache: long`, which this table does not show; Claude's renderer
writes it into each one's hosts/claude/shell/agents/ file as a second frontmatter setting (docs/content.md):

```yaml
experimental:
  cacheTtl: 1h
```

- What it does: the client keeps that agent type's prompt cache for one hour instead of five
  minutes, so a continuation after a long gate or a slow review re-reads the cache instead of
  writing it again. Source for the key, its place (inside the `experimental` map, never at the top
  level), its values (`5m`, `1h`) and its limits: the client's subagent documentation.
- Limits, same source: client 2.1.248 or later; ignored while the subscription draws on usage
  credits; read only from subagent files. That a plugin's agent file is read like a project's is
  inferred, not stated there.
- Only these four rows have it (tests/agents/test_agents.py,
  `test_only_the_long_cache_roles_ask_for_the_one_hour_prompt_cache`): strong-worker, worker,
  docs-writer and weak-worker. The pricing computation in docs/agent-review.md, "Context and cache findings", is untested
  and does not cover docs-writer or weak-worker.
- Check it took: in one run's usage block for any of these four roles, the cache-creation split
  counts tokens in the one-hour field and none in the five-minute field. NOT OBSERVED for any
  of them.
