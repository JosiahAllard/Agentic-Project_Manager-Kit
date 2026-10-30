# Specialty skills: task specialties as skills, not agent types

Status: decided and built. The owner accepted all eight recommendations ("Decisions"). Built: the seven `content/skills/specialty-*` skills and the `analyst` and `docs-writer` types with the hard cutover (branches A and B, "Build split"). Branch C (guard) was not needed. Branch D (repo migration) is owner work in other repositories.

## Owner ruling

The owner's ruling, as relayed by the PM: "specialties become skills instead of agent types; written as a spec
first". Goal: more task specialties without more agent types. A specialty is a skill a generic agent
loads because its brief names it. The agent type sets model, effort and permissions; the skill sets
know-how. Every inference is marked "inference"; the rest is read from the code.

## Findings (code against the brief)

1. `kitlib/backlog/roles.py` needs no code edit. Its groups are read from the role files
   (`_ROSTER = _content.roster(KIT)`, `BUILDERS`, `DOCS`, `READ_ONLY` by `group`/`mode`), so retiring
   or merging a role file moves it.
2. The landing right is keyed on the type NAME: `kitlib/guard/rules.py` `LANDER = 'lander'` and
   `lander_check` (`event.agent_role != LANDER`, then `.kit.json` `roles.lander.edit_main_checkout`).
3. `docs/ROSTER.md` and `content/skills/coordinator-dispatch/roster.md` ("Choosing") say "Effort is
   not settable per dispatch: pick the role for it". The owner's "effort set per dispatch" for the
   docs type contradicts that. Effort is a role front-matter key (`kitlib/host/content.py`
   `OPTIONAL`, `EFFORTS`), rendered per file. I found no per-dispatch effort input in the kit;
   whether a host's dispatch call accepts one is unverified, and the rule is still in `roster.md`.
4. Capabilities are a union. The analyst carries both the reviewer's and the researcher's tools
   (`content/roles/analyst.md`: `read, search, find, bash, web-search, web-fetch, skill`); a skill
   cannot take a tool away, so review gains web tools and research gains `bash`. A type needs `skill`
   to load a skill (`CAPABILITIES`; docs/content.md "Agent view" step 6).
5. Worker types are `capabilities: all`, so creative work runs with a wider tool set than the retired
   creative types had (inference: least privilege shrinks; the skill text must say what the brief
   allows, e.g. paid generation needs the owner).
6. A skill has no tokens (docs/content.md "Skills"), so the specialty skills are host-neutral text: a
   short checklist plus companion files (as `coordinator-dispatch/roster.md`).
7. `strong-worker` is strong tier at medium effort, so the retired creative types' strong/high
   equivalent is `senior-worker` (strong tier, high effort).
8. `kitlib/gate/land.py` `is_author` compares reviewer and author agent types as free text; no
   landing code names `reviewer`. Review must stay on a read type: a worker type reviewing a
   worker's branch would match.
9. `kitlib/content/__init__.py` `ALIASES` maps one old name to one new name; it cannot say "dispatch
   a worker and load skill X", and the plugin lists each agent file (`.claude-plugin/plugin.json`).
10. Codex skill loading from outside its plugin folder is unverified (docs/content.md "Codex");
    specialty skills inherit that open item.

## Target roster

| type | tier / effort | mode, group | note |
|---|---|---|---|
| weak-worker, worker, strong-worker, senior-worker | light/high, standard/medium, strong/medium, strong/high | write, builder | the brief picks one per task; specialty skills ride on these |
| analyst (reviewer + researcher) | light / high | read, read-only | skills `specialty-review`, `specialty-research` |
| docs-writer (records + spec-writer) | standard / medium, raised for specs | write, docs | skill `specialty-docs` |
| lander, test-runner, coordinator, pm, consultant, deep-worker | unchanged | unchanged | |

Retired: asset-smith, tech-artist, sound-smith, researcher, reviewer, records, spec-writer (7 files);
added: `analyst` and `docs-writer`. `content/roles/` holds 13 role files, the helper included.
The analyst keeps the reviewer's light/high pin, so the "Reviewer model" rule is unchanged; `roster.md` raises a research dispatch to the standard tier.

## Specialty skills

Seven folders `content/skills/specialty-<name>/SKILL.md`, long material in companion files, shipped
by the existing render (Claude and Codex both load `content/skills/` from the plugin;
docs/content.md "Skills"). The `specialty-` prefix: a skill named
`branch-review` already exists (the coordinator's), a bare `records` would read as the retired type,
and names start with a letter (`_NAME` in `kitlib/host/content.py`).

| skill | replaces | brief names | keeps from the role |
|---|---|---|---|
| specialty-3d-assets | asset-smith | senior-worker | script is the asset, budgets, contact sheet, options-first; props here, characters and rigs in specialty-rigging |
| specialty-rigging | - (new) | senior-worker | scripted rig to the repo's body contract, weight and bend checks, contact sheet, skins on one shared rig |
| specialty-tech-art | tech-artist | senior-worker | art that is code, checked by rendering, art-direction stop |
| specialty-sound | sound-smith | worker; senior-worker for design | measure clips, ends in a question for the owner |
| specialty-review | reviewer | analyst | brief + diff range + logs, report format |
| specialty-research | researcher | analyst | concepts with evidence, spec-writer hand-off |
| specialty-docs | records, spec-writer | docs-writer | records: transcribe, never decide; spec: findings first, defaults labelled, inferences marked |

Each SKILL.md opens with a "Dispatch" line: the minimum worker tier (never weak-worker for creative
work) and the profile sections it reads (the creative skills read **Art** or **Audio**, and **Shared
resources**). The creative brief block (FEEL / REFERENCES / AVOID / SCOPE OF CHOICE) and options-first
live in `coordinator-dispatch/creative.md`.

## The merged read type (reviewer + researcher)

Mode `read`, sandbox `read-only`, effort high: both inputs agree. The researcher was standard tier;
the analyst is pinned to the light tier and the "Reviewer model" rule in `roster.md` raises it per
dispatch. Guard: `LOCK_VERBS['read'] = {'status'}`, checkout read-only to the shell, owner paths
read-only (`kitlib/guard/rolepolicy.py` `RULES`).

What replaced the researcher's `write` capability (decided): the report is returned as text, plus a
file in the agent's scratch folder for long notes (the report itself as text, never a report file: `coordinator-dispatch` "Dispatch traps").
Scratch is outside the checkout, so `rolepolicy.check` role-edit never fires. No `writes`, no repo
write path; `write` is dropped from the capabilities.

## The docs type (records + spec-writer)

Mode `write`, `group: docs`, sandbox `workspace-write`; both inputs had identical mode, group,
sandbox and capabilities, so the backlog rights (`DOCS`: may `add`, may not `pull`) and the guard
are unchanged. "Docs only" is prose, not a guard rule. `skill` is in `capabilities`.

Effort (decided): records was low, spec-writer high; a role file takes one value. The role file says
`effort: medium`; a spec is raised, by model (the existing upward override). "Set per dispatch" needs
a host input the kit does not carry (Finding 3). Open: check whether a host's dispatch call can carry
an effort at all; if it can, the roster rule "Effort is not settable per dispatch" is rewritten and
the brief states effort.

## Lander and test-runner stay separate (confirmed)

`kitlib/guard/rolepolicy.py`, by mode:

| | lander | test |
|---|---|---|
| edit in own checkout | refused (`NO_EDIT`) | refused (`NO_EDIT`) |
| shell write to own checkout | allowed (not in `READ_ONLY_CHECKOUT`) | refused (`READ_ONLY_CHECKOUT`) |
| lock verbs (`LOCK_VERBS`) | `status`, `run` only | `AGENT_LOCK`: also `background`, `stop`, `hold`, `division`, `game` |
| `git fetch` / `git merge --ff-only` in main | allowed by name: `rules.lander_check`, needs `.kit.json` `roles.lander.edit_main_checkout` | not allowed by it |

The modes are opposites (landing rights and a writable checkout against more lock verbs and a
read-only checkout). A skill is text; mode, tools and the name test in `lander_check` belong to the
type. Merging would force the union (landing rights plus test lock verbs on a light-tier agent), a
widening of a strict path. Keep both.

## Dispatch skill changes

`content/skills/coordinator-dispatch/SKILL.md` and `roster.md`:

1. "Which role, which tier" gains a step: match the task to a specialty skill; the brief names it
   ("Load skill `specialty-sound` first") and names the worker tier. The tier ladder stays the
   owner's tier text; this adds only the specialty's minimum tier.
2. "Roles and what each is for": the seven names are gone, the analyst and docs-writer are in, with
   one line listing the specialty skills.
3. "Choosing": delete "Effort is not settable per dispatch" only if the effort check (above) finds a
   host input. The Reviewer-model rule takes the analyst's name.
4. "Creative dispatches" points at the creative skills; a repo with no art or audio says "specialty
   skills not used" in its profile's roster line (an owner edit, to-do 2).

## Guard and rolepolicy mapping

| type | `mode` | group (`roles.py`) | `LOCK_VERBS` | edit rule |
|---|---|---|---|---|
| analyst | read | read-only | status | `role-edit`, checkout read-only to shell |
| docs-writer | write | docs | `AGENT_LOCK` | none (prose: docs only) |
| worker type with a specialty | write | builder | `AGENT_LOCK` | none |
| lander, test-runner | lander, test | lander, read-only | as above | as above |

No change to `kitlib/guard/**` or `rolepolicy.py`: modes are read from the role file by
`kitlib.content.role_mode`. The guard cannot see which skill an agent loaded, so no rule differs per
specialty (inference: that is what the owner wants).

## Migration inventory

The retired names appeared in the role and skill files, the rendered agents and plugin manifest, the
kit's docs, tests and templates, and in downstream repos (profiles, per-role topic maps). The kit
side is cut over; `tests/content/test_role_names.py` fails on a leftover name.

Not names to change: "reviewer" as a function word in `docs/agent-profile.md`, `strict_defaults.py`,
risk maps and templates. `doc_index.py --role <name>` exits 2 on an unknown role, so a repo's per-role
topic map (a `.kit.json` `doc_index.roles`, or a brief template via `roles_doc`) carries the new names, and
per-specialty topics move into the brief.

Deprecation (decided): a hard cutover in one landing. Nothing that dispatches the old type names may
be running when it lands. Shims for seven agent files would keep the list this ruling shrinks, and
`ALIASES` cannot carry a skill (Finding 9). A stale dispatch of a removed type fails loudly in the
host.

### Owner to-do (owner-only paths in downstream repos; not checkable from this repo)

1. In a repo's `.kit.json` `doc_index.roles`: replace the keys reviewer, researcher, records,
   spec-writer, tech-artist, asset-smith, sound-smith with `analyst` and `docs-writer` (topics merged).
2. In agent-profile and coordinator-profile lines naming `records`, `spec-writer`, `tech-artist`,
   `asset-smith`, `sound-smith`: the new names. A repo with no art or audio states "specialty skills
   not used" in its coordinator profile.

## Decisions

The owner accepted all eight recommendations.

1. Names: the read type is `analyst`, the docs type is `docs-writer`.
2. Research is returned as text plus a scratch file ("The merged read type").
3. `docs-writer` runs at medium effort, raised for specs; check whether a dispatch can set effort at
   all ("The docs type").
4. A hard cutover in one landing; nothing that dispatches the old type names may be running when it
   lands ("Migration inventory").
5. Minimum tiers as in the skills table, with no weak-worker on creative work ("Specialty skills").
6. The wider tools: the read type has `bash` and web; creative work gets a worker's full tool set,
   limits in skill text (Findings 4 and 5).
7. Skills are named `specialty-*`.
8. Repos with no art or audio say "specialty skills not used" in their coordinator profile (an owner
   edit, not a kit change; to-do 2).

## Build split

| branch | work | status |
|---|---|---|
| A skills | `content/skills/specialty-*/` (7), then rendered into `hosts/codex/shell/skills/` (a copy since retired), tests | built |
| B types and dispatch (atomic hard cut) | seven role files deleted, two added; dispatch and review skills, `coordinator.md`, rendered agents and manifest, docs, `templates/common/AGENTS.md`, tests, `command_canary.py` | built |
| C guard | none (research returns text plus a scratch file, no `writes`) | not built, not needed |
| D repo migration | the owner to-do above, in downstream repos | owner work |

Strict paths touched (`content/roles/**`, `content/skills/**`, `hosts/*/shell/agents/**`,
`hosts/*/adapter/**`) reach the full gate with a fresh strong reviewer
(`kitlib/gate/strict_defaults.py`). No lock, landing or guard code is touched.
