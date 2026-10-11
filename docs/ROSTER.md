# Agent roster

The kit's plugin ships these agent definitions in `claude/agents/`. Each is a generic copy of the live
definition; repo specifics live in the repo's `.claude/agent-profile.md` (every agent) and
`.claude/coordinator-profile.md` (coordinator only). The plugin registers each one under its prefix:
dispatch a row as `pm-kit:<row>` (`subagent_type`, `--agent`). A repo's own `.claude/agents/<row>.md`
is a separate agent named `<row>`; it does not replace `pm-kit:<row>`. A repo that ships its own
file lists that agent type in its `.claude/coordinator-profile.md` and is dispatched by the bare
name; every other row is dispatched as `pm-kit:<row>` (tests/agents/test_override.py,
tests/agents/test_dispatch_names.py).

This table is the one place to set which agent runs at which model and effort, and when to use it.
Edit a row, then `python tools/agent_roster.py --write` pins model and effort into that agent's
frontmatter; `--check` (the default) lists any disagreement. `-` = no effort key. Never `fable`
unless the owner asks; override a dispatch only upward.

| agent | model | effort | use for |
|---|---|---|---|
| asset-smith | opus | high | 3D assets as Blender Python scripts against a decided art direction, validated against budgets, with a contact sheet for the owner. No rigging or animation. |
| coordinator | opus | medium | Main session (`claude --agent pm-kit:coordinator`), never a subagent. Splits work, dispatches agents, reviews branches, serialises landings, brings decisions to the owner. Writes no code. |
| consultant | opus | high | Own long-lived launched session per repository (never an Agent dispatch; 1M via launcher). Answers engineering questions from coordinators and workers; never edits, lands or dispatches builds; idle until asked. |
| deep-worker | opus | xhigh | Own launched session (never an Agent dispatch; 1M via launcher). Root cause or concurrency work strong-worker failed at or plainly cannot do; never builds or plans. |
| lander | haiku | medium | Lands one reviewed branch (or a batch) on main on the coordinator's go. Never edits code. |
| opus-worker | opus | medium | High-risk build and fix work on standard paths: public API changes, live-behaviour fixes, ordinary tool features, generated-data wiring. Not plan-only or sync/prep work (`pm-kit:worker`). |
| records | sonnet | low | Writes settled facts into docs only: decisions, landing notes, plan ticks. |
| researcher | sonnet | high | Read-only ideas and prior art; proposes concepts for the owner to choose from, builds nothing. |
| reviewer | sonnet | high | Read-only review of one branch against its brief; reports defects, scope creep and untested changes. |
| sound-smith | sonnet | medium | Sound effects from synthesis, free libraries, text-to-speech and local music generation; measures clips, the owner judges. Mechanical work at this row; design dispatches override to opus/high (upward only). |
| spec-writer | sonnet | high | Extends a named design or spec document from the owner's answers. Docs only. |
| strong-worker | opus | high | High-risk changes: networking, concurrency, simulation or ordering, landing or lock tools, migrations, CI config, known-area crash fixes. |
| tech-artist | opus | high | Art that is code: shaders, materials, palettes, UI themes, procedural meshes and textures, lighting; checked by rendering. |
| test-runner | haiku | - | Runs test tiers in a given worktree and reports only PASS/FAIL plus failing lines. |
| worker | sonnet | medium | Default coder for routine low and medium risk work: additive content, UI, pure logic with tests, small fixes with a known cause, behaviour-neutral moves and renames; plan-only and sync or prep work (merging main into a branch, rebasing, conflict-free syncs, preparation). |

Model choices rest on measured A/B runs: docs/model-ab.md.

## Changes versus the live files

Source: the files in the project manager's `kit/agents/` at the time of the copy. Every other file is byte-identical to its live file. Behaviour is unchanged in all cases.

The live files held no ruling id, no repo name and no owner path, so the scrub changed only two files.

- **coordinator.md**, startup step 3 (check in with the project manager): the id of the newest ruling in the state note was replaced by plain words, and later the step stopped asking for an id at all: it asks for "owner decisions since your last check-in". Reason: the ids were the project manager's own numbering.
- **researcher.md**, end of "Repo tools": removed one line, a research exclusion for a single repository. Reason: it named a repo, and is a repo-specific research exclusion. It belongs in that repo's `.claude/agent-profile.md`; the agent already reads the profile. Until a repo's profile carries it, an agent could research those topics.

Left as is, on purpose:
- Role words "PM" and "project manager" and the launcher commands `launch.py deep` (coordinator, deep-worker) and `launch.py consultant` (consultant; `launcher\start-*.ps1` paths before the launcher moved into the kit, docs/launcher.md): the kit's own roles and launcher, not owner-specific.
- `~/.claude/...` paths (templates, comfy tools, knowledge folder): home-relative, no user name.
