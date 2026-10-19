# Agent roster

The kit's plugin ships these agent definitions in `hosts/claude/shell/agents/`, rendered from `content/roles/`
(docs/content.md). Each is a generic copy of the live definition; repo specifics live in the repo's `.claude/agent-profile.md` (every agent) and
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
| asset-smith | strong | high | 3D assets as Blender Python scripts against a decided art direction, validated against budgets, with a contact sheet for the owner. No rigging or animation. |
| consultant | deep | high | Own long-lived launched session per repository (never dispatched; 1M via launcher). Answers engineering questions from coordinators and workers; never edits, lands or dispatches builds; idle until asked. |
| coordinator | strong | medium | Main session, never a subagent. Splits work, dispatches agents, reviews branches, queues every LAND verdict for landing at once, brings decisions to the owner. Writes no code. |
| deep-worker | deep | xhigh | Own launched session (never dispatched; 1M via launcher). Root cause or concurrency work senior-worker failed at or plainly cannot do; never builds or plans. |
| lander | light | medium | Optional wrapper: submits one reviewed branch (or a batch) for landing, waits for the result, reports. Never edits code. Dispatched with no model override, and never overridden. |
| pm | strong | high | The project manager: the owner's one main session (the after-reboot resume starts it so), never a subagent. Keeps every repository's coordinator running and briefed, records owner decisions in the ledger, schedules the machine, runs the Action Hub. Builds and lands nothing itself. |
| records | standard | low | Writes settled facts into docs only: decisions, landing notes, plan ticks. |
| researcher | standard | high | Read-only ideas and prior art; proposes concepts for the owner to choose from, builds nothing. |
| reviewer | light | high | Read-only review of one branch against its brief; reports defects, scope creep and untested changes. Raised to the strong tier per dispatch only by the "Reviewer model" rule in the dispatch skill's roster file. |
| senior-worker | strong | high | High-risk changes: networking, concurrency, simulation or ordering, landing, gate or lock tools, build-cache and codegen design, migrations, CI config, known-area crash fixes. |
| sound-smith | standard | medium | Sound effects from synthesis, free libraries, text-to-speech and local music generation; measures clips, the owner judges. Mechanical work at this tier; design dispatches override to the strong tier at high effort (upward only). |
| spec-writer | standard | high | Extends a named design or spec document from the owner's answers. Docs only. |
| strong-worker | strong | medium | High-risk build and fix work on standard paths: public API changes, live-behaviour fixes, ordinary tool features, generated-data wiring. Not plan-only or sync/prep work (worker). The one role with a one-hour prompt cache (see "The one-hour prompt cache" below). |
| tech-artist | strong | high | Art that is code: shaders, materials, palettes, UI themes, procedural meshes and textures, lighting; checked by rendering. |
| test-runner | light | - | Runs test tiers in a given worktree and reports only PASS/FAIL plus failing lines. |
| weak-worker | light | high | Small, well-defined, low-risk changes expected under 100k tokens: named edits to named files, doc lines, small additive changes with their test. Escalates to worker when the task is bigger or less clear than briefed. |
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

`account-default` writes no `model` line: the role runs on the model the Codex account is set to, as
the Codex branch's roles had it. On Codex a tier differs by reasoning effort: a role's `effort`, else
light `low`, standard `medium`, strong `high`, deep `xhigh` (hosts/codex/adapter/content.py
`TIER_EFFORT`). The owner names Codex models per tier in hosts.json when they are chosen, and the table follows on the next render.
With no light-tier model mapped for Codex, weak-worker there runs the account default at its own effort, high
(content/roles/weak-worker.md), until the owner maps one.

Model choices rest on measured A/B runs: docs/model-ab.md.

## The one-hour prompt cache

content/roles/strong-worker.md says `cache: long`, which this table does not show; Claude's
renderer writes it into hosts/claude/shell/agents/strong-worker.md as a second frontmatter setting (docs/content.md):

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
  `test_only_strong_worker_asks_for_the_one_hour_prompt_cache`). Why this row: the untested pricing
  computation in docs/agent-review.md, "Context and cache findings", made for the opus/medium default
  coder, which was senior-worker (opus-worker before it) until the two roles swapped charters on
  2026-10-15; the setting moved with the charter.
- Check it took: in one strong-worker run's usage block, the cache-creation split counts tokens
  in the one-hour field and none in the five-minute field. Not yet observed for this file.

## Changes versus the live files

Source: the files in the project manager's `kit/agents/` at the time of the copy. Every other file is byte-identical to its live file. Behaviour is unchanged in all cases.

The live files held no ruling id, no repo name and no owner path, so the scrub changed only two files.

- **coordinator.md**, startup step 3 (check in with the project manager): the id of the newest ruling in the state note was replaced by plain words, and later the step stopped asking for an id at all: it asks for "owner decisions since your last check-in". Reason: the ids were the project manager's own numbering.
- **researcher.md**, end of "Repo tools": removed one line, a research exclusion for a single repository. Reason: it named a repo, and is a repo-specific research exclusion. It belongs in that repo's `.claude/agent-profile.md`; the agent already reads the profile. Until a repo's profile carries it, an agent could research those topics.

Left as is, on purpose:
- Role words "PM" and "project manager" and the launcher commands `launch.py deep` (coordinator, deep-worker) and `launch.py consultant` (consultant; `launcher\start-*.ps1` paths before the launcher moved into the kit, docs/launcher.md): the kit's own roles and launcher, not owner-specific.
- The host's per-user folder (`{{user_folder}}`, `~/.claude` on Claude): home-relative, no user name. The comfy tools and the knowledge folder are named in the kit since (`<kit>/optional/comfy`, `<kit>/knowledge/asset-generation`; machine setup links the old `~/.claude` paths to them).
