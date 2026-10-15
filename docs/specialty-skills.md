# Specialty skills: task specialties as skills, not agent types

Status: DRAFT for owner review (record KIT-ROLES-SPECIALTY-SKILLS). Nothing here is built.
Written for the state after R-1983 (senior-worker opus/high, strong-worker opus/medium) and R-1984
(weak-worker, haiku 5.5) land; those rows are read from the owner's rulings, not from main.

## Owner ruling

R-1985, as relayed by the PM: "specialties become skills instead of agent types; written as a spec
first". Goal: more task specialties without more agent types. A specialty is a skill a generic agent
loads because its brief names it. The agent type sets model, effort and permissions; the skill sets
know-how. Every inference is marked "inference"; the rest is read from the code.

## Findings (code against the brief)

1. `kitlib/backlog/roles.py` needs no code edit. Its groups are read from the role files
   (`_ROSTER = _content.roster(KIT)`, `BUILDERS`, `DOCS`, `READ_ONLY` by `group`/`mode`), so retiring
   or merging a role file moves it. Only the module docstring ("records and spec-writer: docs") goes
   stale: one owner-path text edit.
2. The landing right is keyed on the type NAME, not only the mode: `kitlib/guard/rules.py`
   `LANDER = 'lander'` and `lander_check` (`event.agent_role != LANDER`, then
   `.kit.json` `roles.lander.edit_main_checkout`). A merged lander would lose it or need a code change.
3. `docs/ROSTER.md` and `content/skills/coordinator-dispatch/roster.md` ("Choosing") say "Effort is
   not settable per dispatch: pick the role for it". The owner's "effort is set per dispatch" for
   the docs type contradicts that. Effort is a role front-matter key (`kitlib/host/content.py`
   `OPTIONAL`, `EFFORTS`), rendered per file. I did not find a per-dispatch effort input in the kit;
   whether a host's dispatch call accepts one is unverified.
4. Capabilities are a union problem. `content/roles/reviewer.md` has `[read, search, find, bash]`;
   `researcher.md` has `[read, search, find, web-search, web-fetch, write]`. A merged read type
   carries both lists; a skill cannot take a tool away, so a review agent gains web tools and a
   research agent gains `bash`. Neither list has `skill`, which a type needs to load a skill
   (`CAPABILITIES`; docs/content.md "Agent view" step 6).
5. The three creative types have bounded lists (no `skill`, no web). Generic workers are
   `capabilities: all`, so creative work runs with a wider tool set than today (inference: least
   privilege shrinks; the skill text must say what the brief allows, e.g. paid generation needs the owner).
6. A skill has no tokens (docs/content.md "Skills"); the creative role bodies use `{{profile_path}}`,
   `{{user_folder}}`, `{{instructions_file}}` (19 token uses in the three files, by grep) and must be
   re-worded host-neutrally. The role bodies are 10.5, 9.8 and 10.5 KB: long prose, while a skill is
   a short checklist plus companion files (as `coordinator-dispatch/roster.md` is).
7. R-1983 changes the creative mapping: asset-smith and tech-artist are strong/high today, and
   strong-worker becomes opus/medium. Their equivalent is senior-worker (opus/high).
8. `kitlib/gate/land.py` `is_author` compares the verdict's reviewer and author agent types as
   free text; no landing code names `reviewer`. A merged read type changes nothing there. Review must
   stay on a read type: a worker type reviewing a worker's branch would match `is_author`.
9. `kitlib/content/__init__.py` `ALIASES` is one old name to one new name. It cannot say "dispatch a
   worker and load skill X", and the plugin lists each agent file (`.claude-plugin/plugin.json`).
   An alias keeps a session mark or `.kit.json` `roles.<old>` key working; it keeps no dispatch name.
10. Codex skill loading from outside its plugin folder is not verified (docs/content.md "Codex",
    skills copy); specialty skills inherit that open item.

## Target roster

| type | tier / effort | mode, group | note |
|---|---|---|---|
| weak-worker, worker, strong-worker, senior-worker | per R-1983/R-1984 | write, builder | the brief picks one per task; specialty skills ride on these |
| read type (reviewer + researcher) | standard / high | read, read-only | skills `specialty-review`, `specialty-research` |
| docs type (records + spec-writer) | standard / see Q3 | write, docs | skills `specialty-records`, `specialty-spec` |
| lander, test-runner, coordinator, pm, consultant, deep-worker | unchanged | unchanged | |

Retired: asset-smith, tech-artist, sound-smith, researcher, reviewer, records, spec-writer (7 files);
added: read type, docs type. Role files 16 now, 12 with weak-worker (R-1984).

## Specialty skills

Seven folders `content/skills/specialty-<name>/SKILL.md`, each with companion files for long
material, shipped by the existing render to both hosts (Claude loads `content/skills/`; Codex gets
the copy `hosts/codex/shell/skills/`, `docs/content.md` "Skills"). Names carry a `specialty-` prefix:
a skill named `branch-review` already exists (the coordinator's), and a bare `records` would read as
the retired type. Names start with a letter (`_NAME` in `kitlib/host/content.py`).

| skill | replaces | brief names a worker tier of (inference) | keeps from the role |
|---|---|---|---|
| specialty-3d-assets | asset-smith | senior-worker (was strong/high) | script is the asset, budgets, contact sheet, options-first, no rigging |
| specialty-tech-art | tech-artist | senior-worker (was strong/high) | art that is code, checked by rendering, art-direction stop |
| specialty-sound | sound-smith | worker; senior-worker for design (was an upward override) | measure clips, ends in a question for the owner |
| specialty-review | reviewer | read type | brief + diff range + logs, report format |
| specialty-research | researcher | read type | concepts with evidence, spec-writer hand-off |
| specialty-records | records | docs type | transcribe, never decide |
| specialty-spec | spec-writer | docs type | findings first, defaults labelled, inferences marked |

Each SKILL.md opens with a "Dispatch" line: the minimum worker tier, never weak-worker for creative
work, and the profile sections it reads (the creative roles read **Art**, **Shared resources**).
The creative brief block (FEEL / REFERENCES / AVOID / SCOPE OF CHOICE) and options-first move from
`coordinator-dispatch` "Creative dispatches" into the three creative skills' companion file.

## The merged read type (reviewer + researcher)

Mode `read`, sandbox `read-only`, standard tier, effort high: both inputs agree, so nothing
changes. The reviewer stays at the default standard tier; the "Reviewer model" rule in `roster.md`
raises it per dispatch, unchanged. Guard: `LOCK_VERBS['read'] = {'status'}`, checkout read-only
to the shell, owner paths read-only (`kitlib/guard/rolepolicy.py` `RULES`); identical to today.

What replaces the researcher's `write` capability:

- A. Report returned as text, plus a file in the agent's own scratch folder for long notes (the
  rule already in skill `subagent-dispatch` step 4). Scratch is outside the checkout, so
  `rolepolicy.check` role-edit never fires. No `writes`.
- B. `writes: [docs/ops/research/*.md]` in the role file. `role_may_write` honours it for file-tool
  edits only (a shell write stays refused; `*` never crosses `/`). It applies to every dispatch of
  the type, so a review agent could write there too, the notes land in a worktree no read type can
  commit, and the policy is opt-in (`.kit.json` `guard.role_policy`).
- C. Text only, `write` dropped. Loses the survivable long-notes file.

Recommendation: A. It is the researcher's behaviour today (its `capabilities` list `write`, its body
says scratch only), adds no repo write path, and the output the owner wants is the report.
Inference: the notes-folder option buys nothing a scratch file does not.

## The docs type (records + spec-writer)

Mode `write`, `group: docs`, sandbox `workspace-write`; both inputs have identical mode, group,
sandbox and capabilities, so the backlog rights (`DOCS`: may `add`, may not `pull`) and the guard
are unchanged. "Docs only" is prose today, not a guard rule; the merge keeps it prose. Add `skill`
to `capabilities`.

Effort: records is low, spec-writer high; the role file takes one value. The owner's "set per
dispatch" needs a host input the kit does not carry (Finding 3). Default: `effort: medium` in the
role file, and a spec that needs more is raised in model (the existing upward override), not effort.
Open: Q3.

## Lander and test-runner stay separate (confirmed)

`kitlib/guard/rolepolicy.py`, by mode:

| | lander | test |
|---|---|---|
| edit in own checkout | refused (`NO_EDIT`) | refused (`NO_EDIT`) |
| shell write to own checkout | allowed (not in `READ_ONLY_CHECKOUT`) | refused (`READ_ONLY_CHECKOUT`) |
| lock verbs (`LOCK_VERBS`) | `status`, `run` only | `AGENT_LOCK`: also `background`, `stop`, `hold`, `division`, `game` |
| `git fetch` / `git merge --ff-only` in main | allowed by name: `rules.lander_check`, needs `.kit.json` `roles.lander.edit_main_checkout` | not allowed by it; the shared rules judge |

The modes are opposites (landing rights and a writable checkout against more lock verbs and a
read-only checkout). A skill is text; mode, tools and the name test in `lander_check` belong to the
type, so a skill cannot grant or remove either side. Merging would force one mode that is the union
(landing rights plus the test lock verbs on a light-tier agent): a widening of a strict path. Keep both.

## Dispatch skill changes

`content/skills/coordinator-dispatch/SKILL.md` and `roster.md`:

1. "Which role, which tier" gains a step: match the task to a specialty skill (table above); the
   brief names it ("Load skill `specialty-sound` first") and names the worker tier. The tier ladder
   stays the owner's R-1983/R-1984 text; this adds only the specialty's minimum tier.
2. "Roles and what each is for": drop 7 names, add the read and docs types; one line listing the
   specialty skills.
3. "Choosing": delete "Effort is not settable per dispatch" only if Q3 verifies a host input;
   otherwise it stays. Reviewer-model rule: reword "reviewer" to the read type's name.
4. "Creative dispatches": point at the three skills; the profile's roster line ("less art or audio")
   becomes a list of specialty skills the repo does not use.
5. Briefs to the docs type name `specialty-records` or `specialty-spec`; `subagent-dispatch` step 3
   and `branch-review` step 3 take the new read type name.

## Guard and rolepolicy mapping

| type | `mode` | group (`roles.py`) | `LOCK_VERBS` | edit rule |
|---|---|---|---|---|
| read type | read | read-only | status | `role-edit`, checkout read-only to shell |
| docs type | write | docs | `AGENT_LOCK` | none (prose: docs only) |
| any worker type with a specialty | write | builder | `AGENT_LOCK` | none |
| lander, test-runner | lander, test | lander, read-only | as above | as above |

No change to `kitlib/guard/**` or `rolepolicy.py` is needed: modes are read from the role file by
`kitlib.content.role_mode`. The guard cannot see which skill an agent loaded, so no rule can differ
per specialty (inference: that is what the owner wants).

## Migration inventory

Counts by Grep on main 85e31d20, the five retired names plus researcher/spec-writer
(`asset-smith|tech-artist|sound-smith|spec-writer|researcher`: 105 hits, 41 files) and qualified
reviewer/records forms (`agent:`, `pm-kit:`, `kit-`, "role"/"agent": 31 hits, 18 files).

| area (kit: hits / files; other repos: lines / files) | retired names | reviewer, records forms |
|---|---|---|
| content/roles | 9 / 6 (incl. `coordinator.md`, `{{agent:reviewer}}` example) | 2 / 1 |
| content/skills | 10 / 3 | 1 / 1 (`branch-review`) |
| rendered Claude agents + `.claude-plugin/plugin.json` | 14 / 6 + 5 / 1 | 2 / 1 |
| rendered Codex agents `kit-*.toml` + skills copy | 14 / 6 + 10 / 3 | 4 / 3 |
| kit docs (agent-review 12, ROSTER 6, guard 2, go-live 2, content 1) | 23 / 5 | 2 / 2 |
| kit code | `kitlib/backlog/roles.py` docstring 1 / 1 | `hosts/codex/adapter/command_canary.py` 4; `kitlib/gate/land.py` 1 (prose) |
| kit templates | 0 | `templates/common/AGENTS.md` 1 (`pm-kit:records`) |
| kit tests | 17 / 8 (`test_agents`, `test_render`, `test_roles`, fakehost fixtures) | 13 / 6 (`test_start`, codex, `test_land_strict`) |
| retribution | 7 / 5 (`.claude/coordinator-profile.md`, `.claude/agent-profile.md`, `docs/ops/REPO.md` 2, `brief-template.md` 2, `conventions-digest.md`) | risk-map: generic word |
| GriffBall | 15 / 4 (`.claude/agent-profile.md`, `docs/ops/coordinator-runbook.md` 5, `assets.md` 2, `.kit.json` `doc_index.roles` 7 keys) | risk-map: generic word |
| other repos | 0 live (project-manager audit notes and refusal logs: history) | |

Not names to change: the word "reviewer" used as a function in `docs/agent-profile.md`,
`strict_defaults.py`, risk maps and templates. `doc_index.py --role <name>` exits 2 on an unknown
role, so a repo's per-role topic map (GriffBall `.kit.json`; retribution `brief-template.md` via
`roles_doc`) must carry the new names, and per-specialty topics move into the brief.

Deprecation: recommend a hard cut in one landing. Shims for seven agent files would keep the list
this ruling shrinks, and `ALIASES` cannot carry a skill (Finding 9). A stale dispatch of a removed
type fails loudly in the host. `tests/content/test_role_names.py` already fails on a leftover name.

### Owner to-do (owner-only paths, literal)

1. `kitlib/backlog/roles.py`, module docstring: replace "(records and spec-writer: docs)" with the
   docs type's chosen name. Text only; no code change.
2. GriffBall `.kit.json`, `doc_index.roles`: replace the keys reviewer, researcher, records,
   spec-writer, tech-artist, asset-smith, sound-smith with the two new type names (topics merged).
3. GriffBall `.claude/agent-profile.md` (the `pm-kit:records` / `pm-kit:spec-writer` line) and
   retribution `.claude/agent-profile.md` ("`records` and `spec-writer` may edit") and
   `.claude/coordinator-profile.md` ("less `tech-artist`, `asset-smith` and `sound-smith`"): new names.
4. The kit's own `docs/agent-profile.md`, `AGENTS.md`, `.kit.json`, `tools/gate.json`,
   `.github/workflows/`: no retired name found; nothing to edit.

## Candidate names (owner picks)

- Read type: `analyst`, `inspector`, `investigator`. Recommend `analyst` (neither `reviewer` nor
  `researcher`, so neither task reads as the odd one out). Fourth option: keep `reviewer` and drop
  `researcher`: fewest renames (launcher tests, codex canary, both repos' profiles), but research
  dispatches carry a misleading name.
- Docs type: `docs-writer`, `scribe`, `documenter`. Recommend `docs-writer` (the owner's term; the
  backlog group is already `docs`).

## Owner questions

1. Names for the read and docs types (Candidate names). Recommend `analyst` and `docs-writer`.
2. Researcher write replacement: A (text + scratch file), B (`writes` notes folder) or C (text only).
   Recommend A.
3. Docs type effort. Recommend `effort: medium` in the role file, spec raised by model; and one
   check first of whether the hosts' dispatch call can carry an effort at all. If yes, the roster
   rule "Effort is not settable per dispatch" is rewritten and the brief states effort.
4. Hard cut or shim period. Recommend hard cut, one landing.
5. Specialty to worker tier: 3D assets and tech-art to senior-worker, sound to worker (senior-worker
   for design), never weak-worker for creative work. Recommend as in the skills table.
6. Accept the widened tools: the read type has both `bash` and web tools, creative work runs with a
   worker's full tool set; the skills carry the limits in text. Recommend accept; the alternative is
   keeping asset-smith, tech-artist and sound-smith as types.
7. Skill names `specialty-*` prefix. Recommend yes.
8. Do repos that exclude art or audio (retribution) say so as "specialty skills not used" in the
   coordinator profile? Recommend yes (owner edit, to-do 3).

## Build split

Order: A, then B (after R-1983/R-1984 land: `docs/ROSTER.md` and `roster.md` conflicts), then C, D.

| branch | files | risk | size | waits for |
|---|---|---|---|---|
| A skills | `content/skills/specialty-*/` (7 folders), rendered `hosts/codex/shell/skills/`; tests/skills, tests/content | strict: `content/skills/**`; High | L | Q2, Q5, Q7 |
| B types and dispatch (atomic hard cut) | delete 7 `content/roles/*.md`, add 2; `coordinator-dispatch/{SKILL,roster}.md`, `subagent-dispatch`, `branch-review`, `coordinator.md` example; rendered `hosts/*/shell/agents/**`, `.claude-plugin/plugin.json`; `docs/ROSTER.md`, `docs/guard.md`, `docs/content.md`, `docs/go-live.md`; `templates/common/AGENTS.md`; tests (`test_agents`, `test_roles`, `test_render`, fakehost fixtures, `test_start`, codex, `test_land_strict`); `command_canary.py` | strict: `content/roles/**`, `content/skills/**`, `hosts/*/shell/agents/**`, `hosts/*/adapter/**`; High | L | A, R-1983/R-1984, Q1, Q3, Q4, Q6 |
| C guard | none planned; only if Q2 is B (role file `writes:`, test in `tests/guard/test_rolepolicy.py`) | strict: `kitlib/guard/**` if touched; High | S | Q2 |
| D repo migration | to-do 1 to 3 (owner paths); non-owner repo docs (`REPO.md`, `brief-template.md`, `coordinator-runbook.md`, `assets.md`, `conventions-digest.md`) | docs: Low; owner paths: owner-only | S | B landed |

Gates: A and B reach the full gate (`python tools/gate.py --full`; strict landing needs a fresh
strong reviewer, `kitlib/gate/strict_defaults.py`); `tools/render_content.py --check` clean.
No lock, landing or guard code is touched unless Q2 is B.
