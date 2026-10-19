# Multi-host architecture plan

The record of the multi-host refactor. Its steps are built; code and docs cite its decisions and sections by number.

**Goal:** one host-neutral kit, with a thin entry-point plugin for each AI platform. Adding a host means adding a folder, not forking the kit.

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

## 2. Target shape

The layers and tree are in ARCHITECTURE.md.

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
- **Registry inversion.** kitlib never imports a host. The host is selected at the edge:
  - A host's entry points build their adapter and pass it in.
  - Shared CLIs in `tools/` read `PM_KIT_HOST` and resolve the adapter through `hosts.json`,
    which is data, not a kitlib import.
  - One `--host` flag, which defaults from `PM_KIT_HOST`.
- **Roles.** `PM_KIT_ROLE` is the only role marker (decision 4). The host adapter sets
  `PM_KIT_HOST`. The child-environment scrub is core code: it clears every host's session variables when a child is launched on a different
  host, so a Claude-marked parent cannot leak its role or session into a Codex child.

### 2.2 Guard and policy, once
- `kitlib.guard.decide(action: Action, ctx) -> Decision`. Reading a host's payload is the adapter's `parse` and the hook output its `render`; Codex's adapter produces `Action` directly.
- The role policy that names no host is `kitlib/guard/rolepolicy.py` and applies to every host: role edit bans, read-only owner paths, the shell-cwd-in-checkout check, the lock-verb allowlist and `shell_gap`.
- The fail-closed deny, timeout handling and settings-entry rendering live once in `kitlib.host.hookmain`; the one hook entry is `boot/hook.py` (decision 20).

### 2.3 Single-source content (`content/`) and the generated files
Each role and skill is written once in `content/`; `tools/render_content.py` (logic in `kitlib/content`) asks every host adapter to write its own files from it: `hosts/<name>/shell/agents/*`, the host's skills copy, instructions file and plugin manifest (which carries the one version, decision 6). The generated files are committed, with a test that fails when they differ from what the renderer writes. Every role exists on every host (decision 2), the role and skill bodies are neutral prose with no host blocks, and the few values that must differ use tokens (`{{worktrees_dir}}`). See docs/content.md.

### 2.4 The bridge as an optional install
`optional/bridge/` holds the bridge core, service and MCP frontend, with each host's half under `optional/bridge/hosts/<name>/`; the core kit never imports it. Installing it registers its MCP server and hook in each host's shell; not installed, no host shell mentions it. The declared port is `optional/bridge/port.py` (`SenderAttestor`, `DeliveryAdapter`, optional `ReturnChannel` and `HookSink`), and the service loops over installed hosts with no per-host `if`. The Phase 0 probe code is deleted (git history keeps it). Design and status: optional/bridge/README.md and STATUS.md.

### 2.5 One release value
- `kit.json` at the root holds the one `version` and is the input to the release identity (decision 6); the renderer writes it into every host manifest.
- `kitlib.kitrelease` exports one release containing the core, every host shell and, when installed, the bridge, with one `PM_KIT_RELEASE_ID`.
- Every host's entry points run the same stdlib-only boot check, `boot/kitboot.py`, before any kitlib import (docs/kitboot.md).

### 2.6 Conformance tests per adapter
`tests/host/conformance.py` is one shared `unittest` mixin that each host's test module subclasses with recorded payload fixtures: parse, render with the host's declared degradation, session listing, content rendering, the boot check and, with the bridge installed, the bridge port. Passing it is what "supported host" means.

### 2.7 Adding a host (the slot)
The checklist is docs/new-host.md. No file in `kitlib/`, `content/` or another host's folder changes; a test checks that host names appear only in `hosts.json`, `hosts/`, `optional/bridge/hosts/` and tests.

## 3. Migration order

Steps 0–10, each landed through the gate with Claude's behaviour unchanged: ARCHITECTURE.md amended (0), host-neutral library parts taken from the Codex branch (1), `kitlib/host/` and `hosts.json` (2), the guard on `Action`/`Decision` (3), role policy (4), `content/` and the renderer (5), Claude leaks removed from kitlib (6), `claude/` moved to `hosts/claude/` with `kit.json` and `boot/kitboot.py` (7), the conformance suite (8), Codex ported as `hosts/codex/` (9; live parity unverified, hosts/codex/README.md "Not verified") and the bridge as `optional/bridge/` (10).

## 5. The form of the shared skill text (decided: C)

A checklist plus a pointer: one line per step with its command, the "never" list, then the tool doc for edge cases. Tool names are neutral ("your file-edit tool"), commit identity comes from `tools/commit.py`, and repo-specific detail lives in the repo profile. A test checks that every `tools/*.py` command and flag a skill names exists.

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

## 7. Instructions, roles and zero repo setup

Steps 11–20 are built; the Windows gate and live host checks are owed (docs/go-live.md).

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

### 7.2 Risks to watch
- With an auto-detected gate, the wrong checks can run until someone overrides them. The "auto-detected" label is how the PM sees this.
- Owner rules depend on the machine config. With no owner email stored, owner-only paths refuse every edit; they never allow them.
- Setting `core.hooksPath` changes how git behaves for humans in that repo too, so it is done only once and never overwrites an existing value.
