# Codex host

Codex's slot in the multi-host kit (docs/multi-host-plan.md step 9; docs/new-host.md). Ported from the
`codex/companion-plugin` branch onto the host contract. **Nothing here is installed or activated
on any machine, and live parity with the default host is not verified.** The package passes the
conformance suite on payloads in the branch's recorded shapes; no installed Codex client has run it.

## What is here

| Part | Where | State |
|---|---|---|
| Registry entry | `hosts.json` `codex` | Windows; hooks PreToolUse, SessionStart, PostCompact, SubagentStart; no ask; no subagent identity in hooks; MCP; skills; TOML agents |
| Hook entry | `shell/hooks/run.py <event>` | PreToolUse through `kitlib.host.hookmain` with Codex's parse, screen and render; a start event adds the kit header (and a PM's or coordinator's start checks) |
| Hook policy | `adapter/policy.py` | a marked session: unknown tools and non-lock MCP tools refused, `apply_patch` through the shell refused |
| Agents, skills | `shell/agents/kit-<role>.toml`, `shell/skills/` | every role and skill, rendered from `content/` (`python tools/render_content.py`) |
| Manifests | `shell/plugin.json`, `shell/mcp.json`, `shell/hooks/hooks.json` | rendered from `kit.json` |
| Lock MCP | `shell/mcp/lock_server.py` | the lock's read tools only (`adapter/lock.py`) |
| Sessions | `adapter/sessions.py` | read-only app-server reads (`ADAPTER.sessions()`) |
| Runtime broker | `adapter/runtime.py`, `dispatch.py`, `controller.py`, `launcher.py`, `workflow.py` | runs kit tools in a bound native thread's sandbox; launch, controller and landing execution refuse |
| Native checks | `shell/scripts/runtime_probe.py`, `command_canary.py`, `permissions_canary.py`, `workflow_canary.py` | run by hand against an installed `codex.exe`; none runs in the gate |

The plugin root is `shell/`. Copied out of the kit (a plugin cache), its entries find the kit through
`PM_KIT_ROOT` and a pinned release through `PM_KIT_RELEASE_ID` (docs/kitboot.md).

## Not verified

- **Hook payloads.** The shapes come from the Codex hooks documentation and the branch's tests. No
  payload from an installed client was captured; the MCP tool-name format (`mcp__<server>__<tool>`)
  and the session variable (`CODEX_THREAD_ID`) are inferred.
- **Which agent runs.** Codex's hooks carry no child identity: a child's payload names its parent's
  session and the child inherits the parent's `PM_KIT_ROLE`. The kit therefore judges every Codex
  call as the main session with its mark (the role-only policy). The branch held every marked call
  as a subagent; under the role-only policy a session marked with a write role (`worker`) is not
  held by the main-checkout rule or the write jail, which key on subagent identity or the lead marks.
  The runtime broker does bind a child's role from native metadata (`adapter/runtime.py`).
  `adapter/evidence.py` checks a captured payload against a binding.
- **Owner sessions.** The branch's hooks did nothing in an unmarked session. Here the shared rules
  judge it as they judge the owner's session on every host; only Codex's own screen stays off.
- **Plugin loading.** The tested binary (0.162.0-alpha.17.2) found portable skills but not portable
  hooks or MCP; the branch exported `.codex-plugin/plugin.json` and `.mcp.json` for it. That export
  is not ported. Hook trust, cached paths and client refresh are untested.
- **Runtime.** The branch's canaries passed on that binary against fixtures; the native local Git
  preparation stayed refused (`workflow_canary` reports `complete=false`). Rerun them for any new
  binary. Live controller, store and lock contention, and landing are not accepted.
- **Models.** Every tier runs the account's model (`account-default`); a tier differs by reasoning
  effort. The owner chooses model ids (docs/ROSTER.md).
- **Kit boot from a cache.** A copied plugin loads `boot/kitboot.py` from `PM_KIT_ROOT`, the folder it
  then verifies; shipping the boot check inside the plugin export is open.

## Checks

    python tools/render_content.py --check
    python -m unittest tests.host.test_conformance_codex
    python tools/kit_tests.py tests/codex
