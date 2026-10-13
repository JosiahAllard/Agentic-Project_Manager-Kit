# Codex host

Codex's slot in the multi-host kit (docs/multi-host-plan.md step 9; docs/new-host.md). Ported from the
`codex/companion-plugin` branch onto the host contract. **Nothing here is installed or activated
on any machine, and live parity with the default host is not verified.** The package passes the
conformance suite on payloads in the branch's recorded shapes; no installed Codex client has run it.
Every rendered agent file says so in its first line.

## What is here

| Part | Where | State |
|---|---|---|
| Registry entry | `hosts.json` `codex` | Windows; hooks PreToolUse, SessionStart, PostCompact, SubagentStart; no ask; no subagent identity in hooks; MCP; skills; TOML agents |
| Hook entry | `shell/hooks/run.py <event>` | the owner's reads pass before boot; else PreToolUse through `kitlib.host.hookmain` (7 s for the whole call) with Codex's parse, screen and render; a start event adds the kit header (and a PM's or coordinator's start checks) |
| Hook policy | `adapter/policy.py` | a marked session: unknown tools, other MCP servers' tools, `apply_patch` through the shell, and calls the guard cannot fully judge (no named shell, cmd.exe, a TTY, fed input) refused |
| Boot check | `shell/boot/kitboot.py` | a byte copy of `boot/kitboot.py`, drift-checked; a copied plugin verifies the kit it binds with it |
| Agents, skills | `shell/agents/kit-<role>.toml`, `shell/skills/` | every role and skill, rendered from `content/` (`python tools/render_content.py`); review templates, not installed |
| Manifests | `shell/plugin.json`, `shell/mcp.json`, `shell/hooks/hooks.json` | rendered from `kit.json` |
| Lock MCP | `shell/mcp/lock_server.py` | the lock's read tools only (`adapter/lock.py`), server `machine-lock` |
| Sessions | `adapter/sessions.py` | read-only app-server reads (`ADAPTER.sessions()`) |
| Runtime broker | `adapter/runtime.py`, `dispatch.py`, `controller.py`, `launcher.py`, `workflow.py` | runs kit tools in a bound native thread's sandbox, every call held as an agent of the bound role with the role policy on; launch, controller and landing execution refuse |
| Native checks | `shell/scripts/runtime_probe.py`, `command_canary.py`, `permissions_canary.py`, `workflow_canary.py` | run by hand against an installed `codex.exe`; none runs in the gate |

The plugin root is `shell/`. In a kit checkout its entries boot that kit; copied out of it (a plugin
cache), they bind `PM_KIT_ROOT` and a pinned release `PM_KIT_RELEASE_ID`, verified by the plugin's own
boot check (docs/kitboot.md).

## How a session is held

Codex's hooks carry no child identity: a child's payload names its parent's session, and a child
inherits the parent's `PM_KIT_ROLE`. So the kit holds every edit, shell and MCP call of a **marked**
session as an agent of its mark's role (`kitlib.host.hookmain`, agent `unidentified`): the
main-checkout rule and the write jail apply to it as to a subagent on any host, and the role policy
by its mode. A Codex PM or coordinator therefore cannot edit the main checkout or land from it:
stricter than the default host, where a launched lead's main session may, and looser than the
branch, which denied every marked call outright. The owner's **unmarked** session is judged by the
shared rules alone, as on every host; the branch's hooks did nothing there. The runtime broker binds
a role from native metadata, not the mark (`adapter/runtime.py`).

## Not verified

- **Hook payloads.** The shapes come from the Codex hooks documentation and the branch's tests. No
  payload from an installed client was captured; the MCP tool-name format (`mcp__<server>__<tool>`),
  the session variable (`CODEX_THREAD_ID`) and the tool names the rendered roles show
  (`adapter/content.py` `TOOLS`) are inferred. Agent spawning and messaging are refused in a marked
  session; the rendered roles say so where they name them. `adapter/evidence.py` checks a captured
  payload against a binding.
- **Plugin loading.** The tested binary (0.162.0-alpha.17.2) found portable skills but not portable
  hooks or MCP. Hook trust, cached paths and client refresh are untested.
- **Runtime.** The branch's canaries passed on that binary against fixtures; the native local Git
  preparation stayed refused (`workflow_canary` reports `complete=false`). Rerun them for any new
  binary. Live controller, store and lock contention, and landing are not accepted. A read or lander
  role never gets a writable sandbox on Codex, whatever its role file says for another host.
- **Models.** Every tier runs the account's model (`account-default`); a tier differs by reasoning
  effort. The owner chooses model ids (docs/ROSTER.md).

## Not ported, with no replacement

From the branch's `codex/` tree. Each is open work before activation.

- `pmkit_codex/diagnostics.py` and `scripts/check.py`: the package and readiness checks (`--ready`
  activation blockers). The conformance suite and `render_content --check` cover the package shape
  only, not readiness.
- `pmkit_codex/installation.py`: package plan, disposable apply, rollback and relocated-cache
  validation of a reviewed export.
- `pmkit_codex/install_acceptance.py`: native marketplace registration in a private `CODEX_HOME`,
  cached skill, hook and MCP discovery, read-only MCP start.
- `scripts/package.py` and `scripts/shared_release.py`: the command lines over those and over
  `kitlib.kitrelease.store`.
- The native-compat export the tested binary needs: `.codex-plugin/plugin.json` and `.mcp.json`,
  generated at export from `plugin.json` and `mcp.json` (the portable root must then be absent).

## Checks

    python tools/render_content.py --check
    python -m unittest tests.host.test_conformance_codex
    python tools/kit_tests.py tests/codex
