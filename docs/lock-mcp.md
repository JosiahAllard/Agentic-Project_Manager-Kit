# Lock MCP server

`mcp/lock_server.py` serves the machine lock as a stdio MCP server, the adapter named in ARCHITECTURE.md "Scheduler UI". Its tools are thin wrappers over `kitlib.lock.api` (docs/lock-api.md) and call nothing else. The logic is `kitlib/lockmcp/server.py`.

- **Transport:** JSON-RPC 2.0, one message per line on stdin and stdout. It is written on the standard library, because the `mcp` SDK is not installed and the kit uses no packages (CLAUDE.md). It answers `initialize` (protocol 2025-06-18, 2025-03-26 or 2024-11-05), `ping`, `tools/list` and `tools/call`.
- **Plugin:** `.claude-plugin/plugin.json` names it `machine-lock`. That manifest holds this server, the skills (`hosts/claude/shell/skills/`), the agents (`hosts/claude/shell/agents/`) and the hooks of ARCHITECTURE.md "Packaging".
- **Not live:** it is registered in no settings and no `.mcp.json`.

## Results

- A tool returns its payload as JSON text, and the same object as `structuredContent`.
- A refusal or lock error is a tool result with `isError: true` and the payload `{"ok": false, "error": {code, message, field}}`. The server never crashes on one.
  - Codes are the api's (docs/lock-api.md "Writes"). A store that cannot be read (`HoldsUnreadable`, `QueueUnreadable`) is `store`. Arguments that fail the tool's schema are `invalid`, with `field` naming the key. A bug in the lock is `internal`, with a short trace.
- An unknown tool is JSON-RPC error -32602, and a line that is not JSON is -32700.
- Run and hold ids must match the lock's id shape (`kitlib.lock.store.ID_RE`, `r-` or `h-`) before they reach the api, which checks them again (`kitlib.lock.api._id`): the id names a store file.

## Read tools (open to all)

| tool | arguments | returns |
|---|---|---|
| `lock_snapshot` | `history_days`, `since`, `until` (all optional) | `api.snapshot()`: the Snapshot of docs/lock-api.md, with `revision`, `slots`, `runs`, `queue`, `holds` and `holds_error`, and `game`. |
| `lock_wait_change` | `revision`, `timeout_s` (default 30, capped at 60) | `api.wait_change()`: the snapshot once the revision moves, or after the timeout. |
| `lock_stats` | `days`, `since`, `until` (all optional) | `api.stats()`. |
| `lock_run` | `id` | `{found, run}`: `api.run_record()`, live (`live: true`) or ended. An error result from the api (`invalid`) is the tool error. |
| `lock_hold` | `id` | `{found, hold}`: `api.hold_record()`, live (`live_record: true`) or ended. Holds that cannot be read are the `store` tool error (`api.hold_record` returns that error result). |

## Write tools (owner-only)

Each description starts with `OWNER-ONLY`, and each returns `{ok, result | error, revision}`. That is the api's write result with `snapshot` replaced by its `revision`; `lock_snapshot` gives the whole snapshot.

| tool | arguments | api call |
|---|---|---|
| `lock_hold_create` | `spec` | `hold_create(actor, spec)` |
| `lock_hold_edit` | `id`, `changes` | `hold_edit(actor, id, changes)` |
| `lock_hold_end` | `id` | `hold_end(actor, id)` |
| `lock_set_game_mode` | `enabled` | `set_game_mode(actor, enabled)` |
| `lock_set_games` | `games` | `set_games(actor, games)` |
| `lock_set_division` | `division` (object or null) | `set_division(actor, division)` |
| `lock_end_run` | `id` | `end_run(actor, id)` |

- The actor passed is `{"name": "lock-mcp"}`. On the machine's store, `api.who` reads who is asking from the server's process tree, and `api.authorize` decides.

## What guard.py must deny agents

guard.py denies agents these seven tools, matched on the tool name's suffix. How it tells the owner's session from an agent's is still open: the `agent_id` field (ARCHITECTURE.md "Contracts", Roles) marks only subagents.

`lock_hold_create`, `lock_hold_edit`, `lock_hold_end`, `lock_set_game_mode`, `lock_set_games`, `lock_set_division`, `lock_end_run`

- The list in code is `kitlib.lockmcp.server.OWNER_ONLY`, and `tests/lockmcp/test_server.py` pins it.
- Inferred, not yet seen: once the plugin is installed, Claude Code names these with the Claude plugin's MCP prefix, `kitlib.lockmcp.tool_prefix()` + `<tool>` (the host's hosts.json `mcp_tool_format`, which the guard reads too). Check the name against a real PreToolUse payload before the guard matches on it.
