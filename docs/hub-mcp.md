# Hub MCP server

`mcp/hub_server.py` serves the Action Hub (docs/action-hub.md) as a stdio MCP server. Each tool is one HTTP call to the hub server on this machine through `kitlib.hubsite.client.Client`, on the loopback address only: the hub's own checks (roles, fields, versions) are the ones that hold, and this process never opens the hub store. The logic is `kitlib/hubmcp/server.py`; the names the guard reads are in `kitlib/hubmcp/__init__.py`, which imports nothing. The stdio JSON-RPC loop and response shapes (`ok`, `err`, `serve`) are `kitlib/mcpwire`, shared with the lock server. In `tools/list`, `destructiveHint` is true for a tool that changes an existing document (a non-GET with an `{id}` in its path) and false for one that only adds; `readOnlyHint` is true for the GET tools.

- **Transport:** JSON-RPC 2.0, one message per line on stdin and stdout, on the standard library, in the shape of the lock MCP server (docs/lock-mcp.md). It answers `initialize` (protocol 2025-06-18, 2025-03-26 or 2024-11-05), `ping`, `tools/list` and `tools/call`.
- **Plugin:** `.claude-plugin/plugin.json` names it `hub` (`kitlib.hubmcp.SERVER_NAME`), rendered from `hosts/claude/adapter/content.py` `MCP_SERVERS`. A client shows its tools as `mcp__plugin_pm-kit_hub__<tool>`. Codex's plugin carries it as server `hub` (`hosts/codex/shell/mcp/hub_server.py`); `hosts/codex/adapter/policy.py` refuses its tools to a marked Codex session.
- **Port:** read from the machine's hub settings (`kitlib.hubsite.settings.load().port`: `hub.port`, docs/action-hub.md "Serving it") at each call, so a hub started later or on another port is found. A setting that cannot be read is the `config` tool error.
- **Tokens:** the token files of `kitlib.hubsite.auth.folder()`. Every tool carries the agent's token, reads included (`server.AgentClient`; unlike the CLI's `Client.token`, it never falls back to the owner's for an `ANY` route). With no usable agent token a tool answers `no_agent_token`, and no tool ever opens the owner's token file (`tests/hubmcp/test_server.py`, `Pinned.test_no_tool_reads_the_owner_token` and `Errors.test_a_read_never_falls_back_to_the_owner_token`).
- **Source:** every write names itself `mcp` (`kitlib.hubsite.serve.VIA_MCP`, the `X-Hub-Via` header), so the hub records `via: mcp` on what it writes; `tools/hub.py` writes `cli`.
- **Session mark:** `mcp/hub_server.py` reads the session's mark once at start (`kitlib.sessionmark.mark`, docs/guard.md "Session mark").

## Results

- A tool returns the hub's JSON object as text, and the same object as `structuredContent`.
- A failure is a tool result with `isError: true` and `{"ok": false, "error": {code, message, field}}`; the server keeps answering.
  - A refusal from the hub (`kitlib.hubsite.client.Refused`) keeps the hub's code, message and field, plus `status`, the HTTP status (docs/action-hub.md "For the PM").
  - `not_running`: nothing listens on the port (`serve.NotRunning`); `tools/hub.py serve` starts it.
  - `no_agent_token`: the agent's token file is absent or unusable (`server.NoAgentToken`); nothing is sent.
  - `client`: what answered is not the hub (`serve.ServeError`).
  - `config`: the machine's hub settings cannot be read.
  - `invalid`: arguments that fail the tool's schema (unknown or missing keys, wrong types, a value outside an enum, a blank id), with `field` naming the key; nothing is sent.
  - `internal`: a bug in the server, with a short trace.
- An unknown tool is JSON-RPC error -32602, an empty batch `[]` is -32600, and a line that is not JSON is -32700.
- Ids are quoted as one path segment (`kitlib.hubsite.client.part`); the hub checks their shape.

## Read tools

Endpoints are below `/api/v1` (`kitlib.hubsite.api.BASE`).

| tool | arguments | endpoint |
|---|---|---|
| `hub_pending` | `for` | `GET /pending` |
| `hub_wait` | `since`, `timeout_s` (default 30, at most 55: `api.WAIT_MAX_S`), `pending`, `for` | `GET /wait` through `Client.wait` |
| `hub_list` | `collection` (questions, todos, stories or to_pm), `status`, `repo` | `GET /<collection>` |
| `hub_inbox` | none | `GET /snapshot`, its inbox as `{ok, items}` (the api has no inbox list route) |
| `hub_backlog` | `repo`, `state`, `fresh` | `GET /backlog` |
| `hub_completed` | `repo`, `days` | `GET /completed` |
| `hub_view` | `view` (notices, repos, usage or agents) | `GET /<view>` |
| `hub_health` | none | `GET /health` |

`hub_pending` and `hub_wait` take an optional `for`, a session key, sent as the `for` query of the
hub's api as given (blank sends none). With it they list or wait for that key's pending items only:
a session waits for its own answers with `hub_wait` `pending: true, for: <its launcher key>`, and
`for: pm` is the PM's items and every answered question (docs/action-hub.md "Who handles a
pending item"). A session key's `for` that returns one of that key's items hands it over, as the
CLI's does; `for: pm` hands nothing over (`api.Api.handed`). The
hub refuses `for` on a wait without `pending` (`invalid`, field `for`). Without `for` they see
every pending item, including a coordinator's to-do routed to that coordinator and not yet acked:
a pending `hub_wait` returns at once on such an item. `tests/hubmcp/test_server.py`
`Routes.test_pending_and_wait_pass_for_through_as_the_query` and
`LiveKeys.test_for_reaches_the_hub_on_pending_and_wait` check it.

## Agent write tools

Each calls a route the agent's token may call (`kitlib.hubsite.api.ROUTES`, role AGENT, or ANY for the two delete routes); `tests/hubmcp/test_server.py` `Routes.test_every_shipped_tool_maps_to_a_route_that_is_not_the_owners` checks every shipped tool against `api.match`.

| tool | arguments (required first) | endpoint |
|---|---|---|
| `hub_ask` | `title`; `context`, `options`, `recommended`, `repo`, `from`, `refs`, `urgent`, `story` | `POST /questions` |
| `hub_question_log` | `id`, `ruling`, `if_version` | `POST /questions/<id>/log` |
| `hub_question_edit` | `id`, `from`, `if_version`; `title`, `context`, `options`, `recommended`, `refs`, `urgent` | `POST /questions/<id>/edit` |
| `hub_question_withdraw` | `id`, `from`, `if_version` | `POST /questions/<id>/delete` |
| `hub_todo_add` | `title`; `detail`, `commands`, `steps` ("To-do steps"), `repo`, `refs`, `urgent`, `from` | `POST /todos` |
| `hub_todo_ack` | `id`; `if_version` | `POST /todos/<id>/ack` |
| `hub_todo_edit` | `id`, `from`, `if_version`; `title`, `detail`, `commands`, `repo`, `refs`, `urgent` | `POST /todos/<id>/edit` |
| `hub_todo_withdraw` | `id`, `from`, `if_version` | `POST /todos/<id>/delete` |
| `hub_story_set` | `id`, `status` (`api.AGENT_STORY_STATUSES`), `if_version`; `reviewer`, `review_note`, `backlog_id`, `closed_note` | `POST /stories/<id>` |
| `hub_to_pm_reply` | `id`; `reply`, `ruling`, `if_version` | `POST /to_pm/<id>/reply` |
| `hub_inbox_post` | `text`; `title`, `ref`, `asked`, `repo`, `from`, `refs` | `POST /inbox` |

The `from` of `hub_ask` is the session the answer goes back to: a launcher session key or `pm`,
the default when it is absent or blank. The hub refuses any other value, a free-text name
included (`invalid`, field `from`, status 400: `kitlib/hubsite/api.py` `questions_add`;
`LiveKeys.test_a_question_comes_from_a_session_key_or_pm_only`). The `from` of `hub_todo_add`
and `hub_inbox_post` is a name in printable ASCII (`api._agent_from`).

The four edit and withdraw tools change only an open item the caller added: `from` must be the
item's own (`pm` for one with none), checked as a claim because all agents share one token
(docs/action-hub.md "questions/Q-NNN"). `hub_todo_edit` takes no `steps` and judges the changed
text as `hub_todo_add` does before it calls the hub (`kitlib/hubmcp/server.py` `Server._t_hub_todo_edit`).

## To-do steps

The hub's `POST /todos` takes `steps` (`kitlib/hubsite/api.py` `todo_steps`), from `hub_todo_add`, from `tools/hub.py todos add --steps F` (docs/action-hub.md) or from any other client: a list of what the owner is to do, and `kitlib.hubsite.todosteps` turns each into a PowerShell command the owner pastes (`render`), appended after any `commands` given. One line per step (`summary`), such as `replace 2 lines in <file>` or `run python t.py`, ends the to-do's detail, after a blank line. A step has exactly one verb key:

| step | command |
|---|---|
| `{edit: <absolute file>, old: <text>, new: <text>}` | replaces the one place `old` is with `new`; prints `ok <file>`, or `MISS <file>: <why>` with the file unchanged |
| `{open: <absolute file>, line: <n>}` (`line` optional) | opens the file in Notepad++ (`todosteps.NOTEPADPP`, looked for under `$env:ProgramFiles`, then `${env:ProgramFiles(x86)}`, when the command runs), or prints `MISS` when neither holds it |
| `{run: <command>}` | the command, verbatim |

The edit command is one Windows PowerShell 5.1 `& { ... }` block (`todosteps` module docstring):

- `old` and `new` go in as readable single-quoted literals. A text holding a single quote (U+0027 or U+2018 to U+201B, which PowerShell reads as one), a control character other than a newline (C0, DEL and C1, U+0080 to U+009F), a tab (pasted into a console, it can start completion), the line and paragraph separators U+2028 and U+2029, a bidi control or invisible character (U+202A to U+202E, U+2066 to U+2069, U+200B to U+200F, U+FEFF, and the no-break space U+00A0) or a character outside the Basic Multilingual Plane goes in as base64 of its UTF-8 bytes instead, with a readable preview, control and invisible characters escaped, in a `<# #>` comment. The preview shows only the first 60 characters (`todosteps.PREVIEW`); the rest of the text is in the base64 alone. A file, text or run command holding a lone surrogate (U+D800 to U+DFFF) is refused as a bad step.
- A block whose texts hold no newline is one line. A text with a newline makes the block span lines, and the owner pastes the whole block as one unit.
- Newlines reach the script as LF and take the file's own ending: CRLF when the file has one, LF otherwise. A file with both is a `MISS` when `old` or `new` holds a newline.
- The file is read as bytes: a UTF-8 or UTF-16 BOM, or none, and then it must be valid UTF-8 (an ANSI file is a `MISS`). It is written back with the same BOM and encoding.
- `old` must occur exactly once (an ordinal count, overlapping matches included); any other count, or any error, is a `MISS` and nothing is written.

`todosteps.check` refuses an agent's to-do whose own text (title, detail, `commands`, `run` steps; never edit text) does any of these, as `invalid` naming the field:

- a command runs plain Notepad (Notepad++ is not matched): give an `open` step;
- the text asks the owner to open a file and change it by hand ("open X and apply the edits", "make the following changes"): give `edit` steps;
- a command is coordinator work, judged per segment between `&&`, `||`, `;`, `|` and newlines: `git worktree add`/`remove`/`move`/`prune`, `gate.py`, `land.py`, `new_worktree.py`, `worktree_remove.py`, `sync_base.py`, a branch or tag delete, `update-ref -d` or `--stdin`, and a push that deletes, forces, mirrors or prunes.

The rules match words, so they can refuse a to-do that only mentions one of these in a command; `git merge` and a plain push are not refused. The hub's `POST /todos` applies them to every agent's to-do (`api.todo_steps`; the owner's own text is not judged), and refuses a lone surrogate in any `commands` entry for either role, as `invalid` field `commands`. `hub_todo_add` runs the same check before it calls the hub, so a refused to-do never reaches it, and posts `steps` as given for the hub to render (`kitlib.hubmcp.server` `_t_hub_todo_add`).

The hub server loads this code when it starts, and the MCP server when its client starts it, so after a change to it lands restart both together (`hub.py restart`, docs/action-hub.md "Serving it"): a new MCP server with an older hub is refused with "this route takes no 'steps' field", and an older MCP server with a newer hub posts already-rendered commands, which the hub's check can refuse when an edit's text holds a word the rules match.

## The owner's tools (pinned, not shipped)

`kitlib.hubmcp.OWNER_ONLY` names four tools for the owner's hub writes, and `kitlib.hubmcp.OWNER_ROUTES` the owner route each is to call:

| tool | endpoint |
|---|---|
| `hub_answer` | `POST /questions/<id>/answer` |
| `hub_todo_done` | `POST /todos/<id>/done` |
| `hub_story_submit` | `POST /stories` |
| `hub_to_pm_send` | `POST /to_pm/send` |

- None ships yet. `tools/list` leaves them out, and `tools/call` refuses them without reading the owner's token: `forbidden` in a session a launcher marked, `not_available` (saying that the guard already denies them and the tools are not shipped yet, `server.GUARD_WAIT`) otherwise.
- The guard already denies them, by these names, to every session but the owner's own: its `hub-mcp` rule (`kitlib/guard/rules.py` `hub_mcp`) reads `kitlib/guard/hub.py` `OWNER_MCP_TOOLS`, which is this `OWNER_ONLY` imported (docs/guard.md "Hub secrets"). Shipping the tools themselves is still to come, and `server.GUARD_WAIT` and the `not_available` text say so.
- `tests/hubmcp/test_server.py` `Pinned` pins the four names, checks each maps to an OWNER route of `api.ROUTES`, and checks none is listed or ever reaches the hub.
