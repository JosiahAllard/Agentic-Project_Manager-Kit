# Action Hub

One private claude.ai page where you, the owner, answer the PM's questions and work through hand
steps when it suits you. Nobody waits on it.

## Using the hub

- **Inbox.** Questions from the PM, each with options and a recommendation; pick one and add a
  note if you like. The Answers tab holds the PM's replies to questions you asked in a note.
- **To PM.** Write the PM a question or an FYI. Add keeps it queued; Send all sends every queued
  item and alerts the PM.
- **To-do.** Hand steps for you, each with the exact commands and a Copy button. Tick one when
  done. You can add your own.
- **Parked.** Work a coordinator has paused until you answer something.
- **Backlog.** Write a new story for a repository, see each repository's open work, and see what
  closed recently.
- **Info.** Account usage and which model each agent runs on.
- **Alert the PM** (header button) tells a watching PM session that something waits on it.
  With no PM watching, the PM picks it up at its next turn.

Links ending `#todo`, `#topm`, `#parked`, `#backlog` or `#info` open that section;
`#questions` and `#inbox` open the Inbox's Questions and Answers tabs; `#usage` and `#agents`
open that Info tab (`#stories` still works). The Questions, Inbox and To-do lists filter by
repository.

## For the PM

- Page: `claude/hub/action-hub.html`. Publish it once with the Artifact tool, declaring
  `capabilities: {db: {rules: [{path: "parked", read: "view", write: "admin"}]}, user: {}, comments: {}}`
  (the rule: "parked/<generated id>"), and pin it. Each account keeps one copy, filled from the
  mirror ("One copy per account" below).
- The PM reads and writes the store with the ArtifactData tool. Coordinators never write to it;
  they send questions and hand steps to the PM.
- Before sending a batch with ArtifactData, run `python claude/hub/hub_validate.py --batch <file>
  [--against <folder of the hub as last read>]` (`kitlib.hubvalidate`): it checks the hub data
  against the field tables below and exits 1 on an error (a `commands` that is not a list of
  strings, an unknown `status`, bad `options` or `recommended`, an unreadable batch entry). A
  warning (a ref to an id that is not in the hub, a repo tag that is no repos/ name or alias, an
  open PM to-do without `commands`, a `pages`/`page_html` document without a title or `html`) exits
  0, or 1 with `--strict`; exit 2 is a usage error or an unreadable folder or batch. Refs, repo
  tags and `page_html` ids are judged only with `--against`. `--dir <folder>`
  checks a whole out_dir or mirror folder. Nothing in this repository calls ArtifactData, so the
  PM runs it itself.
- Inbox section: one button with two tabs, Questions (questions/) and Answers (inbox/). Its badge
  counts open questions plus unread answers; the button reopens the last tab used.
- The repo filter lists every known repo; see repos/ below.
- At each start, resume and compact, the PM session-start checklist (docs/pm-start.md) prints the hub steps:
  republish (or publish and record a copy), start the ArtifactComments watch on the copy, check the
  `backlog` collection and refill the regenerated collections when it is empty, then process the hub.

### One copy per account

A page belongs to the Claude account that published it, so each account the PM runs on has its
own copy. `pm_start.hub_urls` maps an account label to its copy (docs/pm-start.md "Config"). The
label (`kitlib.launcher.accounts.current_account`; `python tools/hub_mirror.py account` prints
it) is the `launcher.accounts` entry listing this session's `CLAUDE_CONFIG_DIR` (unset:
`~/.claude`), else `default`.

The data of record is a mirror in git, `hub/<collection>/<doc_id>.json` in the PM data worktree
(`pm_data.path`, docs/pm-data-branch.md), else in the absolute folder `pm_start.hub_mirror`;
`tools/hub_mirror.py` refuses with neither. Mirrored (`kitlib.hubmirror.MIRRORED`): questions,
inbox, to_pm, todos, parked, stories, pages, page_html. Not mirrored (`REGENERATED`), refilled on
a new copy: backlog/ and completed/ (`hub_backlog.py --force`), repos/ (`hub_repos.py`, then any
aliases again), usage/ and agents/ (posted as at session start).

- **Save** after each time the PM processes the hub, and before it ends a session: read every
  mirrored collection with ArtifactData into one `out_dir` folder, every page of each (a
  collection read in part loses the rest from the mirror), then
  `python tools/hub_mirror.py save --from <folder> --commit --push`. Save also runs the checks of
  `kitlib.hubvalidate` on what it read (repos/, when `<folder>` has it, only to judge repo tags;
  it is never mirrored) and prints each finding as a `VALIDATE` line; it saves anyway and its exit
  code is unchanged. A collection folder present
  replaces that collection in the mirror; one absent is left alone. An empty collection needs its
  empty folder: if the read of a collection with no documents leaves no folder in `out_dir`,
  create it before saving, or the documents deleted from the hub stay in the mirror and a
  restore brings them back. `--commit` commits this save's changed mirror paths through
  `kitlib.commit` (tools/commit.py's checks: never a main checkout, refused when anything else is
  already staged or a staged file names a machine path), so the mirror must be in a linked work
  tree on its `.kit.json` main branch, as the PM data worktree is; a work tree of the kit itself
  (its code, or the kit's repository on any branch but the recorded data branch) is refused, as
  the kit is public. Mirror paths an earlier save left uncommitted are counted, not committed.
  A hub document that quotes a machine path is refused by that scan, stays uncommitted and is
  lost on a restore from another machine, unless the data work tree's `.kit.json`
  `"machine_paths".allow` has an entry for it, its glob naming the mirror folder (`{"glob": "hub/*", "line": "<regex>", "why": "..."}`;
  `kitlib/machinepaths` docstring, docs/commit.md "`.kit.json` keys read").
  A mirror path git ignores is skipped silently: `git status` does not list it, so it is never
  committed, and a save whose only changes are ignored prints `nothing to commit`.
  `--push` pushes that branch to `pm_data.remote` with an explicit refspec.
- **New copy:** publish the page as above, record it with
  `python tools/hub_mirror.py set-url --account <label> <url>`, then
  `python tools/hub_mirror.py restore --out <file>` writes ArtifactData batch files (`<file>`,
  `<stem>-2.json`, ...; at most 50 `set` entries and under 1 MiB each; each document's fields in
  its own file under `<stem>.docs/`, named by the entry's `file_path`). Send each with ArtifactData.
  An empty mirror writes none; a document too big for one batch (`kitlib.hubmirror.BATCH_BUDGET`,
  960 KiB with its entry, most likely a large page_html) refuses the restore before anything is
  written. Numbered batch files a longer earlier restore left are removed. Then refill the
  regenerated collections.
- `pm_start.hub_url` is the one copy of an install that has not moved to `hub_urls`; it is read
  only while `hub_urls` has no entry. To move, set-url the old URL under its account first;
  set-url prints a NOTE when its first `hub_urls` entry leaves a different `hub_url` unread (it
  does not move it: which account published that copy is not known).
- The JSON ArtifactData writes under `out_dir` has not been seen: save reads either a wrapped
  document (a `data` object plus only `kitlib.hubmirror.WRAPPER_KEYS`) or bare fields, and keeps
  neither `id` nor `version` (a document's id is its file name). A `data` object beside any other
  key is refused and nothing is saved: it cannot be told from a document with a `data` field.

## questions/Q-NNN
| Field | Written by | Meaning |
|---|---|---|
| status | PM, page | `open`, then `answered` (page), then `logged` (PM) |
| repo, from, asked_at | PM | where it came from; asked_at is ISO time |
| title | PM | the question itself |
| context | PM | everything needed to answer; nothing is assumed from chat |
| options | PM | `[{label, description}]`, recommendation first |
| recommended | PM | index into options, normally 0 |
| refs | PM | ruling ids |
| urgent | PM | true sorts it to the top with an Urgent marker; only for items that block work or have a deadline |
| answer | page | `{choice: index or "other", label, note, at}` |
| ruling | PM | the ruling id once logged |

The PM logs each answer as a ruling (provenance `selected`, or `typed` when the choice is "other" or the note changes it), then sets `status: logged` and `ruling`, pinning `if_version`.

## inbox/M-NNN
The owner's inbox: the PM's answer to every question the owner asks in a note (on a question, a to-do or a story). The question card links to its answers; the Answers tab counts the unread ones.

| Field | Written by | Meaning |
|---|---|---|
| ref | PM | the question, to-do or story id the owner asked on |
| asked | PM | the owner's question, quoted from the note |
| text | PM | the answer, complete on its own |
| title | PM | optional one-line heading |
| repo, from, at, refs | PM | provenance; at is ISO time |
| read, read_at | page | the owner marks it read (or unread again) |

PM steps: when processing the hub, read every answer note and every to-do or story detail for a question; post the answer here, and still log any ruling the note makes.

### Lock-window notices
Also in inbox/: the machine lock's own notices that a heavy or exclusive window started or ended, so the owner hears of it without anyone relaying it (`kitlib/lock/notice.py`). Off unless `machine_lock.notice.dir` is set in the machine config (`kitlib/lock/site.py`). Notices are per run, not per window: two heavy runs side by side post two start and two end notices, and the machine is free again only once every started run has its end notice. Ids are `L-<run id>-start` and `L-<run id>-end`; fields are title, text, at, repo, from (`machine lock`), kind (`lock-window`), run and phase, with no ref or asked. The lock writes each as a file in that folder; the PM watcher prints `LOCKNOTICE <id> <title>` for each new one. PM steps: `claude/hub/hub_notices.py --out <file>` writes the ArtifactData batch of the pending ones; apply it, then `claude/hub/hub_notices.py --posted <file>` moves them to the folder's `posted/`.

## parked/<generated id>
One doc per item a coordinator has parked waiting on the owner, shown in the Parked section grouped by `repo / session`, oldest first, with its age (minutes, hours or days since `parked_at`; amber past 2 days), the linked question (a button to that question when `q` is in questions/) and the branch. The count badge is the number of docs. The page only reads this collection.

| Field | Written by | Meaning |
|---|---|---|
| item | PM | the backlog id that is parked |
| repo, session | PM | the repository and the coordinator's session name |
| q | PM | the Q-id the item waits on, or null |
| branch | PM | the item's branch, or null |
| parked_at | PM | ISO time it was parked |
| note | PM | optional one line on what it waits for |

PM steps: when a coordinator reports an item parked, `set` a doc in parked/ with these fields (the doc id may be the item id). When it reports the item pulled again, or the question is logged and the item resumes, `delete` the doc; the page does not clear anything itself. A parked item whose question is still open should link that question, so the owner finds it in one click.

Publish: the db capability needs a rule so only an editor writes it: `capabilities: {db: {rules: [{path: "parked", read: "view", write: "admin"}]}, user: {}, comments: {}}`. Rules are a full-set declaration on publish, so a later republish restates them (the `artifact-capabilities` skill, section db).

## to_pm/P-NNN
The To PM section, directly after Inbox: the owner's questions and FYIs to the PM. Inbox is PM -> owner and To PM is owner -> PM; the two never share a collection, and replies to To PM items do not go to inbox/ (the Inbox Answers tab stays answers to notes on PM items).

The owner writes in the compose box at the top (type chip Question or FYI, default Question; optional repo: a repo name, `pm` or `all`; urgent; text). Add saves the item as `queued` and alerts nobody. Tabs: Queued (the owner can edit or delete), Sent (waiting on the PM), Done (the PM's reply, or "noted", and any ruling id; the last 30 days by done time, with a Show older control). The section badge counts queued plus sent.

Send all sets every queued item to `sent` with `sent_at`, then fires the Alert-the-PM button's code path once (`fire` in `setupAlert`), so the alert text lists them. Without a watching PM session the items are still sent and the PM reads them at its next turn.

| Field | Written by | Meaning |
|---|---|---|
| (doc id) | page | `P-001`, `P-002`, ...: the next number after the highest in the collection; the document's id, not a field |
| kind | page | `question` or `fyi` |
| repo | page | a repo name, `pm`, `all`, or empty |
| urgent | page | true marks it Urgent and makes the badge hot |
| text | page | the message, as written |
| status | page, PM | `queued` (page), `sent` (page, Send all), `done` (PM) |
| created_at, sent_at | page | ISO times; sent_at is null until sent |
| reply | PM | the answer, complete on its own; null or empty shows as "noted" |
| ruling | PM | the ruling id the PM logged for it, if any |
| done_at | PM | ISO time the PM finished it; the Done window and order use it (falling back to sent_at, then created_at) |

PM steps: on an alert (or at session start) list `to_pm` where `status` is `sent`; each is also listed under "To PM" in the alert text. Answer a Question with `reply`, and log any ruling it makes first (provenance `typed`) and put its id in `ruling`; for an FYI set `reply` to a short acknowledgement or leave it empty. Then `update` the doc with `status: done`, `done_at`, `reply`, `ruling`. The owner can write to the collection; the PM may restrict `done` writes by a db rule if wanted (the page only writes queued and sent).

Logic: the pure functions (`pmNextId`, `pmRows`, `pmBadge`, `pmSendAll`, `pmAlertLines`) sit between the `pm-pure:begin` and `pm-pure:end` markers in `claude/hub/action-hub.html`; `tests/hubtopm` runs that block under node.

## todos/T-NNN (or a generated id for the owner's own items)
| Field | Meaning |
|---|---|
| status | `open` or `done` (the owner ticks it) |
| title, detail | what to do and why |
| commands | a list of strings (a bare string is an error in `hub_validate.py`): the exact PowerShell commands, each with a Copy button, on every item that involves running something; to edit a file by hand, a command that opens it |
| repo, from, added_at, refs | provenance |
| done_at | set by the page when ticked |
| urgent | true sorts it to the top; the owner can set it on their own items |
| pm_ack | PM: set once it has followed up on a done item or picked up an owner-added one; keeps it out of the next alert |

The owner can add items (`from: "you"`). The PM checks the done items and follows up.

## Backlog section
Three tabs: New story (the form, then the owner's stories in progress and filed), Current backlog, and Completed (1, 7, 30 or 90 days).

One backlog dropdown at the top (All backlogs, then every repo) is shared by all three tabs and remembered. It filters each tab; the story form shows the chosen backlog as read-only text and submits only when one is chosen.

Current backlog filters, each remembered per viewer: priority chips P1 to P5 and Unpriced (P1 to P3 on by default), status chips Open, Blocked, Parked and Pulled (all on), and a search over titles and ids. Each chip shows how many items it would add under the other filters.

### backlog/<repo> and completed/<repo>
One document per repository, written by `claude/hub/hub_backlog.py --out <file>` (`kitlib.hubbacklog`), never by hand: {repo, posted_at, items: [{id, t: title, p: priority or null, s: state}]} for every open record, and {repo, posted_at, days: 90, items: [{id, t, d: closed date}]} for every record closed in the last 90 days, newest first. Both read the store `.kit.json` backlog.store names, resolved by the store itself (`kitlib.hubbacklog.store_at` over `open_backend`): by default the local copy of `refs/backlog/main`, unfetched; for `main` or a folder, that folder on the main branch. So only published records show, and a repository with no store yet shows none. A record closes by a `closed:` line or by its file being deleted from the store (the deleting commit's date, the summary from before). The tool covers every repository `known_repos` lists that has a folder (`pm_start.repo_paths`, else the PM repository's sibling); it writes a document only when it changed since the last run (`--force` writes all), keeping each document's digest and store version in a state file and pinning each write to that version. The state file is `--state` when given, else `hub_backlog_state.json` in the folder `kitlib.config.user.DIR_NAME` names under the home folder (`hub_backlog.STATE`); while that file is absent the retired folder's `hub_backlog_state.json` is read instead, and the new one is always the one written (`kitlib.config.migration.state_paths`; docs/kit-json.md "Moving the machine config folder"). Each document goes to its own file beside the batch, which names it by `file_path`. A document over 200 KiB has its titles shortened, then its last items dropped (`cut` counts them). The PM sends the batch file with ArtifactData.


### repos/<name>
{name, added_at, aliases?}: every repository the PM knows. `aliases` (optional, a list of strings) names old tags an item's `repo` may still carry; the page files such an item under this repo (case ignored). With no `aliases` anywhere, a tag that names no repo shows only under All. `hub_repos.py` never writes it: the PM sets it with an ArtifactData update. No repo menu in the page is hard-coded: the backlog dropdown lists repos/ plus every repo named in stories, backlog and completed (one per name, whatever its case); the Questions, Inbox and To-do repo filter lists repos/ plus every repo their items name, the same way. An item whose repo is `all` is for every repo: it is never a menu choice and shows under each one. `claude/hub/hub_repos.py` prints the list (`kitlib.pmstart.known_repos`: each session's repo in `sessions_file`, the PM's repository, this kit's repository and the optional `pm_start.repos`); `--have <stored names> --out <file>` writes the ArtifactData batch that adds the missing ones. Store items use these names as their `repo`.

### stories/<generated id>
The owner writes a story and picks the backlog it belongs in. It reaches the backlog only through that repo's coordinator.

| Field | Written by | Meaning |
|---|---|---|
| status | page, PM | `submitted` (page), `review`, `questions`, `added` or `declined` (PM); `withdrawn` (page, only while submitted) |
| repo | page | a repo name, such as `alpha` or `pm` |
| title, story, urgent, from, submitted_at | page | the owner's story as written |
| reviewer | PM | the coordinator reviewing it |
| review_note | PM | the coordinator's summary, shown on the card |
| backlog_id, added_at | PM | the record the coordinator filed |
| closed_note, closed_at | PM, page | why it was not added, or when it was withdrawn |

Follow-up questions are ordinary `questions` docs with `story: <story id>`; the page links them both ways.

PM steps:
1. On a `submitted` story, send it verbatim to that repo's coordinator (coordinators stay in scope) and set `status: review`, `reviewer`.
2. The coordinator reviews it against its backlog (duplicates, size, fit) and either files it or sends follow-ups to the PM. The PM posts each follow-up as a question with `story` set and sets `status: questions`.
3. The PM logs answers as usual and relays the R-ids to the coordinator; the story goes back to `review`.
4. When the coordinator reports the filed record, set `status: added`, `backlog_id`, `added_at`, and a one-line `review_note`. A coordinator that declines (duplicate, out of scope) gives a reason; set `declined` and `closed_note`.

The coordinator files the record itself with its own backlog tool, citing the story; pricing follows that repo's usual rules.

## Alert the PM
The header button sends the PM session a "Send to Claude" comment summarising what waits on it: answered questions, submitted stories, and to-dos ticked done or added by the owner without `pm_ack`, and every To PM item with status `sent`. Send all in the To PM section fires this same alert. It works only while a PM session watches the artifact with auto-replies armed; otherwise the page says so and the PM picks things up at its next turn. A relaunched PM runs `ArtifactComments watch` on the hub URL. On an alert, the PM processes the hub, replies in the thread with what it did, and resolves it.

## Info section
Reference material in two tabs, Usage and Agents. The Info button shows the usage of the account in use now: its higher of five-hour and weekly, hot at 90%.

### usage/<account>
One doc per account (`1`, `2`, `3`, `codex`): `five_hour`, `seven_day` (a number or "reset"), `five_reset`, `seven_reset` (text from `launch.py usage`), `measured_at` (when `launch.py usage` last measured it), `posted_at`, `who` (sessions on it), and `current` (true on the one account in use now; `launch.py usage` prints it as "current account", the account whose usage file was written in the last 15 min). The PM posts all of them from `launch.py usage` (docs/launcher.md "Usage") at session start and on each pm_watch USAGE/RESET event. The Usage tab (in Info) shows amber at 90% and red at 95%, and flags a measurement older than 2 h.

### agents/<name> (the Agents tab)
One doc per agent definition: `model`, `effort` (null = the model's default), `role` (one line), `posted_at`. Read-only display of the defaults; the PM reposts them whenever a definition file changes. Coordinators may still raise a model by risk at dispatch.
