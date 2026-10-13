# Agent roster

The kit's plugin ships these agent definitions in `claude/agents/`. Each is a generic copy of the live
definition; repo specifics live in the repo's `.claude/agent-profile.md` (every agent) and
`.claude/coordinator-profile.md` (coordinator only). The plugin registers each one under its prefix:
dispatch a row as `pm-kit:<row>` (`subagent_type`, `--agent`). A repo's own `.claude/agents/<row>.md`
is a separate agent named `<row>`; it does not replace `pm-kit:<row>`. A repo that ships its own
file lists that agent type in its `.claude/coordinator-profile.md` and is dispatched by the bare
name; every other row is dispatched as `pm-kit:<row>` (tests/agents/test_override.py,
tests/agents/test_dispatch_names.py). That bare-name dispatch is for the Agent tool only: the launcher
qualifies every bare kit agent name, so it always starts the kit's version, and a repo's own file for a
launched row (coordinator, consultant, deep-worker) cannot be started by the launcher under that name
(docs/launcher.md "The window").

This table is the one place to set which agent runs at which model and effort, and when to use it.
Edit a row, then `python tools/agent_roster.py --write` pins model and effort into that agent's
frontmatter; `--check` (the default) lists any disagreement. `-` = no effort key. Never `fable`
unless the owner asks; override a dispatch only upward, and never the lander's. The per-dispatch
rules have one home, claude/skills/coordinator-dispatch/roster.md, "Choosing": which reviews run
on opus (from the review pack's printed risk groups and the strict landing paths), and the
built-in agent types (`general-purpose`, `Explore`, `Plan`), which this table cannot pin, on
`model: sonnet`.

| agent | model | effort | use for |
|---|---|---|---|
| asset-smith | opus | high | 3D assets as Blender Python scripts against a decided art direction, validated against budgets, with a contact sheet for the owner. No rigging or animation. |
| coordinator | opus | medium | Main session (`claude --agent pm-kit:coordinator`), never a subagent. Splits work, dispatches agents, reviews branches, lands every LAND verdict as soon as the lane is free, brings decisions to the owner. Writes no code. |
| consultant | opus | high | Own long-lived launched session per repository (never an Agent dispatch; 1M via launcher). Answers engineering questions from coordinators and workers; never edits, lands or dispatches builds; idle until asked. |
| deep-worker | opus | xhigh | Own launched session (never an Agent dispatch; 1M via launcher). Root cause or concurrency work strong-worker failed at or plainly cannot do; never builds or plans. |
| lander | haiku | medium | Lands one reviewed branch (or a batch) on main on the coordinator's go. Never edits code. Dispatched with no `model`: never overridden. |
| pm | opus | high | The project manager: the owner's one main session (`claude --agent pm-kit:pm`; after-reboot resumes it so), never a subagent. Keeps every repository's coordinator running and briefed, records owner decisions in the ledger, schedules the machine, runs the Action Hub. Builds and lands nothing itself. |
| records | sonnet | low | Writes settled facts into docs only: decisions, landing notes, plan ticks. |
| researcher | sonnet | high | Read-only ideas and prior art; proposes concepts for the owner to choose from, builds nothing. |
| reviewer | sonnet | high | Read-only review of one branch against its brief; reports defects, scope creep and untested changes. Raised to opus per dispatch only by the "Reviewer model" rule in the dispatch skill's roster file. |
| senior-worker | opus | medium | High-risk build and fix work on standard paths: public API changes, live-behaviour fixes, ordinary tool features, generated-data wiring. Not plan-only or sync/prep work (`pm-kit:worker`). The one row with a one-hour prompt cache: see "The one-hour prompt cache" below. |
| sound-smith | sonnet | medium | Sound effects from synthesis, free libraries, text-to-speech and local music generation; measures clips, the owner judges. Mechanical work at this row; design dispatches override to opus/high (upward only). |
| spec-writer | sonnet | high | Extends a named design or spec document from the owner's answers. Docs only. |
| strong-worker | opus | high | High-risk changes: networking, concurrency, simulation or ordering, landing or lock tools, migrations, CI config, known-area crash fixes. |
| tech-artist | opus | high | Art that is code: shaders, materials, palettes, UI themes, procedural meshes and textures, lighting; checked by rendering. |
| test-runner | haiku | - | Runs test tiers in a given worktree and reports only PASS/FAIL plus failing lines. |
| worker | sonnet | medium | Default coder for routine low and medium risk work: additive content, UI, pure logic with tests, small fixes with a known cause, behaviour-neutral moves and renames; plan-only and sync or prep work (merging main into a branch, rebasing, conflict-free syncs, preparation). |

Model choices rest on measured A/B runs: docs/model-ab.md.

## The one-hour prompt cache

claude/agents/senior-worker.md (content/roles/senior-worker.md, `cache: long`) carries a second frontmatter setting, outside this table and untouched
by `agent_roster.py --write` (tests/roster/test_roster.py, `test_write_leaves_a_frontmatter_map_alone`):

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
- Only this row has it (tests/agents/test_agents.py,
  `test_only_senior_worker_asks_for_the_one_hour_prompt_cache`). Why this row: the untested pricing
  computation in docs/agent-review.md, "Context and cache findings".
- Check it took: in one senior-worker run's usage block, the cache-creation split counts tokens
  in the one-hour field and none in the five-minute field. Not yet observed for this file.

## Changes versus the live files

Source: the files in the project manager's `kit/agents/` at the time of the copy. Every other file is byte-identical to its live file. Behaviour is unchanged in all cases.

The live files held no ruling id, no repo name and no owner path, so the scrub changed only two files.

- **coordinator.md**, startup step 3 (check in with the project manager): the id of the newest ruling in the state note was replaced by plain words, and later the step stopped asking for an id at all: it asks for "owner decisions since your last check-in". Reason: the ids were the project manager's own numbering.
- **researcher.md**, end of "Repo tools": removed one line, a research exclusion for a single repository. Reason: it named a repo, and is a repo-specific research exclusion. It belongs in that repo's `.claude/agent-profile.md`; the agent already reads the profile. Until a repo's profile carries it, an agent could research those topics.

Left as is, on purpose:
- Role words "PM" and "project manager" and the launcher commands `launch.py deep` (coordinator, deep-worker) and `launch.py consultant` (consultant; `launcher\start-*.ps1` paths before the launcher moved into the kit, docs/launcher.md): the kit's own roles and launcher, not owner-specific.
- `~/.claude/...` paths (templates, comfy tools, knowledge folder): home-relative, no user name.
