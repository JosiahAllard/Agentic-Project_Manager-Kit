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
the link instead). `serve` prints the full `open` command on its start line. A paired browser stays
paired when the server restarts: the server keeps each session, as a keyed hash and never the
cookie itself, in a `browser-sessions-<port>.json` file beside the token files
(`kitlib/hubsite/auth.py`, "Sessions"). `serve --port 0` binds a new port each run, so it keeps
no such file and its browsers are paired again after a restart (`kitlib/hubsite/serve.py`,
`build`). A session lasts 30 days (`auth.SESSION_TTL_S`) and is
renewed to a full 30 days when the browser uses it with less than half of that left, so a browser
you use at least every two weeks never needs pairing again. `python tools/hub.py unpair` ends
every browser's session (the owner's token only; with no server running it empties that file
itself), and a new `owner.token` ends them all too. A browser that is not paired (a first visit,
or after an unpair or a session that ran out) is told so on the page and to run
`python tools/hub.py open` in the kit folder and open the link it prints. The server's health is
`GET /api/v1/health`.

The sidebar lists the sections in groups under small headings: Home on its own; Your work (Questions,
To-dos, Approvals); Projects (Backlog, Schedule, Docs); More (Message the PM, Usage and agents);
Upkeep (Lint findings, Automatic tasks). A count sits at the right edge of its entry, and is highlighted
for what waits on you: any pending approval, urgent question or to-do. Lint findings shows a dash until
the page has read `GET /api/v1/findings` once (at the start and after each wait wake, without drawing the
section), then the number of open findings. On a phone the sidebar becomes a bar of five tabs: Home,
Questions, Approvals, Schedule and More, a page of the other sections
with their counts (`#more`; a wider window opens Home instead). Between 761 and 1100 pixels the detail
pane stacks under the list instead of beside it.

Home is titled "Today <date>" and lists what waits on the owner in four groups: Decisions (open questions),
Approvals (pending), Hand steps (open to-dos) and Replies from the PM (`to_pm` notes with status `done` and a
`reply`, whose `done_at` is within `HOME_REPLY_DAYS` (7) days, newest first; the snapshot has no read marker).
Each row opens its card in the detail pane, and the first is open on a wide screen. The machine strip sits under
the title.

- **Questions.** Two tabs. Questions: what the PM and the coordinators ask you, each with its context,
  options and a recommendation; pick one and add a note if you like. Its own tabs are Open,
  Submitted (answered, not yet logged by the PM), Logged, Filed (shows only when a filed question
  exists; a filed answer sits there until its asker logs it), and Other, which shows only when a
  question has another status. Answers: the PM's replies to what you asked in a note, in Unread
  and Read tabs.
- **Message the PM** (the `#topm` section). Write the PM a question or an FYI. Add keeps it queued; Send all sends every queued
  item at once. Tabs: Queued (you can edit or delete), Sent (waiting on the PM), Done (the PM's
  reply; the last 30 days, with Show older). Each message is a short row (its first line, and
  Urgent, its type and its repository as chips); the row opens the whole message in the detail
  pane, with Edit and Delete on a queued one and the PM's reply on a done one.
- **Approvals.** What an agent asked to run or change. Two tabs: Waiting (pending approvals, each
  with Approve and Deny, which need a strong session) and Decided (approved, denied, used or expired
  ones from the snapshot, newest first, with the decision and no buttons). The tab is not an address.
  The Approvals count is the pending ones; Questions no longer lists approvals.
- **To-dos.** Hand steps for you, each with the exact commands and a Copy button. Tick one when
  done. You can add, edit and delete your own. Tabs: To do and Done.
- **Backlog.** Three tabs: Open (each repository's open work under a heading per priority, with
  priority, status and text filters, which stay chips), Parked (backlog items a coordinator has
  parked waiting on you, by repository, with their age) and Completed (1, 7, 30 or 90 days). Each
  item is a short row; a row opens its detail beside the list: the fields the backlog view
  carries (priority, status, branch, session, question) and three buttons that open its record
  page: Edit fields and Edit body open that section's editor, and Retire opens the page with its
  delete form open (the delete still asks for a reason and the typed id; there is no other
  retire route). The backlog view carries no effort, scope or body, so the detail reads the record (`GET /records/<repo>/<id>`, the route the record page uses) when it opens and adds Effort, Scope (one line per path) and Body; if that read fails it says so and the buttons stay. A completed
  row's detail has Open the record. Open also shows your
  stories: those in progress above the table, those filed or closed below it, with Withdraw while
  a story is still submitted; the Backlog button counts the stories in progress (highlighted when
  one has questions for you). The New story button opens a blank record page ("The record page" below). The New story tab and its form
  are gone; `stories add` still files a story.
- **Docs.** The agents' documents (a plan, research, a visual choice), newest first in two groups,
  This week and Earlier (by the document's last change; one with no time is Earlier), from the
  store's `pages/` and `page_html/` ("pages/<id> and page_html/<id>" below). A row opens the
  document's detail: its summary as the excerpt (the body is not read for it), its refs, and the
  buttons. Ask the PM about it puts a line about the document in the Message the PM form, under
  anything already typed there, and opens that section; it sends nothing. Read full screen reads the
  document's body from the server and opens it in a full-screen reader; Close shuts it, and so does
  Esc while the focus is outside the document. A ref on any card (a question, a to-do, a note or
  a document) that names a listed document is a button that opens it the same way
  (`test_a_page_id_in_a_question_or_a_to_do_opens_the_reader`). Delete (pressed twice) removes a document; the page writes
  none, agents publish them.
- **Usage and agents** (`#info`). Usage (each account's plan usage), Agents (each agent's model and
  effort) and Lock notices. Usage and Agents are rows (an account with its 5-hour and weekly
  percentages, an agent with its model and effort); a row opens the account's meters or the agent's
  definition in the detail pane. Lock notices stay a list.
- **Schedule.** A section of its own (the sidebar's Projects group and the phone's Schedule tab; the
  hash `#schedule`), drawn from `GET /lock/view` ("Machine lock routes" below), with four tabs:
  - Now lists the runs now (each with a progress line against the time expected, and a Late chip
    past it), the line behind them (Next up, from `queue_items`), today's holds, and a closed
    History row that opens the finished runs, filterable to those that failed or were stopped. A
    run's detail has End run (asked twice) and a waiting run's has Move to top. The sidebar count
    is the number of runs now.
  - Week is the week of half hours (back two weeks, on four). Reserve time on a card opens it in
    pick mode: the half hour a window starts in, then the one it ends in, then Reserve the whole
    machine or half, with a note.
  - Holds lists the owner's holds and the agents' holds. New hold opens a form (the whole machine,
    half, or named slots, with typed times); Edit changes a hold's window or note and Release (asked
    twice) ends it, whoever made it.
  - Settings has the Game mode card (on or off, and the games list) and the Division card (what is in
    force, what the owner set, the default for each key; change it or use the defaults).
  - History (under Now) also shows what ran over the last 1, 7, 30 or 90 days.

  One watcher reads the lock for every view (the Home machine strip, the count, this section): it
  waits on `GET /lock/wait`, stops while the browser tab is hidden and reads again when shown,
  backing off like the other waits. Home's machine strip (the slot boxes drawn by the same `schBoxes` as the Schedule's machine card, and a summary line) is one link to `#schedule`. Every control of the
  old lock page now lives on the Schedule; `/lock.html` is removed and answers 404, with no redirect
  (`tests/hubsite/test_page.py`, `ServedTest`). Times are the PC's local
  time and a reserved window is sent in UTC.

  A control this login may not use is disabled and says why (a "Read only" line gives the reason):
  holds and the division need the strong owner; a hub started under an agent session changes
  nothing (`tests/hubsite/test_page.py`, `ScheduleTest`; `ScheduleChangesTest`). The lock routes themselves stay the owner's: an agent is refused them
  (`tests/hubsite/test_api_roles.py`, `tests/hubsite/test_lockdoor.py`).

In the sidebar's foot (when the hub was read, then the PM line and the push line) a line says whether a PM is listening: "A PM is listening." while a PM holds a
`wait` open, else "Push delivery reaches the PM." while the snapshot's `push` state is `up` and
its `last` is `written` (a try to the PM was written), else when one last asked; another session's wait `--for` its own key does not count (`Api.seen` in
`kitlib/hubsite/api.py`). With a push worker running, the line ends with the PM watcher's
heartbeat from the snapshot's `watcher` field ("Push notifications"): "The PM's watcher is
running.", "The PM's watcher has stopped: its last heartbeat is <age> old." or "No PM watcher
heartbeat is found." (`pmSeenText` in `kitlib/hubsite/web/index.html`). On a phone the header also shows a pill, "PM listening" or "No PM listening". A second line gives the push notifications'
status from the snapshot's `push` field ("Push notifications"): "Push delivery is up" or "is
down" (with the state of the latest try to the PM), and how many items are undelivered, or "Push notifications
are off on this hub server." when the server runs no push worker. The page reads everything at once
(`GET /api/v1/snapshot`), then waits for the next change (`GET /api/v1/wait`, about 55 seconds on
the server), so what the PM writes shows without a reload. A view the server cannot give says so
at the top.

Links ending `#inbox`, `#approvals`, `#more`, `#todo`, `#topm`, `#backlog` (or `#stories`), `#parked`, `#docs`, `#autotasks`, `#findings` or `#info` open that
section; `#usage` and `#agents` open that Info tab. The Questions, Answers, To do and Docs lists filter by
repository; Questions, To do and Docs also by id or title. Automatic tasks lists the schedules
("schedules/") under "Schedules", and the two PC-set sweeps (`worktree-sweep`, `agent-temp-sweep`, which
machine setup registers with Windows) under "Set on the PC": read-only, with a lock, and a plain row with no times
where the hub holds no schedule doc for one. A schedule's card shows its last run and next due (`last_run`,
`next_due`; a failed start is a chip, and a record of `skipped` reads "skipped", not "never"). It is edited with a segmented Off / Daily / Weekly control, seven day chips
(weekly only) and a 24-hour time; the sweeps are not offered by the New schedule form.

Lint findings (the Upkeep group, and the More page on a phone) lists the maintenance lint's findings, read from `GET
/api/v1/findings` and `GET /api/v1/finding_marks` each time the section is shown and after each
mark (`renderFindings` and `findingCard` in `kitlib/hubsite/web/index.html`). A row gives the check,
the repo, the target and the finding's text, with its age as context. The rows come in three groups,
each headed with its count and left out when empty: New this run (the lint's `new` flag on the
finding: its fingerprint was not in that repo's previous run, so it is false on a repo's first run
and true again for a finding that went away and came back), Still open, and Handled (the marked
ones, muted, each with a Passed on, Dismissed or Done tag). Above the rows, each repo's last run gives its ledger as counts,
and "last run skipped: <reason>, <time>" only when the server names a skip later than that run
("finding_marks/<fingerprint>"). The card of an unmarked finding offers:

- for a check that names a record, a button named for what is done, which runs in the detail pane
  without leaving the section: Set priority (`unpriced`), Unblock (`keyword-hold`, `dead-blocker`,
  `answered-question`), Edit scope (`scope-path-gone`, `body-path-gone`, `file-moved`) or Retire
  (`duplicate`, one per record, `dead-pull`, `landed-still-pulled`) (`FINDING_ACTS`, `findingAct`).
  The panel reads `GET records/{repo}/{id}` for the fields and version, then calls the existing owner
  routes (edit, unblock, delete): Set priority and Edit scope `POST .../edit` with the version read
  (`changes` = `priority`, or `scope` one path per line); Unblock `POST .../unblock` with the
  reason (and `on`, when the record has several blockers); Retire `POST .../delete` with the reason
  and the record's id typed again (the button waits for both). Unblock and Retire take no version, so
  the panel reads the record again first and refuses when its version moved. The ruling id is asked
  for only when the store's refusal names one, and a refusal or conflict shows in the panel in the
  store's words. A done action leaves the finding listed until the next lint run; an "Open <id>"
  button beside it opens the record page;
- Pass on: an act note to the PM with the note field's text (prefilled as the server would write
  it; unchanged, the server writes it), queued in To PM like any note, then the mark `passed_on`. A
  mark that fails after the note was taken is retried by Pass on without a second note;
- Dismiss: the mark `dismissed`.

A target or fix shows as the server gives it; the page never rebuilds a path. In To PM an act note
shows as Act, its edit offers no kind, and the server's refusal shows in its own words.

## Serving it

- `hub.py setup` makes the owner's and the agent's token files where they are absent and prints
  where they are; it never writes the machine config.
- `hub.py serve` serves until Ctrl+C or `hub.py stop`, either of which commits the last changes
  and releases the store lock.
  The port is `hub.port` of the machine config (default 8790); the store is `hub.store`, else
  `hub/` in the PM data worktree (docs/pm-data-branch.md). `--store PATH` serves a scratch copy
  with git sync off. The server needs no `hub` key: without one every default holds. A trial
  run with `--store` therefore never writes the `hub` key; the owner
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
- Stale code. A running `hub serve` compares the `.py` files of `kitlib/hubsite` (name, modified
  time, size; `serve.code_stamp`) with what they were at its start, at most every 30 seconds. When
  they differ it logs one error line to its console and says so in a `code` object, `{"stale",
  "restart"}`, in GET health (the local listener; the tunnel's fixed health carries none) and in
  GET snapshot (`kitlib.hubsite.codestale`, one text for all of them). `restart` is the owner's
  to-do: `python <kit>/tools/hub.py restart`, or, with no autostart task, `hub.py stop` then
  `hub.py serve`. Three places show it, each reading that object: the page's banner under the
  title (`#codeerr`), a `hub: WARN` line in the PM session-start checklist (docs/pm-start.md), and a
  `HUB` event of pm_watch (docs/pm-watch.md), said once per stale episode. Nothing restarts the hub
  by itself. Known limit: only the hub package's own files are compared, so a landing that changes
  only other packages the hub imports (for example `kitlib/pmdata`, `kitlib/config`) does not warn.
  `web/index.html` is not compared either: the server reads it from disk per request
  (`httpd.static_file`), so a page-only change needs no restart. Known limit: a hub started before this field existed gives no `code`, so none of the three warn about
  it; it needs one restart.
- Remote access is only through the tunnel listener: with `hub.access` set (an Access `team`,
  the application's audience tag `aud` and the allowed `emails`; all three), the server also
  listens on `hub.tunnel_port` (default 8791, loopback too) for a Cloudflare tunnel, and every
  request there needs a Cloudflare Access token in the header `Cf-Access-Jwt-Assertion`
  (`kitlib.hubsite.tunnel`). The tool never runs the tunnel program. Both listeners bind the
  loopback address only, and `hub.port` and `hub.tunnel_port` must differ. If `hub.access` is
  incomplete the tunnel stays off and the local listener keeps serving; `status` says why. The
  owner's step-by-step guide to reach the hub from a phone: docs/hub-remote-access.md. A valid
  Access token also makes the caller the strong owner, the only caller a route of role
  `approver` (`kitlib.hubsite.api.APPROVER`) answers; from it the tunnel's
  `/api/v1/pair/strong` makes a one-time link that pairs the PC's browser as the strong owner on
  the local listener, a session kept on disk under a key derived from the owner token and
  `approval.key`, which the server makes beside the tokens at start (docs/hub-remote-access.md
  "The strong owner"). The
  health answer through the tunnel holds fixed words only: `ok`, `version`, `store` (`ok`),
  `git` (`ok`, `failed`, `off` or `unknown`) and `tunnel` (`on` or `bound, keys not fetched`);
  no folder, branch, reason or revision. A health answer the server cannot give leaves as its
  status and error code with the message `health could not be answered`.
- Autostart (opt-in): `python tools/hub.py setup --autostart` registers a scheduled task that
  runs `hub serve` at logon; `--whatif` shows what it would do and changes nothing. Without the
  flag nothing is registered. It is refused, with nothing registered, when no store folder is
  set. The server then runs windowless, so Ctrl+C does not apply: `hub.py restart` stops it
  cleanly and starts the task again, and `hub.py stop` stops it (docs/hub-remote-access.md
  "8. Restart the hub"). A server that was cut off without a clean close leaves its store lock,
  which the next start takes over with a note.
- `hub.py stop [--port N] [--timeout S]` and `hub.py restart [--port N] [--timeout S]`
  (`kitlib.hubsite.control`) post `POST /api/v1/stop` (`kitlib.hubsite.httpd.STOP_ROUTE`) with
  the owner's Bearer token. The route takes that token only, never a browser session (403
  `forbidden` otherwise), and only on the local listener: through the tunnel it answers 404 (403
  `access` first without a valid Access token). It answers `ok`, `stopping`, and the server's
  `pid` and `ctime`, then the server closes as on Ctrl+C. `stop` waits until the port is free and
  that process has ended: `STOPPED: ...`, or `nothing to stop: ...` (exit 0) when nothing answers
  on the port. A server stopped a moment ago may still be closing then: it frees its port before
  its last git pass and the store's close (`kitlib.hubsite.control`). `restart` reads the
  scheduled task's state first and starts the task again only when the task ran the server it
  stopped (the task is `Running` or `Queued`), or when nothing ran and the task is ready; then it
  waits until the new server answers on `hub.port` (`RESTARTED: ...`). A task that cannot be read
  or started after the stop prints the `STOPPED` line, why, and how to start the task (exit 1).
  A `restart` that starts nothing, like a failed start, points at the task (`setup --autostart`,
  `Enable-ScheduledTask`, `Start-ScheduledTask`), never at `hub.py serve`: a server started in a
  console ends with that shell, the task's does not.
  Neither ever kills a process: past `--timeout` (default 300 s, approximate: one request in
  flight may run past it) they print `TIMED OUT: ...` and exit 4.
- `python tools/hub.py setup --check` writes nothing: it says whether `hub.access` is unset, set
  or cannot serve, fetches the Access keys once when it can serve (`access keys: ...`), and lists
  each kit copy on the machine that would refuse the `hub` key (`kitlib.config.copies`). It ends
  `READY: ...` (exit 0) or `NOT READY: ...` (exit 1). Run it before writing the `hub` key
  (docs/hub-remote-access.md "Before you start").
- `hub.py validate [--store PATH]` checks the store folder against the field tables below, with
  no server, and prints `VALIDATE <LEVEL> <document> <field>: <message> [<code>]`.

The machine config's `hub` key: docs/kit-json.md "Machine config keys". Its settings are `port`,
`tunnel_port`, `store` (an absolute path), `commit_every_s`, `push`, `backlog_refresh_s`,
and `access`; an unknown key is refused, except a leftover `relay` (a retired setting), which is ignored.

Exit codes of every verb: 0 ok, 1 refused (also a Ctrl+C other than the one that closes a
running server, or a server that did not close cleanly), 2 usage error or an unreadable machine
config, 3 the server is not running (the start command is printed), or a `restart` that started
nothing (no task, a disabled one, or a server started by hand: it says which and prints the
start command), 4 a `wait --timeout` that ran out, or a `stop` or `restart` that ran out of time
(nothing was killed). A `wait` whose server goes down after one of its polls was answered (a
restart: the port refuses the connection, or the hub answers `REFUSED store`) asks again each
second (`client.RETRY_PAUSE_S`) from the revision it last had, within its `--timeout`, or for up to
600 s down in a row (`client.RESTART_WAIT_S`) when it has none; then it ends with that reason: 3
and the start command, or 1 (`REFUSED store: ...`). A server already down at the wait's first poll
ends it at once the same way. A connection dropped mid-request still ends it with 1
(`REFUSED: port N did not answer as the hub does: ...`) (`kitlib.hubsite.client.Client.wait`).

## For the PM

The PM reads and writes the hub only through `tools/hub.py` or the hub MCP server (docs/hub-mcp.md),
both with the agent's token. A coordinator writes only its own questions: it posts each with
`hub.py questions add --from <its launcher session key>` (the `hub key:` line of its session-start
context, `kitlib.coordinatorstart.hub_key`), reads `hub.py pending --for <its key>` once at start,
runs no background wait (the hub pushes the answer, "Push notifications"), and acts on the answer
itself when it comes. A to-do for its
own work it files with `hub.py todos add --from <its key>`, and acknowledges it (`todos ack`) once
it has acted on it; it hands every other step (notes, stories) to the PM. Who handles each pending
item: "Who handles a pending item" below. For a question, the server takes `--from` only when it is `pm` or a
key of the launcher's sessions.json, read again on each add; any other value is refused, and a
question without `--from` is the PM's. The answer stays pending for everyone, the PM included,
until the PM logs the ruling in the rulings ledger and then on the question (`questions log`), or
marks it done when it needs no ruling (`questions done`), as for any other answer, so each `pending` read lists it again and a plain wait would print it again at once: a caller
that does wait passes `--except` and every id it has acted on (`wait --pending --for
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
| `questions add --title T [--repo R] [--context-file F] [--option "label::description" ...] [--recommended N] [--refs ID ...] [--urgent] [--story S-n] [--record REPO:ID] [--from KEY]` | `ASKED Q-2`, then any warning lines |
| `questions log Q-1 <ruling id> --if-version V` | `LOGGED Q-1 <ruling id>` |
| `questions done Q-1 --why TEXT --if-version V` | `DONE Q-1` |
| `questions file [Q-1]` | `FILED Q-1` and `NOT FILED Q-2: <why> (<code>)` lines, or `nothing to file` ("Filing the answer") |
| `questions edit Q-1 --from KEY --if-version V [--title T] [--context-file F] [--option "label::description" ...] [--recommended N] [--refs ID ...] [--urgent \| --no-urgent] [--record REPO:ID \| --no-record]` | `EDITED Q-1` |
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
| `serve [--port N] [--store PATH]`, `status [--port N]`, `setup`, `open [--port N] [--print]` | see "Serving it" |

Notes on the table:
- `todos` lines carry no version; `todos --json` gives each to-do's `version`.
- `questions add --story` is the story a follow-up belongs to; `stories set`, `questions log`,
  `questions done` and every `edit` and `withdraw` require `--if-version`.
- `hub.py pending` lists the owner input not yet handled: questions answered and not logged or done,
  stories submitted, To PM items sent, to-dos done or added by the owner without `pm_ack`, and
  approvals the owner decided that are not consumed (`kitlib.hubsite.store.pending_items`; an
  approved one until it is consumed or expires 24 hours after the decision, a denied one for 24
  hours after it). Each item carries `from`, the session key it is for, and
  `route`, the key that is to handle it now (the line's `from=` and `to=`). A question or to-do is
  for the launcher session key it was posted `--from` while that key is still in sessions.json;
  everything else (a to-do's free-text `from`, the owner's own items, stories, To PM items, and a
  question whose session has gone) is for `pm` (`kitlib.hubsite.api.Api.pending`). An approval has
  no `from`: it is for the coordinator of its repository, the configured repository whose origin
  remote is the approval's `repo`, keyed by that repository's name while the name is in
  sessions.json, and for `pm` otherwise. `pending --for KEY` lists only KEY's items, and
  `pending --for pm` the items routed to `pm`, every answered question and every decided
  approval; reading it marks nothing, so the item stays pending for every reader until it is
  logged, acknowledged, consumed or past its window.
- `hub.py wait --pending` waits until an item is pending (one already pending ends it at once) and
  prints `HUB <rev> <kind> <id> to=<route> from=<key> <title>` for each; with `--for KEY` it waits
  until an item for KEY is pending and other sessions' items do not end it. `--except ID ...`
  leaves out the items with those ids (ones already acted on, which stay pending until the PM logs
  or acknowledges them), so only another item ends the wait; it marks nothing
  (`kitlib.hubsite.api.wait`). `wait [--since REV]` waits for any change and prints
  `HUB <rev> <collection> <id> <op> <status>`; `--for` or `--except` without `--pending` is a
  usage error. `--timeout S` exits 4 with no line when nothing came. No session keeps a wait
  running: the PM and each coordinator read `pending --for <key>` once at start and the hub
  pushes the rest ("Push notifications"); the verb stays for a caller that wants it. The page
  still shows a PM wait as listening, so with push alone it shows no PM listening. A plain `wait --pending` would
  end at once on an item a creator holds and not yet acknowledged, again and again.

`--if-version V` is the version of the document the PM read and decided on (the `v2` of the
`questions` and `stories` lines); a document that has moved on is refused (`REFUSED conflict`). At
each start, resume and compact the PM session-start checklist (docs/pm-start.md) says whether the
server is up, how many items are pending, and the push and watcher state from `GET /api/v1/health`.

### Who handles a pending item

Each pending item goes to the session that created it while that session can be reached, and to
the PM otherwise, so the PM is the fallback for a creator that is gone (`route`, "Routes" in the
`kitlib/hubsite/api.py` docstring):
- An item for `pm` is the PM's.
- An item for a session key, before it is handed over, is that key's while the key can be reached:
  a push was written to it within `ACK_WINDOW_S` (`Api.pushed`, which the push worker calls for
  each written try to a key other than `pm`, "Push notifications"), the usual case since no
  session keeps a wait open; or a `wait --pending --for <key>` is open now, or one started or
  ended within `REACH_GRACE_S` (the 55-second wait limit plus 30 seconds). Otherwise
  it is the PM's.
- A `pending` or `wait` `--for <key>` that returns one of the key's items hands it over. The item
  is then the key's for `ACK_WINDOW_S` (30 minutes) from the first hand-over, and the PM's after
  that while it is still pending. The creator ends it by acting on it: `todos ack` for a to-do;
  an answered question stays pending until the PM logs it (`questions log`) or, when it needs no
  ruling (a one-off go such as a landing or archive go, which the ledger keeps out), marks it
  done (`questions done <id> --why <text> --if-version V`; status `done`, with `done_why` and
  `done_at`). A done question is no longer pending and
  its push ends (`kitlib/hubsite/api.py` `questions_done`). A question bound to a record whose
  answer the hub filed (status `filed`, "Filing the answer") stays pending, with `filed` true on
  the item, until its asker logs it or marks it done: both take a filed question as they take an
  answered one.
- `--for pm` lists the PM's items and every answered question, whoever asked it, because the PM
  logs each ruling, and every decided approval. A filed question is on it only while it is the
  PM's: the PM asked it, or its asker cannot be reached (the rules above), so an answer filed
  while its asker is gone still reaches someone. A PM wait is also woken when an item turns to the
  PM with no write.
- A decided approval is for its repository's coordinator (`hub.py pending` above) and listed for
  the PM too. It stops being pending with no write: once consumed, at its expiry, or 24 hours
  after a denial.
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
- What fires: a question answered (or answered again; `answered`: not bound to a record, its
  filing failed, or filed with no `filed_as`), a bound question's answer filed into its record
  (`filed`, nothing while the filing is under way: "Filed answers" below), an answer already
  given made a standing rule (`standing`, to the PM: "A standing rule"), a to-do done (or done again), a done to-do
  reopened, a to-do the owner added (to the PM only), a story the owner submitted (`submitted`,
  to the PM only), a note to the PM the owner sent (`sent`, to the PM only), an approval the owner approved or denied
  (`decided`), and an approval still approved and not consumed 20 hours after the decision
  (`expiring`, `push.EXPIRING_S`), the one reminder before it expires at 24 hours, so a missed
  first push cannot lose the owner's approval. Each item has a fingerprint (a question's phase,
  `answer.at`, a digest of the answer and, once it is a standing rule, `standing.at`; a to-do's status and `done_at`; a story's status and
  `submitted_at`; a note's status and `sent_at`; an approval's status,
  `decided_at` and phase, `decided` then `expiring`, read at the worker's clock each round), and
  only a changed fingerprint fires, so `pm_ack` alone never does. An approval's phase ending
  (consumed, expired, a denial's 24 hours past) drops what was still owed and sends nothing.
- Who hears: the item's `from`, unless that is the PM's own (`pm`, the owner, empty), and the
  PM, once; for an approval, the coordinator key its `repo` names ("Who handles a pending item")
  and the PM. A launcher key is resolved through sessions.json; `pm` through coordinators.json's
  `pm_session_id`, then the names `Project Manager` and `pm-session`.
- Filed answers (`filed`): who hears depends on what the filing did to the record, the question's
  `filed_as` ("Filing the answer"):
  - `pulled` (someone is working on the record): its asker alone, until it picks it up; when
    the PM asked, the PM, sent once, for its information (the worker on the record reads it).
  - `freed` (the record was parked or blocked and is now open): the PM alone, sent once, and
    only when no live session can take the record up: the delivery's `resolve` finds no live
    session for the asker, the record's repository or the question's repo. A delivery with no
    `resolve` counts as none live, and a key whose `resolve` raises as not live (logged), so the
    PM is told.
  - `held` (the record still waits on more): nobody. The asker reads it in its pending list, and
    the PM start's digest counts the answers filed since the last start that are still `filed`,
    waiting for their askers (docs/pm-start.md "Filed answers"); a logged or done one drops out.
  The phase a question goes through while it is being filed is skipped: a filed answer is
  judged against the question as it was before the answer (a first answer or a re-answer).
  An answer that is a standing rule also goes to the PM, whatever its `filed_as`, and a
  standing rule made of an answer already pushed adds the PM to the push still owed for it
  (`push.py` `_fire_filed`).
- What it says: a fixed header, `[hub push] <kind> <id> <status> repo:<repo> from:<from>
  route:<recipient> attempt:<k>`, and one line of what to do; never a title, a note or an
  answer, since the session takes the message as a prompt. A to-do's line is `A done mark is not
  proof: verify each step on disk, then: hub todo ack <id>`: the owner marks a to-do done even
  when a step inside it failed, so the id is there to check against the disk. A filed answer's
  line names the record (`<repo>:<id>`) and what to do: pick it up (`pulled`, the asker), read
  it with no ack needed (`pulled` to the PM that asked, and `freed`, which also says to start a
  coordinator of the record's repository), and, for a standing rule, log it in the ledger
  (`hub questions log <id> <ruling-id>`), which acknowledges it.
- Until when: each recipient is sent each item until it is acknowledged. The asking session
  acknowledges by picking the item up (`pending --for` or `wait --pending --for` hands it over,
  "Who handles a pending item"), or by `todos ack`; the PM by `todos ack` for a to-do and by
  `questions log` or `questions done` (no ruling) for a question; for a question the hub filed
  into its record (status `filed`, "Filing the answer") asked by another session, by the filing
  itself, since its asker reads it; for a filed answer's `pulled` note to the PM that asked it
  and its `freed` ping, by the first written try; for a standing rule, only by logging it
  (`questions log`: a standing rule is never marked done, "A standing rule"); for a story, any change of its status (`stories set`), and for a
  note to the PM, its reply (`to-pm reply`). An approval's push is sent once to each: the first written
  try acknowledges it. An item withdrawn, reopened or answered again replaces or
  ends its push. A session that picked up a first answer or done mark before the worker saw it
  is not sent it. A question's phase is `push.question_phase`: an answer to a bound question that
  is neither filed nor failed is `filing` and fires nothing, a filed one with a `filed_as` is
  `filed` (the rules above), and one filed with no `filed_as` (filed before the field existed, or
  its record could not be read) counts as `answered`: it fires one `answered` push, with `filed`
  in its header, and its asker is sent it until it picks it up, logs it or marks it done. An
  answer whose filing failed (`filing` set) is `answered` too, pushed as any answer. One message lists at most 20 items (`push.MAX_ITEMS`) and a `+N more` line;
  the rest wait for the next slot. Retries come 2, 5, 15, 30 and 60 minutes after a try, then hourly, and stop a
  day after the first try with one `gave-up` line (`push.SCHEDULE_S`, `push.GIVE_UP_S`). A write
  whose end is unknown waits for the next slot like any other outcome.
- How it is sent on Claude Code: one message is two JSON lines written once to the live
  session's peer pipe (an auth frame with that session's peer token, then a user frame wrapped as
  a cross-session message in prompting mode), by a hidden helper in a kill-on-close job
  (`hosts/claude/adapter/peer_worker.py`), so a blocked write never blocks the server. The pipe,
  pid and creation time come from the session's registry record at send time
  (docs/launcher.md "Pipe addresses"); only a record whose pid still runs with that creation time
  counts, because `launch.py stop` leaves records behind. The token is read from the key file
  beside the record only when sending, checked against the record's creation time, and never
  logged or stored. The hub publishes no registry record of its own and gets no receipt:
  `written` means the bytes reached the pipe, not that the session read them. Two live sessions
  of one name: the one `pipes.json` names, else the newest, logged `ambiguous`. Whether a desktop
  session wakes on a push is unproven; a session also reads `pending --for <key>` at each start,
  which catches what a push missed.
- Routing: each written try to a session key other than `pm` calls `Api.pushed`, so that key keeps
  its items for `ACK_WINDOW_S` with no wait open ("Who handles a pending item").
- The PM watcher: every `push.WATCHER_POLL_S` (30 s) the worker's thread reads the heartbeat
  pm_watch writes beside its state file (`kitlib.pmwatch.heartbeat_file`; the state file is the
  `pm_watch` config's `state_file`, else `kitlib.pmwatch.default_state_file`). A heartbeat at most
  `push.WATCHER_STALE_S` (300 s) old is `up`, an older one `down`, none readable `none`. When a
  heartbeat this worker saw fresh goes stale or missing, it pushes the PM once `[hub push] watcher
  down age:<seconds|none> route:pm attempt:<k>` and a line to start pm_watch again
  (docs/pm-watch.md); a fresh heartbeat acknowledges it, a written try ends it, and a try not
  written is retried on the same schedule. It fires again only after the heartbeat was fresh
  again, and never on a server that has not seen a fresh one (it is not kept across a restart).
  A pm_watch run with its own `--state` path is not seen.
- Files, in `launcher.state_dir`: `push-outbox.json` (what is still to send, rewritten after each
  round that changed it, so a server ended without a clean close loses at most that round; read
  again at start: an entry whose item changed while the server was down fires afresh, one whose
  item is gone is dropped) and `push-undelivered.jsonl` (one line per try not written, per `ambiguous`
  pick and per give-up, a `watcher down` try as kind `watcher`, id `down`; never a token or any item text; rotated once to `.1` past 1 MiB,
  `push.LOG_MAX`).
- Status: `GET /api/v1/snapshot` carries `push`, null when the server runs no worker, else
  `PushWorker.status()`: `last` (the state of the latest delivery to the PM, null before the
  first), `state` (`down` until the worker's start has finished, once its thread has ended, or
  when `last` is not `written`; else `up`) and `undelivered` (outbox items some recipient has not
  yet had a written try of). Only the PM's result sets `last`: an asking session that has ended
  (`no-live-session`, routine) counts in `undelivered` but never makes delivery `down`. `last`
  and `state` change only when a delivery to the PM is tried, so with an empty outbox the line
  shows the last state indefinitely, and an item given up appears only in
  `push-undelivered.jsonl`. The page shows it under the PM line ("Using the hub"). It is read
  again with each snapshot, so a retry's outcome shows at the next change or wait timeout, not
  at once. The snapshot and `GET /api/v1/health` also carry `watcher`: null with no worker, else
  `PushWorker.watcher_status()`, `{"state": "up"|"down"|"none", "age": <seconds or null>}` at the
  worker's latest read (a status that raises is `down` with a null age). The health answer
  through the tunnel leaves it out ("Serving it"). `GET /api/v1/health` also carries `push`, the
  snapshot's value from `Api.push_seen`: null with no worker, else `{"state", "last", "undelivered"}`
  (`state` `down` once the worker thread has ended or its status raises).
- Stopping: `Hub.close` tells the worker to begin no further send before the store closes and
  joins it after, for at least `push.STOP_S` (15 s), longer than a host's delivery can take
  (Claude: `peer.WRITE_TIMEOUT_S` plus the helper's reap and pump joins), so the last outbox
  write is not cut off.
- Known limit: an answer made in the moments before the server closes that the worker had not
  yet seen is not pushed after a restart: the outbox holds only what the worker saw. The PM's
  `pending --for pm` at its next start still shows it.
- Known limit: an approval's 20-hour reminder (`expiring`) is not sent when the server is down
  across the 20-hour mark after its `decided` push was delivered. The approval stays pending
  for its coordinator and the PM ("Who handles a pending item") until it is consumed or expires.
- Known limit: a `freed` answer's live check takes the asker, the record's repository and the
  question's repo as session keys as they are, not through the repository-to-key map an
  approval's push uses (`Api.repo_key`). A coordinator whose key is none of them is not found,
  so the PM is told when it need not be: an extra ping, never a lost one.

Tests: tests/hubsite/test_push.py (filed answers: `FiledRecipientsTest`, `StandingPushTest`),
tests/host/test_claude_peer.py, tests/launcher/test_pipes.py,
tests/hubsite/test_hub_cli.py `PushCase`, tests/hubsite/test_pending_approvals.py.

## questions/Q-NNN
| Field | Written by | Meaning |
|---|---|---|
| status | PM, owner, hub | `open`, then `answered` (owner), then `logged` (PM), or `done` (PM: no ruling needed, `questions done`), or `filed` (hub: the answer was filed into the bound record, "Filing the answer"), then `logged` or `done` (its asker) |
| repo, from, asked_at | PM, coordinator | where it came from; `from` is `pm` or the asking coordinator's launcher session key, the session its answer goes back to; asked_at is ISO time |
| title | PM | the question itself |
| context | PM | everything needed to answer; nothing is assumed from chat |
| options | PM | `[{label, description}]`, recommendation first |
| recommended | PM | index into options, normally 0 |
| urgent | PM | true sorts it to the top with an Urgent marker; only for items that block work or have a deadline |
| refs, story | PM | ids it refers to; the story a follow-up question belongs to |
| answer | owner | `{choice: index or "other", label, note, at}` |
| ruling | PM | the ruling id it was logged as |
| done_why, done_at | PM | why the answer needed no ruling, and when it was marked done (ISO time) |
| record | PM, coordinator | `{repo, id}`: the backlog record the question is about; absent or null on an unbound question (every question asked before the field existed reads so) |
| filing | hub | `{at, code, message}`: why the last filing of the answer failed; null once filed or answered again |
| filed_at | hub | when the answer was filed into the record (ISO time); null after a re-answer |
| filed_as | hub | what the filing did to the record: `pulled`, `freed`, `held`, or null when the record's state could not be read; null after a re-answer ("Filing the answer") |
| standing | owner | `{at, via}`: the owner made the answer a standing rule, for the PM to log ("A standing rule"); null after a re-answer without the box |

### A question bound to a record
`record` is given when the question is asked (`questions add --record REPO:ID`, MCP `hub_ask`
`record`) or by an edit while it is open (`questions edit --record REPO:ID`, `--no-record` or a null
`record` to unbind it). The hub reads the record then, through the record view the record page
uses (`kitlib.hubsite.backlog` `Views.record`: the configured repositories only, read now with no
fetch), and stores `repo` as the configured name. The kit's own records are repo `pm`. Each of
these is refused, field `record`, and nothing is written (`kitlib.hubsite.api` `_bound`):
- `invalid`: a `record` that is not exactly `{repo, id}` with two non-blank strings, a name that
  can be no repository or record id, or a record that is completed (closed, retired or taken out
  of the store);
- `not_found`: a repo the hub has not configured, or an id that repo's backlog does not have;
- `unavailable`: a record the hub cannot check (a server with no backlog views, no checkout of the
  repo on this machine, a store that cannot be read). It fails closed: an answer is meant to be
  filed into its record, so a binding nobody checked is never stored. Ask again, or ask unbound.

An edit checks its asker and its status first, so a refused edit reads no backlog. Tests:
tests/hubsite/test_api.py `QuestionRecordTest`, tests/hubsite/test_hub_cli.py
`RecordArgumentCase`.

#### Filing the answer
The hub files the owner's answer to a bound question into the record itself, in this order
(`kitlib.hubsite.api` `questions_answer`, `file_answer`):
1. The answer is stored as for any question: status `answered`, `answer`. A re-answer also clears
   `filing`, `filed_at` and `filed_as`, and a `filed` question takes a new answer like an answered
   one.
2. The record is written through the backlog library (`kitlib.hubsite.recordwrite` `answer`, the
   store's `plan_answer`, with the role check made: the hub files only the answer the owner gave,
   whoever's call starts it): an `answer: <qid> <at>` line, a `## Owner answer (Action Hub, <at>)`
   body section, and, when no other question bound to the record is still open, the record's
   park or owner blocker ends. The store's compare-and-swap plans the write again after a lost
   race. The same question and `at` again changes nothing, and an older `at` never overwrites a
   newer answer, so this step can be repeated after a crash.
3. The question is marked `filed`, with `filed_at` and `filed_as`, pinned to the version of
   step 1. `filed_as` is what the answer did to the record, from its `state` read through the
   record view just before step 2 and again after it (`kitlib.hubsite.api` `_record_state`,
   `_filed_as`): `pulled` when it was pulled before (someone is working on it), `freed` when it
   was parked or blocked and is now open (a free-text Other answer that ends the wait frees it
   too), `held` otherwise (it still waits on more, or it was open), and null when a read that
   decides it cannot tell (no record view, a view that raised, no fields, a completed record). The push reads it to choose who hears ("Push notifications"). A filed
   question stays pending for its asker, with `filed` true, until the asker logs it (`questions
   log`, where a ruling is bound) or marks it done (`questions done`), so an asker busy
   when the owner answers still finds the answer in the push and on its next `pending --for KEY`. It is the
   PM's only when the PM asked it or its asker cannot be reached ("Who handles a pending item").
   The page shows a filed card as `filed in <record id> · waiting for <asker> to read it` (the PM
   for its own question) and keeps its Change answer button: a re-answer files again.
   A delegation run line answered on a bound question waits for the asker's log step too: a
   filed question is refused until it is logged (`kitlib.delegations.run` `hub_order`).

When step 2 fails the question stays `answered`, so it stays pending for the PM, and gains
`filing` `{at, code, message}`: `not_found` (the record is completed or gone) and `invalid` (the
store refused the answer) leave the answer to the PM, who handles it as an unbound one, and so
does `no_store` (the repository's `.kit.json` names no `"backlog".store`, so the hub may write
none: `kitlib.hubsite.recordwrite` `answer` refuses the implicit store with that code); `store`
and `unavailable` are tried again. `message` is a fixed phrase (`FILING_SAYS`), never the store's
text, which can name a path; the local owner's answer route alone carries that text, in its
`filing`. A retry that fails the same way (the code and phrase `filing` already has) writes
nothing and keeps the first `at`, so it moves no version an asker's `--if-version` holds. A mark
(step 3, or `filing`) whose pin no longer holds writes nothing: the question was answered again,
logged or marked done meanwhile, and the call that moved it does its own filing.

Every answered, bound question not filed yet is filed again by `hub.py questions file [Q-1]`
(`POST /api/v1/questions/file`, agent, optional `{id}`; it answers `{filed: [ids], failed: [{id,
code, message}]}`) and once at each hub start, on a thread after the listeners start (`kitlib.
hubsite.serve` `Hub._refile`, which logs each failure as a note). The sweep with no id, and so
the hub start, skips a question whose filing failed `no_store` (`FILING_TERMINAL`); `questions
file <id>` still tries it, which files it once the repository names a store. A crash after step 1
or after step 2 is mended there. The answer route's own answer carries `filing` (`{id, filed, version,
revision}`, `{id, filed: false, code, message}`, or `{id, filed: false, moved: true}`), and its
`status`, `version` and `revision` are those of the last write. An unbound question is never
filed: the PM logs it as before. Tests: tests/hubsite/test_api.py `FilingTest`,
`AnswerStoreTest`, `AnswerNoStoreTest`, `FiledAsTest`; tests/hubsite/test_record_write.py
`Answer`; tests/hubsite/test_push.py `AckTest`; tests/delegations/test_run.py `HubTrigger`.

Known limits:
- A filed question has its own Filed tab on the page (no attention chip), and `hub.py pending` and
  `wait --pending` mark it ` (filed)` after the title.
- After the store reports an Uncommitted error the line is already in the record, so the retry
  changes nothing and marks the question `filed` before the store's catch-up applies it (the
  answer audit of a later slice allows for this).

#### A standing rule
An answer to a bound question goes to its asker, so the PM would not log it as a ruling. When the
owner wants the answer to hold beyond this record, so the same question is not asked again, the
owner makes it a standing rule, which the PM logs in the ledger (`questions log`, docs/ledger.md).
- On the answer: `POST /api/v1/questions/{id}/answer` with `standing` true beside `choice` sets
  `standing` `{at, via}` (the answer's own). A re-answer without it clears `standing`. It is
  refused `invalid`, field `standing`, on a question bound to no record: the PM logs every such
  answer already.
- Afterwards (promote): the same route with `standing` true and no `choice` keeps the answer as it
  is and files nothing again; it sets `standing` `{at, via}` at the time of the promote
  (`kitlib.hubsite.api` `_promote`). It takes an optional `if_version`; a `note` is refused `invalid`, field
  `note` (a note comes with a choice). It is refused `invalid`, field `status`, unless the
  question is `answered` or `filed` (an answer whose filing failed can be promoted), field
  `record` for a question bound to no record, and field `standing` for one already standing.
  There is no separate route.
- Who reads it: a filed standing rule stays pending for its asker and is also pending for the PM
  (`pending --for pm`, with `standing` true), and the push tells the PM to log it until it is
  logged ("Push notifications"). `questions done` is refused `invalid`, field `standing`: the
  asker's routine done would end the PM's push with nothing logged.
- The page: a bound question's card has a "Make this a standing rule" box beside the options,
  ticked again on a re-answer of a standing one; it sends `standing` true only when ticked. A
  filed card that is not standing has a "Make it a standing rule" button (the promote, with the
  version it shows), and a standing card says `a standing rule, for the PM to log`.

Tests: tests/hubsite/test_api.py `StandingRuleTest`; tests/hubsite/test_push.py
`StandingPushTest`; tests/hubsite/test_page.py `DomTest`
`test_the_answer_sends_standing_only_when_a_bound_questions_box_is_ticked`,
`test_only_a_filed_bound_answer_not_standing_yet_can_be_promoted`,
`test_a_bound_question_offers_the_standing_box_and_sends_it_ticked`,
`test_a_filed_answer_is_promoted_by_its_button_and_a_standing_one_says_so`.

Known limits:
- Until the PM logs it, a filed standing rule stays in its asker's pending list too: the asker
  cannot mark it done.
- The page offers the promote only on a filed card: an answer whose filing failed is promoted
  through the route, or by answering again with the box ticked.

While a question is open its asker may change it (`POST /api/v1/questions/{id}/edit`, agent only:
`hub.py questions edit`, the MCP tool `hub_question_edit`) or withdraw it (`POST
/api/v1/questions/{id}/delete`: `questions withdraw`, `hub_question_withdraw`), which removes the
document. Both take `from` and `if_version`, both required, and are refused `forbidden` unless
`from` is the question's own (`pm` for a question with none), and `conflict` once it is answered or
logged (`kitlib.hubsite.api` `questions_edit`, `questions_delete`). An edit's fields replace the old
ones; `options` replaces the whole list. The owner withdraws an open question with the card's
Withdraw button (the same delete route, owner token, version optional) and never edits one. All
agents share one token, so `from` is a claim checked against the item: the check stops honest
mistakes, not a hostile agent, and any agent claiming `pm` can change the PM's items. An answer
sent without `--if-version` (`hub.py answer`) after the asker changed the options is taken against
the options as they are now; the page always sends the version it shows.

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
The page shows them under Info, Lock notices (`#notices`), from the snapshot's `views.notices`:
newest first, the latest 100, read only. The tab's count is the runs with a start notice and no
end notice yet, marked while any is open.

## to_pm/P-NNN
The owner's questions and FYIs to the PM. Inbox is PM -> owner and To PM is owner -> PM; replies
to To PM items do not go to inbox/.

| Field | Written by | Meaning |
|---|---|---|
| (doc id) | server | `P-001`, `P-002`, ...: the document's id, not a field |
| kind | owner | `question`, `fyi` or `act` (pass a maintenance finding on: below) |
| repo | owner | a repo name, `pm`, `all`, or empty |
| urgent | owner | true marks it Urgent and makes the badge hot |
| text | owner | the message, as written |
| refs, finding | owner | ids the note names; `finding`, an `act` note's only, is the finding's fingerprint |
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
a UTF-8 JSON file. The server refuses, as `invalid` naming the field: a bad step (`steps`), a
`commands` entry holding a lone surrogate (U+D800 to U+DFFF, `commands`), and, on an agent's to-do,
text the owner's to-do rules forbid (`todosteps.check`: plain Notepad, a hand edit described in
prose, coordinator work). The owner's own to-dos are not judged by those rules. A raw worktree,
branch, tag or ref delete has no carve-out: its refusal names `worktree_remove.py <slug> --abandon
"<why>"`, which the coordinator runs on a ruling (docs/worktree-remove.md "Tools"); a ref delete's
also says a ref outside `refs/heads`, `refs/tags`, `refs/stash` and `refs/backlog` the
coordinator deletes itself, in its own repository (docs/guard.md "Rules", `git-shared-ref`).

While a to-do is open its creator may change it (`POST /api/v1/todos/{id}/edit` with the agent's
token: `hub.py todos edit`, the MCP tool `hub_todo_edit`) or withdraw it (`POST
/api/v1/todos/{id}/delete`: `todos withdraw`, `hub_todo_withdraw`), with the same `from` and
`if_version` rules and refusals as a question ("questions/Q-NNN" above). An agent's edit takes no
`steps`, its changed text is judged by the to-do rules above, and it clears `pm_ack`. `todos edit`
replaces `commands` only with the `--command` values it is given, so it cannot clear them. Only
the owner's own to-do can have its commands cleared (an empty list to the route); an agent's open
to-do needs at least one, and the hub refuses an agent's edit that empties them (`invalid`:
`tests/hubsite/test_api.py` `test_an_agent_to_do_edit_is_judged_by_the_to_do_rules`). The owner
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

Follow-up questions are ordinary questions with `story` set; the page links them both ways. The
page has no story form: the owner writes new work on the record page instead ("The record page"
below), and `stories add` still files a story.

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

## schedules/<task>-<repo>
When one of the kit's automatic tasks runs for one repo, set by the owner. The hub only stores
and serves it: nothing in the hub runs a task, registers one or starts a process.

| Field | Meaning |
|---|---|
| task | `maint-lint`, `worktree-sweep` or `agent-temp-sweep` (`kitlib.hubsite.api.SCHEDULE_TASKS`, a fixed table) |
| repo | a repo the hub knows (a repos/ name or alias, or the PM's configured repos), stored as its name |
| cadence | `off` (kept, runs nothing), `daily` or `weekly` |
| days | weekly only, required: one or more of `Mon` to `Sun`, stored once each in week order |
| at | the time of day as HH:MM, 24-hour |
| set_at, via | server: when and by which path it was last set |

`POST /api/v1/schedules` (owner) takes {task, repo, cadence, at, days?, if_version?} and puts the
whole document; an agent is refused `forbidden`. Any other field, or a bad value, is `invalid`
naming the field, with nothing written. Setting an existing schedule again takes its
`if_version`; without it, or with another, it is `conflict`, whose error names the schedule's
`id`: the page opens that one, with what was picked on the new form carried onto it unsaved
(unless it holds unsaved changes of its own), so one Save sets it. A daily or weekly per-repo task
whose repo is not exactly one checkout in the machine config's `launcher.repos` is `invalid`
naming `repo`, since the tick could not run it; so is one set while the machine config's
launcher list cannot be read. The answer carries `first_run`, the first slot
as YYYY-MM-DDTHH:MM in the hub machine's local time with no zone (null when off); the task
starts at the first tick at or after it, up to about 15 minutes later. `tick.last` is UTC with
its offset; the page shows `first_run` as it is and says it is the hub machine's time. `GET /api/v1/schedules` (anyone) lists them,
filtered by `repo` like every list, with `tick`: {last, running, every_s}: when the tick was last
seen, whether that is recent enough to call it running, and its period in seconds
(`kitlib.hubsite.api` "Schedules"). Each listed schedule also carries three derived fields,
never stored: `last_outcome`, the tick's record for it (`started`, `skipped`, `seen` or `failed`,
from the tick's state file through `Api.tick_state`, default `tick_run_state`; null for none);
`last_run`, that record's time when it is `started` (null otherwise, so a `failed` record is no
run); and `next_due`, the first slot after now by `kitlib.autotasks.due`'s rule, in the hub
machine's local time like `first_run` (null when off).

## finding_marks/<fingerprint>
The owner's mark on one maintenance-lint finding (docs/maint-lint.md), so it stops showing:
{status, repo, check, target, text, note?, marked_at, via}. status is `dismissed`, `passed_on`
or `done` (`kitlib.hubvalidate.STATUSES`); repo, check, target and text are copied from the
finding when it is marked. `GET /api/v1/findings` (anyone) lists the lint's last findings, every
repo (`kitlib.hubsite.derived.findings` over `<pm_data>/maint-lint/<repo>/result.json`),
filtered by `repo`, less every marked one, with `runs`: each repo's last run as {repo, started,
ledger, skipped}, ledger its whole-number counts `stale_cites`, `superseded` and
`superseded_live` or null, skipped the skip marker's {reason, at} only when `at` is later than
`started` and both parse, else null (`_lint_runs`; from the server's `lint_runs` view,
`kitlib.hubsite.derived.lint_runs`, always there: an empty list when there is no lint folder).
`POST /api/v1/finding_marks` (owner) takes
{fingerprint, status, note?, if_version?}; a fingerprint the lint does not show is `not_found`,
and marking again takes the mark's `if_version`, as for a schedule. `POST
/api/v1/finding_marks/{fingerprint}/delete` (owner) takes the mark back; `GET
/api/v1/finding_marks` (anyone) lists the marks. `POST /api/v1/to_pm` with kind `act` and
`finding` passes one on: repo, refs (the fingerprint and the finding's target first) and, when
none is given, text are filled from the finding; an edit never changes a kind to or from `act`.
Only the local owner sees a target or fix whole: any other caller, the tunnel included, gets
each absolute path cut to `.../<its last part>`, and no act note's refs carry one. An agent is
refused `forbidden` on each write (`kitlib.hubsite.api` "Findings").

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
`conflict`. One lock in the hub process (`PAGES_LOCK` in `kitlib/hubsite/store.py`, which
`api.py` takes) is held from a publish's reads to its last write, and over a delete, so of two publishers with the same pin the
second reads only after the first has written body and card, and is `conflict` with nothing
written. The body is put first and the card second, so a card never names a missing body
(`test_the_body_is_written_before_the_card` and
`test_two_publishers_with_one_pin_the_second_writes_nothing` in `tests/hubsite/test_api.py`).
Deleting is the owner's alone: the Docs tab's Delete, with no CLI verb and no MCP tool. It
deletes the card, then the body; when the body's delete fails the card stays deleted and the body
is an orphan, which `kitlib.hubvalidate` warns of and the next publish of that id writes over
(`test_a_body_delete_that_fails_leaves_the_card_deleted_and_an_orphan_body`).

What a body may hold (`kitlib/hubsite/pagecheck.py`, its docstring has the full list): at most
512 KiB of UTF-8 (`MAX_BYTES`); no `script`, `iframe`, `object`, `embed`, `form`, `link`,
`base` or other element of `TAGS`; a `meta` only as exactly `<meta charset="utf-8">` or
`<meta name content>` with a name of `META_NAMES` (viewport, color-scheme, description, author,
generator), each attribute once; a `<title>` only as `<title>text</title>`, with no attribute and
no `<` in its text (`TITLE_SAYS`); no `on*` attribute; no `href` that is not a `#fragment` (a
`data:` image on an svg `image` is the one exception); `src` and CSS `url()` only as `data:`; no
`@import`, CSS backslash escape or function of `CSS_FUNCTIONS` (`image-set()`, `image()`,
`src()`). Markup that the check's parser and a browser
could read differently is refused too: `<` or `&` in a style element's text, a self-closing
`<style/>`, and the words of `RAW` anywhere in the text: `<textarea`, `<xmp`,
`<noembed`, `<noframes`, `<plaintext`, and the comment and CDATA endings the two may place
differently (`DIFFERENTIAL` and `RAW_ONLY` in
`tests/hubsite/test_pagecheck.py`). A whole document (doctype, `html`, `head`, `body`) passes
(`FULL_DOCUMENT` there). A body that breaks a rule is refused whole, never stripped
(`invalid`, field `html`, nothing written). Mermaid is not rendered by the kit: a body with `class="mermaid"` or a
` ```mermaid ` fence is refused with "render it to svg first", and the publisher draws the
diagram as inline `<svg>`. The listener refuses any request body over 1 MiB (`MAX_BODY` in
`kitlib/hubsite/httpd.py`) with 413 before the check runs, so a body near the 512 KiB cap whose
JSON encoding about doubles it (mostly quotes, backslashes or escaped non-ASCII) is refused there
(`test_a_body_the_check_lets_through_can_still_be_over_the_listeners_limit` in
`tests/hubsite/test_page.py`: 512 KiB of `"` is 1048618 bytes as JSON).

`GET /api/v1/snapshot` carries the cards and leaves every body out
(`test_snapshot_carries_the_cards_but_not_the_bodies` in `tests/hubsite/test_api.py`). Every card
has a live Read full screen: the reader asks `GET /api/v1/pages/<id>` when it opens and says "This
document has no body on the hub server." on a 404 (`DocsTest` in `tests/hubsite/test_page.py`).

The body is agent-written html. The page draws it nowhere but in the reader's one frame, made in
script with a `sandbox` attribute that grants nothing (no scripts, forms, same-origin, popups or
navigation of the hub page) and given the body through `srcdoc` behind a meta policy,
`default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'`
(`DOC_CSP` in `kitlib/hubsite/web/index.html`; `DocsTest` and
`PageTextTest.test_the_reader_is_one_locked_frame` in `tests/hubsite/test_page.py`). The policy
blocks what the body loads by itself: images other than `data:`, CSS files, fonts, scripts, frames
and form posts. A diagram therefore shows only as an inline `<svg>` or a `data:` image. Links are
not blocked. A link clicked inside the reader moves the frame itself to that address, and the
policy no longer applies there; the sandbox still blocks scripts and forms on that page. Some
browsers may also look up the host names of links in advance (DNS prefetch). The page check
refuses such a link, `<link>` and every `<meta>` but the ones above at publish, along with any markup where a browser
could build one the check's parser never saw (the paragraph above), so a body published through
the check has none. A body placed in the store by hand is not checked.
A whole document goes in unchanged, the policy still first
(`test_a_whole_document_is_framed_with_the_policy_still_first`). That a browser still applies it
there is inferred from the HTML spec, not tested: an iframe srcdoc document is always no-quirks,
and the policy meta opens the head, so the body's later doctype and `<head>` are ignored. The
owner checks it in a real browser.

Esc closes the reader only while the focus is outside the frame: keys typed inside the sandboxed
document never reach the hub page. The Close button always works and gives the focus back to the
button that opened the reader. An open reader keeps the body it was opened with until it is closed,
even when a later read of the snapshot changes or removes the page; a body that arrives after its
reader was closed is dropped.

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

An approval of kind `op` approves one run of a delegated operation (docs/delegations.md "Hub
approvals"), not a change. It has no `head`, `files` or `diff`, and holds instead:

| Field | Written by | Meaning |
|---|---|---|
| kind | agent | `op`; a document with no kind is a change |
| op, repo | agent | the operation's id `D-nnn` and its repo name, or `*` |
| line | agent | the run line `run D-nnn NAME=VALUE ...`, names sorted, each once (`approvals.new_op_doc`) |
| what | agent | the operation's `what`, shown above the line as the agent's note, not signed; optional |
| args_sha256 | server | the SHA-256 of `line`, computed by the server (`approvals.line_sha256`) |
| consumed_at | server | once the runner consumes it; an op approval has no `commit` |

Its signature covers the id, kind, op, repo, `args_sha256`, the decision and `decided_at`; the line
is bound through `args_sha256`. A pending op request expires after 24 hours
(`approvals.PENDING_OP_TTL_S`). Its routes:

- POST `/approvals` (agent): body `kind` `op`, `op`, `repo`, `line`, `title`, optional `reason`
  and `what`. No checkout is read. Any other `kind` is `invalid`, field `kind`.
- POST `/approvals/{id}/consume` (agent): body `args_sha256` (in place of `commit`) and
  `if_version`. Both `commit` and `args_sha256` is `invalid`.
- GET `/approvals/{id}/verify` (agent): query `kind=op`, `op`, `repo` and `args` (the line's
  SHA-256); answers `{ok, valid, why, version}`. The version pins the consume that follows. Only
  `approved` verifies: a consumed op approval was used once.

The change checks (`check`, `consume_fields`) refuse an op approval, and the op checks (`check_op`, `consume_op_fields`) a change approval. The
page draws an op card with the operation, its `what` labelled as the agent's unsigned note, and its
exact line, `run D-nnn` and then one `NAME=VALUE` a line; no head or diff. The signature does not
cover `what`, `title` or `reason`: what the owner approves is the operation id, the repo and the line.
`delegated.py request` asks for one, and `delegated.py run --approval` verifies and consumes it.

The agent's side of an approval is `commit.py --owner-approval hub:A-<n>` (docs/commit.md "Owner
approval"): it verifies, writes and commits the approved files, then consumes the approval for that
commit and verifies it with the commit; `gate land` and the pre-push hook verify it again
(`kitlib.guard.ownercommits`). An approval verifies for 24 hours after the decision, consumed or
not (`approvals.APPROVED_TTL_S`).

A decided approval that is not consumed is pending owner input ("Who handles a pending item"):
for the coordinator of its repository and for the PM, until it is consumed or expires, and a
denied one for 24 hours after the decision. The server pushes it to both at the decision and, if
it is still approved and not consumed, once more 20 hours after it ("Push notifications").

An agent asks for one with `hub.py approvals add --title T --file PATH CONTENT_FILE [--file ...]
[--checkout DIR] [--repo R] [--head SHA] [--reason TEXT]`, or the MCP tool `hub_approval_request`
(docs/hub-mcp.md); both build the body with `kitlib.hubapprove` and post it to POST `/approvals`,
and neither decides or consumes a request. Each PATH (relative to the repository top, with `/`)
gets CONTENT_FILE's exact bytes as its content, read as UTF-8 with its line endings kept, so the
blob is the one `commit.py --owner-approval` writes; the work tree is never read for it, since the
consumer needs the file still at its base. The base is PATH's blob at `--head` (default: the
checkout's HEAD), none for a file absent there; `--repo` defaults to the checkout's origin.
Refused before anything is posted, in the builder's or `approvals.new_doc`'s words: a checkout that
is not a git work tree, a repository the checkout's origin cannot name (no `--repo`), a checkout with
no HEAD commit (no `--head`), a head that is not 40 hex digits or not a commit of the checkout, no
file, a path that is not a plain path of the repository, a path an owner approval may not carry (a
render source in a kit checkout, or a path the repository's owner rules do not lock, as `commit.py`
reads them: `hubapprove.owner_paths_error`, whose refusal says where each named file is committed
instead), a kit configuration of the checkout that is refused, a path at head that is not a file, a
new file the work tree already has, a file at head whose work tree copy git does not hash to its
base (the consumer's prewrite check refuses both), a content file that cannot be read, is not UTF-8
or is over `approvals.MAX_CONTENT_BYTES`, content that is the file at head, content git would store
as another blob (`git hash-object --path=PATH --stdin` differs from its blob: core.autocrlf or a
text, eol or filter attribute changes it on staging, which the consumer's staged check refuses),
and what `new_doc` refuses (its `MAX_FILES`, `MAX_PATH`, `MAX_TITLE` and `MAX_REASON` included).
What the hub refuses comes back
as the hub said it (`REFUSED <code>: <message>`).

Known limits:

- `approvals add` (and `hub_approval_request`) cannot request a file delete: every file it names
  gets content. A delete is asked for by a direct POST `/approvals` with a null `content`.
- A request has no `from`: the document has no field for the session key that asked, and adding
  one is a change to the document's schema, so neither the verb nor the tool takes `--from`.

`GET /api/v1/snapshot` carries the collection whole, and `strong`: true only on the strong owner's
call (`test_the_snapshot_says_strong_to_the_strong_owner_alone` in `tests/hubsite/test_api.py`).
The page draws each pending request above the open questions in Inbox (`approvalCard` in
`kitlib/hubsite/web/index.html`), counted in the Inbox and Questions badges: its title, repo, head,
reason and the stored diff as text. Approve and Deny post the decide route with the version the page
read; they are disabled unless the snapshot says `strong`, and a refusal (a 403 included) is shown
on the card in the server's words (`DomTest` in `tests/hubsite/test_page.py`).

## Machine lock routes
The hub server reads and changes the machine lock in-process through `kitlib.lock.api` (the
owner's decision), for the schedule page. `kitlib/hubsite/api.py` docstring "Machine lock" is
the source; the server's door to the lock is `kitlib/hubsite/lockdoor.py`, made by
`serve.machine_sources` and first opening the lock on its first call.
- Three reads, all the owner's: `GET /api/v1/lock/view`, `GET /api/v1/lock/stats` and
  `GET /api/v1/lock/wait`. An agent token, or no token, is refused every one with 403
  `forbidden` (field `role`) on the local and the tunnel listener alike, before the lock is
  read. The lock MCP server keeps no write path, by the owner's decision.
- `lock/view` answers `{lock_revision, view, writer, writes}`: the schedule window's view as JSON
  (`kitlib.lockui.model.to_json`): holds, runs, the strip, lanes, details, cores, memory,
  warnings, the game slots and the CPU division. `queue_items` is the line behind the runs (id,
  position, repo, slug, kind, what it needs, estimated start, why it waits) and each finished run in
  the strip carries its `slug`. One allow-list of keys serves both
  listeners: no pid, process tree, log path, session, store folder or run note is in it.
- `lock/stats?days=N&since=T&until=T` is `kitlib.lock.api.stats`: `days` 1 to 366, the times ISO
  with their offset; a bad one is 400 `invalid` naming the field.
- `lock/wait?since=REV&timeout=S` answers the view plus `changed` once the lock's revision
  differs from `since` (default: the revision now), or after `timeout` (at most 55 seconds) with
  `changed: false`. It looks at the revision every half second. It takes one of the listener's
  wait slots like `GET wait`: past `max_waits` it is 429 `busy` with a Retry-After. An agent
  takes no slot: it gets the 403.
- Through the tunnel the view is the same, except that a warning that could name a file on
  this machine (an unreadable `queue.json`, holds file or kinds file, a layout problem) is a
  fixed phrase (`model.REMOTE_SAYS`). Only the tunnel listener's own `via` asks for that; a
  local client that sends the tunnel's word gets the local view.
- With no lock door, or a lock that cannot be read, a route answers 503 `unavailable` (field
  `lock`), naming only the error's class, never its text.
- `writer` is `owner` when the lock takes the hub's own process for the owner, else `agent` (a
  hub started under an agent session). `writes` is `{lock, strong, why}` for this caller: `lock`
  true when it may make the `lock-write` writes, `strong` true when it may make every write, and
  `why` the reason it may not, for the page to show.

Eight writes, by the owner's decision. Each answers `{ok, result}` plus the view after the write
(`lock_revision`, `view`, `writer`, `writes`):

| Route (POST `/api/v1/...`) | Body | Role |
|---|---|---|
| `lock/holds` | `{kind: reserve, scope, start, end, note}` or `{kind: hold, scope, start, end, note, cpu, gpu}` | approver |
| `lock/holds/{id}/edit` | `{start, end, note}`, each optional | approver |
| `lock/holds/{id}/end` | `{}` | approver |
| `lock/division` | `{fields}` or `{defaults: true}` | approver |
| `lock/runs/{id}/end` | `{confirm}`, which repeats the run id | lock-write |
| `lock/runs/{id}/top` | `{}` | lock-write |
| `lock/game_mode` | `{enabled}` | lock-write |
| `lock/games` | `{text}`, the Games dialog's lines | lock-write |

- `lock-write` (`kitlib.hubsite.api.LOCK_WRITE`) is the owner on the PC's listener, and only the
  strong owner through the tunnel. `approver` is the strong owner on either listener (a browser
  paired as the strong owner, or the Access login; docs/hub-remote-access.md "The strong owner").
  An agent may make no write. Warning: the local listener takes the owner token for a lock
  writer, so a future relay that forwards remote requests to the local listener must close that
  path.
- The page makes owner holds only; it may edit or end any hold. A reserve's `start` and `end`
  are ISO times with their offset. A hold's and an edit's typed times (`HH:MM`,
  `YYYY-MM-DD HH:MM`, `+2h`) are read in the PC's local zone, whatever zone the browser is in
  (`kitlib.lockui.actions`); an edit's missing note keeps the hold's own.
- The lock records the writer as `hub`, or `hub-tunnel` for the tunnel's calls. A hub whose
  `writer` is `agent` refuses every write with 403 `forbidden` (field `lock`) and the reason,
  before the lock is asked.
- The hub's writes run one at a time. The lock's refusals are the hub's codes: `invalid` 400
  (the lock's field), `forbidden` 403 (field `lock`), `not_found` 404 (field `id`), `conflict` and
  the lock's `busy` 409 (busy says to try again), a store failure 503 `unavailable`. Through the
  tunnel a store failure, and any message or note that names a folder, is a fixed phrase
  (`lockdoor.REMOTE_SAYS`).

## Views read live
Never stored; read from their sources (`kitlib.hubsite.derived`, `kitlib.hubsite.backlog`):
- backlog and completed: each repository's backlog store (`.kit.json` backlog.store), every open
  record and every record closed in the last 90 days, fetched every `hub.backlog_refresh_s`.
  Parked is the backlog items whose state is parked. A repository's folder is
  `pm_start.repo_paths[name]`, else the PM repository's sibling. While that timer runs,
  `GET /api/v1/snapshot` (the page's load) answers these two from what the timer's reads left,
  however old (one round per `backlog_refresh_s`; inferred: older if a round is slow), and starts no git. A
  repository the first round has not reached shows with error "not read yet"; before any read
  the snapshot lists the view in `unavailable` as `NotReadYet`, which the page shows as "The
  backlog view is unavailable: NotReadYet" (the page text is inferred from `unavailable` being
  shown as `gone`). `GET /api/v1/backlog?fresh=1` runs a fetch round and reads live. With no
  timer (`backlog_refresh_s` unset) the snapshot reads as the routes do.
- usage: `launch.py usage` (docs/launcher.md "Usage").
- agents: the agent definitions in `claude/agents`.
- findings: the maintenance lint's last result of each repo, never in the snapshot
  ("finding_marks/<fingerprint>" above).

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
- `POST /api/v1/records/{repo}/{id}/unblock` (owner only), body `on`, `reason`, `ruling`: the
  backlog library's unblock (`recordwrite.unblock`). One blocker opens the record; with several,
  `on` names the one to clear and `reason` is required, and a call with no `on` is `invalid` on
  field `on`. Clearing an owner blocker takes nothing more.
- These routes are the owner's, so they meet the store's owner gates (docs/backlog.md "Owner
  gates"; recordwrite passes `role_checked`). `ruling` is still accepted and ignored: no ruling
  is checked or written into a record.
- Each write answers `lines`, what the store printed (its notes included), and an add or edit
  also answers the record as it reads now. A store refusal is `invalid` in the store's words; a
  record not there is `not_found`. A repository whose `.kit.json` names no backlog store, or whose
  store cannot be written from the hub's checkout (a folder store outside a linked worktree), is
  `store`: the owner sees the store's text, an agent or the tunnel the fixed phrase.

On the page (the record page block in `kitlib/hubsite/web/index.html`, `RecordPageTest` in
`tests/hubsite/test_page.py`), a backlog row's detail in Open, Parked or Completed opens the record (Edit fields, Edit body, Retire
or Open the record), and Back to the backlog returns. The record shows its id, repository and where it is (`open` or
`completed`), then four sections:
- Fields: every header field except the hold keys, one `key: value` line per value.
- Blocker or hold: each `blocked_on` (with a button that opens the record it names), `owner_ask`,
  `hold_ruling` and `q`, or "Not blocked or held."
- Body: the record's text.
- Branch: the branch, its tip, date, worktree and how far it is ahead of and behind main. Read
  only.

On an open record, Fields, Blocker or hold and Body each have an Edit button that opens the
section as text. In the two field sections a key left out is unset, a changed key is sent with all
its values, and a line that is not `key: value` is refused on the page. Save sends only what
changed, at the version the page read, and draws the record the answer carries; a refusal shows
as "Not saved: " and the store's text. A Ruling id box appears only after a refusal that names a
ruling, and a ruling typed there is sent with the next save.

Delete… (open records only) opens a panel with a reason and a box to type the id again; the
Delete button stays disabled until both are filled and the typed id matches. After the delete the
page reads the record again, which now shows as removed and has no Edit or Delete. A record the
store cannot read ("A header line of this record is not key: value") says so above its sections.

The New story button (Backlog, beside the backlog menu) opens a blank record for the backlog that
menu picks, else the first repository: a backlog menu, then id, summary, scope (one path per line)
and effort, defaulting to the first effort in the template. Create is enabled once id, summary and
scope are filled; it adds the record and opens it. "Not added: " shows the store's text, such as a
repository whose `.kit.json` names no backlog store (this kit's repository, on the implicit
store, is one).

## Data from the old hub
The hub was once a claude.ai artifact page, one copy per Claude account; it is retired. What the
owner and the PM wrote on it is now in the hub store. A file the old hub left in the store
folder has no `version` and is read at version 0 (`kitlib.hubsite.store`); a question's
`recommended` that is text standing for a number is read as that number (the rule:
`schema.whole_recommended` in `kitlib/hubsite/schema.py`).
