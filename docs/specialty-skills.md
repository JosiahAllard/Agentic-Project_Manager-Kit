# Specialty skills: task specialties as skills, not agent types

Status: decided (ready to build); the owner accepted all eight recommendations ("Decisions"). Nothing here is built. Written for the state after two pending
changes land: senior-worker on opus/high with strong-worker on opus/medium (the tier change), and a
weak-worker on haiku 5.5 (the weak-worker change). Those rows come from the owner's rulings, not from main.

## Owner ruling

The owner's ruling, as relayed by the PM: "specialties become skills instead of agent types; written as a spec
first". Goal: more task specialties without more agent types. A specialty is a skill a generic agent
loads because its brief names it. The agent type sets model, effort and permissions; the skill sets
know-how. Every inference is marked "inference"; the rest is read from the code.

## Findings (code against the brief)

1. `kitlib/backlog/roles.py` needs no code edit. Its groups are read from the role files
   (`_ROSTER = _content.roster(KIT)`, `BUILDERS`, `DOCS`, `READ_ONLY` by `group`/`mode`), so retiring
   or merging a role file moves it. Only the module docstring ("records and spec-writer: docs")
   goes stale: one owner-path text edit.
2. The landing right is keyed on the type NAME: `kitlib/guard/rules.py` `LANDER = 'lander'` and
   `lander_check` (`event.agent_role != LANDER`, then `.kit.json` `roles.lander.edit_main_checkout`).
3. `docs/ROSTER.md` and `content/skills/coordinator-dispatch/roster.md` ("Choosing") say "Effort is
   not settable per dispatch: pick the role for it". The owner's "effort set per dispatch" for the
   docs type contradicts that. Effort is a role front-matter key (`kitlib/host/content.py`
   `OPTIONAL`, `EFFORTS`), rendered per file. I found no per-dispatch effort input in the kit;
   whether a host's dispatch call accepts one is unverified.
4. Capabilities are a union. `content/roles/reviewer.md`: `[read, search, find, bash]`;
   `researcher.md`: `[read, search, find, web-search, web-fetch, write]`. A merged read type carries
   both; a skill cannot take a tool away, so review gains web tools and research gains `bash`.
   Neither list has `skill`, which a type needs to load one (`CAPABILITIES`; docs/content.md "Agent
   view" step 6). The docs types lack it too.
5. The three creative types have bounded lists (no `skill`, no web). Worker types are
   `capabilities: all`, so creative work runs with a wider tool set (inference: least privilege
   shrinks; the skill text must say what the brief allows, e.g. paid generation needs the owner).
6. A skill has no tokens (docs/content.md "Skills"); the creative role bodies use `{{profile_path}}`,
   `{{user_folder}}`, `{{instructions_file}}` (19 token uses, by grep) and need host-neutral
   rewording. The bodies are 10.5, 9.8 and 10.5 KB; a skill is a short checklist plus companion
   files (as `coordinator-dispatch/roster.md`).
7. The tier change alters the creative mapping: asset-smith and tech-artist are strong/high today, and
   strong-worker becomes opus/medium, so their equivalent is senior-worker (opus/high).
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
| weak-worker, worker, strong-worker, senior-worker | per the tier and weak-worker changes | write, builder | the brief picks one per task; specialty skills ride on these |
| read type (reviewer + researcher) | standard / high | read, read-only | skills `specialty-review`, `specialty-research` |
| docs type (records + spec-writer) | standard / medium, raised for specs | write, docs | skills `specialty-records`, `specialty-spec` |
| lander, test-runner, coordinator, pm, consultant, deep-worker | unchanged | unchanged | |

Retired: asset-smith, tech-artist, sound-smith, researcher, reviewer, records, spec-writer (7 files);
added: `analyst` (the read type) and `docs-writer` (the docs type); the generic terms are used below. Role files: 16 now, 12 with weak-worker.

## Specialty skills

Seven folders `content/skills/specialty-<name>/SKILL.md`, long material in companion files, shipped
by the existing render (Claude loads `content/skills/`; Codex gets the copy
`hosts/codex/shell/skills/`, docs/content.md "Skills"). The `specialty-` prefix: a skill named
`branch-review` already exists (the coordinator's), a bare `records` would read as the retired type,
and names start with a letter (`_NAME` in `kitlib/host/content.py`).

| skill | replaces | brief names (inference) | keeps from the role |
|---|---|---|---|
| specialty-3d-assets | asset-smith | senior-worker | script is the asset, budgets, contact sheet, options-first, no rigging |
| specialty-tech-art | tech-artist | senior-worker | art that is code, checked by rendering, art-direction stop |
| specialty-sound | sound-smith | worker; senior-worker for design | measure clips, ends in a question for the owner |
| specialty-review | reviewer | read type | brief + diff range + logs, report format |
| specialty-research | researcher | read type | concepts with evidence, spec-writer hand-off |
| specialty-records | records | docs type | transcribe, never decide |
| specialty-spec | spec-writer | docs type | findings first, defaults labelled, inferences marked |

Each SKILL.md opens with a "Dispatch" line: the minimum worker tier (never weak-worker for creative
work) and the profile sections it reads (the creative roles read **Art**, **Shared resources**). The
creative brief block (FEEL / REFERENCES / AVOID / SCOPE OF CHOICE) and options-first move from
`coordinator-dispatch` "Creative dispatches" into the creative skills' companion file.

## The merged read type (reviewer + researcher)

Mode `read`, sandbox `read-only`, standard tier, effort high: both inputs agree. The reviewer stays
at the default standard tier; the "Reviewer model" rule in `roster.md` raises it per dispatch,
unchanged. Guard: `LOCK_VERBS['read'] = {'status'}`, checkout read-only to the shell, owner paths
read-only (`kitlib/guard/rolepolicy.py` `RULES`); identical to today.

What replaces the researcher's `write` capability (decided): the report is returned as text, plus a
file in the agent's scratch folder for long notes (the rule already in `subagent-dispatch` step 4).
Scratch is outside the checkout, so `rolepolicy.check` role-edit never fires. No `writes`, no repo
write path; `write` is dropped from the capabilities. This is the researcher's behaviour today (its
body says scratch only).

## The docs type (records + spec-writer)

Mode `write`, `group: docs`, sandbox `workspace-write`; both inputs have identical mode, group,
sandbox and capabilities, so the backlog rights (`DOCS`: may `add`, may not `pull`) and the guard
are unchanged. "Docs only" is prose today, not a guard rule; the merge keeps it prose. Add `skill`
to `capabilities`.

Effort (decided): records is low, spec-writer high; a role file takes one value. The role file says
`effort: medium`; a spec is raised, by model (the existing upward override). "Set per dispatch" needs
a host input the kit does not carry (Finding 3). After the build, check whether a host's dispatch
call can carry an effort at all; if it can, the roster rule "Effort is not settable per dispatch"
is rewritten and the brief states effort.

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
2. "Roles and what each is for": drop 7 names, add the read and docs types, one line listing the
   specialty skills.
3. "Choosing": delete "Effort is not settable per dispatch" only if the post-build effort check finds a host input. The
   Reviewer-model rule takes the read type's name.
4. "Creative dispatches" points at the three skills; the profile's roster line ("less art or audio")
   becomes "specialty skills not used", stated by a repo with no art or audio (an owner edit, to-do 3).
5. `subagent-dispatch` step 3 and `branch-review` step 3 take the new read type name.

## Guard and rolepolicy mapping

| type | `mode` | group (`roles.py`) | `LOCK_VERBS` | edit rule |
|---|---|---|---|---|
| read type | read | read-only | status | `role-edit`, checkout read-only to shell |
| docs type | write | docs | `AGENT_LOCK` | none (prose: docs only) |
| worker type with a specialty | write | builder | `AGENT_LOCK` | none |
| lander, test-runner | lander, test | lander, read-only | as above | as above |

No change to `kitlib/guard/**` or `rolepolicy.py`: modes are read from the role file by
`kitlib.content.role_mode`. The guard cannot see which skill an agent loaded, so no rule differs per
specialty (inference: that is what the owner wants).

## Migration inventory

Grep counts on main at the spec's base: retired names (`asset-smith|tech-artist|sound-smith|
spec-writer|researcher`) 105 hits in 41 files; qualified reviewer/records forms (`agent:`,
`pm-kit:`, `kit-`, "role"/"agent") 31 hits in 18 files.

| area (hits / files) | retired names | reviewer, records forms |
|---|---|---|
| content/roles | 9 / 6 (incl. `coordinator.md`, `{{agent:reviewer}}` example) | 2 / 1 |
| content/skills | 10 / 3 | 1 / 1 (`branch-review`) |
| rendered Claude agents; `.claude-plugin/plugin.json` | 14 / 6; 5 / 1 | 2 / 1 |
| rendered Codex agents `kit-*.toml`; skills copy | 14 / 6; 10 / 3 | 4 / 3 |
| kit docs (agent-review 12, ROSTER 6, guard 2, go-live 2, content 1) | 23 / 5 | 2 / 2 |
| kit code | `kitlib/backlog/roles.py` docstring 1 | `hosts/codex/adapter/command_canary.py` 4; `kitlib/gate/land.py` 1 (prose) |
| kit templates | 0 | `templates/common/AGENTS.md` 1 (`pm-kit:records`) |
| kit tests | 17 / 8 (`test_agents`, `test_render`, `test_roles`, fakehost fixtures) | 13 / 6 (`test_start`, codex, `test_land_strict`) |
| downstream repo with an art pipeline (a) | 15 lines / 4 files: agent profile, coordinator runbook 5, assets doc 2, `.kit.json` `doc_index.roles` 7 keys | |
| downstream repo (b) | 7 lines / 5 files: agent and coordinator profiles, REPO.md 2, brief template 2, conventions digest | |
| other repos | 0 live (audit notes and refusal logs are history) | |

Not names to change: "reviewer" as a function word in `docs/agent-profile.md`, `strict_defaults.py`,
risk maps and templates. `doc_index.py --role <name>` exits 2 on an unknown role, so a repo's per-role
topic map (a's `.kit.json`; b's brief template via `roles_doc`) carries the new names, and
per-specialty topics move into the brief. `tests/content/test_role_names.py` fails on a leftover name.

Deprecation (decided): a hard cutover in one landing. Nothing that dispatches the old type names may
be running when it lands. Shims for seven agent files would keep the list this ruling shrinks, and
`ALIASES` cannot carry a skill (Finding 9). A stale dispatch of a removed type fails loudly in the
host.

### Owner to-do (owner-only paths, literal)

1. `kitlib/backlog/roles.py`, module docstring: replace "(records and spec-writer: docs)" with
   `docs-writer`. Text only.
2. Repo (a) `.kit.json`, `doc_index.roles`: replace the keys reviewer, researcher, records,
   spec-writer, tech-artist, asset-smith, sound-smith with `analyst` and `docs-writer` (topics merged).
3. Agent-profile and coordinator-profile lines naming `records`, `spec-writer`, `tech-artist`,
   `asset-smith`, `sound-smith` in repos (a) and (b): new names. The PM's report lists the lines.
   A repo with no art or audio states "specialty skills not used" in its coordinator profile.
4. The kit's own `docs/agent-profile.md`, `AGENTS.md`, `.kit.json`, `tools/gate.json`,
   `.github/workflows/`: no retired name found; nothing to edit.

## Decisions

The owner accepted all eight recommendations.

1. Names: the read type is `analyst`, the docs type is `docs-writer`.
2. Research is returned as text plus a scratch file ("The merged read type").
3. `docs-writer` runs at medium effort, raised for specs; after the build, check whether a dispatch
   can set effort at all ("The docs type").
4. A hard cutover in one landing; nothing that dispatches the old type names may be running when it
   lands ("Migration inventory").
5. Minimum tiers as in the skills table, with no weak-worker on creative work ("Specialty skills").
6. The wider tools: the read type has `bash` and web; creative work gets a worker's full tool set,
   limits in skill text (Findings 4 and 5).
7. Skills are named `specialty-*`.
8. Repos with no art or audio say "specialty skills not used" in their coordinator profile (an owner
   edit, not a kit change; to-do 3).

## Build split

Order: A, then B (after the tier and weak-worker changes land: `docs/ROSTER.md` and `roster.md` conflicts), then C, D.

| branch | files | risk | size | waits for |
|---|---|---|---|---|
| A skills | `content/skills/specialty-*/` (7), rendered `hosts/codex/shell/skills/`; tests/skills, tests/content | strict `content/skills/**`; High | L | nothing |
| B types and dispatch (atomic hard cut) | delete 7 `content/roles/*.md`, add 2; `coordinator-dispatch`, `subagent-dispatch`, `branch-review`, `coordinator.md`; rendered `hosts/*/shell/agents/**`, `plugin.json`; ROSTER, guard, content, go-live docs; `templates/common/AGENTS.md`; tests; `command_canary.py` | strict `content/roles/**`, `content/skills/**`, `hosts/*/shell/agents/**`, `hosts/*/adapter/**`; High | L | A, the tier changes |
| C guard | none (research returns text plus a scratch file, no `writes`): not built | | | |
| D repo migration | to-do 1 to 3 (owner paths); non-owner docs in repos (a) and (b) | docs Low; owner paths owner-only | S | B landed |

Gates: A and B reach the full gate (`python tools/gate.py --full`; strict landing needs a fresh
strong reviewer, `kitlib/gate/strict_defaults.py`); `tools/render_content.py --check` clean. No
lock, landing or guard code is touched.
