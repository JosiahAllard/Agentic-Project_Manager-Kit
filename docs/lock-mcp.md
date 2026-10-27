# Lock MCP server

`mcp/lock_server.py` serves the machine lock as a stdio MCP server, the adapter named in ARCHITECTURE.md "Scheduler UI". Its tools are thin wrappers over `kitlib.lock.api` (docs/lock-api.md) and call nothing else. The logic is `kitlib/lockmcp/server.py`; the stdio JSON-RPC loop and response shapes (`ok`, `err`, `serve`) are `kitlib/mcpwire`, shared with the hub server.

- **Transport:** JSON-RPC 2.0, one message per line on stdin and stdout. It is written on the standard library, because the `mcp` SDK is not installed and the kit uses no packages (CLAUDE.md). It answers `initialize` (protocol 2025-06-18, 2025-03-26 or 2024-11-05), `ping`, `tools/list` and `tools/call`.
- **Plugin:** `.claude-plugin/plugin.json` names it `machine-lock`. That manifest holds this server, the skills (`content/skills/`), the agents (`hosts/claude/shell/agents/`) and the hooks of ARCHITECTURE.md "Packaging".
- **Live through the plugin:** a Claude session with the `pm-kit` plugin enabled starts it from that manifest; no settings entry or `.mcp.json` names it. A session's tools are named `mcp__plugin_pm-kit_machine-lock__<tool>` (the prefix is inferred from Claude Code's plugin naming: `kitlib.lockmcp.tool_prefix`). The entry point boots the kit (`boot/kitboot.py`) before it imports kitlib, and exits when that check fails.

## Results

- A tool returns its payload as JSON text, and the same object as `structuredContent`.
- A refusal or lock error is a tool result with `isError: true` and the payload `{"ok": false, "error": {code, message, field}}`. The server never crashes on one.
  - Codes are the api's (docs/lock-api.md "Writes"). A store that cannot be read (`HoldsUnreadable`, `QueueUnreadable`) is `store`. Arguments that fail the tool's schema are `invalid`, with `field` naming the key. A direct in-process `Server.call` of a name the server does not serve is `unknown_tool`, with `field` `name` (over JSON-RPC that name gets -32602 first). A bug in the lock is `internal`, with a short trace.
- An unknown tool is JSON-RPC error -32602, and a line that is not JSON is -32700.
- Run and hold ids must match the lock's id shape (`kitlib.lock.store.ID_RE`, `r-` or `h-`) before they reach the api, which checks them again (`kitlib.lock.api._id`): the id names a store file.

## Read tools (open to all)

| tool | arguments | returns |
|---|---|---|
| `lock_snapshot` | `history_days`, `since`, `until` (all optional) | `api.snapshot()`: the Snapshot of docs/lock-api.md, with `revision`, `slots`, `runs`, `queue`, `holds` and `holds_error`, and `game`. |
| `lock_wait_change` | `revision`, `timeout_s` (default 30, capped at 60) | `api.wait_change()`: the snapshot once the revision moves, or after the timeout. |
| `lock_stats` | `days`, `since`, `until` (all optional) | `api.stats()`. |
| `lock_run` | `id` | `{found, run}`: `api.run_record()`, live (`live: true`) or ended. An error result from the api (`invalid`, or `store` for a live record that cannot be read) is the tool error. |
| `lock_hold` | `id` | `{found, hold}`: `api.hold_record()`, live (`live_record: true`) or ended. Holds that cannot be read are the `store` tool error (`api.hold_record` returns that error result). |

## No write tools

The server has no write tool, on any host. Asked "Drop the lock MCP's owner-only write tools so the lock never trusts a session mark, keeping the read tools?", the owner answered "yes i want that", and decided that the lock MCP ships read-only. The owner changes the lock outside any agent session: `reserve_ui.pyw`, or the lock CLI run by hand in a terminal no agent host started. The PM places and releases no owner hold through MCP, since there is no write tool. Owner writes confirmed through the hub (O3) are a later follow-up.

- Because no tool writes, the server never asks who is calling: it reads no session mark, and `mcp/lock_server.py` installs no owner check, so `kitlib.lock.api` keeps its default one.
- Seven write tools were retired: `lock_hold_create`, `lock_hold_edit`, `lock_hold_end`, `lock_set_game_mode`, `lock_set_games`, `lock_set_division` and `lock_end_run`. Their names are pinned in `kitlib.lockmcp.OWNER_ONLY`. `tools/list` never lists one. `tools/call` answers one as an unknown tool (-32602), and a direct in-process `Server.call` refuses it with the error code `unknown_tool`. At import the server checks that it serves none of them (`check_read_only`, which raises RuntimeError: a real check, not an `assert`, so `python -O` keeps it). `tests/lockmcp/test_server.py` pins this.
- The guard still denies those names (docs/guard.md "Lock MCP"). It matches the host's MCP prefix plus the name; on Claude the prefix is `mcp__plugin_pm-kit_machine-lock__`. This is a second layer: a server process started from an older release keeps serving its old tools until it restarts (docs/lock-release.md).
