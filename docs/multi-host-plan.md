# Multi-host architecture plan

Status: proposal for the owner. Nothing here changes ARCHITECTURE.md until the owner approves it.

**Goal:** one host-neutral kit, with a thin entry-point plugin for each AI platform. The platforms
today are Claude Code and Codex, and adding another should mean adding a folder, not forking the
kit. The tools are shared already. The hooks, agent definitions, skills and instructions are
still written separately for each host. The agent-to-agent bridge from the Codex branches is a
host-neutral service that each host plugs into.

Sources read: `main` @ 3e64fec, `origin/codex/companion-plugin` @ 82cff0d and
`origin/codex/robust-bridge-plan` @ 1cdc244. The bridge branch carries all of companion-plugin
except its last 4 commits.

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

Four layers, each depending only on the layers below it:

```
 ┌──────────────────────────────────────────────────────────────────┐
 │ 4. Plugin shells (per host, data + ≤20-line entries)             │
 │    .claude-plugin/plugin.json      codex/plugin.json, mcp.json    │
 │    claude/hooks/hooks.json         codex/hooks/hooks.json         │
 │    claude/agents/*.md  (generated) codex/agents/*.toml (generated)│
 │    claude/skills/      (generated) codex/skills/      (generated) │
 ├──────────────────────────────────────────────────────────────────┤
 │ 3. Host adapters (per host, Python)                              │
 │    claude/adapter/ … implements kitlib.host.HostAdapter          │
 │    codex/adapter/  … implements kitlib.host.HostAdapter          │
 │    (event parse/render, sessions, delivery, sender attestation,  │
 │     content renderer, launcher)                                  │
 ├──────────────────────────────────────────────────────────────────┤
 │ 2. Host contract (kitlib/host/) — protocols + neutral types only │
 │    Action, Decision, HostEvent, HostProfile, SessionHost,        │
 │    CommandHost, DeliveryAdapter, SenderAttestor, ContentRenderer │
 ├──────────────────────────────────────────────────────────────────┤
 │ 1. Core (kitlib/*) — tools' logic, guard rules, role policy,     │
 │    lock, gate, bridge core (outbox/dispatch/pairing/mcp), release│
 └──────────────────────────────────────────────────────────────────┘
   content/ (canonical roles, skills, instructions) → rendered into layer 4
```

### 2.1 The host contract (`kitlib/host/`)
One package of protocols and frozen dataclasses. It contains no host names and imports nothing
from the host folders.

- `HostEvent` → `Action(kind, tool, path, command, cwd, role, agent)` and
  `Decision(allow | deny | ask, rule, reason, instead)`.
  - `kind` is one of `edit`, `shell`, `mcp` or `session_start`.
  - `agent` is an opaque subagent identity, or `None` for the main session.
- `HostAdapter`, one per host. It has:
  - `name`, `profile`
  - `parse(event_name, payload) -> Action`
  - `render(event_name, Decision) -> (stdout, exit_code)`
  - `sessions() -> SessionHost`
  - `command_host() -> CommandHost | None`
  - `bridge() -> BridgeHostPort | None`
  - `content() -> ContentRenderer`
- `HostProfile`: keep only the fields that have consumers, and make every field consumed. That
  means owner paths, worktree root, state dir, the agent process image names (for the lock's
  `AGENT_HOSTS`), the role env var and the MCP tool-name format.
- **Registry inversion.** kitlib never imports a host. Host selection happens at the edge:
  - Entry points under `claude/` and `codex/` construct their adapter and pass it in.
  - Shared CLIs in `tools/` read `PM_KIT_HOST`. They find the adapter's module through a small
    `hosts.json` at the kit root, which is data, not a kitlib import. If `PM_KIT_HOST` is unset,
    they default to `claude` for back-compatibility.
  - `--host` and `--provider` merge into one `--host` flag, which defaults from `PM_KIT_HOST`.

### 2.2 Guard and policy, once
- `kitlib.guard.decide(action: Action, ctx) -> Decision`. Every Claude-payload field read moves
  into `claude/adapter/events.py`:
  - `rules.py` tool-name sets
  - `agent_id` / `agent_type`
  - `tag_role`
  - `config/policy.role_name`
  - `backlog/roles`

  `hook_output()` moves to the Claude adapter's `render`.
- Codex's `actions.py` becomes `codex/adapter/events.py`. It produces `Action` directly instead
  of Claude-shaped dicts.
- Codex's policy that names no host moves into kitlib as `kitlib/guard/rolepolicy.py`, and
  applies to both hosts: role edit bans, read-only owner paths (`ConfigView`), the
  shell-cwd-in-checkout check, the lock-verb allowlist and `shell_gap`.
- Where a host cannot express something, the gap is declared as data in its profile. For
  example, Codex has no `ask`. The core decides how to degrade: `ask` → `deny` with the reason.
  Adapters do not each improvise their own mapping.
- **One hook entry per host, ≤20 lines.** For example, `claude/hooks/run.py <event>` puts the
  kit on the path, builds the adapter and calls `kitlib.host.hookmain.run(adapter, event,
  stdin)`. The fail-closed deny, timeout handling and settings-entry rendering live in
  `kitlib.host.hookmain` once.

### 2.3 Single-source content (`content/`)
- **Roles.** Each role lives in `content/roles/<role>.md`, with neutral front matter and a body:

  ```yaml
  name: worker
  summary: Build routine, bounded changes in one assigned worktree.
  tier: standard        # light | standard | strong | deep  (host maps tier→model)
  effort: medium
  sandbox: workspace-write   # read-only | workspace-write
  capabilities: [dispatch, message]   # host maps to tool lists
  hosts: [claude, codex]     # opt-out per host if a role cannot exist there
  ```

  In the body, host-specific words are tokens:
  - `{{agent:test-runner}}` renders as `pm-kit:test-runner` for Claude and `kit-test-runner`
    for Codex.
  - `{{worktrees_dir}}`, `{{profile_path}}` and `{{instructions_file}}`.
  - Fenced `<!-- host:claude -->…<!-- /host -->` blocks hold host-only paragraphs. They should
    stay rare.
- **Skills.** `content/skills/<skill>/SKILL.md` uses the same token and block rules. The 11
  forked skills are merged into one text each. Where the forks differ, keep the more precise
  procedure and lift the facts that are true for one host only into host blocks. Host-only skills
  live there too: `coordinator-dispatch` for Claude, and `kit-setup` and `kit-pm` for Codex.
- **Rendering.** `tools/render_content.py` (logic in `kitlib/content`) asks each host adapter's
  `ContentRenderer` to write `claude/agents/*.md`, `claude/skills/**`, `codex/agents/*.toml` and
  `codex/skills/**`. The output is **committed**: plugins load from the repo and no build step
  runs at install. A test asserts that the rendered output equals the committed files, and the
  gate fails on drift.
- **Roster.** `docs/ROSTER.md` becomes the table of tier → model for each host, plus each role's
  tier. `kitlib/roster` checks it against `content/roles`. The `KIT_AGENTS` list in
  `kitlib/launcher/start.py` is derived from it, not hand-copied.
- **Instructions.** `AGENTS.md` is canonical. `CLAUDE.md` becomes the one-line import
  `@AGENTS.md` plus any Claude-only line. `templates/common/` gets `AGENTS.md`, and its
  `CLAUDE.md` becomes the same pointer. The repo profile moves from `.claude/agent-profile.md` to
  `{{profile_path}}`. That path is a neutral `docs/agent-profile.md` that each host's
  instructions point at; the old path is kept as a stub during migration.

### 2.4 Bridge as a host-neutral service
- **Core** (`kitlib/bridge/`): `messages`, `outbox`, `dispatch`, `pairing`, `mcp`, `callers`,
  `owner`, `prompt` and `jsonwire`. This code is already neutral.
- **Declared port.** Replace the dead `ports.py` with the seam that is actually used, as
  `kitlib/host/bridge.py`:
  - `SenderAttestor.attest(caller_proof) -> SenderAuthority`
  - `DeliveryAdapter.describe() / send(msg) / reconcile(msg, native_id)`
  - `ReturnChannel` (optional)
  - `HookSink` (optional; any host may need one, not only Claude)

  `tools/bridge_service.py` loops over the configured hosts' `adapter.bridge()` with no
  per-host `if`.
- **Host halves.** `claude/bridge_*.py` → `claude/adapter/bridge/`, and `codex/bridge_*.py` →
  `codex/adapter/bridge/`.
- **Windows IPC promoted.** `bridgeprobe/winpipe.py` → `kitlib/proc/pipe.py`, next to `job`
  and `process`. `ProbeError`, `identifier` and `digest` move into `kitlib/bridge/errors.py` and
  a small `kitlib/ident.py`.
- **Probe code quarantined.** After the moves above, `kitlib/bridgeprobe/`,
  `claude/bridge_phase0.py`, `bridge/{bootstrap,journal,pipeline,restart,delegation,
  schema_probe,ports,disabled,status}.py` and `tools/bridge{,_live_probe,_schema_probe}.py`
  have no production importers. Either delete them, keeping them in git history, or move them
  to `optional/bridgeprobe/`. A test asserts that nothing under `kitlib/` imports them.
- **Docs consolidated.** The 24 bridge docs and 3 evidence JSON files become:
  - `docs/bridge.md`: design and contracts
  - `docs/bridge-status.md`: a capability matrix per host and direction, kept current
  - `docs/bridge-history/`: the milestone narrative, review docs and evidence, unindexed or
    under one entry

### 2.5 Packaging and release
- Both plugins bind to the same pinned runtime release (`kitlib.kitrelease`). The pre-import
  verification in `codex/pmkit_codex/{profile,release_binding}.py` moves to a stdlib-only
  `bootstrap/kitboot.py` at the kit root. Every host's entry point calls it before importing
  kitlib. It is already written to run before kitlib is trusted, so moving it changes nothing
  about its trust.
- Claude's `.claude-plugin/plugin.json` gains `PM_KIT_ROOT` / `PM_KIT_RELEASE_ID` the way Codex's
  `mcp.json` has them. With neither set, it keeps today's source-checkout behaviour.
- `kitlib/launcher` and `kitlib/pluginsource/claude.py` become adapter capabilities
  (`adapter.launcher()` and `adapter.plugin_installer()`), so a Codex launcher fits the same
  slot later.

### 2.6 Conformance tests per adapter
- `tests/host/conformance.py` is one shared `unittest` mixin. Each adapter's test module
  subclasses it with recorded payload fixtures. It covers:
  - parse → `Action` for every guarded tool
  - render of allow, deny and ask (with the host's declared degradation)
  - session listing on fixture files
  - the bridge port's describe and send on a fake pipe
  - content rendering is stable
- Adding a host means passing this suite. That is the definition of "supported host".

## 3. Migration order

Each step lands on its own through the gate and keeps Claude's behaviour unchanged.

| Step | Work | Depends on |
|---|---|---|
| 0 | Owner decisions (§5). Merge the decisions into ARCHITECTURE.md "Host split" as an amendment. | — |
| 1 | Land the shared, low-risk parts of the Codex branch on main: `kitlib.attribution`, `packageexport`, `kitrelease`, `hostrpc`, the worktree `--host` support and `hoststart`. Leave `codex/` itself and the bridge on their branches. | 0 |
| 2 | Create `kitlib/host/` (types and protocols). Invert the registry (`hosts.json` plus `PM_KIT_HOST`) and delete `kitlib/hosts`' importlib of host folders. Unify `--host`/`--provider`. | 1 |
| 3 | Move the guard to `Action`/`Decision`. Claude's adapter does parse and render. Add `hookmain`. Claude's hooks become ≤20-line entries. The existing guard tests run via the Claude fixtures. | 2 |
| 4 | Lift the Codex role policy into `kitlib/guard/rolepolicy.py`, so Claude gets it too. Rebase `codex/` onto steps 2–4: `actions.py` → adapter, and `guard_adapter` shrinks to the host-specific parts. | 3 |
| 5 | `content/` plus the renderer. Migrate Claude agents first, and check that the rendered output is byte-identical to today's files. Then merge the 11 forked skills, then generate the Codex TOMLs from the same roles. Make AGENTS.md canonical. | 2 |
| 6 | Remove the Claude leaks from kitlib, one PR per cluster: lock actor key → `host`, session env, worktree defaults, MCP prefix, commit model env, launcher, pluginsource. | 2 |
| 7 | Bridge refactor: winpipe promotion, declared port, host halves into adapters, probe quarantine, doc consolidation. Done on the bridge branch before it merges. | 2, 4 |
| 8 | Shared `kitboot` and the Claude release binding. | 1 |
| 9 | Conformance suite. It is required in the gate from then on. | 3, 5, 7 |

Steps 5 and 6 can run alongside 3–4. Step 7 is the largest, and the bridge work in flight is
the most exposed to it, so it should start once the Codex owner agrees the port shape.

## 4. What does not change
- Standard library only, Windows only, no argparse or printing in kitlib.
- Tool paths and exit codes. A merged flag keeps the old one as an alias for one release.
- Guard semantics for Claude: every existing guard test passes unchanged through the adapter.
- The bridge's security posture: fail closed, no resend of unknown messages, owner-selected
  routes.

## 5. Open questions for the owner
1. **Order with the Codex branches.** Should the Codex branches merge first and the refactor
   follow, or should main be refactored (steps 1–6) and the branches rebased onto it?
   *Recommendation:* step 1 cherry-picks the shared parts now, and the rest of `codex/` waits
   for step 4. The bridge is rebased after step 2.
2. **One roster or two?** Should every role exist on every host, with differences only in tier
   → model? Or are `pm`, `implementation-worker` and `opus-worker` intentionally host-specific?
3. **One skill text.** Do you accept a single merged text per skill, which means the detailed
   Claude procedures also become Codex's? Or should Codex stay deliberately terse?
4. **Role marker.** Should there be one `PM_KIT_ROLE` plus a `PM_KIT_HOST`, or does Codex's
   separate `PM_KIT_CODEX_ROLE` guard against something real, such as a Claude-marked parent
   launching Codex?
5. **Bridge placement.** Is the bridge part of the core kit, or an optional package or separate
   plugin that hosts opt into? Can the Phase 0 probe code be deleted, or must it stay as
   evidence?
6. **Release binding for Claude.** Should Claude also run from the pinned shared release, or keep
   loading from the source checkout?
7. **Generated files committed.** Is committing rendered agents and skills, with a drift test,
   acceptable, or should rendering happen at install?
8. **Future hosts.** Is anything beyond Claude and Codex on the horizon (Gemini CLI, Cursor)? If
   so, the content tokens and the `HostEvent` kinds should be checked against it now.
