# Multi-host architecture plan

Status: approved direction. The owner's decisions are in §0, and ARCHITECTURE.md was amended to match (step 0).

**Goal:** one host-neutral kit, with a thin entry-point plugin for each AI platform. The platforms
today are Claude Code and Codex, and adding another should mean adding a folder, not forking the
kit. The tools are shared already. The hooks, agent definitions, skills and instructions are
still written separately for each host. The agent-to-agent bridge from the Codex branches is a
host-neutral service that each host plugs into.

Sources read: `main` @ 3e64fec, `origin/codex/companion-plugin` @ 82cff0d and
`origin/codex/robust-bridge-plan` @ 1cdc244. The bridge branch carries all of companion-plugin
except its last 4 commits.

## 0. Owner decisions (2026-10-13)

| # | Question | Decision | What it means for the plan |
|---|---|---|---|
| 1 | Order with the Codex branches | Refactor main first, then rebase the branches | Steps 1–6 land on main. `codex/companion-plugin` and `codex/robust-bridge-plan` are rebased onto the result, not merged first. |
| 2 | One roster or two | Every role exists on every host | One role list. A host differs only in how it maps tier to model and capability to tools. `pm`, `implementation-worker` and `opus-worker` are reconciled into the one list. |
| 3 | Skill text | Identical on every host, in a form decided later | Skill bodies have no host blocks. Host differences live in the host's instructions and adapter, never in a skill. |
| 4 | Role marker | Codex uses the same role variable | One `PM_KIT_ROLE` everywhere. `PM_KIT_CODEX_ROLE` is retired. The host is named by a separate `PM_KIT_HOST`. |
| 5 | Bridge placement | An optional install | The bridge ships as an optional package that the core never imports. Phase 0 is explained in §2.4. |
| 6 | Release binding | One release value for the entire plugin | One kit version and one release ID cover the core and every host shell. Every manifest takes it from one source. |
| 7 | Generated files | Owner asked what these are | Answered in §2.3. |
| 8 | Future hosts | Scale to any platform | A new host is one folder under `hosts/<name>/` that follows a written checklist and passes the conformance suite (§2.6, §2.7). |
| 9 | ARCHITECTURE.md | Change it to match | Done with this plan (step 0). |
| 10 | `implementation-worker` vs `worker` | Merge into one `worker` role | Codex's `implementation-worker` brief folds into `worker`. |
| 11 | PM on Claude | The PM is an agent file | `content/roles/pm.md`, rendered for every host. On Claude the launcher starts it with `claude --agent pm-kit:pm`. |
| 12 | Rename `opus-worker` | `senior-worker` | The rename lands with step 5. The old name stays as an alias in the launcher for one release. |
| 13 | Folder layout | Uniform `hosts/<name>/` | `claude/` moves to `hosts/claude/` in step 7, and Codex lands as `hosts/codex/` in step 9. |
| 14 | Form of the shared skill text | C: checklist + pointer | One line per step with its command, the "never" list, then the tool doc. A test checks that each `tools/*.py` command and flag a skill names exists (§5). |
| 15 | Linux and macOS | A separate refactor after this plan | The host work keeps Windows behaviour identical. Rules in §6 stop it adding Windows debt. The OS work is §6. |

## 1. Where things stand

### What is shared and works
- The tools (`tools/` → `kitlib/`): gate, lock, commit, worktree, ledger, pending and the others.
- `kitlib.guard.decide`. Codex calls it too, with no copy.
- `kitlib.lockmcp.server`. Both hosts' lock MCP entry points wrap it.
- `kitlib.attribution`, from the Codex branch. It removed the trailer logic that commit and
  syncbase each held.
- `kitlib.packageexport` and `kitlib.kitrelease`: one immutable runtime release that both
  plugins can bind to.
- `kitlib.hoststart.shared_checks`: PM and coordinator start checks with no host in them.
  Only Codex uses it so far.

### What is duplicated or forked
| Concern | Claude (`main`) | Codex (branch) | Problem |
|---|---|---|---|
| Agent definitions | 15 × `claude/agents/*.md`, 63–170 lines of detailed prose each | 16 × `codex/agents/*.toml`, generated from `codex/roles.json`, about 6 lines each with the same 900-character preamble | Two rosters (Codex adds `pm` and `implementation-worker` and drops `opus-worker`). The prose is written twice. No test checks the committed TOMLs against their generator. |
| Skills | 12 under `claude/skills/` | 15 under `codex/skills/`; 11 have the same names | Each shared skill is a full rewrite: only the `name:` line survives. The procedures drift, e.g. branch-sync has 10 steps on one side and 3 paragraphs on the other. |
| Instructions | `CLAUDE.md` and `AGENTS.md` are near-duplicates, and CLAUDE.md is stale (`hooks/` vs `claude/`). `templates/common/CLAUDE.md` has no AGENTS.md twin. | Codex reads AGENTS.md | Two files hold the same rules. |
| Hook entries | `claude/hooks/guard.py` (97 lines), `coordinator_session_start.py`, `pm_session_start.py` | `codex/hooks/guard.py` (53 lines), `session_start.py` | The boilerplate is written twice: fail-closed deny, settings entry, pythonw lookup. |
| Guard input | `kitlib.guard.decide` takes the **raw Claude PreToolUse payload** (`tool_name`, `agent_id`, `agent_type`) and `hook_output()` emits Claude JSON from inside kitlib | `codex/pmkit_codex/actions.py` reshapes Codex payloads into *Claude-shaped* events | The neutral `Action` that ARCHITECTURE.md "Host split" promises was never built. Codex works around that by pretending to be Claude. |
| Role policy | Implicit in guard rules | Extra layer in `guard_adapter.evaluate_bound` and `kit_adapter._workflow`: per-role edit bans, owner paths read-only, `shell_gap`, lock-verb allowlist | Policy with no host in it lives in a host folder, so Claude does not get it. |
| Role marker | `PM_KIT_ROLE` | `PM_KIT_CODEX_ROLE` | Two variables for one concept. |
| Host selection | none | `--host` on new_worktree, `--provider` on commit, sync_base, ledger and pending | Two flag names for one concept, chosen separately in each tool. |
| Session discovery | `claude/claude_host.py`, injected into pmwatch by hand | `codex/pmkit_codex/session_host.py` implements `SessionHost` | Claude has not adopted the protocol it is named after. |
| Release binding | the plugin runs from the source checkout | `codex/pmkit_codex/profile.py` and `release_binding.py` verify the pinned release before importing kitlib | Claude would need the same bootstrap, and it lives in a Codex folder. |

### Structural faults
1. **Reversed layer dependency.** `kitlib/hosts/__init__.py` uses `importlib` to load
   `claude.host_profile` and `codex.host_profile`. So kitlib depends on the host folders, which
   breaks the rule "kitlib never imports this package" (`claude/__init__.py`).
2. **A half-used abstraction.** Of the `kitlib.hosts.HostProfile` fields, only `name` and the
   worktree fields have consumers. `process_names`, `role_environment`, `state_directory`,
   `owner_paths` and `lock_tool` are declared and read nowhere. A second, unrelated class named
   `HostProfile` lives in `codex/pmkit_codex/profile.py`.
3. **Claude leaks into kitlib**, around 60 sites. The main ones:
   - `.claude/worktrees` defaults
   - `~/.claude/machine-lock`
   - `CLAUDE_CODE_SESSION_ID` and the lock actor key `"claude"`
   - `CLAUDE_AGENT_MODEL`
   - the `mcp__plugin_pm-kit_machine-lock__` prefix in guard rules
   - the whole of `kitlib/launcher` and `kitlib/pluginsource/claude.py`
   - `claude.exe` Remote Control names in pmstart and coordinatorstart

   Each one is a place where a third host would need an `if`.
4. **The bridge's interface is not declared.** `kitlib/bridge/ports.py` holds the seam as first
   designed, and nothing imports it. The real seam is duck-typed in `bridgewin/bootstrap.py`.
   `tools/bridge_service.py` wires hosts with `if 'claude'` / `if 'codex'`, and `hook_sink` is
   looked up only for `'claude'`.
5. **Production code imports probe code.** `kitlib.bridgeprobe.winpipe` is the named-pipe base
   for production code. `bridgeprobe.runtime.ProbeError` and the Phase 0
   `bridge/bootstrap.py` helpers (`identifier`, `digest`) are imported almost everywhere.
   About 3,500 of the bridge's ~11,100 code lines are probe, Phase 0 or scaffold code.


## 2. Target shape

Four layers, each depending only on the layers below it. Every host-specific thing sits in one
folder per host, so a new platform is slotted in by adding a folder:

```
 ┌───────────────────────────────────────────────────────────────────┐
 │ 4. Host shells: hosts/<name>/shell/  (data + entries ≤20 lines)    │
 │    manifests, hook registration, rendered agents/skills/instr.    │
 ├───────────────────────────────────────────────────────────────────┤
 │ 3. Host adapters: hosts/<name>/adapter/  (Python)                 │
 │    implements kitlib.host.HostAdapter: event parse/render,        │
 │    sessions, command host, content renderer, launcher,            │
 │    and (if bridge installed) the bridge port                      │
 ├───────────────────────────────────────────────────────────────────┤
 │ 2. Host contract: kitlib/host/  (protocols + neutral types only)  │
 ├───────────────────────────────────────────────────────────────────┤
 │ 1. Core: kitlib/*  (tools' logic, guard rules, role policy, lock, │
 │    gate, release); tools/ (thin CLIs)                             │
 └───────────────────────────────────────────────────────────────────┘
   content/   canonical roles, skills, instructions (host-neutral)
   optional/bridge/  bridge core + per-host bridge halves (optional install)
   hosts.json registry: host name → adapter module, shell dir
```

Proposed tree:

```
kitlib/                core, never imports hosts/ or optional/
kitlib/host/           the host contract
tools/                 shared CLIs (read PM_KIT_HOST)
content/roles/         one file per role
content/skills/        one folder per skill, identical on every host
content/instructions/  AGENTS.md body and the profile template
hosts.json             {"claude": {...}, "codex": {...}}
hosts/claude/adapter/  Python adapter
hosts/claude/shell/    .claude-plugin/, hooks/hooks.json, agents/, skills/ (rendered)
hosts/codex/adapter/
hosts/codex/shell/     plugin.json, mcp.json, hooks/hooks.json, agents/, skills/ (rendered)
optional/bridge/       bridge core, service, frontend; hosts' bridge halves under
                       optional/bridge/hosts/<name>/
kit.json               the one version and release identity (§2.5)
```

Moving `claude/` to `hosts/claude/` changes paths that kitlib refers to today: `claude/hub/...`,
the gate's strict defaults and machine setup's guard path. Those references become lookups
through the host registry as part of step 6, and the move itself is step 7. The Claude
marketplace entry (`.claude-plugin/marketplace.json`) stays at the repo root. As built in step 7, so
does `.claude-plugin/plugin.json`, pointing into `hosts/claude/shell`: the plugin root must hold
the manifest, and it is the whole kit because the hooks and the MCP server run kitlib and tools
beside them (an installed plugin reaches only files under its root).

### 2.1 The host contract (`kitlib/host/`)
One package of protocols and frozen dataclasses. It contains no host names and imports nothing
from `hosts/` or `optional/`.

- `HostEvent` → `Action(kind, tool, path, command, cwd, role, agent)` and
  `Decision(allow | deny | ask, rule, reason, instead)`.
  - `kind` is one of `edit`, `shell`, `mcp`, `session_start` or `subagent_start`.
  - `agent` is an opaque subagent identity, or `None` for the main session.
- `HostAdapter`, one per host. It has:
  - `name` and `profile`
  - `parse(event_name, payload) -> Action`
  - `render(event_name, Decision) -> (stdout, exit_code)`
  - `sessions() -> SessionHost`
  - `command_host() -> CommandHost | None`
  - `content() -> ContentRenderer`
  - `launcher() -> Launcher | None`
  - `plugin_installer() -> Installer | None`
- `HostCapabilities`, declared as data in `hosts.json` and read by the core. It records:
  - which hook events the host has
  - whether it supports `ask`
  - whether a subagent's identity reaches its hooks
  - whether it has MCP and skills
  - its agent file format and its MCP tool-name format

  The core uses these to degrade one way for every host. For example, no `ask` becomes `deny`
  with the reason, and no subagent identity in hooks becomes the role-only policy. Adapters do
  not each improvise.
- `HostProfile` keeps only fields that have consumers: owner paths, worktree root, state dir,
  agent process image names (for the lock's `AGENT_HOSTS`) and the MCP tool-name format.
  The role variable is no longer per host (decision 4).
- **Registry inversion.** kitlib never imports a host. The host is selected at the edge:
  - A host's entry points build their adapter and pass it in.
  - Shared CLIs in `tools/` read `PM_KIT_HOST` and resolve the adapter through `hosts.json`,
    which is data, not a kitlib import. Unset means `claude`, for back-compatibility.
  - `--host` and `--provider` merge into one `--host` flag, which defaults from `PM_KIT_HOST`.
    `--provider` stays as an alias for one release.
- **Roles.** `PM_KIT_ROLE` is the only role marker (decision 4). The host adapter sets
  `PM_KIT_HOST`. The child-environment scrub that Codex does today (`environment.py`) becomes
  core code: it clears every host's session variables when a child is launched on a different
  host, so a Claude-marked parent cannot leak its role or session into a Codex child.

### 2.2 Guard and policy, once
- `kitlib.guard.decide(action: Action, ctx) -> Decision`. Every Claude-payload field read moves
  into `hosts/claude/adapter/events.py`:
  - `rules.py` tool-name sets
  - `agent_id` / `agent_type`
  - `tag_role`
  - `config/policy.role_name`
  - `backlog/roles`

  `hook_output()` moves to the Claude adapter's `render`.
- Codex's `actions.py` becomes `hosts/codex/adapter/events.py`. It produces `Action` directly,
  not Claude-shaped dicts.
- Codex's policy that names no host moves into kitlib as `kitlib/guard/rolepolicy.py` and
  applies to every host: role edit bans, read-only owner paths (`ConfigView`), the
  shell-cwd-in-checkout check, the lock-verb allowlist and `shell_gap`.
- **One hook entry per host event, ≤20 lines.** For example, `hosts/<name>/shell/hooks/run.py
  <event>` runs the kit's boot check (§2.5), builds the adapter and calls
  `kitlib.host.hookmain.run(adapter, event, stdin)`. The fail-closed deny, timeout handling and
  settings-entry rendering live in `hookmain` once.

### 2.3 Single-source content (`content/`) and the generated files
**What "generated files" means.** A host needs agents, skills and instructions in its own file
format:
- Claude: agents as `.md` with YAML front matter, skills as `SKILL.md` folders, and `CLAUDE.md`.
- Codex: agents as `.toml` with `developer_instructions`, skills as `SKILL.md` folders, and
  `AGENTS.md`.

Today both sets are written by hand. The plan writes each one **once** in `content/`. A script
(`tools/render_content.py`, logic in `kitlib/content`) then asks every host adapter to write its
own files from it. The written-out files are the "generated files":
- `hosts/<name>/shell/agents/*`
- `hosts/<name>/shell/skills/**`
- the host's instructions file
- the host's plugin manifest, which carries the one version (decision 6)

Codex already works this way: `codex/roles.json` is rendered into `codex/agents/*.toml`, which
are committed.

**Recommendation:** commit the generated files, with a test that fails when they differ from
what the renderer writes now.
- Plugins load straight from the repo or the release, so no build step runs at install.
- Reviewers see the real files each host reads.
- The drift test stops anyone editing a generated file by hand.

The other option is rendering at install. That keeps the repo smaller but adds a build step to
every install and update. It also means nobody reviews exactly what the agents read.

- **Roles.** Each role lives in `content/roles/<role>.md`, and every role exists on every host
  (decision 2):

  ```yaml
  name: worker
  summary: Build routine, bounded changes in one assigned worktree.
  tier: standard        # light | standard | strong | deep  (host maps tier→model)
  effort: medium
  sandbox: workspace-write   # read-only | workspace-write
  capabilities: [dispatch, message]   # host maps to its tool lists
  ```

  The body is neutral prose. A role or skill is named by its bare role name ("dispatch the
  `test-runner` role"). The host's instructions say once how a role name maps to that host's
  dispatch name (`pm-kit:test-runner` on Claude, `kit-test-runner` on Codex). The few values that
  must differ, such as the worktree root, use tokens (`{{worktrees_dir}}`). There are no host
  blocks.
- **Reconciled roster.** One list, which is the union of today's two lists:
  - Claude's 15 roles.
  - `pm`, an agent file on every host. On Claude the launcher starts it with
    `claude --agent pm-kit:pm` (decision 11).
  - Codex's `implementation-worker` merged into `worker` (decision 10).
  - `opus-worker` renamed `senior-worker` (decision 12).
- **Skills.** `content/skills/<skill>/SKILL.md` is byte-identical on every host (decision 3).
  The 11 forked skills are merged into one text each, which the owner decides on later.
  Host-only skills (`coordinator-dispatch`, `kit-setup`, `kit-pm`, `kit-coordinator`,
  `kit-worker`) are either made neutral and shipped everywhere, or their content moves into
  role files or the host's instructions. A skill that only one host ships breaks decision 3.
- **Roster table.** `docs/ROSTER.md` holds each role's tier, plus a tier → model table per host.
  `kitlib/roster` checks it against `content/roles`. The `KIT_AGENTS` list in the launcher is
  derived from it, not copied by hand.
- **Instructions.** `content/instructions/AGENTS.md` is the canonical text and renders to
  `AGENTS.md`. `CLAUDE.md` renders as `@AGENTS.md` plus the one Claude-specific line. The repo
  profile moves from `.claude/agent-profile.md` to a neutral `docs/agent-profile.md`, and the
  old path stays as a stub during migration. `templates/common/` follows the same pattern for
  target repos.

### 2.4 The bridge as an optional install
**What the Phase 0 probe code is.** Before building the bridge, the Codex agent wrote throwaway
experiments to test whether each idea was possible on Windows. They covered:
- isolating a helper in an AppContainer sandbox
- electing one leader among several bridge processes
- handing pipe handles between processes
- passing work to a worker broker
- reading Claude's and Codex's live session files and wire schemas to see what they contain

They saved the results as evidence (`docs/bridge-phase0-*.md` and three `*-evidence.json`
files). That code is:
- `kitlib/bridgeprobe/` (1,858 lines)
- `claude/bridge_phase0.py`
- `tools/bridge_live_probe.py` and `tools/bridge_schema_probe.py`
- `kitlib/bridge/{bootstrap,journal,pipeline,restart,delegation,schema_probe}.py`
- the unused scaffold `kitlib/bridge/{ports,disabled,status}.py` and `tools/bridge.py`

It is not the working bridge. Three of its helpers became load-bearing by accident:
- `bridgeprobe/winpipe.py`: named pipes, pipe ACLs and process SIDs
- `bridgeprobe.runtime.ProbeError`
- `bridge/bootstrap.identifier` and `bridge/bootstrap.digest`

**Plan:**
- **Layout:** `optional/bridge/` holds the bridge core (`messages`, `outbox`, `dispatch`,
  `pairing`, `mcp`, `callers`, `owner`, `prompt`, `jsonwire`), the service and the MCP frontend.
  Each host's half lives in `optional/bridge/hosts/<name>/`. The core kit never imports any of
  it.
- **Installing:** installing the bridge registers its MCP server and hook in each host's shell
  through the host adapter. With the bridge not installed, no host shell mentions it.
- **Declared port** (`optional/bridge/port.py`), replacing the dead `ports.py`:
  - `SenderAttestor.attest(caller_proof) -> SenderAuthority`
  - `DeliveryAdapter.describe() / send(msg) / reconcile(msg, native_id)`
  - optional `ReturnChannel` and `HookSink`

  The service loops over the installed hosts with no `if 'claude'` / `if 'codex'`.
- **Promoted helpers:** `winpipe` → `kitlib/proc/pipe.py`, next to `job` and `process`.
  `ProbeError`, `identifier` and `digest` → `kitlib/proc/errors.py` and a small
  `kitlib/ident.py`.
- **Probe code:** once nothing imports it, delete it. Git history keeps it. A test asserts that
  nothing outside the deleted set imports it.
- **Docs:** the 24 bridge docs and 3 evidence files collapse into:
  - `optional/bridge/README.md`: design and contracts
  - `optional/bridge/STATUS.md`: a capability matrix per host and direction
  - the history, left in git

### 2.5 One release value
- `kit.json` at the root holds the one `version` and is the input to the release identity
  (decision 6).
- The renderer writes that version into every host manifest: `.claude-plugin/plugin.json` and
  marketplace, and Codex `plugin.json` and `mcp.json`. Today Claude is at 0.1.0 and Codex at
  0.3.5.
- `kitlib.kitrelease` exports one release that contains the core, every host shell and, when
  installed, the bridge. Each release has one `PM_KIT_RELEASE_ID`.
- Every host's entry points run the same stdlib-only boot check, `boot/kitboot.py`. It is
  lifted from Codex's `profile.py` and `release_binding.py`, and runs before any kitlib import.
  With no `PM_KIT_ROOT` or release ID set, it uses the source checkout, which is today's
  behaviour.
- The release `ROOTS` list is read from `hosts.json` instead of naming `claude` and `codex`.

### 2.6 Conformance tests per adapter
- `tests/host/conformance.py` is one shared `unittest` mixin. Each host's test module subclasses
  it with recorded payload fixtures. It covers:
  - parse → `Action` for every guarded tool
  - render of allow, deny and ask, with the host's declared degradation
  - session listing on fixture files
  - content rendering matching the committed files
  - the release boot check
  - when the bridge is installed, the bridge port's describe and send on a fake pipe
- Passing the suite is what "supported host" means.

### 2.7 Adding a host (the slot)
`docs/new-host.md` becomes the checklist. A new host needs:
1. **An entry in `hosts.json`:** adapter module, shell directory, capabilities and tier → model
   map.
2. **`hosts/<name>/adapter/`**, implementing:
   - `parse` and `render` for its hook events
   - `SessionHost`
   - `ContentRenderer`, which turns the neutral role, skill and instruction files into the
     host's formats
   - optionally `CommandHost`, `Launcher` and `Installer`
3. **`hosts/<name>/shell/`:** a manifest template and the hook entries, each ≤20 lines and the
   same for every host apart from the host name.
4. **Recorded payload fixtures** under `tests/hosts/<name>/`, and a test class that subclasses
   the conformance mixin.
5. **Optionally,** `optional/bridge/hosts/<name>/` for messaging.

No file in `kitlib/`, `content/` or another host's folder changes. A test enforces this by
checking that host names appear only in `hosts.json`, `hosts/`, `optional/bridge/hosts/` and
tests.

## 3. Migration order

Main is refactored first (decision 1). Each step lands through the gate and keeps Claude's
behaviour unchanged.

| Step | Work | Depends on |
|---|---|---|
| 0 | ARCHITECTURE.md amended: new Host contract, Hosts, Content and Optional layers, the Roles contract, Packaging, and a Hosts section in place of "Host split". **Done.** | — |
| 1 | Cherry-pick the host-neutral library parts of the Codex branch onto main: `kitlib.attribution`, `packageexport`, `kitrelease`, `hostrpc`, `hoststart`, and the worktree `--host` support. These are needed by both hosts and conflict least. | 0 |
| 2 | `kitlib/host/` (types and protocols), `hosts.json` and the `PM_KIT_HOST` / `PM_KIT_ROLE` selection. Delete `kitlib/hosts`' importlib of host folders. Merge `--host` and `--provider`. | 1 |
| 3 | Move the guard to `Action`/`Decision`, with the Claude adapter doing parse and render. Add `hookmain`. Claude's hooks become ≤20-line entries. The existing guard tests run unchanged through Claude fixtures. | 2 |
| 4 | `kitlib/guard/rolepolicy.py`. Lift the role policy from the Codex branch's `guard_adapter` and `kit_adapter`, so Claude gets it too. | 3 |
| 5 | `content/` and the renderer, starting with Claude. The rendered agents must be byte-identical to today's files before anything else changes. Then the reconciled roster, the neutral profile and AGENTS.md as the canonical instructions. The skill merge waits for the owner's skill-text decision. | 2 |
| 6 | Remove the Claude leaks from kitlib, one PR per cluster: lock actor key → `host`, session env, worktree defaults, MCP prefix, commit model env, hub paths, launcher, pluginsource. | 2 |
| 7 | Move `claude/` → `hosts/claude/{adapter,shell}`. `kit.json` and the one release value. `boot/kitboot.py`. **Done**, with the rendered manifests and the kit header hook (docs/content.md "Manifests", "Kit header"; docs/kitboot.md; docs/machine-setup.md "Moved hook paths"). | 5, 6 |
| 8 | Conformance suite, required in the gate. `docs/new-host.md`. | 3, 7 |
| 9 | **Rebase `codex/companion-plugin`** onto main as `hosts/codex/`: `actions.py` → adapter events, `guard_adapter` reduced to what only Codex has, roles rendered from `content/`, `PM_KIT_CODEX_ROLE` removed. It must pass the conformance suite. | 8 |
| 10 | **Rebase `codex/robust-bridge-plan`** as `optional/bridge/`: promote the helpers, declare the port, move the host halves, delete the probe code, collapse the docs. | 9 |

Steps 3–4 and 5–6 can run in parallel. Tell the Codex agent about steps 9–10 now, so it can
stop building on paths that are about to move.

## 4. What does not change
- Standard library only and Windows only. No argparse or printing in kitlib.
- Tool paths and exit codes. A merged flag keeps the old one as an alias for one release.
- Guard semantics for Claude: every existing guard test passes through the adapter.
- The bridge's security posture: fail closed, no resend of unknown messages, routes chosen by
  the owner.

## 5. The form of the shared skill text (decided: C)

Decision 3 makes each skill's text identical on every host. The two forks differ in **form**,
not only in host wording. Here is `branch-sync` side by side.

**Claude's version (main, 19 lines): the full procedure.**
- It has 10 numbered steps with the exact commands, such as `git merge --no-ff <hash>`,
  `git merge-base --is-ancestor`, `git checkout <hash> -- <path>` and `git merge --abort`.
- It names each trap, for example "a union resurrects rows the base retired", untracked `.uid`
  files, and why `git commit --no-edit` is refused.
- It names the tool to finish with (`tools/commit.py`) and what to prove after the merge.
- An agent can follow it without opening another file.

**Codex's version (branch, 16 lines): a pointer.**
- Three paragraphs say: read `docs/sync-base.md` and the repo profile, use the shared sync
  helper, never rebase or force-push, resolve only your own conflicts, re-gate and report.
- The detail lives in the tool's doc, so an agent opens a second file to learn the steps.

Neither is wrong. They trade off differently:

| | A. Full procedure (Claude style) | B. Pointer (Codex style) | C. Checklist + pointer (hybrid) |
|---|---|---|---|
| Shape | Every step and command in the skill | A few lines; the doc holds the steps | One line per step with its command, the "never" list, then "details: docs/X.md" |
| Strength | Followable alone. Weaker models make fewer mistakes. | One source of truth (the tool doc). Short, so less context per load. | Followable alone for the common path, with the doc for edge cases |
| Weakness | Duplicates the tool docs, so the two can drift. Longest to load. | The agent must read a second file, and may skip it. Easier to miss a trap. | Some duplication, but only of commands, which a test can check against the CLI |
| Typical size | 20–50 lines | 10–20 lines | 15–30 lines |

Whichever form is chosen, merging the forks must also handle three things that are host
differences today. None of them needs host blocks:

1. **Tool names:** "the Edit tool" (Claude) vs "Codex's patch tool". Use neutral wording such
   as "your file-edit tool"; each host's instructions say once what that is.
2. **Commit identity:** `--provider codex --model <id>` vs a trailer naming your model. The
   tools take the host from `PM_KIT_HOST` and the model from the environment, so the skill just
   says "use `tools/commit.py`".
3. **Repo-specific detail:** for example, Godot `.uid` files. This moves to the repo profile,
   not the shared skill.

**Recommendation: C.** Keep Claude's steps and "never" lists as one-liners, take the pointer
from Codex, and add a test that every `tools/*.py` command a skill names exists and takes the
flags it shows. The 11 merges in step 5 wait for this choice. Nothing else is blocked.

## 6. Platform seam (later refactor: Linux and macOS)

The kit is to run on Windows, Linux and macOS. That is a second axis, separate from the AI host:
any host on any OS. It is done **after** steps 0–10 as its own refactor (decision 15). Doing it
then means each change has one moving part, and the host work can still be proved by the
existing Windows suite.

### Rules for the host work, starting now
1. **No new Windows calls outside `kitlib/proc` and `kitlib/lock`.** This covers ctypes,
   `msvcrt`, `winreg`, window flags and junction calls. New code in `kitlib/host/`,
   `kitlib/content/`, `content/` and the renderer uses only portable standard library code.
2. **The guard refactor (step 3) keeps today's path rules exactly**, but routes every path
   normalisation through one function. The OS refactor then changes one place.
3. **`hosts.json` carries a `platforms` field per host**, set to `["windows"]` for now.
4. **Rendered hook commands are written by one helper** that chooses the interpreter command
   line (`pythonw` today). No manifest or settings entry hard-codes `pythonw`.

Separately and early: Windows CI (GitHub Actions) for the gate, so every step has real proof.

### What is Windows-only today
| Area | Today | Linux / macOS equivalent | Size |
|---|---|---|---|
| Process control (`kitlib/proc`: job objects, pid shield, tree kills, CPU pinning, process start time and parent chain) | Win32 through ctypes, about 1,900 lines | Process groups and `killpg`. Linux: cgroups v2 for memory and cores, `/proc` for identity. macOS: no cgroups and no core pinning, so a weaker model. | Large |
| Machine lock (`kitlib/lock/store.py`, `run.py`) | Win32 mutex and the owning-thread rule | `fcntl.flock` on a lock file | Medium |
| Guard path rules (`kitlib/guard`) | Drive letters, backslashes, case-insensitive paths, junctions, PowerShell tokenizing | POSIX paths (case-sensitive on Linux, usually not on macOS), symlinks, Bash tokenizing | Medium–large |
| Hook and child launching | `pythonw`, `CREATE_NO_WINDOW` and similar (34 files) | `python3` with no window flags | Small |
| Installers | `installer/*.ps1` | Logic in Python (`machine_setup.py`), with thin `.ps1` and `.sh` wrappers | Medium |
| Machine paths | `LOCALAPPDATA`, `APPDATA`, `USERPROFILE`, registry reads | `~/.local/state` and `~/.config`; `~/Library/Application Support` | Small |
| Links | Junctions (35 files) | Symlinks | Small–medium |
| Scheduled work (sweep task, daily full, bridge startup) | Task Scheduler | systemd user timers or cron; launchd | Medium |
| Folder holders (`worktree/holders.py`) | Finds processes that block deleting a folder | POSIX does not block deletes, so mostly a no-op | Small |
| Small extras | Foreground-window lock, `msvcrt.locking`, `GetTickCount` | No-op, `fcntl`, `time.monotonic` | Small |
| Launcher | Windows Terminal, `claude.exe` names | Terminal command and process names per OS | Medium |
| Bridge | Named pipes with ACLs, AppContainer, the Codex Desktop pipe | Unix domain sockets (0600) with a peer-credential check | Unknown: needs a probe of what each host offers natively per OS |

### Shape
- `kitlib/platform/`: one interface with `windows`, `linux` and `darwin` implementations,
  chosen by `sys.platform`. It covers:
  - spawning a hidden process
  - process groups and tree kills
  - process identity and parent chain
  - CPU and memory limits
  - named locks
  - state and config folders
  - links
  - scheduled tasks
  - folder holders

  This lives in kitlib because it is OS code using only the standard library, not host code.
- The rest of kitlib never checks `os.name` or `sys.platform`; a test enforces it.
- Hosts declare their supported OSes in `hosts.json`.
- CI runs the suite on all three OSes.

### Questions for when this starts
1. Which OS comes after Windows: Linux, macOS, or both together?
2. On macOS, may the lock only limit how many heavy runs go at once, still killing the whole
   tree, or must it give the same guarantees on every OS?
3. Can GitHub Actions run Windows, Ubuntu and macOS runners for this repo?
4. Does the bridge stay Windows-only at first?
