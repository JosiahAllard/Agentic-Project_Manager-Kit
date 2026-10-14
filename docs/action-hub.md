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
the link instead). `serve` prints the full `open` command on its start line. A browser that is not
paired (a first visit, or any browser after the server restarts: sessions live in the server's
memory) is told so on the page and to run `python tools/hub.py open` in the kit folder and open
the link it prints. The server's health is `GET /api/v1/health`.

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
- **Backlog.** Four tabs: New story (the form, then your stories in progress and filed; Withdraw
  while a story is still submitted), Current backlog (each repository's open work, with priority,
  status and text filters), Parked (backlog items a coordinator has parked waiting on you, with
  their question, branch and age) and Completed (1, 7, 30 or 90 days).
- **Info.** Usage (each account's plan usage) and Agents (each agent's model and effort).

Under the header a line says whether a PM is listening: "A PM is listening." while a PM holds a
`wait` open, else when one last asked. The page reads everything at once
(`GET /api/v1/snapshot`), then waits for the next change (`GET /api/v1/wait`, about 55 seconds on
the server), so what the PM writes shows without a reload. A view the server cannot give says so
at the top.

Links ending `#inbox`, `#todo`, `#topm`, `#backlog` (or `#stories`), `#parked` or `#info` open that
section; `#usage` and `#agents` open that Info tab. The Questions, Answers and To do lists filter by
repository.

## Serving it

- `hub.py setup` makes the owner's and the agent's token files where they are absent and prints
  where they are; it never writes the machine config.
- `hub.py serve` serves until Ctrl+C, which commits the last changes and releases the store lock.
  The port is `hub.port` of the machine config (default 8790); the store is `hub.store`, else
  `hub/` in the PM data worktree (docs/pm-data-branch.md). `--store PATH` serves a scratch copy
  with git sync off. The server needs no `hub` key: without one every default holds. A trial
  run with `--store` (and `migrate --store`) therefore never writes the `hub` key; the owner
  writes that key only for the live store (docs/go-live.md "Switches").
- The PM session-start checklist (docs/pm-start.md) runs its site checks whether or not the `hub`
  key is written: it uses the key's settings, or the defaults (port 8790) when there is none.
  Writing the key does not switch the checklist.
- `hub.py status` prints the running server's health, one line each: `revision N`, `store ...`,
  `git: ...` (`running, branch B, nothing pending`, or what is pending, a pass that failed, or
  `git sync is off on this server`), `backlog <repo>: fetched X ago`, the tunnel line, and
  `unavailable: ...` for a view the server could not read. The tunnel line is `tunnel: on` (the
  Access keys were fetched), `tunnel: bound, keys not fetched` (shown until the first request
  with a token; a failed fetch adds ` (reason)`, such as `keys-fetch`) or `tunnel: off: <why>`.
  `on` does not say that a tunnel program reaches the listener.
- Remote access is only through the tunnel listener: with `hub.access` set (an Access `team`,
  the application's audience tag `aud` and the allowed `emails`; all three), the server also
  listens on `hub.tunnel_port` (default 8791, loopback too) for a Cloudflare tunnel, and every
  request there needs a Cloudflare Access token in the header `Cf-Access-Jwt-Assertion`
  (`kitlib.hubsite.tunnel`). The tool never runs the tunnel program. Both listeners bind the
  loopback address only, and `hub.port` and `hub.tunnel_port` must differ. If `hub.access` is
  incomplete the tunnel stays off and the local listener keeps serving; `status` says why. The
  owner's step-by-step guide to reach the hub from a phone: docs/hub-remote-access.md. The
  health answer through the tunnel holds fixed words only: `ok`, `version`, `store` (`ok`),
  `git` (`ok`, `failed`, `off` or `unknown`) and `tunnel` (`on` or `bound, keys not fetched`);
  no folder, branch, reason or revision. A health answer the server cannot give leaves as its
  status and error code with the message `health could not be answered`.
- Autostart (opt-in): `python tools/hub.py setup --autostart` registers a scheduled task that
  runs `hub serve` at logon; `--whatif` shows what it would do and changes nothing. Without the
  flag nothing is registered. It is refused, with nothing registered, when no store folder is
  set. The server then runs windowless, so Ctrl+C does not apply: end it with
  `Stop-ScheduledTask -TaskName 'PM kit hub server'` and start it again with
  `Start-ScheduledTask`. A server that was cut off without a clean close leaves its store lock,
  which the next start takes over with a note (docs/hub-remote-access.md).
- `hub.py validate [--store PATH]` checks the store folder against the field tables below, with
  no server, and prints `VALIDATE <LEVEL> <document> <field>: <message> [<code>]`.

The machine config's `hub` key: docs/kit-json.md "Machine config keys". Its settings are `port`,
`tunnel_port`, `store` (an absolute path), `commit_every_s`, `push`, `backlog_refresh_s`,
`access` and `relay`; an unknown key is refused.

Exit codes of every verb: 0 ok, 1 refused (also a Ctrl+C other than the one that closes a
running server, or a server that did not close cleanly), 2 usage error or an unreadable machine
config, 3 the server is not running (the start command is printed), 4 a `wait --timeout` that ran
out. A `wait` whose server closes mid-wait ends with 3 and the start command when the next poll
is refused, or with 1 (`REFUSED: port N did not answer as the hub does: ...`) when the
connection dropped mid-request.

## For the PM

The PM reads and writes the hub only through `tools/hub.py` or the hub MCP server (docs/hub-mcp.md),
both with the agent's token; coordinators
never write to it, they send questions and hand steps to the PM. Every verb takes `--port N` and
`--json` (the server's answer as one JSON line). The server alone decides what a role may do: the
owner's verbs (answer, todos done, stories add and withdraw, to-pm add and send, inbox read) are
refused to the agent.

Verbs (`tools/hub.py` docstring is the source), with the line each prints:

| Verb | Prints |
|---|---|
| `questions [--status S] [--repo R]` | `Q-1 v2 open [repo] title` |
| `questions add --title T [--repo R] [--context-file F] [--option "label::description" ...] [--recommended N] [--refs ID ...] [--urgent] [--story S-n] [--from NAME]` | `ASKED Q-2`, then any warning lines |
| `questions log Q-1 <ruling id> --if-version V` | `LOGGED Q-1 <ruling id>` |
| `answer Q-1 --choice N\|other [--note TEXT] [--if-version V]` (owner) | `ANSWERED Q-1 <label>` |
| `todos [--status S] [--repo R]` | `T-1 open [repo] title` |
| `todos add --title T [--detail D] [--command C ...] [--repo R] [--refs ID ...] [--urgent] [--from NAME \| --owner]` | `ADDED T-2` |
| `todos done T-1 [--undo] [--if-version V]` (owner) | `DONE T-1` or `REOPENED T-1` |
| `todos ack T-1` | `ACKED T-1` |
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
| `pending` | `<kind> <id> [repo] title` |
| `wait [--since REV] [--timeout S] [--pending]` | `HUB <rev> <kind> <id> <title>` (pending) or `HUB <rev> <collection> <id> <op> <status>`; `HUB <rev> reset: the store is behind --since; read it again` after a reset |
| `validate [--store PATH]` | `VALIDATE <LEVEL> <document> <field>: <message> [<code>]` |
| `serve [--port N] [--store PATH]`, `status [--port N]`, `setup`, `open [--port N] [--print]`, `migrate` | see "Serving it" and "Data from the old hub" |

Notes on the table:
- `todos` lines carry no version.
- `questions add --story` is the story a follow-up belongs to; `stories set` and `questions log`
  require `--if-version`.
- `hub.py pending` lists the owner input not yet handled: questions answered and not logged,
  stories submitted, To PM items sent, and to-dos done or added by the owner without `pm_ack`
  (`kitlib.hubsite.store.pending_items`).
- `hub.py wait --pending` waits until an item is pending (one already pending ends it at once) and
  prints `HUB <rev> <kind> <id> <title>` for each; `wait [--since REV]` waits for any change and
  prints `HUB <rev> <collection> <id> <op> <status>`. `--timeout S` exits 4 with no line when
  nothing came. The PM keeps one `wait --pending` running in the background, handles what it
  prints, then starts it again; a PM waiting there is what the page shows as listening.

`--if-version V` is the version of the document the PM read and decided on (the `v2` of the
`questions` and `stories` lines); a document that has moved on is refused (`REFUSED conflict`). At
each start, resume and compact the PM session-start checklist (docs/pm-start.md) says whether the
server is up, how many items are pending and whether `wait --pending` runs.

## questions/Q-NNN
| Field | Written by | Meaning |
|---|---|---|
| status | PM, owner | `open`, then `answered` (owner), then `logged` (PM) |
| repo, from, asked_at | PM | where it came from; asked_at is ISO time |
| title | PM | the question itself |
| context | PM | everything needed to answer; nothing is assumed from chat |
| options | PM | `[{label, description}]`, recommendation first |
| recommended | PM | index into options, normally 0 |
| urgent | PM | true sorts it to the top with an Urgent marker; only for items that block work or have a deadline |
| refs, story | PM | ids it refers to; the story a follow-up question belongs to |
| answer | owner | `{choice: index or "other", label, note, at}` |
| ruling | PM | the ruling id it was logged as |

The PM files each answer, then logs it (`questions log`), pinning `--if-version`.

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

## Views read live
Never stored; the server reads each from its source on request (`kitlib.hubsite.derived`,
`kitlib.hubsite.backlog`):
- backlog and completed: each repository's backlog store (`.kit.json` backlog.store), every open
  record and every record closed in the last 90 days, fetched every `hub.backlog_refresh_s`.
  Parked is the backlog items whose state is parked. A repository's folder is
  `pm_start.repo_paths[name]`, else the PM repository's sibling.
- usage: `launch.py usage` (docs/launcher.md "Usage").
- agents: the agent definitions in `claude/agents`.

## Data from the old hub
The hub was once a claude.ai artifact page, one copy per Claude account; it is retired. What the
owner and the PM wrote on it was mirrored in git as `hub/<collection>/<doc_id>.json` in the PM
data worktree, else in the absolute folder `pm_start.hub_mirror` (`kitlib.hubmirror`,
`tools/hub_mirror.py`). `python tools/hub.py migrate --from DIR [--from DIR ...] [--store PATH] [--dry-run]` copies
those documents into the hub site's store (`kitlib.hubsite.migrate`). Use a new store folder.
It only copies in, never replaces a document the store holds, and is refused while a server
holds the store. A second run prints `nothing changed`.

- A store document with a `version` that differs from the source is kept (the newer truth) and
  does not fail the run. One without a `version` that differs is a conflict, and the run exits 1.
- Two sources that differ, with nothing in the store, write neither side: a conflict.
- A file that is no document, or has a bad or mismatched id, is skipped, and the run exits 1.
- A source folder the store has no collection for is left alone. A question's text
  `recommended` is written as a number.
- `--dry-run` writes nothing, and takes the store lock only if the lock file already exists.
- The report has `hub store: ...`, `source: ...`, per-collection counts, `NOT COMPLETE: ...` when
  it stopped short and `imported N; no source was changed` (a dry run ends `dry run: ... nothing
  was written`).

`tools/hub_mirror.py save` refuses a mirror
folder that is a live hub store (`kitlib.hubsite.store.META_NAME`): the server is the only
writer there.
