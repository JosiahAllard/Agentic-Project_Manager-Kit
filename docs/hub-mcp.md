# Hub MCP server

`mcp/hub_server.py` serves the Action Hub (docs/action-hub.md) as a stdio MCP server. Each tool is one HTTP call to the hub server on this machine through `kitlib.hubsite.client.Client`, on the loopback address only: the hub's own checks (roles, fields, versions) are the ones that hold, and this process never opens the hub store. The logic is `kitlib/hubmcp/server.py`; the names the guard reads are in `kitlib/hubmcp/__init__.py`, which imports nothing.

- **Transport:** JSON-RPC 2.0, one message per line on stdin and stdout, on the standard library, in the shape of the lock MCP server (docs/lock-mcp.md). It answers `initialize` (protocol 2025-06-18, 2025-03-26 or 2024-11-05), `ping`, `tools/list` and `tools/call`.
- **Plugin:** `.claude-plugin/plugin.json` names it `hub` (`kitlib.hubmcp.SERVER_NAME`), rendered from `hosts/claude/adapter/content.py` `MCP_SERVERS`. A client shows its tools as `mcp__plugin_pm-kit_hub__<tool>`. Codex's plugin does not carry it.
- **Port:** read from the machine's hub settings (`kitlib.hubsite.settings.load().port`: `hub.port`, docs/action-hub.md "Serving it") at each call, so a hub started later or on another port is found. A setting that cannot be read is the `config` tool error.
- **Tokens:** the token files of `kitlib.hubsite.auth.folder()`. Read tools carry the agent's token (the owner's when there is no agent token, as `Client.token` does for `ANY` routes); write tools carry the agent's. While the agent's token file exists, no tool reads the owner's (`tests/hubmcp/test_server.py`, `Pinned.test_no_tool_reads_the_owner_token`).
- **Source:** every write names itself `mcp` (`kitlib.hubsite.serve.VIA_MCP`, the `X-Hub-Via` header), so the hub records `via: mcp` on what it writes; `tools/hub.py` writes `cli`.
- **Session mark:** `mcp/hub_server.py` reads the session's mark once at start (`kitlib.sessionmark.mark`, docs/guard.md "Session mark").

## Results

- A tool returns the hub's JSON object as text, and the same object as `structuredContent`.
- A failure is a tool result with `isError: true` and `{"ok": false, "error": {code, message, field}}`; the server keeps answering.
  - A refusal from the hub (`kitlib.hubsite.client.Refused`) keeps the hub's code, message and field, plus `status`, the HTTP status (docs/action-hub.md "For the PM").
  - `not_running`: nothing listens on the port (`serve.NotRunning`); `tools/hub.py serve` starts it.
  - `client`: no token file, or what answered is not the hub (`serve.ServeError`).
  - `config`: the machine's hub settings cannot be read.
  - `invalid`: arguments that fail the tool's schema (unknown or missing keys, wrong types, a value outside an enum, a blank id), with `field` naming the key; nothing is sent.
  - `internal`: a bug in the server, with a short trace.
- An unknown tool is JSON-RPC error -32602, and a line that is not JSON is -32700.
- Ids are quoted as one path segment (`kitlib.hubsite.client.part`); the hub checks their shape.

## Read tools

Endpoints are below `/api/v1` (`kitlib.hubsite.api.BASE`).

| tool | arguments | endpoint |
|---|---|---|
| `hub_pending` | none | `GET /pending` |
| `hub_wait` | `since`, `timeout_s` (default 30, at most 55: `api.WAIT_MAX_S`), `pending` | `GET /wait` through `Client.wait` |
| `hub_list` | `collection` (questions, todos, stories or to_pm), `status`, `repo` | `GET /<collection>` |
| `hub_inbox` | none | `GET /snapshot`, its inbox as `{ok, items}` (the api has no inbox list route) |
| `hub_backlog` | `repo`, `state`, `fresh` | `GET /backlog` |
| `hub_completed` | `repo`, `days` | `GET /completed` |
| `hub_view` | `view` (notices, repos, usage or agents) | `GET /<view>` |
| `hub_health` | none | `GET /health` |

## Agent write tools

Each calls a route the agent's token may call (`kitlib.hubsite.api.ROUTES`, role AGENT); `tests/hubmcp/test_server.py` `Routes.test_every_shipped_tool_maps_to_a_route_that_is_not_the_owners` checks every shipped tool against `api.match`.

| tool | arguments (required first) | endpoint |
|---|---|---|
| `hub_ask` | `title`; `context`, `options`, `recommended`, `repo`, `from`, `refs`, `urgent`, `story` | `POST /questions` |
| `hub_question_log` | `id`, `ruling`, `if_version` | `POST /questions/<id>/log` |
| `hub_todo_add` | `title`; `detail`, `commands`, `repo`, `refs`, `urgent`, `from` | `POST /todos` |
| `hub_todo_ack` | `id`; `if_version` | `POST /todos/<id>/ack` |
| `hub_story_set` | `id`, `status` (`api.AGENT_STORY_STATUSES`), `if_version`; `reviewer`, `review_note`, `backlog_id`, `closed_note` | `POST /stories/<id>` |
| `hub_to_pm_reply` | `id`; `reply`, `ruling`, `if_version` | `POST /to_pm/<id>/reply` |
| `hub_inbox_post` | `text`; `title`, `ref`, `asked`, `repo`, `from`, `refs` | `POST /inbox` |

## The owner's tools (pinned, not shipped)

`kitlib.hubmcp.OWNER_ONLY` names four tools for the owner's hub writes, and `kitlib.hubmcp.OWNER_ROUTES` the owner route each is to call:

| tool | endpoint |
|---|---|
| `hub_answer` | `POST /questions/<id>/answer` |
| `hub_todo_done` | `POST /todos/<id>/done` |
| `hub_story_submit` | `POST /stories` |
| `hub_to_pm_send` | `POST /to_pm/send` |

- None ships yet. `tools/list` leaves them out, and `tools/call` refuses them without reading the owner's token: `forbidden` in a session a launcher marked, `not_available` (saying it waits on the guard, `server.GUARD_WAIT`) otherwise.
- They ship with the guard change that teaches the guard to deny them to agents by these names. Until then the guard does not judge any hub tool: `kitlib/guard/rolepolicy.py` `mode` leaves every MCP call that is not the lock's alone.
- `tests/hubmcp/test_server.py` `Pinned` pins the four names, checks each maps to an OWNER route of `api.ROUTES`, and checks none is listed or ever reaches the hub.
