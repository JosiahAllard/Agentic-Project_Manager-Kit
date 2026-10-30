# Multi-host architecture plan

Status: approved direction. The owner's decisions are in §0, and ARCHITECTURE.md was amended to match (step 0).

**Goal:** one host-neutral kit, with a thin entry-point plugin for each AI platform. The platforms
today are Claude Code and Codex, and adding another should mean adding a folder, not forking the
kit. The tools, the hook entry, the role and skill text and the release value are shared; a host
adds its adapter and shell. The agent-to-agent bridge is a host-neutral optional service that
each host plugs into.

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
| 16 | Owner paths in every repo | A deliberate tightening, a superset of before | `kitlib/config/schema.py` `OWNER_PATHS` (the default of `.kit.json` `owner.paths`) now also protects `AGENTS.md`, `docs/agent-profile.md` and `.codex/` (its worktrees folder excepted) in every repository, beside every host's `owner_paths`. Each path protected before still is; a repository that names its own `owner.paths` keeps its list. |

## 1. Where things stand

The plan below is built through step 10 and §7 (steps 11-20); the section that follows describes the target shape as it stands. What is shared today is the tools (`tools/` over `kitlib/`), the guard (kitlib.guard.decide over a neutral Action), the lock MCP server, kitlib.attribution, kitlib.packageexport, kitlib.kitrelease and kitlib.hoststart.shared_checks. The pre-refactor inventory of what each host duplicated (agent definitions, skills, hook entries, role marker, release binding) and the structural faults it caused is in git history.

## 2. Target shape

Four layers, each depending only on the layers below it. Every host-specific thing sits in one
folder per host, so a new platform is slotted in by adding a folder:

```
 ┌───────────────────────────────────────────────────────────────────┐
 │ 4. Host shells: hosts/<name>/shell/  (data + entries ≤20 lines)    │
 │    manifests, hook registration, rendered agents (and skills)     │
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
   content/   canonical roles and skills (host-neutral)
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
hosts.json             {"claude": {...}, "codex": {...}}
hosts/claude/adapter/  Python adapter
hosts/claude/shell/    hooks/hooks.json, agents/ (rendered)
hosts/codex/adapter/
hosts/codex/shell/     plugin.json, mcp.json, hooks/hooks.json, agents/ and skills/ (rendered)
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
- **One hook entry for every host** (decision 20): `boot/hook.py <host> <event>` runs the kit's
  boot check (§2.5), builds the adapter and calls
  `kitlib.host.hookmain.run(adapter, event, stdin)`. The fail-closed deny, timeout handling and
  settings-entry rendering live in `hookmain` once.

### 2.3 Single-source content (`content/`) and the generated files
**What "generated files" means.** A host needs agents, skills and instructions in its own file
format:
- Claude: agents as `.md` with YAML front matter, skills as `SKILL.md` folders, and `CLAUDE.md`.
- Codex: agents as `.toml` with `developer_instructions`, skills as `SKILL.md` folders, and
  `AGENTS.md`.

Roles and skills are written **once** in `content/`. A script (`tools/render_content.py`, logic
in `kitlib/content`) asks every host adapter to write its own files from it. The written-out
files are the "generated files":
- `hosts/<name>/shell/agents/*` (skills are not rendered: every host loads `content/skills/`
  itself; decision 29)
- the host's plugin manifest, which carries the one version (decision 6)

`AGENTS.md` and `CLAUDE.md` are hand-written, not rendered.

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
  Each forked skill is merged into one text, in the form decided in §5. A skill that only one
  host ships breaks decision 3, so a host-only skill is made neutral and shipped everywhere, or
  its content moves into role files or the host's instructions.
- **Roster table.** `docs/ROSTER.md` holds each role's tier, plus a tier → model table per host.
  `kitlib/roster` checks it against `content/roles`. The `KIT_AGENTS` list in the launcher is
  derived from it, not copied by hand.
- **Instructions.** `AGENTS.md` is the one rules file, and `CLAUDE.md` imports it with `@AGENTS.md`.
  The repo profile is the neutral `docs/agent-profile.md`; each host's own-folder stub
  (`.claude/agent-profile.md`, `.codex/agent-profile.md`) points to it.

### 2.4 The bridge as an optional install
**What the Phase 0 probe code was.** Before building the bridge, throwaway experiments tested
whether each idea was possible on Windows (an AppContainer-isolated helper, leader election among
bridge processes, pipe-handle passing, a worker broker, reading Claude's and Codex's live session
files). It was never the working bridge. Three of its helpers had become load-bearing and were
promoted (below); the rest was deleted (optional/bridge/README.md).

**Plan (built in step 10):**
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
- **Docs:** the bridge docs and evidence files collapse into:
  - `optional/bridge/README.md`: design and contracts
  - `optional/bridge/STATUS.md`: a capability matrix per host and direction
  - the history, left in git

### 2.5 One release value
- `kit.json` at the root holds the one `version` and is the input to the release identity
  (decision 6).
- The renderer writes that version into every host manifest: `.claude-plugin/plugin.json` and
  marketplace, and Codex `plugin.json` and `mcp.json`.- `kitlib.kitrelease` exports one release that contains the core, every host shell and, when
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
4. **Recorded payload fixtures** under `tests/host/<name>/`, and a test class
   (`tests/host/test_conformance_<name>.py`) that subclasses the conformance mixin.
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
| 8 | Conformance suite, required in the gate. `docs/new-host.md`. **Done**: `tests/host/conformance.py`, run for Claude and for a test-only fake host (`tests/host/fakehost`); the bridge-port check is `tests/host/bridge_conformance.py` (step 10). | 3, 7 |
| 9 | **Rebase `codex/companion-plugin`** onto main as `hosts/codex/`: `actions.py` → adapter events, `guard_adapter` reduced to what only Codex has, roles rendered from `content/`, `PM_KIT_CODEX_ROLE` removed. It must pass the conformance suite. **Done:** `hosts.json` registers Codex, `hosts/codex/` passes the suite on payloads in the branch's recorded shapes, `parse` may return several Actions (one `apply_patch`, several files) and an adapter may `screen`, the child-environment scrub is core (`kitlib.sessionmark.child_environment`, hosts.json `env_prefixes`), Codex's renderer fills every content token (docs/content.md "Tokens"). Nothing is installed; live parity is unverified (hosts/codex/README.md "Not verified"). The bridge owns the Codex bridge client at `optional/bridge/hosts/codex/client.py` (step 10). Codex's diagnostics either drop it or probe for it lazily and optionally by importing that module, and they never keep a copy. | 8 |
| 10 | **Rebase `codex/robust-bridge-plan`** as `optional/bridge/`: promote the helpers, declare the port, move the host halves, delete the probe code, collapse the docs. **Done:** optional/bridge/README.md (layout, port, install, what was deleted) and STATUS.md. Claude and Codex are both served. | 9 |

## 4. What does not change
- Standard library only and Windows only. No argparse or printing in kitlib.
- Tool paths and exit codes. A merged flag keeps the old one as an alias for one release.
- Guard semantics for Claude: every existing guard test passes through the adapter. Two
  changes are deliberate: the role policy (docs/guard.md "Role policy"), which runs only where a
  repository's `.kit.json` sets `guard.role_policy` (opt in; off, every call is judged as before),
  and the owner paths' tightening of decision 16 (`AGENTS.md`, `docs/agent-profile.md` and `.codex/`
  added to the default `owner.paths`, a superset of before).
- The bridge's security posture: fail closed, no resend of unknown messages, routes chosen by
  the owner.

## 5. The form of the shared skill text (decided: C)

Decision 3 makes each skill's text identical on every host. The two forks differed in **form**,
not only in host wording: Claude's skills gave the full procedure (every step, command and trap),
and Codex's pointed to the tool's doc. Neither is wrong. They trade off differently:

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
flags it shows.

## 6. Platform seam (later refactor: Linux and macOS)

The kit is to run on Windows, Linux and macOS. That is a second axis, separate from the AI host:
any host on any OS. It is done **after** steps 0–10 as its own refactor (decision 15). Doing it
then means each change has one moving part, and the host work can still be proved by the
existing Windows suite.

Status: docs/linux-support-plan.md supersedes this section for Linux (its own status line says
how much is built). `kitlib/platform/` has `windows` and `linux` folders and no `darwin`; both
hosts in `hosts.json` still declare `platforms: ["windows"]`. macOS is not started.

### Rules for the host work, starting now
1. **No new Windows calls outside `kitlib/proc` and `kitlib/lock`.** This covers ctypes,
   `msvcrt`, `winreg`, window flags and junction calls. New code in `kitlib/host/`,
   `kitlib/content/`, `content/` and the renderer uses only portable standard library code.
2. **The guard refactor (step 3) keeps today's path rules exactly**, but routes every path
   normalisation through one function. The OS refactor then changes one place.
3. **`hosts.json` carries a `platforms` field per host**, set to `["windows"]` for now.
4. **Rendered hook commands are written by one helper** that chooses the interpreter command
   line (`pythonw` today). No manifest or settings entry hard-codes `pythonw`.

Planned, not built: Windows CI (GitHub Actions) for the gate. The repo has no `.github` folder.

### What is Windows-only today
| Area | Today | Linux / macOS equivalent | Size |
|---|---|---|---|
| Process control (`kitlib/proc`: job objects, pid shield, tree kills, CPU pinning, process start time and parent chain) | Win32 through ctypes | Process groups and `killpg`. Linux: cgroups v2 for memory and cores, `/proc` for identity. macOS: no cgroups and no core pinning, so a weaker model. | Large |
| Machine lock (`kitlib/lock/store.py`, `run.py`) | Win32 mutex and the owning-thread rule | `fcntl.flock` on a lock file | Medium |
| Guard path rules (`kitlib/guard`) | Drive letters, backslashes, case-insensitive paths, junctions, PowerShell tokenizing | POSIX paths (case-sensitive on Linux, usually not on macOS), symlinks, Bash tokenizing | Medium–large |
| Hook and child launching | `pythonw`, `CREATE_NO_WINDOW` and similar | `python3` with no window flags | Small |
| Installers | `installer/*.ps1` | Logic in Python (`machine_setup.py`), with thin `.ps1` and `.sh` wrappers | Medium |
| Machine paths | `LOCALAPPDATA`, `APPDATA`, `USERPROFILE`, registry reads | `~/.local/state` and `~/.config`; `~/Library/Application Support` | Small |
| Links | Junctions | Symlinks | Small–medium |
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
- CI is to run the suite on all three OSes (planned; docs/linux-support-plan.md section 4).

### Questions for when this starts
1. Which OS comes after Windows: Linux, macOS, or both together?
2. On macOS, may the lock only limit how many heavy runs go at once, still killing the whole
   tree, or must it give the same guarantees on every OS?
3. Can GitHub Actions run Windows, Ubuntu and macOS runners for this repo?
4. Does the bridge stay Windows-only at first?

## 7. Instructions, roles and zero repo setup

The §7 agenda was settled in a planning session with the owner. Steps 11-20 are built; the live host checks are tracked in docs/go-live.md.

### 7.0 Decisions

| # | Question | Decision |
|---|---|---|
| 17 | Where the role list lives | Only in `content/roles/`. `docs/ROSTER.md` is rendered. `KIT_AGENTS` and the `kitlib/backlog/roles.py` groupings are read from the role files' `mode:`. A role is added or removed with one file and a render. |
| 18 | Showing what a role is given | `tools/agent_view.py <role> [--host <name>]`, built from the same code that renders and enforces, plus `content/README.md` ("where do I change X"). |
| 19 | Generated files | Every rendered file opens with a "Generated from `content/...`: edit that, then render" banner. The guard refuses hand edits to rendered folders and points to the source. |
| 20 | Hook entries | One shared `boot/hook.py <host> <event>` for every host. Each host's guarded tool list is data in `hosts.json`, so the fast path still skips unguarded calls without importing kitlib. A new host adds no hook scripts. |
| 21 | Tool stubs in repos | None. Skills and roles call `python <kit>/tools/<x>.py -C <worktree>`, using the kit header's path. |
| 22 | `.kit.json` | Optional overrides only. Main branch from `origin/HEAD`, repo name from the remote or folder, owner from the machine config. The safe protections (no force push to main) are on with no file. |
| 23 | Worktrees | They stay at each host's `worktrees_directory` (`.claude/worktrees/<slug>`). On first use the kit adds the anchored line (`/.claude/worktrees/`) to `.git/info/exclude`, which is local and never committed. The owner's condition: work in a worktree must still push to its remote branch. It does, because a worktree shares the repo's remotes and has its own index. The exclude only hides the folder from the main checkout's untracked list. A test proves the push. |
| 24 | Gate with no `gate.json` | Detects the stack and runs its usual test and lint steps. The record and report are labelled "auto-detected". `gate.json` overrides. |
| 25 | Git hooks | The kit sets the local `core.hooksPath` on first use, and leaves an existing hooks path alone. |
| 26 | Doc index and repo profile | Both opt-in. The index check runs only if `docs/INDEX.md` exists. With no repo profile, roles use the kit's default profile and read the repo's README or AGENTS.md if there is one. |
| 27 | Machine setup | Shrinks to installing the plugin on each host, making the agent temp folder, and storing the owner's email. It writes nothing into repos by default. A `--repo` mode keeps the old committed setup for those who want it. |
| 28 | `claude/hooks/` stubs | Removed in the same change that adds `boot/hook.py`. The owner re-runs machine setup once on each machine. |
| 29 | What is generated, and when | Skills are no longer copied: each host's manifest points at `content/skills/` (Codex after a live check that it loads skills from outside its plugin folder; until then its copy stays). Roles and manifests stay generated, since each host needs its own header and file format and a role body works best as the system prompt (pointer files were weighed and rejected). Rendering happens at commit time: `tools/commit.py` renders when a path it commits is under `content/` or is `kit.json` or `hosts.json`, and adds the rendered files it changed to the same commit. The opt-in pre-commit git hook does the same for plain `git commit`. The gate's drift check stays as the backstop. |

### 7.1 Steps

Each step is one branch, reviewed and merged before the next, in the same way as §3.

11. **Role list from the role files.**
    - `kitlib/content` gains a roster reader (name, mode, tier, group).
    - `KIT_AGENTS` and the backlog groupings read it, which fixes the test-runner disagreement.
    - `docs/ROSTER.md` is rendered.
    - A test fails on a role name in prose or code that no role file defines.
12. **Generated files: fewer, marked, rendered on commit.**
    - The Claude manifest points at `content/skills/`, and `hosts/claude/shell/skills/` is deleted.
    - Codex follows once a live install confirms the path.
    - The renderer writes the banner, in a comment form that suits each file type, and the drift test checks it.
    - `tools/commit.py` and the opt-in pre-commit hook render and stage changed output when `content/`, `kit.json` or `hosts.json` is committed.
    - The guard refuses edits under `hosts/*/shell/agents/**` and `hosts/*/shell/skills/**`, naming the source file.
13. **Visibility.**
    - `tools/agent_view.py` lists the role file, AGENTS.md, the user instructions file, the kit header text, the start checklist, skills, the profile in use, mode, writes, capabilities and the active `.kit.json` switches.
    - Add `content/README.md`.
14. **Shared hook entry.**
    - `boot/hook.py` takes over the per-host entries, and `hosts.json` gains `guarded_tools`.
    - Manifests are rendered to call it.
    - `claude/hooks/` and the per-host entry scripts are deleted.
    - Conformance covers the new entry.
    - Owner action: re-run `tools/machine_setup.py --apply` on each machine.
15. **No stubs.**
    - Skills and roles use `python <kit>/tools/<x>.py -C <worktree>`.
    - `machine_setup` stops writing stubs except under `--repo`.
    - The kit's own repo keeps working through the same calls.
16. **Defaults without `.kit.json`.**
    - The config loader works out the main branch, name and owner.
    - Protections are on by default.
    - A broken file still refuses every call, as it does now.
    - Document the defaults in `docs/kit-json.md`.
17. **First-use repo prep (local only).**
    - One idempotent `kitlib` routine runs from the PM's start and the worktree tool.
    - It writes the `.git/info/exclude` lines for every registered host's worktree folder.
    - It sets the local `core.hooksPath` only if none is set.
    - Tests:
      - Nothing tracked changes.
      - A commit in a worktree pushes to its remote branch.
      - An existing hooks path is kept.
18. **Gate auto-detect.**
    - Detectors for Python (unittest, pytest, pyproject), Node (package.json scripts), Rust, Go and .NET.
    - The gate record and report carry `source: auto`.
    - `gate.json` wins.
19. **Opt-in docs and default profile.**
    - The index check is skipped when `docs/INDEX.md` is missing.
    - The kit ships `content/default-profile.md`.
    - Roles fall back to it, then to the repo's README or AGENTS.md.
20. **Machine setup shrink.**
    - Plugin install per host, agent temp folder, owner email.
    - `--repo` keeps the old behaviour.
    - Proof: a fresh clone of an unrelated repo runs a PM-to-worker-to-land cycle with nothing committed.

### 7.2 Risks to watch
- With an auto-detected gate, the wrong checks can run until someone overrides them. The "auto-detected" label is how the PM sees this.
- Owner rules depend on the machine config. With no owner email stored, owner-only paths refuse every edit; they never allow them.
- Setting `core.hooksPath` changes how git behaves for humans in that repo too, so it is done only once and never overwrites an existing value.
