# Codex host

Codex's slot in the multi-host kit (docs/multi-host-plan.md step 9; docs/new-host.md): the Codex
companion plugin's behaviour on the host contract. **Nothing here is installed or activated on any
machine, and live parity with the default host is not verified.** The package passes the conformance
suite on payloads in the branch's recorded shapes; no installed Codex client has run it. Every
rendered agent file says so in its header comment.

## What is here

| Part | Where | State |
|---|---|---|
| Registry entry | `hosts.json` `codex` | Windows; hooks PreToolUse, SessionStart, PostCompact, SubagentStart; no ask; no subagent identity in hooks; MCP; skills; TOML agents |
| Hook entry | none of its own: `shell/boot/hook.py codex <event>`, the plugin's copy of the kit's one entry `boot/hook.py` | in the owner's unmarked session a tool `hosts.json` `hook.guarded_tools` leaves out passes before boot (`fast_path: unmarked`); else PreToolUse through `kitlib.host.hookmain` (`ADAPTER.DEADLINE`, 7 s for the whole call, boot included) with Codex's parse, screen and render; a start event adds the kit header (and a PM's or coordinator's start checks, `ADAPTER.start_text`) |
| Hook policy | `adapter/policy.py` | a marked session: unknown tools, other MCP servers' tools, `apply_patch` through the shell, calls the guard cannot fully judge (no named shell, cmd.exe, a TTY, fed input) and a read, test or lander role's edits in its own checkout refused |
| Boot folder | `shell/boot/kitboot.py`, `shell/boot/hook.py`, `shell/hosts.json` | byte copies of `boot/kitboot.py` and `boot/hook.py`, and `hosts.json`'s Codex entry alone, drift-checked; a copied plugin verifies the kit it binds with them, and reads its hook data before the kit boots |
| Agents, skills | `shell/agents/kit-<role>.toml`, `shell/skills/` | every role and skill, rendered from `content/` (`python tools/render_content.py`), each agent file opening with the generated-file line; the skills a byte copy until the live check below; the guard refuses a hand edit of either (docs/guard.md "Generated files"); review templates, not installed |
| Manifests | `shell/plugin.json`, `shell/mcp.json`, `shell/hooks/hooks.json` | rendered from `kit.json`; the plugin export places `shell/plugin.json` at `.codex-plugin/plugin.json`, stamped, and its paths (`./content/skills/`, the hooks file, `mcp.json`) are from the export folder's root (docs/plugin-source.md "Codex") |
| Lock MCP | `shell/mcp/lock_server.py` | the lock's read tools, server `machine-lock`; the lock server has no write tools on any host (docs/lock-mcp.md "No write tools"), and `adapter/lock.py` lists and answers nothing else |
| Hub MCP | `shell/mcp/hub_server.py` | the hub's tools (docs/hub-mcp.md), server `hub`; a marked session is refused them by `adapter/policy.py` |
| Sessions | `adapter/sessions.py` | read-only app-server reads (`ADAPTER.sessions()`) |
| Runtime broker | `adapter/runtime.py`, `dispatch.py`, `controller.py`, `launcher.py`, `workflow.py` | runs kit tools in a bound native thread's sandbox, every call held as an agent of the bound role with the role policy on; launch, controller and landing execution refuse |
| Native checks | `shell/scripts/runtime_probe.py`, `command_canary.py`, `permissions_canary.py`, `workflow_canary.py` | run by hand against an installed `codex.exe`; none runs in the gate |

The plugin root is `shell/`, so its hooks cannot name the kit's `boot/hook.py`: a plugin cache holds
the plugin folder alone, so the plugin ships its own copy of the kit's boot folder and of its
`hosts.json` entry. In a kit checkout its entries boot that kit; copied out of it (a plugin cache),
they bind `PM_KIT_ROOT` and a pinned release `PM_KIT_RELEASE_ID`, verified by the plugin's own boot
check (docs/kitboot.md).

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

Codex agent files carry no tool list, so the hook policy holds a read, test or lander role (its role
file's `mode`) to no edits in its own checkout (`host-role-edit`) whatever the repository's
`guard.role_policy` says, as the branch's guard adapter did. Files its role file's `writes` names,
and temp and scratch folders under the write jail, stay open to it.

What the owner's session leaves unjudged (a trade-off: the owner keeps an interactive shell):

- A call with a TTY, input fed to a command, and text sent to a running process (`write_stdin`).
  The guard sees the command that starts a process, never what is typed into it later. A marked
  session refuses all three.
- A cmd.exe call's text is judged in the guard's cmd.exe dialect (handed over as one PowerShell
  literal, `cmd /c '<text>'`, so no PowerShell comment or quote hides part of it), and the shared
  git rules (reset --hard, a force push or a push to main, commit -a, clean, checkout -- .) apply
  to it. A command built from a variable (`call %G% ...`) or words joined by a caret are not
  modelled, as with `cmd /c` on the default host. A marked session refuses cmd.exe.
- A call that names no shell is judged as PowerShell.

## Not verified

- **Hook payloads.** The shapes come from the Codex hooks documentation and the branch's tests. No
  payload from an installed client was captured; the MCP tool-name format (`mcp__<server>__<tool>`),
  the session variable (`CODEX_THREAD_ID`) and the tool names the rendered roles show
  (`adapter/content.py` `TOOLS`) are inferred. Agent spawning and messaging are refused in a marked
  session; the rendered roles say so where they name them. `adapter/evidence.py` checks a captured
  payload against a binding.
- **Plugin loading.** Installed from the plugin export in a private `CODEX_HOME` on 0.165.0-alpha.1.2
  (2026-10-29, docs/plugin-source.md "Codex"): the export's `.codex-plugin/plugin.json` gives the 20
  skills from the export's `content/skills/`, the four hooks (untrusted, then trusted) and both MCP
  servers, which start, all from the plugin cache. Not yet seen: a hook running in a model turn, and
  the owner's desktop install loading the export.
- **Skills from outside the plugin folder.** Settled by the export: the Codex plugin root is now the
  export folder, so `./content/skills/` is inside it and a cache copy carries it. `shell/skills/` and
  the `shell/boot/` copies stay until the old `pm-kit-local` copy is retired.
- **Runtime.** The branch's canaries passed on that binary against fixtures; the native local Git
  preparation stayed refused (`workflow_canary` reports `complete=false`). Rerun them for any new
  binary. Live controller, store and lock contention, and landing are not accepted. A read or lander
  role never gets a writable sandbox on Codex, whatever its role file says for another host.
- **Models.** Every tier runs the account's model (`account-default`); a tier differs by reasoning
  effort. The owner chooses model ids (docs/ROSTER.md).

## Known limits

- **A Codex PM or coordinator cannot dispatch or message agents.** Agent spawning and messaging
  are refused in every marked session, and the launcher (`adapter/launcher.py`) plans a launch but
  runs nothing. The rendered roles name spawn_agent and send_message as not available to a Codex
  agent. On Codex the owner starts each agent session by hand.
- `python <kit>/tools/plugin_refresh.py --apply`, from the owner's session, installs the export into Codex with `codex plugin add pm-kit@pm-kit` once config.toml names the export's `pm-kit` marketplace; it prints the switch steps while it names only the old `pm-kit-local` copy. That old copy (the cache and the marketplace copy) is refreshed by the same run, with Codex closed: it mirrors the export's `shell/` skills, agents, hooks and `.codex-plugin` into the folders `config.toml` names (`adapter/plugin_copy.py`, the step `ADAPTER.plugin_copy` hands it; docs/plugin-source.md "Host plugin copies"). A changed `hooks.json` makes Codex ask the owner to trust the hook again. The session-start hook still warns when the copy's skills are behind the kit's (`adapter/start.py` `stale_line`).
- A main checkout with no `.kit.json` lets a force push to main through, as the shared guard does
  on every host today (baseline behaviour, not this host's). The runtime broker refuses every call
  while a `.kit.json` exists but does not load (`host-runtime-error`).

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

## Checks

    python tools/render_content.py --check
    python -m unittest tests.host.test_conformance_codex
    python tools/kit_tests.py tests/codex
