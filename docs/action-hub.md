# Action Hub

One private web page, served on your own PC, where you, the owner, answer the PM's questions, work
through hand steps and write new stories when it suits you. Nobody waits on it. The page is
`kitlib/hubsite/web/index.html`; the server is `tools/hub.py serve` (`kitlib.hubsite`), which keeps
the hub's documents in a store folder in git and answers an HTTP API on the loopback address. The
PM uses the same API through the `tools/hub.py` verbs; agents can also reach it through the hub MCP
server (docs/hub-mcp.md), which carries the agent's token and none of the owner's verbs.

## Using the hub

Start the server with `python tools/hub.py serve`, then `python tools/hub.py open`: it opens the
page signed in as you (a one-time pairing link sets the browser's session cookie; `--print` prints
the link instead). `serve` prints the full `open` command on its start line. A paired browser stays paired across server restarts; a session
lasts 30 days (`auth.SESSION_TTL_S`) and renews itself with use. `python tools/hub.py unpair`
ends every browser's session, and a new `owner.token` ends them all too. A browser that is not
paired is told to run `python tools/hub.py open` and open the link it prints. The server's health is
`GET /api/v1/health`.

- **Inbox.** Two tabs. Questions: what the PM and the coordinators ask you, each with its context,
  options and a recommendation; pick one and add a note if you like. Its own tabs are Open,
  Submitted (answered, not yet logged by the PM), Logged, and Other, which shows only when a
  question has another status. Answers: the PM's replies to what you asked in a note, in Unread
  and Read tabs.
- **To PM.** Write the PM a question or an FYI. Add keeps it queued; Send all sends every queued
  item at once. Tabs: Queued (you can edit or delete), Sent (waiting on the PM), Done (the PM's
  reply; the last 30 days, with Show older).
- **To do.** Hand steps for you, each with the exact commands and a Copy button. Tick one when
  done. You can add, edit and delete your own. Tabs: To do and Done.
- **Backlog.** Three tabs: Current backlog (each repository's open work, with priority, status
  and text filters), Parked (backlog items a coordinator has parked waiting on you, with their
  question, branch and age) and Completed (1, 7, 30 or 90 days). Current backlog also shows your
  stories: those in progress above the table, those filed or closed below it, with Withdraw while
  a story is still submitted; the Backlog button counts the stories in progress (highlighted when
  one has questions for you). A row's id in any of the three tabs opens its record page, and the
  New story button opens a blank one ("The record page" below). 
- **Docs.** The agents' documents (a plan, research, a visual choice), newest first, from the
  store's `pages/` and `page_html/` ("pages/<id> and page_html/<id>" below). Read here reads the
  document's body from the server and opens it in a full-screen reader; Close shuts it, and so does
  Esc while the focus is outside the document.  A ref on any card that names a listed document opens it the same way. Delete (pressed twice) removes a document; the page writes
  none, agents publish them.
- **Info.** Usage (each account's plan usage) and Agents (each agent's model and effort).

Under the header a line says whether a PM is listening: "A PM is listening." while a PM holds a
`wait` open, else when one last asked; another session's wait `--for` its own key does not
count (`Api.seen` in `kitlib/hubsite/api.py`). The page reads everything at once
(`GET /api/v1/snapshot`), then waits for the next change (`GET /api/v1/wait`, about 55 seconds on
the server), so what the PM writes shows without a reload. A view the server cannot give says so
at the top.

Links ending `#inbox`, `#todo`, `#topm`, `#backlog` (or `#stories`), `#parked`, `#docs` or `#info` open that
section; `#usage` and `#agents` open that Info tab. The Questions, Answers, To do and Docs lists filter by
repository; Questions, To do and Docs also by id or title.

## Serving it

- `hub.py setup` makes the owner's and the agent's token files where they are absent and prints
  where they are; it never writes the machine config.
- `hub.py serve` serves until Ctrl+C or `hub.py stop`, either of which commits the last changes
  and releases the store lock.
  The port is `hub.port` of the machine config (default 8790); the store is `hub.store`, else
  `hub/` in the PM data worktree (docs/pm-data-branch.md). `--store PATH` serves a scratch copy
  with git sync off. The server needs no `hub` key: without one every default holds.

- `hub.py status` prints the running server's health, one line each: `revision N`, `store ...`,
  `git: ...` (`running, branch B, nothing pending`, or what is pending, a pass that failed, or
  `git sync is off on this server`), `backlog <repo>: fetched X ago`, the tunnel line, and
  `unavailable: ...` for a view the server could not read.  The tunnel line's forms are in docs/hub-remote-access.md "9. Check".
- Remote access is only through the tunnel listener: with `hub.access` set (`team`, `aud` and
  `emails`, all three), the server also listens on `hub.tunnel_port` (default 8791, loopback
  too) for a Cloudflare tunnel, and every request there needs a Cloudflare Access token
  (`kitlib.hubsite.tunnel`). The tool never runs the tunnel program. A valid Access token also
  makes the caller the strong owner. The owner's guide: docs/hub-remote-access.md.
- Autostart (opt-in): `python tools/hub.py setup --autostart` registers a scheduled task that
  runs `hub serve` at logon; `--whatif` shows what it would do and changes nothing. Without the
  flag nothing is registered. It is refused, with nothing registered, when no store folder is
  set. The server then runs windowless, so Ctrl+C does not apply: `hub.py restart` stops it
  cleanly and starts the task again, and `hub.py stop` stops it (docs/hub-remote-access.md
  "8. Restart the hub"). 
- `hub.py stop [--port N] [--timeout S]` and `hub.py restart [--port N] [--timeout S]`
  (`kitlib.hubsite.control`) ask the server to close with the owner's token, on the local
  listener only. `stop` waits until the port is free (`STOPPED: ...`, or `nothing to stop: ...`,
  exit 0). `restart` starts the scheduled task again only when the task ran the server it
  stopped, or nothing ran and the task is ready, then waits for the new server
  (`RESTARTED: ...`). Neither ever kills a process: past `--timeout` (default 300 s) they print
  `TIMED OUT: ...` and exit 4.
- `python tools/hub.py setup --check` writes nothing: it says whether `hub.access` is unset, set
  or cannot serve, fetches the Access keys once when it can serve (`access keys: ...`), and lists
  each kit copy on the machine that would refuse the `hub` key (`kitlib.config.copies`). It ends
  `READY: ...` (exit 0) or `NOT READY: ...` (exit 1). Run it before writing the `hub` key
  (docs/hub-remote-access.md "Before you start").
- `hub.py validate [--store PATH]` checks the store folder against the field tables below, with
  no server, and prints `VALIDATE <LEVEL> <document> <field>: <message> [<code>]`.

The machine config's `hub` key: docs/kit-json.md "Machine config keys". Its settings are `port`,
`tunnel_port`, `store` (an absolute path), `commit_every_s`, `push`, `backlog_refresh_s`,
`access` and `relay`; an unknown key is refused.

Exit codes of every verb: 0 ok, 1 refused (also a Ctrl+C other than the one that closes a
running server, or a server that did not close cleanly), 2 usage error or an unreadable machine
config, 3 the server is not running (the start command is printed), or a `restart` that started
nothing (no task, a disabled one, or a server started by hand: it says which and prints the
start command), 4 a `wait --timeout` that ran out, or a `stop` or `restart` that ran out of time
(nothing was killed). A `wait` whose server closes mid-wait ends with 3 and the start command when the next poll
is refused, or with 1 (`REFUSED: port N did not answer as the hub does: ...`) when the
connection dropped mid-request.

## For the PM

The PM reads and writes the hub only through `tools/hub.py` or the hub MCP server (docs/hub-mcp.md),
both with the agent's token. A coordinator writes only its own questions: it posts each with
`hub.py questions add --from <its launcher session key>` (the `hub key:` line of its session-start
context, `kitlib.coordinatorstart.hub_key`), keeps one `hub.py wait --pending --for
<its key>` running in the background, and acts on the answer itself when it comes. A to-do for its
own work it files with `hub.py todos add --from <its key>`, and acknowledges it (`todos ack`) once
it has acted on it; it hands every other step (notes, stories) to the PM. Who handles each pending
item: "Who handles a pending item" below. For a question, the server takes `--from` only when it is `pm` or a
key of the launcher's sessions.json, read again on each add; any other value is refused, and a
question without `--from` is the PM's. The answer stays pending for everyone, the PM included,
until the PM logs the ruling in the rulings ledger and then on the question (`questions log`), as
for any other answer, so a plain wait would print the same answer again at once: the
coordinator restarts its wait with `--except` and every id it has acted on (`wait --pending --for
<its key> --except Q-7 T-3`), which leaves those ids out and ends only on another item. Every verb takes `--port N` and
`--json` (the server's answer as one JSON line). The server alone decides what a role may do: the
owner's verbs (answer, todos done, stories add and withdraw, to-pm add and send, inbox read) are
refused to the agent. The creator of an open question or to-do changes it with `edit` and removes
it with `withdraw`, naming the `from` it was added with and the version it read; the owner never
edits an agent's item and only removes one ("questions/Q-NNN" and "todos/T-NNN" below).

Verbs (`tools/hub.py` docstring is the source), with the line each prints:

| Verb | Prints |
|---|---|
| `questions [--status S] [--repo R]` | `Q-1 v2 open [repo] title` |
| `questions add --title T [--repo R] [--context-file F] [--option "label::description" ...] [--recommended N] [--refs ID ...] [--urgent] [--story S-n] [--from KEY]` | `ASKED Q-2`, then any warning lines |
| `questions log Q-1 <ruling id> --if-version V` | `LOGGED Q-1 <ruling id>` |
| `questions edit Q-1 --from KEY --if-version V [--title T] [--context-file F] [--option "label::description" ...] [--recommended N] [--refs ID ...] [--urgent \| --no-urgent]` | `EDITED Q-1` |
| `questions withdraw Q-1 --from KEY --if-version V` | `WITHDRAWN Q-1` |
| `answer Q-1 --choice N\|other [--note TEXT] [--if-version V]` (owner) | `ANSWERED Q-1 <label>` |
| `todos [--status S] [--repo R]` | `T-1 open [repo] title` |
| `todos add --title T [--detail D] [--command C ...] [--steps F] [--repo R] [--refs ID ...] [--urgent] [--from NAME \| --owner]` | `ADDED T-2` |
| `todos done T-1 [--undo] [--if-version V]` (owner) | `DONE T-1` or `REOPENED T-1` |
| `todos ack T-1` | `ACKED T-1` |
| `todos edit T-1 --from NAME --if-version V [--title T] [--detail D] [--command C ...] [--repo R] [--refs ID ...] [--urgent \| --no-urgent]` | `EDITED T-1` |
| `todos withdraw T-1 --from NAME --if-version V` | `WITHDRAWN T-1` |
| `stories [--status S] [--repo R]` | `S-1 v2 submitted [repo] title` |
| `stories add --repo R --title T --story-file F [--urgent]` (owner) | `SUBMITTED S-2` |
| `stories set S-1 --status review\|questions\|added\|declined [--reviewer NAME] [--review-note TEXT] [--backlog-id ID] [--closed-note TEXT] --if-version V` | `SET S-1 <status>` |
| `stories withdraw S-1` (owner) | `WITHDRAWN S-1` |
| `to-pm [--status S] [--repo R]` | `P-1 queued [repo] text` |
| `to-pm add --text T [--kind K] [--repo R] [--urgent]` (owner) | `QUEUED P-2` |
| `to-pm send` (owner) | `SENT P-1 P-2` or `SENT nothing` |
| `to-pm reply P-1 TEXT [<ruling id>] [--if-version V]` | `REPLIED P-1` |
| `inbox` | `M-1 unread [repo] title` |
| `inbox add --text T [--title T] [--repo R] [--ref ID] [--asked TEXT] [--refs ID ...] [--from NAME]` | `POSTED M-2` |
| `inbox read M-1 [--undo] [--if-version V]` (owner) | `READ M-1` or `UNREAD M-1` |
| `pending [--for KEY]` | `<kind> <id> [repo] to=<route> from=<key> title` |
| `wait [--since REV] [--timeout S] [--pending [--for KEY] [--except ID ...]]` | `HUB <rev> <kind> <id> to=<route> from=<key> <title>` (pending) or `HUB <rev> <collection> <id> <op> <status>`; `HUB <rev> reset: the store is behind --since; read it again` after a reset |
| `validate [--store PATH]` | `VALIDATE <LEVEL> <document> <field>: <message> [<code>]` |
| `serve [--port N] [--store PATH]`, `status [--port N]`, `setup`, `open [--port N] [--print]`, `migrate` | see "Serving it" and "Data from the old hub" |

Notes on the table:
- `todos` lines carry no version; `todos --json` gives each to-do's `version`.
- `questions add --story` is the story a follow-up belongs to; `stories set`, `questions log` and
  every `edit` and `withdraw` require `--if-version`.
- `hub.py pending` lists the owner input not yet handled: questions answered and not logged,
  stories submitted, To PM items sent, and to-dos done or added by the owner without `pm_ack`
  (`kitlib.hubsite.store.pending_items`). Each item carries `from`, the session key it is for, and
  `route`, the key that is to handle it now (the line's `from=` and `to=`). A question or to-do is
  for the launcher session key it was posted `--from` while that key is still in sessions.json;
  everything else (a to-do's free-text `from`, the owner's own items, stories, To PM items, and a
  question whose session has gone) is for `pm` (`kitlib.hubsite.api.Api.pending`). `pending --for
  KEY` lists only KEY's items, and `pending --for pm` the items routed to `pm` and every answered
  question; reading it marks nothing, so the item stays pending for every reader until it is
  logged or acknowledged.
- `hub.py wait --pending` waits until an item is pending (one already pending ends it at once) and
  prints `HUB <rev> <kind> <id> to=<route> from=<key> <title>` for each; with `--for KEY` it waits
  until an item for KEY is pending and other sessions' items do not end it. `--except ID ...`
  leaves out the items with those ids (ones already acted on, which stay pending until the PM logs
  or acknowledges them), so only another item ends the wait; it marks nothing
  (`kitlib.hubsite.api.wait`). `wait [--since REV]` waits for any change and prints
  `HUB <rev> <collection> <id> <op> <status>`; `--for` or `--except` without `--pending` is a
  usage error. `--timeout S` exits 4 with no line when nothing came. The PM keeps one
  `wait --pending --for pm` running in the background, handles what it prints, then starts it
  again; a PM waiting there is what the page shows as listening. A plain `wait --pending` would
  end at once on an item a creator holds and not yet acknowledged, again and again.

`--if-version V` is the version of the document the PM read and decided on (the `v2` of the
`questions` and `stories` lines); a document that has moved on is refused (`REFUSED conflict`). At
each start, resume and compact the PM session-start checklist (docs/pm-start.md) says whether the
server is up, how many items are pending and whether the PM's `wait --pending --for pm` runs.

### Who handles a pending item

Each pending item goes to the session that created it while that session can be reached, and to
the PM otherwise, so the PM is the fallback for a creator that is gone (`route`, "Routes" in the
`kitlib/hubsite/api.py` docstring):
- An item for `pm` is the PM's.
- An item for a session key, before it is handed over, is that key's while the key can be reached:
  a `wait --pending --for <key>` is open now, or one started or ended within `REACH_GRACE_S` (the
  55-second wait limit plus 30 seconds). Otherwise it is the PM's.
- A `pending` or `wait` `--for <key>` that returns one of the key's items hands it over. The item
  is then the key's for `ACK_WINDOW_S` (30 minutes) from the first hand-over, and the PM's after
  that while it is still pending. The creator ends it by acting on it: `todos ack` for a to-do;
  an answered question stays pending until the PM logs it.
- `--for pm` lists the PM's items and every answered question, whoever asked it, because the PM
  logs each ruling. A PM wait is also woken when an item turns to the PM with no write.
- This state lives in the server's memory only: after a restart every item is the PM's until
  its creator waits again. Older to-dos whose `from` is a free-text name are the PM's.

### Push notifications

When the owner answers a question or marks a to-do done, the server tells the session that asked
and the PM, so neither has to be waiting (`kitlib/hubsite/push.py` docstring is the source).
It is not the store's git push (the `push` setting).

- Who runs it: `hub.py serve` on the live store asks the host adapter for its delivery
  (`push(settings)`; on Claude Code `hosts/claude/adapter/peer.py` `HubPush`) and the launcher's
  `state_dir` (`tools/hub.py` `_push_delivery`). One `PushWorker` thread per server
  (`kitlib/hubsite/serve.py` `build`) then watches the store's events. A scratch store
  (`serve --store`), a host with no delivery or an unset `launcher.state_dir` serves without it,
  with one note line: `push notifications off: <why>`. A worker that fails to start leaves the
  server serving (`push notifications did not start: ...`).
- What fires: a question answered (or answered again), a to-do done (or done again), a done to-do
  reopened, and a to-do the owner added (to the PM only). Each item has a fingerprint (a
  question's status, `answer.at` and a digest of the answer; a to-do's status and `done_at`), and
  only a changed fingerprint fires, so `pm_ack` alone never does.
- Who hears: the item's `from`, unless that is the PM's own (`pm`, the owner, empty), and the
  PM, once. A launcher key is resolved through sessions.json; `pm` through coordinators.json's
  `pm_session_id`, then the names `Project Manager` and `pm-session`.
- What it says: a fixed header, `[hub push] <kind> <id> <status> repo:<repo> from:<from>
  route:<recipient> attempt:<k>`, and one line of what to do; never a title, a note or an
  answer, since the session takes the message as a prompt. A to-do's line is `A done mark is not
  proof: verify each step on disk, then: hub todo ack <id>`: the owner marks a to-do done even
  when a step inside it failed, so the id is there to check against the disk.
- Until when: each recipient is sent each item until it is acknowledged. The asking session
  acknowledges by picking the item up (`pending --for` or `wait --pending --for` hands it over,
  "Who handles a pending item"), or by `todos ack`; the PM by `todos ack` for a to-do and by
  `questions log` for a question. An item withdrawn, reopened or answered again replaces or
  ends its push. Retries come 2, 5, 15, 30 and 60 minutes after a try, then hourly, and stop a
  day after the first try with one `gave-up` line (`push.SCHEDULE_S`, `push.GIVE_UP_S`). 
- How it is sent on Claude Code: one message written to the live session's peer pipe by a
  hidden helper (`hosts/claude/adapter/peer_worker.py`), so a blocked write never blocks the
  server; `written` means the bytes reached the pipe, not that the session read them. Whether a
  desktop session wakes on a push is unproven; the PM's own `wait --pending --for pm` stays its
  path.
- Files, in `launcher.state_dir`: `push-outbox.json` (what is still to send) and
  `push-undelivered.jsonl` (one line per try not written and per give-up; never a token or item
  text).
- Known limit: an answer made just before the server closes that the worker had not yet seen is
  not pushed after a restart. The PM's wait still shows it.

Tests: tests/hubsite/test_push.py, tests/host/test_claude_peer.py, tests/launcher/test_pipes.py,
tests/hubsite/test_hub_cli.py `PushCase`.

## questions/Q-NNN
| Field | Written by | Meaning |
|---|---|---|
| status | PM, owner | `open`, then `answered` (owner), then `logged` (PM) |
| repo, from, asked_at | PM, coordinator | where it came from; `from` is `pm` or the asking coordinator's launcher session key, the session its answer goes back to; asked_at is ISO time |
| title | PM | the question itself |
| context | PM | everything needed to answer; nothing is assumed from chat |
| options | PM | `[{label, description}]`, recommendation first |
| recommended | PM | index into options, normally 0 |
| urgent | PM | true sorts it to the top with an Urgent marker; only for items that block work or have a deadline |
| refs, story | PM | ids it refers to; the story a follow-up question belongs to |
| answer | owner | `{choice: index or "other", label, note, at}` |
| ruling | PM | the ruling id it was logged as |

The PM files each answer, then logs it (`questions log`), pinning `--if-version`; an answer to a
coordinator's question too, which the coordinator has already acted on.

While a question is open its asker may change it (`POST /api/v1/questions/{id}/edit`, agent only:
`hub.py questions edit`, the MCP tool `hub_question_edit`) or withdraw it (`POST
/api/v1/questions/{id}/delete`: `questions withdraw`, `hub_question_withdraw`), which removes the
document. Both take `from` and `if_version`, both required, and are refused `forbidden` unless
`from` is the question's own (`pm` for a question with none), and `conflict` once it is answered or
logged (`kitlib.hubsite.api` `questions_edit`, `questions_delete`). An edit's fields replace the old
ones; `options` replaces the whole list. The owner withdraws an open question with the card's
Withdraw button (the same delete route, owner token, version optional) and never edits one.  All agents share one token, so `from` is a claim: the check stops honest
mistakes, not a hostile agent.

## inbox/M-NNN
The owner's inbox: the PM's answer to every question the owner asks in a note (on a question, a
to-do or a story). The question card links to its answers; the Answers tab counts the unread ones.

| Field | Written by | Meaning |
|---|---|---|
| ref | PM | the question, to-do or story id the owner asked on |
| asked | PM | the owner's question, quoted from the note |
| text | PM | the answer, complete on its own |
| title | PM | optional one-line heading |
| repo, from, at | PM | provenance; at is ISO time |
| read, read_at | owner | the owner marks it read (or unread again) |

PM steps: read every answer note and every to-do or story detail for a question; post the answer
here, and still file any decision the note makes.

### Lock-window notices
The machine lock's own notices that a heavy or exclusive window started or ended, so the owner
hears of it without anyone relaying it (`kitlib/lock/notice.py`). Off unless
`machine_lock.notice.dir` is set in the machine config (`kitlib/lock/site.py`). Notices are per
run, not per window: two heavy runs side by side write two start and two end notices, and the
machine is free again only once every started run has its end notice. Ids are `L-<run id>-start`
and `L-<run id>-end`; fields are title, text, at, repo, from (`machine lock`), kind
(`lock-window`), run and phase. The lock writes each as a file in that folder; they are never
stored in the hub. The server lists them (`GET /api/v1/notices`, `kitlib.hubsite.derived.notices`)
and the PM watcher prints `LOCKNOTICE <id> <title>` for each new one.

## to_pm/P-NNN
The owner's questions and FYIs to the PM. Inbox is PM -> owner and To PM is owner -> PM; replies
to To PM items do not go to inbox/.

| Field | Written by | Meaning |
|---|---|---|
| (doc id) | server | `P-001`, `P-002`, ...: the document's id, not a field |
| kind | owner | `question` or `fyi` |
| repo | owner | a repo name, `pm`, `all`, or empty |
| urgent | owner | true marks it Urgent and makes the badge hot |
| text | owner | the message, as written |
| status | owner, PM | `queued`, `sent` (Send all), `done` (PM) |
| created_at, sent_at | server | ISO times; sent_at is null until sent |
| reply, ruling | PM | the answer, complete on its own (null or empty shows as "noted"); the ruling it was filed as |
| done_at | server | ISO time the PM replied; the Done window and order use it |

PM steps: each sent item is pending. Answer a Question with its reply; for an FYI reply with a
short acknowledgement. `to-pm reply` sets `done`.

## todos/T-NNN (or a generated id for the owner's own items)
| Field | Meaning |
|---|---|
| status | `open` or `done` (the owner ticks it) |
| title, detail | what to do and why |
| commands | a list of strings (a bare string is an error in `kitlib.hubvalidate`): the exact PowerShell commands, each with a Copy button, on every item that involves running something; to edit a file by hand, a command that opens it |
| repo, from, added_at | provenance; `from: you` on the owner's own items |
| done_at | set when ticked |
| urgent | true sorts it to the top; the owner can set it on their own items |
| pm_ack | PM: set once it has followed up on a done item or picked up an owner-added one; takes it off the pending list |

A new to-do (`POST /todos`, so `hub.py todos add` and the MCP tool `hub_todo_add` alike) may also
carry `steps`, which is not stored: `kitlib.hubsite.api.todo_steps` turns each step into a command
appended after `commands` and adds one summary line per step at the end of `detail` (the steps and
their commands: docs/hub-mcp.md "To-do steps"). `hub.py todos add --steps F` reads the list from F,
a UTF-8 JSON file.  The server refuses, as `invalid` naming the field, a bad step and, on an agent's
to-do, text the owner's to-do rules forbid (`todosteps.check`: plain Notepad, a hand edit
described in prose, coordinator work). The owner's own to-dos are not judged by those rules.

While a to-do is open its creator may change it (`POST /api/v1/todos/{id}/edit` with the agent's
token: `hub.py todos edit`, the MCP tool `hub_todo_edit`) or withdraw it (`POST
/api/v1/todos/{id}/delete`: `todos withdraw`, `hub_todo_withdraw`), with the same `from` and
`if_version` rules and refusals as a question ("questions/Q-NNN" above). An agent's edit takes no
`steps`, its changed text is judged by the to-do rules above, and it clears `pm_ack`.  An agent's open to-do needs at least one command. The owner
edits and deletes only their own to-dos (`from: you`) at any status, and deletes an agent's to-do
(never edits it) while it is open (`kitlib.hubsite.api` `todos_edit`, `todos_delete`).

## stories/S-NNN
The owner writes a story and picks the backlog it belongs in. It reaches the backlog only through
that repo's coordinator.

| Field | Written by | Meaning |
|---|---|---|
| status | owner, PM | `submitted` (owner), `review`, `questions`, `added` or `declined` (PM); `withdrawn` (owner, only while submitted) |
| repo | owner | a repo name, such as `alpha` or `pm` |
| title, story, urgent, from, submitted_at | owner | the owner's story as written |
| reviewer | PM | the coordinator reviewing it |
| review_note | PM | the coordinator's summary, shown on the card |
| backlog_id, added_at | PM | the record the coordinator filed |
| closed_note, closed_at | PM, owner | why it was not added, or when it was withdrawn |

Follow-up questions are ordinary questions with `story` set; the page links them both ways. 

PM steps:
1. On a `submitted` story, send it verbatim to that repo's coordinator (coordinators stay in
   scope) and set `review` with `reviewer`.
2. The coordinator reviews it against its backlog (duplicates, size, fit) and either files it or
   sends follow-ups to the PM. The PM asks each follow-up as a question with `--story` and sets
   `questions`.
3. The PM files the answers as usual and relays them to the coordinator; the story goes back to
   `review`.
4. When the coordinator reports the filed record, set `added` with `--backlog-id` and a one-line
   `--review-note`. A coordinator that declines (duplicate, out of scope) gives a reason; set
   `declined` with `--closed-note`.

The coordinator files the record itself with its own backlog tool, citing the story.

## repos/<name>
{name, added_at, aliases?}: `aliases` (optional, a list of strings) names old tags an item's
`repo` may still carry; the page files such an item under this repo (case ignored). The server's
repo list (`GET /api/v1/repos`, `kitlib.hubsite.derived.repos`) is every repository the PM knows
(`kitlib.pmstart.known_repos`: each session's repo in `sessions_file`, the PM's repository, this
kit's repository and the optional `pm_start.repos`) with the aliases the store holds. An item whose
repo is `all` is for every repo and shows under each one.

## pages/<id> and page_html/<id>
An agent's document: `pages/<id>` is its card, `page_html/<id>` = {html} its body. In `pages`,
`kitlib/hubsite/schema.py` requires only `title` (a string); the page also reads `repo`, `from`,
`summary`, `refs` (a list of ids), `at` and the store's own `updated_at`, each as text, and orders
the list by `updated_at`, else `at`, newest first (`pageRows` in `kitlib/hubsite/web/index.html`).
The ids have no prefix: the store assigns none (`Store.add` refuses `pages`); the publisher names
one, and an id that holds a hub item's or a ruling's id anywhere, as `kitlib.hubvalidate`'s ref
pattern finds one (`Q-12`, `R-3`, `plan-Q-1`, `Q-1.v2`), is refused (`_REF_SHAPED` in
`kitlib/hubsite/api.py`), so a ref that names a page is never read as an item.

The routes ("Pages" in the `kitlib/hubsite/api.py` docstring):

| Route | Who | What |
|---|---|---|
| `POST /api/v1/pages` | agent | {id, title, html, repo?, from?, summary?, refs?, if_version?}: writes the body, then the card |
| `GET /api/v1/pages` | anyone | the cards, filtered by `status` and `repo` like every list |
| `GET /api/v1/pages/<id>` | anyone | {id, html, version} of the body; `not_found` when there is none |
| `POST /api/v1/pages/<id>/delete` | owner | deletes the card, then the body; `if_version` optional |

An agent publishes with `hub.py pages publish ID --title T --html-file F` or the hub MCP tool
`hub_page_publish` (docs/hub-mcp.md "Agent write tools"), and reads one back with
`hub.py pages show ID [--out F]` or `hub_page`. Both run `kitlib.hubsite.pagecheck.check` on the
file before sending, so a body the server would refuse is never sent. Publishing over an existing
page needs the card's version as `if_version` (`--if-version`); without it the write is
`conflict`. Publishes and deletes are serialised (`PAGES_LOCK` in `kitlib/hubsite/store.py`), and the body
is written before the card, so a card never names a missing body.
Deleting is the owner's alone: the Docs tab's Delete, with no CLI verb and no MCP tool.

What a body may hold (`kitlib/hubsite/pagecheck.py`, its docstring has the full list): at most
512 KiB of UTF-8 (`MAX_BYTES`); no `script`, `iframe`, `object`, `embed`, `form`, `link`, `meta`,
`base` or other element of `TAGS`; no `on*` attribute; no `href` that is not a `#fragment` (a
`data:` image on an svg `image` is the one exception); `src` and CSS `url()` only as `data:`; no
`@import`, CSS backslash escape or function of `CSS_FUNCTIONS` (`image-set()`, `image()`,
`src()`). Markup that the check's parser and a browser could read differently is refused too. A body
that breaks a rule is refused whole, never stripped (`invalid`, field `html`). Mermaid is not
rendered: draw the diagram as inline `<svg>`. The listener refuses any request body over 1 MiB
(`MAX_BODY` in `kitlib/hubsite/httpd.py`), so a body near the cap that JSON encoding about
doubles is refused there.

`GET /api/v1/snapshot` carries the cards and leaves every body out; the reader asks
`GET /api/v1/pages/<id>` when it opens.

The body is agent-written html. The page draws it only in the reader's one frame, sandboxed
with nothing granted and behind a content policy that blocks everything the body loads by itself
but `data:` images (`DOC_CSP` in `kitlib/hubsite/web/index.html`). Links are not blocked; the
page check refuses them at publish. A body placed in the store by hand or copied in by `migrate`
is not checked.

`python tools/hub.py validate` (`_check_pages` in `kitlib/hubvalidate/__init__.py`) warns when a
page's title is missing or empty, when a `page_html` has no string `html`, and when a `page_html`
has no `pages` document (only when the `pages` collection exists). It does not warn about a page
with no body.

## approvals/A-NNN
A change an agent asks the owner to approve before it commits it. The rules (signature, expiry,
what verifies) are `kitlib/hubsite/approvals.py`, "The document"; the routes are
`kitlib.hubsite.api` ("Approvals" in its docstring).

| Field | Written by | Meaning |
|---|---|---|
| (doc id) | server | `A-1`, `A-2`, ...: the document's id, not a field |
| status | server | `pending`, `approved`, `denied`, `consumed` or `expired` (`approvals.STATUSES`); expiry is read from the times, never written |
| repo | agent | the repository, as the repo_id of its remote URL (`approvals.repo_id`) |
| title, reason | agent | what the change is and why; reason is optional |
| head | agent | the commit the change applies to (40 hex) |
| files | agent, server | `[{path, base_blob, blob, content}]` sorted by path; the server computes `blob` from `content`. A null `base_blob` is a new file, a null `content` (and `blob`) a delete (`approvals.new_doc`) |
| diff | server | the unified diff lines the page shows, from the base read with git from this machine's checkout of `repo` to `content` (`approvals.diff`) |
| created_at | server | ISO time of the request |
| decision, decided_at, sig | server | once the owner decides; `sig` (the approval key's signature) on an approval only |
| commit, consumed_at | server | once an agent consumes the approval for a commit |

Routes, below `/api/v1`:

- POST `/approvals` (agent): body `repo`, `title`, `head`, `files`, optional `reason`; adds a
  pending request. A repository this machine has no checkout of is refused (`invalid`, field
  `repo`), and so is a `base_blob` that is not the file at `head` (field `files`).
- POST `/approvals/{id}/decide` (role `approver`: the strong owner alone, docs/hub-remote-access.md
  "The strong owner"): body `decision` (`approve` or `deny`) and `if_version`. Only a pending
  request is decided.
- POST `/approvals/{id}/consume` (agent): body `commit` and `if_version`; only an approval that is
  `approved` is consumed.
- GET `/approvals/{id}/verify` (agent): query `repo`, `head`, `files` (JSON `{path: blob}`) and an
  optional `commit`; answers `{ok, valid, why}`, never the key or the sig. With `commit`, only an
  approval consumed by that commit is valid.

The agent's side of an approval is `commit.py --owner-approval hub:A-<n>` (docs/commit.md "Owner
approval"): it verifies, writes and commits the approved files, then consumes the approval for that
commit and verifies it with the commit; `gate land` and the pre-push hook verify it again
(`kitlib.guard.ownercommits`). An approval verifies for 24 hours after the decision, consumed or
not (`approvals.APPROVED_TTL_S`).

`GET /api/v1/snapshot` carries the collection whole, and `strong`: true only on the strong
owner's call. The page draws each pending request above the open questions in Inbox; Approve and
Deny are disabled unless the snapshot says `strong`.

## Views read live
Never stored; read from their sources (`kitlib.hubsite.derived`, `kitlib.hubsite.backlog`):
- backlog and completed: each repository's backlog store (`.kit.json` backlog.store), every open
  record and every record closed in the last 90 days, fetched every `hub.backlog_refresh_s`.
  Parked is the backlog items whose state is parked. A repository's folder is
  `pm_start.repo_paths[name]`, else the PM repository's sibling. While that timer runs, the snapshot answers these from the timer's last reads and starts no
git; `GET /api/v1/backlog?fresh=1` runs a fetch round and reads live.
- usage: `launch.py usage` (docs/launcher.md "Usage").
- agents: the kit's agent definitions.

## The record page
One backlog record, read live and never part of the snapshot (`kitlib/hubsite/api.py` `record`,
`record_new` and the record writes block; `kitlib/hubsite/serve.py` `SiteViews`). `{repo}` only
picks a configured repository (`Views._named` in `kitlib/hubsite/backlog.py`); its folder never
comes from the caller.
- `GET /api/v1/records/{repo}/{id}` (owner and agents): `read_record` in
  `kitlib/hubsite/backlog.py`, whose docstring lists the keys: every field, the body, the
  blockers, the hold with its ruling or question, the branch for a pulled or parked record, and
  `version`. `GET /api/v1/records/{repo}/new` is the blank record (`record_template`).
- `POST /api/v1/records/{repo}` (owner only), body `id`, `summary`, `scope` (a list), `effort`,
  `ref`: adds an unpriced record through the backlog library's add (`kitlib/hubsite/recordwrite.py`
  `add`). It is written `found_on: none`, with the store's `Session: owner-hub` trailer and a
  `review: owner-hub <date>` mark, so `pull` refuses it and `next` never offers it until a
  coordinator runs `clear-review` (docs/backlog.md "Records").
- `POST /api/v1/records/{repo}/{id}/edit` (owner only), body `version` (the one the read
  answered), `changes` (`{key: [values]}`), `unset`, `body`, `ruling`: the backlog library's set.
  A record that changed since that read is `conflict` on field `version`; read it again.
- `POST /api/v1/records/{repo}/{id}/delete` (owner only), body `why`, `confirm`, `ruling`:
  `confirm` must repeat the id, or the call is `invalid` on field `confirm` and nothing is
  written. The store's delete rules apply unchanged: a live pull is refused until it is released.
- `ruling` is sent only when the store's refusal asks for one (an owner-priced or owner-held
  record); the store checks it as it checks a ruling given to `tools/backlog.py`.
- Each write answers `lines`, what the store printed (its notes included), and an add or edit
  also answers the record as it reads now. A store refusal is `invalid` in the store's words; a
  record not there is `not_found`. A repository whose `.kit.json` names no backlog store, or whose
  store cannot be written from the hub's checkout (a folder store outside a linked worktree), is
  `store`: the owner sees the store's text, an agent or the tunnel the fixed phrase.

On the page, a row's id in Current backlog, Parked or Completed opens the record: its fields,
blocker or hold, body and branch (read only). On an open record the first three sections each
have an Edit button; Save sends only what changed, at the version the page read, and a refusal
shows the store's text. Delete… needs a reason and the id typed again. The New story button
(Backlog) opens a blank record for the chosen repository.

## Data from the old hub
Documents mirrored from the retired artifact hub sit in git as `hub/<collection>/<doc_id>.json`
in the PM data worktree, else in the absolute folder `pm_start.hub_mirror` (`kitlib.hubmirror`,
`tools/hub_mirror.py`). `python tools/hub.py migrate --from DIR [--from DIR ...] [--store PATH] [--dry-run]` copies
those documents into the hub site's store (`kitlib.hubsite.migrate`). Use a new store folder.
It only copies in, never replaces a document the store holds, and is refused while a server
holds the store. A second run prints `nothing changed`.

A document the store holds with a differing `version` is kept; any other difference is a
conflict, and the run exits 1. `--dry-run` writes nothing.

`tools/hub_mirror.py save` refuses a mirror
folder that is a live hub store (`kitlib.hubsite.store.META_NAME`): the server is the only
writer there.
