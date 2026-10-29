"""kitlib.hubsite.api: the hub's verbs as functions over the store, with no transport in them (the
hub site design, section 2 "HTTP API" and section 9).

  ROUTES     the table every transport iterates: Route(method, pattern, role, handler). A pattern
             is a full path under BASE; a `{name}` segment takes one path segment. role is OWNER,
             AGENT, ANY, APPROVER or LOCK_WRITE: who may call it ("Roles", "Machine lock").
  Api(store, views=None, clock=None, session_keys=None, now=None, approval_key=None,
      checkout=None, lock=None, repo_name=None, schedule_repos=None, tick_seen=None,
      kit_key=None, kit_blob=None)
             .call(method, path, role, body=None, query=None, via='', raise_unexpected=False,
                   agent_words=False, strong=False) -> (status, object)
             via names the transport (web, desktop-mcp, cli, tunnel, ...); every write records
             it as the document's `via` (`api` when none is given), an answer in `answer.via`
             too. A delete leaves no document to carry it. raise_unexpected: an exception
             that is no HubError is raised to the caller, not answered ("What an agent is
             told"). agent_words: an error answer is worded as for an agent whatever the role
             is, which stays the role for what the request may do.
  match(method, path)    (Route, {name: segment}) or None
  sample_path(route)     the pattern with every `{name}` filled in, for a test that walks ROUTES

Roles. The caller resolves who is asking and passes OWNER or AGENT; this module only enforces.
A role that is neither is refused before the path is looked at, a route whose role is not one of
ROLES is refused for everybody, and the check runs before the handler reads the body. So a route
added without a role answers `forbidden`, never the data.
APPROVER is a route role and never a caller: such a route answers only OWNER with strong=True,
the owner the transport saw log in through Cloudflare Access (kitlib.hubsite.tunnel's gate marks
that request, kitlib.hubsite.httpd passes it on). Every other caller is refused `forbidden`,
field `role`, with NOT_STRONG: the owner token, a browser session paired from it and an agent are
never strong. strong is a call argument, never a field of the body or the query, and it opens no
other route: on OWNER, AGENT and ANY routes the role alone decides. A handler reads it as
Request.strong. POST approvals/{id}/decide is an APPROVER route ("Approvals"), and so are the
lock's reservation and division writes. LOCK_WRITE is a route role too: OWNER, and strong as
well when the call came over the tunnel ("Machine lock").

Approvals (kitlib.hubsite.approvals holds the rules; these routes only carry them). POST approvals
(AGENT) adds a pending request: the base of each file is read with git from this machine's
checkout of the request's repo (Api's checkout; no checkout is `invalid`, field repo, and a base
that is not the file at head is `invalid`, field files), and the unified diff lines the page
shows are stored in the document as `diff`. POST approvals/{id}/decide (APPROVER) writes the
owner's decision, signed with Api's approval_key; POST approvals/{id}/consume (AGENT) marks an
approval consumed by a commit. Both take the `if_version` the caller read and are pinned to it,
and an ApprovalError is answered as a HubError of its code and field. POST approvals/{id}/delete (AGENT) withdraws a request still pending (not decided, not
expired: else `conflict`, field status): the body gives `from` (the requester's claim, `pm` as
approvals store none) and `if_version`, as a question's withdrawal does. GET approvals/{id}/verify
(AGENT, query repo, head, files as JSON {path: blob} and an optional commit) answers
{ok, valid, why} and never the key or the sig; an unknown id is valid false, approvals.WHY_UNKNOWN.
An approval of kind `op` (one run of a delegated operation, docs/delegations.md "Hub approvals")
is added with kind, op, repo, line, title and optional reason and what, and no checkout is read;
any other kind is `invalid`, field kind. It is consumed with args_sha256 in place of commit, and
verified with the query kind=op, op, repo and args (the line's sha256), which answers its version
too, the pin of the consume that follows.
GET snapshot carries `strong`: true only on the strong owner's call (Request.strong), so the page
shows Approve and Deny as usable; the decide route checks the role itself all the same.

Pages. POST pages (AGENT) publishes a page: the card `pages/<id>` and its body `page_html/<id>`.
The body is {id, title, html, repo?, from?, summary?, refs?, if_version?}. The id is the
caller's (a document id that holds no hub item's or ruling's id, `[QMTPSARE]-<digits>` with no
letter or digit either side, anywhere, so a ref that names a page is never read as an item);
html is judged by kitlib.hubsite.pagecheck (at most PAGE_HTML_MAX bytes of UTF-8, no script, no
outside link or load, no unrendered Mermaid), and a refusal is `invalid`, field html, with
nothing written. Publishing over a page takes the card's `if_version`; without it, or with
another, it is `conflict`. store.PAGES_LOCK is held from the reads to the last write of a publish or a
delete (a page is two documents), so a second publisher reads only after the first has written
both, and with the same pin
it is `conflict` with nothing written. The body is put first and the card second, each pinned to
the version read (0: not there yet), so a card never names a missing body; a card write that
fails after its body leaves an orphan body, which kitlib.hubvalidate warns of and the next
publish overwrites. The card carries title, repo, summary, refs, from (an agent's name, as a
to-do's), `at` and `bytes`. GET pages lists the cards; GET pages/{id} gives {ok, id, html,
version} of the body. GET snapshot leaves `page_html` out: the Docs tab reads a body when it is
opened. POST pages/{id}/delete (OWNER, if_version optional) deletes the card, then the body; a
body delete that fails leaves the card deleted and the body an orphan, as above. One process
lock is enough: Store.open lets one process write a store (`_store.lock`).

Schedules. POST schedules (OWNER) sets when one of the kit's automatic tasks runs for one repo:
{task, repo, cadence, at, days?, if_version?} into `schedules/<task>-<repo>`. task is one of
SCHEDULE_TASKS; cadence one of CADENCES (`off` keeps the document and runs nothing); at is HH:MM,
24-hour; days, a list from DAYS, is required for `weekly` and refused for the others, and is
stored once each in DAYS order. repo must be a repository the hub knows (its repos/ documents'
names and aliases, and the store's known_repos), case ignored, and is stored as that repo's name.
A task that runs for one repository (kitlib.autotasks.tick.TASKS per_repo), on any cadence but
`off`, also needs exactly one checkout of it in the machine config's launcher.repos, as the tick
finds it (tick.find_repo over Api's schedule_repos, default launcher_checkouts: tick.launcher_repos);
none, or two, is `invalid`, field repo, and so is a launcher config that cannot be read.
A refusal is `invalid` naming the field, with nothing written. Writing over a schedule takes its
`if_version`; without it, or with another, it is `conflict`, as for a page, and that refusal's
error names the schedule's `id` (so a new one for a task and repo that has one can open it,
the repo matched as above). The answer carries
`first_run`: the first slot strictly after set_at by kitlib.autotasks.due's rule, naive local
time to the minute (None when off). GET schedules (ANY) lists them, with `tick`: {last, running,
every_s}, last when the tick was last seen (Api's tick_seen, default tick_last_seen: the time of
its state file) and running false when that is None or more than TICK_STALE_S before the clock.
Each listed schedule also carries `last_outcome` (the tick's record for it: started, skipped, seen
or failed; None for none), `last_run` (that record's `at` when it is `started`, else None) and
`next_due` (the first slot after the clock by due's rule, naive local time to the minute; None when
off), read from Api's tick_state, default tick_run_state; none is stored.
Nothing here runs a task: a tick outside the hub reads the list.

Findings. GET findings (ANY) lists the maintenance lint's last findings (the `findings` view),
filtered by `repo` (case ignored), less every one the owner has marked: a finding_marks/
<fingerprint> document. POST finding_marks (OWNER) marks one, {fingerprint, status, note?,
if_version?}: status is one of hubvalidate.STATUSES['finding_marks'], the fingerprint must be one
the lint shows (`not_found` otherwise), and the mark keeps the finding's repo, check, target and
text as they were. Writing over a mark takes its `if_version`, as for a schedule. POST
finding_marks/{id}/delete (OWNER, if_version optional) takes a mark back; GET finding_marks (ANY)
lists them. POST to_pm with kind `act` passes a finding on to the PM: `finding` (its
fingerprint, required, and refused for any other kind) prefills repo, refs (the fingerprint and
the finding's target first) and, when none is given, text; the page marks the finding itself.
POST to_pm/{id}/edit refuses to make an act note another kind or another note an act note.
A finding's target and fix can name a path on this machine (a worktree's folder, a kit tool):
only the local owner (Request.full: the owner, its error answers in its own words, not through
the tunnel) sees them whole. Any other caller's findings, marks and act-note text have each
absolute path cut to `.../<its last part>`, and no act note's refs carry one; GET to_pm and GET
snapshot cut an act note's text and refs and a mark's target the same way (_unpath_act, _seen),
and GET pending, GET wait and GET snapshot an act note's pending title (_unpath_pending).
GET findings also answers `runs`: [{repo, started, ledger, skipped}], each repo's last run from
the optional lint_runs view: ledger its three ledger counts (LEDGER_COUNTS) or null, skipped the
skip marker's {reason, at} only where its `at` is later than the run's `started` and both parse,
else null (_lint_runs); no view, or one that fails, is an empty list.

Skill edits (docs/action-hub.md "skill_edits/E-NNN"). The owner writes a new text for one kit
skill's source, content/skills/<name>/SKILL.md, and an agent commits it; the hub writes no
repository. GET skills/{name}/source (ANY) gives {name, text, blob}: the whole SKILL.md of a
listed skill as this hub serves it (the skill_source view; CRLF read as LF) and the git blob id
of that text. Only the owner writes an edit, as only the owner writes a story (POST stories,
stories/{id}/withdraw, to_pm and finding_marks are OWNER routes too): POST skill_edits {skill,
text, submit?}, POST skill_edits/{id}/edit {text?, submit?, if_version} and POST
skill_edits/{id}/withdraw {if_version?} are OWNER, so an agent's key is refused `forbidden`, field
role, before the body is read, and nothing is written. An edit is draft, or submitted with
`submit`; base_blob is the blob of the source the hub served when it was written, so it can lag
the kit's main. The text is checked by kitlib.hubsite.skilledits.why_not: at most MAX_BYTES of
UTF-8, no NUL, and front matter whose `name:` is the source's (an edit never renames a skill).
Only a draft, superseded or failed edit changes; a superseded or failed one is put on the source
at its source_blob when kit_text reads that blob (the text GET skill_edits/{id} shows the owner),
else on the served source as it is now; its base_blob is that blob. Submitting when base_blob is
neither the served source's blob nor the blob on main (kit_blob) makes it superseded, with
source_blob the blob on main (the served blob when kit_blob reads none), for the owner to re-apply.
POST skill_edits/{id}/done {branch, blob, if_version} and POST skill_edits/{id}/fail {why,
current_blob?, if_version} are AGENT, and only for a submitted edit: done refuses a blob that is
not git_blob of the edit's text as UTF-8, and it reads the blob of the source on `branch` in
the kit checkout itself (the Api's kit_blob) and refuses one it cannot read or that is not that
blob (field branch), so an agent cannot mark committed a text it never wrote; with no kit_blob it
reads nothing, so done is refused. fail with a current_blob
that is not base_blob (the worker's check against main) makes the edit superseded, else failed.
GET skill_edits (ANY, status) lists the edits, GET skill_edits/{id} (ANY) gives {item, source}:
source is {text, blob} at a superseded or failed edit's source_blob when kit_text reads it, else
null. A
submitted edit is a pending item of kind `skill_edit` ("Routes").

Fields. A write route names the body fields it takes; any other field is refused (`invalid`, the
field named), so a body cannot carry a field another role owns (an agent posting a question
cannot post its answer). status, the time stamps and a to-do's `from: you` are set here from the
route and the role, never taken from the body. A write to an existing document is pinned to the
version this module read (or the caller's `if_version`), so a check made on one version is never
applied to another: the loser gets `conflict`. The `from` an agent gives a to-do is a name in
printable ASCII (0x21 to 0x7E, single spaces inside it, the ends trimmed); anything else is
`invalid`, and so is any spelling of `you`. A question's `from` is the session its answer goes
back to: a key of the launcher's sessions.json, read on each add (Api's session_keys), or `pm`,
which is also what an absent one is; any other is `invalid`, and the refusal names the known
keys, never the file; when sessions.json or the launcher's settings cannot be read, only `pm` is
known and the refusal says the read failed, with the exception's class name. A new to-do's `steps` are made into its commands and detail here and are
not kept (todo_steps); an agent's to-do is judged by the owner's to-do rules (kitlib.hubsite.
todosteps.check) for every client. On the edit routes a null `repo` (and a to-do's
null `detail`) clears the field exactly as "" does; any other null field is read as absent.

Creators. An agent edits (POST questions/{id}/edit, todos/{id}/edit) or withdraws (POST
questions/{id}/delete, todos/{id}/delete) only an open item it added: the body gives the item's
own `from` (`pm` matches an item with none) and the `if_version` it read, both required, and an
owner's to-do (`from: you`) is never an agent's. A wrong `from` is `forbidden`, an item no longer
open is `conflict`, field status, and neither changes it. POST questions/{id}/done takes an
optional `from` (default `pm`) and is `forbidden`, field from, unless it is the question's own
`from` or `pm`. Every agent shares one token, so the `from` is a claim: the check stops a mistake, not an agent that gives another's name. An agent's
to-do edit is judged by the owner's to-do rules (todo_steps), the changed fields only; it takes
no `steps`. The owner edits only a to-do of their own (any status) and deletes one of their own
at any status, or any open question or open to-do; the owner never edits an agent's item. A
withdrawal deletes the document: there is no withdrawn status.

Results. status is 200 with the route's object, or the status of STATUS[code] with
{"ok": false, "error": {"code", "message", "field"}}; both carry `revision`. The codes are the
store's HubError codes plus `forbidden`, `unavailable` (a view this server does not have, or one
that failed) and `internal` (anything unexpected). A write answers the revision after it (send:
after its last note), so a client that waits `since` it is not woken by its own write.

What an agent is told. A `store` refusal names files and the store's folder, and an unexpected
error may: the owner gets that text, any other caller gets the fixed phrase of AGENT_SAYS for a
`store` refusal and only the exception's class name for `internal`. The store's `invalid`,
`not_found` and `conflict` messages name a collection, an id and a field and go to both.
A transport whose answers leave this machine calls with agent_words=True: the owner then gets the
agent's wording too (the same status, code and field), so no answer of that transport names the
store's folder or a file path. kitlib.hubsite.httpd does for the tunnel listener.
A transport that has a log of its own calls with raise_unexpected=True: the unexpected error is
then raised to it and no answer is made here, so the transport can log the cause and send a
fixed message to every caller, the owner too (kitlib.hubsite.httpd does).

Views. The live backlog and the derived views are other modules; `views` is any object with some
of these methods, each one optional. A missing one answers `unavailable` on its own route, and
so does one that raises, with a short reason (a HubError's code, else the exception's class
name; never a message or a path). GET snapshot and GET health never fail for a view: the part
is null and `unavailable` maps its name to that same reason:
  backlog(repo='', state='', fresh=False)   [{repo, fetched_at, items}]
  completed(repo='', days=None)             [{repo, fetched_at, items}]
  notices()  repos()  usage()  agents()     a list each
  skills()                                  {available, skills|reason}: each skill's name and description
  skill(name)                               one skill with its body (GET skills/{name}); not_found
  findings()                                the lint's rows (GET findings; not in GET snapshot)
  lint_runs()                               [{repo, started, ledger, skip}]: each repo's last
                                            result's started and ledger, and its skip marker
                                            (kitlib.maintlint.slot.read_skip) or null
  health()                                  a dict merged into GET health (git, backlog, ...)
  record(repo, id)                          one record (GET records/{repo}/{id})
  record_template(repo)                     a new record's fields (GET records/{repo}/new)
The two record views differ: what they raise as a HubError is the answer (a 404 for a record or
repository that is not there, a 400 for a name that cannot be one), so their messages are fixed
phrases with no path, no id and nothing the caller sent; only anything else is `unavailable`.
Both are read now, never cached, and neither is part of GET snapshot.
  record_add(repo, id, summary, scopes, effort, ref)               the owner's four writes to
  record_edit(repo, id, version, changes, unset, body)             a repository's backlog store
  record_delete(repo, id, why)                                     (OWNER routes; kitlib.hubsite.
  record_unblock(repo, id, on, reason)                             recordwrite)
They raise HubErrors as the record views do; a `store` one carries the store's text, which only
the local owner is given. POST records/{repo}/{id}/delete takes `confirm`, which must repeat the id.
  record_answer(repo, id, qid, at, title, label, note, via, other, waiting)
                                            files a question's answer into the record it is
                                            bound to (recordwrite answer; file_answer)
An answer to a question bound to a record is filed by POST questions/{id}/answer as soon as it is
stored, and again by POST questions/file (AGENT, optional {id}) and at hub start for every
answered one not filed yet: file_answer. A filed question has status `filed` and stays pending,
with `filed` true, until its asker logs it or marks it done (POST questions/{id}/log or done), so
an asker between waits still reads the answer; the PM's list carries it only when it is the PM's
or routed to `pm` (Routes), or a standing rule. One that failed stays `answered` with `filing`,
for the PM. A filed question carries `filed_as`, what the answer did to its record (pulled,
freed, held: _filed_as, from the `record` view read before and after), which the push reads to
choose who hears it. `standing` {at, via} marks an answer the owner made a standing rule (the
answer's `standing` true, or later `standing` alone: _promote): the PM logs it.
POST questions/{id}/log and POST to_pm/{id}/reply take no ruling id (questions_log): a question
is marked logged with none. Documents an older hub logged keep their `ruling`; nothing reads it.

Machine lock. By the owner's decision the hub reads and changes the machine lock in-process,
through `lock`: a door object (kitlib.hubsite.lockdoor.LockDoor on the machine's server; this
module never imports it) with
  revision()                     the lock store's revision
  view(remote=False)             {lock_revision, view, writer}: kitlib.lockui.model to_json's view,
                                 and 'owner' or 'agent': who the lock takes this hub for
  stats(days, since, until)      kitlib.lock.api stats
  reserve, hold, edit, end_hold, end_run, move_to_top, game_mode, games, division
                                 the writes (lockdoor's docstring), each one lock api call, each
                                 taking remote; a refusal is a HubError
Every lock route is closed to an agent (`forbidden`, field role), on every listener: an agent
reads the lock through its own MCP server and changes it with the lock's tools. The reads (GET
lock/view, lock/stats, lock/wait) are OWNER's. The writes, by the owner's decision: on this
machine the owner's login is enough, over the tunnel every lock write needs the strong owner. A
write the decision does not name (a reservation: any hold write; the division) is the strong
owner's on every listener, APPROVER; the others (end a run, move one to the top, game mode, the
games list) are LOCK_WRITE: OWNER with strong, or OWNER on a listener that is not the tunnel
(via is not TUNNEL_VIA), refused with NOT_LOCK_WRITER. A future relay that reaches the local
listener for a remote browser must close that local owner path for its callers.
  POST lock/holds            APPROVER  {kind: reserve, scope: whole|half, start, end (ISO with
                                       offset), note} or {kind: hold, scope, start, end, note,
                                       cpu, gpu} (the Hold dialog's text, read in the PC's zone)
  POST lock/holds/{id}/edit  APPROVER  {start, end, note}: the Edit dialog's text; no note keeps it
  POST lock/holds/{id}/end   APPROVER  {}
  POST lock/division         APPROVER  {fields: {key: text}} or {defaults: true}
  POST lock/runs/{id}/end    LOCK_WRITE {confirm}: confirm repeats the run id
  POST lock/runs/{id}/top    LOCK_WRITE {}
  POST lock/game_mode        LOCK_WRITE {enabled}
  POST lock/games            LOCK_WRITE {text}: the Games dialog's text
A write answers {ok, result} plus the view after it (as GET lock/view; view null when the lock
cannot be read then). The page makes owner holds only; there is no `owner` field. A busy lock is
`conflict` (409). The view answers of the lock routes carry `writes`: {lock, strong, why}, what
this caller may change (lock: the LOCK_WRITE routes, strong: the APPROVER ones too) and, when
one is false, why (WRITES_WHY); a hub the lock takes for an agent may change nothing.
GET lock/view gives the view; `remote` is true only for the tunnel listener's calls (via
TUNNEL_VIA), whose warnings and refusals that can name a file are fixed phrases
(model.REMOTE_SAYS, lockdoor.REMOTE_SAYS); every listener gets the same allow-listed keys. The view
carries `queue_items` (the line behind the runs, with each one's position and why it waits) and its
finished runs carry `slug`; the action hub page's Schedule view (web/index.html) draws them. GET
lock/stats takes days (1 to 366), since and until (ISO times with their offset). GET lock/wait
takes since (a lock revision; default the one now) and timeout (seconds, at most WAIT_MAX_S) and
answers the view plus `changed` once the lock's revision differs from since, or at the timeout
with changed false. The lock store has no wake of its own, so the wait looks at its revision
every LOCK_POLL_S, sleeping in Store.wait so that a close of the hub store ends it at once
(`store`). It is a wait for the listener (WAIT_HANDLERS): it takes one of the listener's wait
slots. With no door every route is `unavailable`, field lock; a door that raises anything but a
HubError is `unavailable` with only the exception's class name (_reason), never its text.

Nothing here opens a socket, starts a thread, prints, parses arguments or exits. GET wait (and GET
lock/wait) blocks in Store.wait for at most WAIT_MAX_S; how many may wait at once is the listener's
rule.

PM seen. GET snapshot carries `pm_seen`: {"waited", "pulled", "waiting"}. waited is the time
(the Api's clock) an AGENT-role caller last entered or left GET wait, pulled the time one last
read the pending work (GET pending, or GET wait with pending=1), waiting the number of agent
waits blocked in Store.wait right now. All three live in this Api object, not in the store: a
new Api (a server restart) starts at null, null and 0 until the next agent call. The owner's
own calls never count, nor does a call with `for` a key other than `pm` (another session's
wait for its own answers is not the PM listening). No lock is taken: each note is one assignment, and an open wait is one
key put into and popped from a dict, which the interpreter does as one step each.

Push status. GET snapshot carries `push`: null when this hub runs no push-notification worker,
else {"state", "last", "undelivered"} from PushWorker.status (kitlib.hubsite.push docstring
"Status"): state 'up' or 'down', last the latest delivery to the PM's state (null before the first),
undelivered the number of items some recipient has not yet had a written try of. The worker
sets Api.push_status when it is made; the snapshot reads it on the request's thread. GET health
carries the same `push` (Api.push_seen), so a hub with no worker or a dead one shows on health.

Watcher. GET snapshot and GET health carry `watcher`: null when this hub runs no push worker,
else {"state", "age"} from PushWorker.watcher_status (kitlib.hubsite.push docstring "Watcher"):
the PM's pm_watch heartbeat at the worker's latest read, state 'up', 'down' (older than
WATCHER_STALE_S) or 'none' (no heartbeat readable), age its seconds (null with none). A status
that raises is 'down' with a null age.

Code. GET snapshot and GET health carry `code`: null when this hub gives none, else {"stale",
"restart"} from Hub.code_state (kitlib.hubsite.codestale): stale is true once the hub code on disk
is newer than this process, restart the owner's to-do text then (null otherwise). The server sets
Api.code_status when it is built; a status that raises or is not an object gives null. The
tunnel's health holds fixed words only and carries none.

Jobs. GET health carries `jobs`: null when this hub runs no timed job, else {name: block} from
kitlib.hubsite.jobs Jobs.status, each block {"every_s", "runs", "failures", "running",
"last_at", "ok_at", "note", "error"} (kitlib.hubsite.jobs docstring). The server sets
Api.jobs_status when it is built; a status that raises or is not an object gives null. The
tunnel's health carries none.

Pending for one session. Every pending item carries `from`, the session key it is for
(Api.pending). GET pending and GET wait (with pending=1) take `for=<key>`: only that key's
items are listed (`for=pm`: see Routes), and such a wait is woken only by one of them, a reset or its timeout, so a
coordinator is not woken by another session's answer. A filtered read changes nothing: the
items it leaves out, and the ones it lists, stay pending for every reader until they are
handled (a question, answered or filed, until it is logged, or marked done when it needs no
ruling). Without `for` every item is listed, as before.
GET wait (with pending=1) also takes `except=<id>,<id>`: the ids the caller has already acted
on are left out, and such a wait is woken only by another item, so an answer that waits for the
PM's log is not returned again at once. It too only hides: nothing is marked.

Routes. Every pending item also carries `route`: who is to handle it now. It is its `from` key
while that session can be reached, else `pm`, so the PM is the fallback for a creator that is
gone. An item whose `from` is `pm` is the PM's. Otherwise, once a GET pending or GET wait `for`
its key has handed the item to that key (an agent call), the route is the key for
ACK_WINDOW_S after that first hand-over and `pm` from then on while it is still pending (the
creator clears a to-do with its ack). Before any hand-over the route is the key while the key is
reachable: a wait `for` it is blocked now, one started or ended within REACH_GRACE_S, or a push
written to it within ACK_WINDOW_S (Api.pushed, which the push worker calls for each written try
to a key other than `pm`), so a session told by push with no wait open keeps its items. `for=pm`
lists the items routed to `pm`, every answered question, whoever it is from (the PM logs each
ruling), and every decided approval; a filed question only when it is routed to `pm` (its asker
reads it). An approval has no `from`: it is for the coordinator of its
repository, the session key named as the configured repository whose origin remote is the
approval's `repo` (Api's repo_name), while that key is in sessions.json, else for `pm`. A
skill edit has no `from` either: it is for the kit's coordinator, the key Api's kit_key gives
(kit_session_key), on the same terms. A wait
`for=pm` is also woken when an item's route turns to `pm` with no write. All of
this lives in this Api object: a new Api (a server restart) routes every item to `pm` until its
creator waits again. As for PM seen, no lock is taken: each note is one dict step, a dict is
copied before it is walked, and a key's map of blocked waits is never removed, so a wait that
opens while another closes is not lost.

Revision. A read's `revision` is taken before the data is copied, so it is never later than the
data: a client that waits `since` it is told of any write the copy missed. GET health gives the
store's folder to the owner only.
"""
import collections
import datetime
import json
import math
import os
import re
import time

from kitlib import hubvalidate
# at load, not on each read: a kit refresh can remove the folder this process imported from
from kitlib.launcher import settings as lsettings, state as lstate

from . import pagecheck
from . import schema
from . import skilledits
from . import todosteps
from .store import PAGES_LOCK, HubError

BASE = '/api/v1'
API_VERSION = 'v1'
OWNER, AGENT, ANY = 'owner', 'agent', 'any'
APPROVER = 'approver'               # a route for the strong owner alone ("Roles")
LOCK_WRITE = 'lock-write'           # a lock write: the owner here, the strong owner remotely
ROLES = (OWNER, AGENT, ANY, APPROVER, LOCK_WRITE)   # what a route may ask for
CALLERS = (OWNER, AGENT)            # what a caller may be
# why a route of role APPROVER refused a caller who is not the strong owner
NOT_STRONG = ('%s %s needs the owner logged in through Cloudflare Access: the owner token, a '
              'browser paired from it and an agent are not enough')
# why a route of role LOCK_WRITE refused a caller
NOT_LOCK_WRITER = ('%s %s is the owner\'s; over the tunnel it needs the owner logged in through '
                   'Cloudflare Access')
# why the page may not write the lock (GET lock/view's `writes`)
WRITES_WHY = {
    'agent': 'the machine lock takes this hub for an agent (it was started under an agent '
             'session): restart the hub from your own console or its scheduled task',
    'tunnel': 'over the tunnel a lock change needs the owner logged in through Cloudflare Access',
    'strong': 'reservations and the division need the strong owner login (a browser paired as '
              'the strong owner, or Cloudflare Access)'}
STATUS = {'invalid': 400, 'forbidden': 403, 'not_found': 404, 'conflict': 409, 'store': 503,
          'unavailable': 503, 'internal': 500}
WAIT_MAX_S = 55
LOCK_POLL_S = 0.5                   # how often GET lock/wait looks at the lock's revision
LOCK_DAYS_MAX = 366                 # GET lock/stats `days`
TUNNEL_VIA = 'tunnel'               # the tunnel listener's via (kitlib.hubsite.httpd): a remote view
ACK_WINDOW_S = 30 * 60              # a creator's time to handle an item handed to it ("Routes")
REACH_GRACE_S = WAIT_MAX_S + 30     # a key is reachable this long after its last wait ("Routes")
ROUTE_SLACK_S = 0.01                # a PM wait looks again this long after a route's turn is due
VIEWS = ('backlog', 'completed', 'notices', 'repos', 'usage', 'agents', 'skills')
OWNER_FROM = 'you'                  # a to-do's or a story's `from` when the owner wrote it
AGENT_FROM = 'pm'                   # a to-do's `from` when an agent gives none
AGENT_STORY_STATUSES = ('review', 'questions', 'added', 'declined')
DAYS_MAX = 3650
# what a caller who is not the owner is told in place of the store's own message, by code
AGENT_SAYS = {'store': 'the hub store is not available: the owner sees why'}

Route = collections.namedtuple('Route', 'method pattern role handler')
# full: the local owner, told the store's own words (agent_words False) and not through the tunnel
Request = collections.namedtuple('Request', 'role params body query via strong full')

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('', '0', 'false', 'no', 'off')
_ID_SPLIT = re.compile(r'([0-9]+)')
_NAME = re.compile(r'[\x21-\x7e]+(?: [\x21-\x7e]+)*')
_SURROGATE = re.compile('[\ud800-\udfff]')     # a lone one: half a character
# A hub item's id or a ruling's anywhere in a page id ("Pages"): hubvalidate._REF's pattern, plus
# A, E and R and the per-repo ledger id H-<repo>-nnnn, searched, so neither `plan-Q-1` nor `Q-1.v2` holds a ref a reader would follow.
_REF_SHAPED = re.compile(r'(?<![A-Za-z0-9])(?:[QMTPSARE]-\d+|H-[a-z0-9][a-z0-9-]*-\d+)(?![A-Za-z0-9])', re.I)
PAGE_HTML_MAX = pagecheck.MAX_BYTES


def error_body(code, message, field=''):
    return {'ok': False, 'error': {'code': code, 'message': message, 'field': field or ''}}


def _invalid(message, field=''):
    return HubError('invalid', message, field)


def _natural(doc_id):
    # split on a group: text at the even places, ASCII digits at the odd ones
    return [int(p) if i % 2 else p.lower() for i, p in enumerate(_ID_SPLIT.split(doc_id))]


def _version(doc):
    v = doc.get('version', 0)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _segments(path):
    return path.split('/') if isinstance(path, str) else []


def match(method, path):
    """(Route, {name: segment}) for the route that method and path name, else None. A literal
    segment wins over a `{name}` one."""
    if not isinstance(method, str):
        return None
    want, best = _segments(path), None
    for route in ROUTES:
        have = _segments(route.pattern)
        if route.method != method.upper() or len(have) != len(want):
            continue
        params, literal = {}, 0
        for h, w in zip(have, want):
            if h.startswith('{') and h.endswith('}'):
                if not w:
                    break
                params[h[1:-1]] = w
            elif h != w:
                break
            else:
                literal += 1
        else:
            if best is None or literal > best[0]:
                best = (literal, route, params)
    return (best[1], best[2]) if best else None


def sample_path(route, value='X-1'):
    """route.pattern with every `{name}` segment replaced by value."""
    return re.sub(r'\{[^/}]*\}', value, route.pattern)


# ------------------------------------------------------------------ reading a request
def _fields(req, allowed, required=(), flags=(), blank=()):
    """The body's fields: an object holding only `allowed` names, a null one read as absent
    (refused for a name in `flags`: null is neither true nor false; read as "" for a name in
    `blank`), every `required` one present."""
    body = req.body
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise _invalid('the request body is a JSON object', 'body')
    for name in sorted(body, key=str):
        if name not in allowed:
            raise _invalid('this route takes no %r field (it takes: %s)'
                           % (name, ', '.join(allowed) or 'none'), str(name))
    for name in flags:
        if name in body and not isinstance(body[name], bool):
            raise _invalid('%s must be true or false, not %s'
                           % (name, 'null' if body[name] is None else type(body[name]).__name__),
                           name)
    out = {k: ('' if v is None else v) for k, v in body.items() if v is not None or k in blank}
    for name in required:
        v = out.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise _invalid('%s is required' % name, name)
    return out


def _typed(b, name, types, what):
    v = b.get(name)
    if v is None:
        return None
    if (isinstance(v, bool) and bool not in types) or not isinstance(v, types):
        raise _invalid('%s must be %s, not %s' % (name, what, type(v).__name__), name)
    return v


def _text(b, name):
    return _typed(b, name, (str,), 'a string')


def _flag(b, name, default):
    v = _typed(b, name, (bool,), 'true or false')
    return default if v is None else v


def _pin(b, required=False):
    v = _typed(b, 'if_version', (int,), 'a whole number')
    if v is None and required:
        raise _invalid('if_version is required: the version of the document you read',
                       'if_version')
    if v is not None and v < 0:
        raise _invalid('if_version must be a whole number, not %r' % (v,), 'if_version')
    return v


def _q(req, name):
    v = (req.query or {}).get(name)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return ''
    if isinstance(v, bool):
        return '1' if v else '0'
    return str(v).strip()


def _q_flag(req, name):
    v = _q(req, name).lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise _invalid('%s is 1 or 0, not %r' % (name, v), name)


def _q_int(req, name, low, high):
    v = _q(req, name)
    if not v:
        return None
    if not re.match(r'^\d{1,12}$', v) or not low <= int(v) <= high:
        raise _invalid('%s is a whole number from %d to %d, not %r' % (name, low, high, v), name)
    return int(v)


def _when(text, field):
    try:
        t = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        t = None
    if t is None or t.tzinfo is None:
        if field:
            raise _invalid('%s is an ISO time with its offset, not %r' % (field, text), field)
        return None
    return t


def _via(req):
    return req.via or 'api'


def _reason(e):
    """Why a view gave nothing, short and with no path in it: a HubError's code, else the
    exception's class name."""
    return e.code if isinstance(e, HubError) else type(e).__name__


def launcher_keys():
    """The session keys of the launcher's sessions.json (kitlib.launcher, imported when this
    module loads), read now. A launcher whose settings or that file cannot be read raises: the
    caller says so (Api.read_session_keys), never an empty set that reads as "no sessions"."""
    return {k for k, _ in lstate.entries(lstate.read_sessions(lsettings.load()))}


def _for(d, for_key):
    """Whether pending item d (with `from`, `route` and `note`) is listed for for_key: every
    item with none; for AGENT_FROM, the ones routed to it, every question not filed (the PM logs
    each ruling; a filed answer is its asker's while the asker can be reached), every filed one
    the owner made a standing rule (`standing`: the PM logs it) and every approval (the owner's
    decision); for another key, its own."""
    if not for_key:
        return True
    if for_key == AGENT_FROM:
        return (d['route'] == AGENT_FROM or d['kind'] == 'approval'
                or (d['kind'] == 'question'
                    and (d.get('note') != 'filed' or d.get('standing') is True)))
    return d['from'] == for_key


def _agent_from(text):
    """The `from` of a to-do an agent adds: text trimmed, AGENT_FROM when that leaves nothing.
    Only printable ASCII with single spaces inside is a name (so no character a reader cannot
    see or tell from another), and no spelling of OWNER_FROM is an agent's."""
    name = text.strip()
    if not name:
        return AGENT_FROM
    if not _NAME.fullmatch(name):
        raise _invalid('from is a name in printable ASCII with single spaces inside, not %s'
                       % ascii(name), 'from')
    if name.replace(' ', '').casefold() == OWNER_FROM:
        raise _invalid('from: %s marks a to-do the owner wrote; an agent cannot use it'
                       % OWNER_FROM, 'from')
    return name


# ------------------------------------------------------------------ the Api
class Api(object):
    """The hub's verbs over `store` (a kitlib.hubsite.store.Store). `views` gives the derived
    views (module docstring) or is None; `clock` gives the time stamp of a write as an ISO
    string (default: the store's clock); `session_keys` gives the launcher's session keys as an
    iterable, called afresh on each use (default: launcher_keys); `now` gives monotonic seconds
    for the routes (default: time.monotonic). `approval_key()` gives the approval key or None,
    read on each decide and verify; `checkout(repo_id)` gives the root of this machine's checkout
    of that repository or None ("Approvals"); both default to None, which no approval passes.
    `repo_name(repo_id)` gives the name of this machine's configured repository with that remote,
    or None (default), which routes a decided approval to the PM alone ("Routes").
    `lock` is the machine lock's door or None ("Machine lock"). `schedule_repos()` gives the
    machine config's launcher.repos as [(label, main checkout, name)] (default:
    launcher_checkouts), `tick_seen()` the ISO time the automatic tasks' tick was last seen or
    None (default: tick_last_seen), `tick_state()` the tick's records by schedule key (default:
    tick_run_state); all are read on each use ("Schedules"). `kit_key()` gives the session key
    of the kit repository's coordinator, '' when none is known (default), so a submitted skill
    edit is the PM's; `kit_blob(branch, path)` gives the blob id of `path` on `branch` in this
    machine's kit checkout, or None when git cannot read it; with none given (default) every
    read is None, so the done route is refused; `kit_text(blob)` gives the text of that blob in
    the kit checkout (CRLF read as LF), or None (default: always None) ("Skill edits")."""

    def __init__(self, store, views=None, clock=None, session_keys=None, now=None,
                 approval_key=None, checkout=None, lock=None, repo_name=None,
                 schedule_repos=None, tick_seen=None, tick_state=None, kit_key=None,
                 kit_blob=None, kit_text=None):
        self.store = store
        self.kit_text = kit_text or (lambda blob: None)
        self.kit_key = kit_key or (lambda: '')
        self.kit_blob = kit_blob or (lambda branch, path: None)
        self.tick_state = tick_state or tick_run_state
        self.schedule_repos = schedule_repos or launcher_checkouts
        self.tick_seen = tick_seen or tick_last_seen
        self.views = views
        self.lock = lock
        self.lock_poll_s = LOCK_POLL_S
        self.clock = clock or store.clock
        self.approval_key = approval_key or (lambda: None)
        self.checkout = checkout or (lambda repo: None)
        self.repo_name = repo_name or (lambda repo: None)
        self._session_keys = session_keys or launcher_keys
        self._seen = {'waited': None, 'pulled': None}
        self._waits = {}            # one key per agent wait that is blocked right now
        self.push_status = None     # PushWorker.status, set by the worker ("Push status")
        # routes (module docstring "Routes"): one dict step per note, no lock
        self.now = now or time.monotonic
        self.ack_window_s = ACK_WINDOW_S
        self.reach_grace_s = REACH_GRACE_S
        self._listening = {}        # session key -> {wait token: True} for its blocked waits
        self._heard = {}            # session key -> `now` its last wait started or ended
        self._handed = {}           # (kind, id) -> (session key, `now` of the first hand-over)
        self._pushed = {}           # session key -> `now` of the latest push written to it
        self.watcher_status = None  # PushWorker.watcher_status, set by the worker ("Watcher")
        self.code_status = None     # Hub.code_state, set by serve.build ("Code")
        self.jobs_status = None     # jobs.Jobs.status, set by serve.build ("Jobs")

    def call(self, method, path, role, body=None, query=None, via='', raise_unexpected=False,
             agent_words=False, strong=False):
        """(status, object) for one request. role is OWNER or AGENT as the caller resolved it;
        body is the decoded JSON body or None; query is {name: value}; via names the transport
        (web, cli, ...) and is recorded on every write. With raise_unexpected an exception that
        is no HubError is raised, not answered: the caller logs it and words its own 500. With
        agent_words=True an error answer says what an agent is told (module docstring), to the
        owner too; only False leaves the owner the error's own text, so a value that is neither
        is read as True. The role decides what the request may do either way. strong=True says
        the caller is the owner logged in through Cloudflare Access (module docstring, "Roles");
        it counts only for OWNER, and any value but True is not strong."""
        # read before the handler copies any data: a write in between then has a later
        # revision, so a client waiting since this one is told of it
        revision = self.store.revision
        full = role == OWNER and agent_words is False       # the error's own text: paths and all
        strong = strong is True and role == OWNER
        try:
            status, obj = 200, self._call(method, path, role, body, query, via, strong,
                                          full and via != TUNNEL_VIA)
        except HubError as e:
            said = e.message if full else AGENT_SAYS.get(e.code, e.message)
            status, obj = STATUS.get(e.code, 500), error_body(e.code, said, e.field)
            extra = getattr(e, 'extra', None)   # what a route adds to its refusal (schedules_set's id)
            if isinstance(extra, dict):
                obj['error'].update((k, v) for k, v in extra.items()
                                    if k not in ('code', 'message', 'field'))
        except Exception as e:      # noqa: BLE001 - a transport gets an answer, never a traceback
            if raise_unexpected:
                raise
            said = '%s: %s' % (type(e).__name__, e) if full else type(e).__name__
            status, obj = 500, error_body('internal', said)
        obj.setdefault('revision', revision)
        return status, obj

    def _call(self, method, path, role, body, query, via, strong, full):
        if not isinstance(role, str) or role not in CALLERS:
            raise HubError('forbidden', 'this request carries no owner or agent credential',
                           'role')
        found = match(method, path)
        if found is None:
            raise HubError('not_found', 'there is no route %s %s' % (method, path), 'path')
        route, params = found
        if route.role not in ROLES:
            raise HubError('forbidden', 'the route %s %s names no role, so nobody may call it'
                           % (route.method, route.pattern), 'role')
        if route.role == APPROVER:
            if not (role == OWNER and strong is True):
                raise HubError('forbidden', NOT_STRONG % (route.method, route.pattern), 'role')
        elif route.role == LOCK_WRITE:
            if not (role == OWNER and (strong is True or via != TUNNEL_VIA)):
                raise HubError('forbidden', NOT_LOCK_WRITER % (route.method, route.pattern),
                               'role')
        elif route.role != ANY and route.role != role:
            raise HubError('forbidden', '%s %s is for the %s, not the %s'
                           % (route.method, route.pattern, route.role, role), 'role')
        if query is not None and not isinstance(query, dict):
            raise _invalid('the query is an object of names and values', 'query')
        out = route.handler(self, Request(role, params, body, query, via if isinstance(via, str)
                                          else '', strong, full is True))
        if not isinstance(out, dict):
            raise HubError('internal', 'the handler of %s gave no object' % route.pattern)
        return out

    # -------------------------------------------------------------- helpers for handlers
    def doc(self, coll, doc_id, b=None):
        """The document, HubError not_found when there is none, conflict when the body pins a
        version it is not at."""
        d = self.store.get(coll, doc_id) if schema.check_id(doc_id) is None else None
        if d is None:
            raise HubError('not_found', 'there is no %s/%s' % (coll, doc_id), 'id')
        pin = (b or {}).get('if_version')
        if pin is not None and pin != _version(d):
            raise HubError('conflict', '%s/%s is at version %d, not %d'
                           % (coll, doc_id, _version(d), pin), 'if_version')
        return d

    def add(self, coll, fields, req, extra=None):
        """A new document of fields, with the path the write came by (`via`)."""
        return self.written(self.store.add(coll, dict(fields, via=_via(req))), extra)

    def change(self, coll, doc_id, doc, changes, req, extra=None):
        """Merge changes into the document, pinned to the version of `doc` (the copy the
        handler judged), record the path the write came by (`via`), and give the write's
        result object."""
        r = self.store.update(coll, doc_id, dict(changes, via=_via(req)),
                              if_version=_version(doc))
        return self.written(r, extra)

    def written(self, r, extra=None):
        out = {'ok': True, 'id': r.id, 'version': r.version, 'revision': r.revision,
               'warnings': [f._asdict() for f in r.warnings]}
        out.update(extra or {})
        return out

    def items(self, coll, req, since=False):
        """The documents of coll as a list, each with its `id`, filtered by the query's status
        and repo (and since, a time, where the route takes it)."""
        allowed = hubvalidate.STATUSES.get(coll, ())
        status, repo = _q(req, 'status'), _q(req, 'repo').lower()
        if status == 'all':
            status = ''
        if status and status not in allowed:
            raise _invalid('status is one of %s, all; not %r' % (', '.join(allowed), status),
                           'status')
        after = _when(_q(req, 'since'), 'since') if since and _q(req, 'since') else None
        out = []
        for doc_id, d in self.store.docs(coll).items():
            if status and d.get('status') != status:
                continue
            if repo and str(d.get('repo') or '').strip().lower() != repo:
                continue
            if after is not None:
                at = _when(d.get('updated_at'), '') or _when(d.get('asked_at'), '')
                if at is None or at < after:
                    continue
            d['id'] = doc_id
            out.append(d)
        return sorted(out, key=lambda d: _natural(d['id']))

    def _view(self, name):
        fn = getattr(self.views, name, None) if self.views is not None else None
        if not callable(fn):
            raise HubError('unavailable', 'the %s view is not available on this server' % name,
                           'views')
        return fn

    def view(self, name, **kw):
        """What the view gives; whatever it raises is the caller's to answer for."""
        return self._view(name)(**kw)

    def shown(self, name, **kw):
        """view() for a route that is that one view: a view that raises is `unavailable` with
        the short reason, never its message."""
        fn = self._view(name)
        try:
            return fn(**kw)
        except Exception as e:      # noqa: BLE001 - a view is another module's code
            raise HubError('unavailable', 'the %s view failed: %s' % (name, _reason(e)), 'views')

    def session_keys(self):
        """The keys a question may come from: the launcher's session keys, read now, and
        AGENT_FROM (the PM's), which is known even when they cannot be read."""
        return self.read_session_keys()[0]

    def read_session_keys(self):
        """(session_keys(), why they could not be read or None): the reason is _reason's short
        one (an exception's class name, never its message or a path)."""
        try:
            return {k for k in self._session_keys() if isinstance(k, str)} | {AGENT_FROM}, None
        except Exception as e:      # noqa: BLE001 - unreadable: only the PM's key is known
            return {AGENT_FROM}, _reason(e)

    def kit_session_key(self):
        """kit_key(): the kit coordinator's session key, '' when it fails or gives no string
        ("Skill edits")."""
        try:
            key = self.kit_key()
        except Exception:           # noqa: BLE001 - no kit coordinator known: the PM hears it
            key = ''
        return key if isinstance(key, str) else ''

    def repo_key(self, repo):
        """The session key of the coordinator of the repository whose remote is the repo_id
        `repo`: the configured repository's name (a repo's main coordinator is keyed by its name
        in sessions.json), '' when none is or repo_name fails. Not checked against the session
        keys: the caller does that."""
        try:
            name = self.repo_name(repo) if isinstance(repo, str) and repo else None
        except Exception:           # noqa: BLE001 - no name, no coordinator: the PM hears it
            name = None
        return name if isinstance(name, str) else ''

    def pending(self, for_key=''):
        """The pending items, each with the session key it is for as `from`: a question's or a
        to-do's own `from` when that is a session key, else AGENT_FROM (so the owner's to-dos,
        free-text names, stories and notes are the PM's), and the key that is to handle it now
        as `route` (module docstring "Routes"). With for_key, only the items for that key; for
        AGENT_FROM, the items routed to it, every question and every approval. An approval's
        `from` is its repository's coordinator key (repo_key). Reading never changes an item:
        what a filtered read leaves out stays pending for every other reader."""
        return [d for d in self._routed()[0] if _for(d, for_key)]

    def route_change_in(self):
        """Seconds until the route of a pending item next turns to AGENT_FROM with no write
        (a hand-over's window or a key's grace running out), or None when none will."""
        at = self._routed()[1]
        return None if at is None else max(0.0, at - self.now())

    def _routed(self):
        """([item], `now` of the next route change or None): the pending items with `from` and
        `route`. Hand-overs of items no longer pending are forgotten here. Each approval's
        repository is resolved (repo_key) once per call, however many approvals name it."""
        found, keys, out, repo_keys = self.store.pending(), None, [], {}
        for p in found:
            d = p._asdict()
            sender = d.pop('sender')
            if d['kind'] == 'approval':
                # no `from`: its repository's coordinator
                repo = d['repo'] if isinstance(d['repo'], str) else ''
                if repo not in repo_keys:
                    repo_keys[repo] = self.repo_key(repo)
                sender = repo_keys[repo]
            elif d['kind'] == 'skill_edit':
                sender = self.kit_session_key()     # the kit's coordinator commits it
            mine = sender and sender not in (AGENT_FROM, OWNER_FROM)
            if mine and keys is None:
                keys = self.session_keys()          # read once, and only when it can matter
            d['from'] = sender if mine and sender in keys else AGENT_FROM
            if d['kind'] == 'question':
                q = self.store.get('questions', d['id']) or {}
                if d.get('note') == 'filed' and isinstance(q.get('standing'), dict):
                    d['standing'] = True        # the PM logs it too (_for)
                if not isinstance(q.get('record'), dict) and q.get('class') in QUESTION_CLASSES:
                    d['class'] = q['class']     # the PM audits a classed answer (hub.py pending)
            out.append(d)
        now, flips = self.now(), []
        live = {(d['kind'], d['id']) for d in out}
        for gone in [k for k in list(self._handed) if k not in live]:
            self._handed.pop(gone, None)
        for d in out:
            d['route'], flip = self._route(d, now)
            if flip is not None:
                flips.append(flip)
        return out, (min(flips) if flips else None)

    def _route(self, d, now):
        """(route, `now` it turns to AGENT_FROM or None) of item d."""
        key = d['from']
        if key == AGENT_FROM:
            return AGENT_FROM, None
        handed = self._handed.get((d['kind'], d['id']))
        if handed is not None and handed[0] == key:
            until = handed[1] + self.ack_window_s
            return (key, until) if now < until else (AGENT_FROM, None)
        if self._listening.get(key):
            return key, None
        heard, pushed = self._heard.get(key), self._pushed.get(key)
        until = max(heard + self.reach_grace_s if heard is not None else -math.inf,
                    pushed + self.ack_window_s if pushed is not None else -math.inf)
        if now < until:
            return key, until
        return AGENT_FROM, None

    def pushed(self, key):
        """Note that a push to session key `key` was written: its items route to it for
        ack_window_s (module docstring "Routes"). One dict step; the push worker's thread."""
        if isinstance(key, str) and key not in ('', AGENT_FROM):
            self._pushed[key] = self.now()

    def listen(self, req, opened=None, closed=None):
        """Note that a wait `for` a session key other than AGENT_FROM started (`opened`, a
        token for a wait that now blocks), ended (`closed`, that token) or was answered at once
        (neither): the key is reachable (module docstring "Routes"). The owner's calls are not
        noted."""
        key = _q(req, 'for')
        if req.role != AGENT or key in ('', AGENT_FROM):
            return
        self._heard[key] = self.now()
        waits = self._listening.setdefault(key, {})
        if opened is not None:
            waits[opened] = True
        if closed is not None:
            waits.pop(closed, None)

    def handed(self, req, items):
        """Note that items went to the session key the request is `for` (an agent's call, a key
        other than AGENT_FROM): each of that key's own is handed over, the first time only. An
        item it now holds within the ack window gets the key as its `route` in `items` too, so
        the answer says who has it after this call, not before."""
        key = _q(req, 'for')
        if req.role != AGENT or key in ('', AGENT_FROM):
            return
        now = self.now()
        for d in items:
            if d.get('from') == key:
                got = self._handed.setdefault((d['kind'], d['id']), (key, now))
                if got[0] == key and now < got[1] + self.ack_window_s:
                    d['route'] = key

    def handed_to(self, kind, doc_id):
        """The session key item (kind, doc_id) was first handed over to by handed(), or None.
        Read-only, one dict read: kitlib.hubsite.push calls it from its own thread to tell that
        the session a push went to picked the item up."""
        got = self._handed.get((kind, doc_id))
        return got[0] if got is not None else None

    def seen(self, req, what, opened=None, closed=None):
        """Note that an agent (a PM session) just waited or pulled (`what`). `opened` is a key
        for a wait that now blocks, `closed` the key of one that ended. The owner's calls are
        not noted, nor another session's (`for` a key other than AGENT_FROM). An ended wait
        stops counting before the clock is read, and a clock that fails then leaves the earlier
        time: it never hides how the wait itself ended."""
        if req.role != AGENT or _q(req, 'for') not in ('', AGENT_FROM):
            return
        if closed is not None:
            self._waits.pop(closed, None)
            try:
                self._seen[what] = self.clock()
            except Exception:       # noqa: BLE001 - the wait's own answer or error stands
                pass
            return
        self._seen[what] = self.clock()
        if opened is not None:
            self._waits[opened] = True

    def pm_seen(self):
        return {'waited': self._seen['waited'], 'pulled': self._seen['pulled'],
                'waiting': len(self._waits)}

    def push_seen(self):
        """The push worker's status, or None when this hub runs none (module docstring "Push
        status"). A status that raises is 'down' with its class name as `last`."""
        fn = self.push_status
        if fn is None:
            return None
        try:
            got = fn()
        except Exception as e:      # noqa: BLE001 - the snapshot answers whatever the worker does
            return {'state': 'down', 'last': type(e).__name__, 'undelivered': None}
        return got if isinstance(got, dict) else None

    def watcher_seen(self):
        """The PM watcher's state as the push worker last read it, or None when this hub runs
        no push worker (module docstring "Watcher"). A status that raises is 'down'."""
        fn = self.watcher_status
        if fn is None:
            return None
        try:
            got = fn()
        except Exception:           # noqa: BLE001 - the snapshot answers whatever the worker does
            return {'state': 'down', 'age': None}
        return got if isinstance(got, dict) else None

    def code_seen(self):
        """The server's `code` object (module docstring "Code"), or None. Never raises."""
        fn = self.code_status
        if fn is None:
            return None
        try:
            got = fn()
        except Exception:           # noqa: BLE001 - the snapshot and health answer whatever it does
            return None
        return got if isinstance(got, dict) else None

    def jobs_seen(self):
        """The timed jobs' health blocks (module docstring "Jobs"), or None. Never raises."""
        fn = self.jobs_status
        if fn is None:
            return None
        try:
            got = fn()
        except Exception:           # noqa: BLE001 - health answers whatever the jobs do
            return None
        return got if isinstance(got, dict) else None


# ------------------------------------------------------------------ handlers
def questions_list(api, req):
    return {'items': api.items('questions', req, since=True)}


RECORD_SHAPE = 'record is {"repo": <a configured repository>, "id": <one of its record ids>}'


def _holders(api, repo, rid):
    """The other configured repo names whose backlog has the record `rid` (a kit record is keyed
    on the PM repo's folder name, not the kit repo's own), for the not_found refusal. Best
    effort: a view that is missing or fails names none."""
    try:
        names = [r.get('name') for r in api._view('repos')() if isinstance(r, dict)]
    except Exception:       # noqa: BLE001 - a view is another module's code
        return []
    held = []
    for name in names:
        if not isinstance(name, str) or name.lower() == repo.lower():
            continue
        try:
            got = api._view('record')(name, rid)
        except Exception:   # noqa: BLE001
            continue
        if isinstance(got, dict) and got.get('where') == 'open':
            held.append(name)
    return held


def _bound(api, value):
    """The `record` a question is bound to, {repo, id}, after the configured repository's
    backlog was read now through the `record` view (docs/action-hub.md "questions/Q-NNN"): repo
    is the configured name, whatever its case was as given. Refused, field record: `invalid` for
    another shape, a name that can be no repository or record id, and a completed record;
    `not_found` for a repository that is not configured and a record its backlog does not have;
    `unavailable` when the record cannot be checked (no record view on this server, or a store
    that cannot be read). It fails closed: an answer is filed into this record, so a binding
    nobody checked is never stored. A record whose view says `writable` false (its repository
    names no backlog store the hub may write, kitlib.hubsite.backlog writable) is `invalid` too:
    its answer could never be filed (recordwrite answer, `no_store`)."""
    if not isinstance(value, dict) or sorted(value) != ['id', 'repo'] or not all(
            isinstance(value[k], str) and value[k].strip() for k in ('repo', 'id')):
        raise _invalid(RECORD_SHAPE, 'record')
    repo, rid = value['repo'].strip(), value['id'].strip()
    try:
        got = api._view('record')(repo, rid)
    except HubError as e:
        if e.code == 'invalid':
            raise _invalid('record: %s' % e.message, 'record')
        named = '%s:%s' % (repo, rid)
        if e.code == 'not_found' and e.field == 'repo':
            raise HubError('not_found', 'record names repo %s, which is not a repository the '
                           'hub has configured' % ascii(repo), 'record')
        if e.code == 'not_found':
            held = _holders(api, repo, rid)
            raise HubError('not_found', "record %s: repo %s's backlog has no record %s%s"
                           % (named, repo, rid, '; the repo key that holds it is %s' % ', '.join(held)
                              if held else ''), 'record')
        raise HubError('unavailable', 'record %s could not be checked: %s; ask again, or ask '
                       'without a record' % (named, e.message), 'record')
    except Exception as e:          # noqa: BLE001 - a view is another module's code
        raise HubError('unavailable', 'record %s:%s could not be checked: the record view '
                       'failed: %s' % (repo, rid, _reason(e)), 'record')
    got = got if isinstance(got, dict) else {}
    name = got.get('repo') if isinstance(got.get('repo'), str) else repo
    if got.get('where') != 'open':
        raise _invalid('record %s:%s is completed: bind a question to an open record'
                       % (name, rid), 'record')
    if got.get('writable') is False:
        raise _invalid('record %s:%s: repo %s names no backlog store the hub may write (.kit.json '
                       '"backlog".store), so its answer could not be filed: ask without a record'
                       % (name, rid, name), 'record')
    return {'repo': name, 'id': rid}


# The class a question bound to no record declares (docs/action-hub.md "A question's class"):
# a standing rule (a delegation run line too), the machine, work across repositories, or a
# one-off go (a landing, an archive). A bound question's answer is filed into its record instead.
QUESTION_CLASSES = ('rule', 'machine', 'cross-repo', 'go')


def _class(b, bound):
    """Check b's `class` for a question that is `bound` to a record or not, once the fields it
    ends with are known: refused `invalid`, field `class`, for a class on a bound question, a
    class not in QUESTION_CLASSES, and no class on an unbound one."""
    given = b.get('class')
    if bound:
        if given is not None:
            raise _invalid('a question bound to a record takes no class: its answer is filed '
                           'into the record', 'class')
        return
    if given is None:
        raise _invalid('a question needs --record REPO:ID or --class %s (one bound to no '
                       'record says what kind it is)' % '|'.join(QUESTION_CLASSES), 'class')
    if given not in QUESTION_CLASSES:
        raise _invalid('class is one of %s, not %s' % (', '.join(QUESTION_CLASSES),
                                                      ascii(given)), 'class')


def questions_add(api, req):
    """A new open question: `record` (the backlog record its answer is filed into) or `class`
    (_class) is required."""
    b = _fields(req, ('repo', 'from', 'title', 'context', 'options', 'recommended', 'refs',
                      'urgent', 'story', 'record', 'class'), required=('title',))
    for name in ('repo', 'from', 'title', 'context', 'story'):
        _text(b, name)
    # the session the answer goes back to: a launcher session key, read now, or the PM's
    who = (b.get('from') or '').strip() or AGENT_FROM
    if who != AGENT_FROM:
        keys, why = api.read_session_keys()
        if who not in keys:
            unread = ('; the launcher\'s session keys could not be read (%s), so only %s is '
                      'known' % (why, AGENT_FROM)) if why else ''
            raise _invalid('from %s is not a session the hub knows: a question comes from a '
                           'launcher session key (known: %s)%s'
                           % (ascii(who), ', '.join(sorted(keys)), unread), 'from')
    b['from'] = who
    _typed(b, 'options', (list,), 'a list of {label, description}')
    _class(b, 'record' in b)
    if 'record' in b:               # checked last: the one part that reads a backlog
        b['record'] = _bound(api, b['record'])
    b.setdefault('options', [])
    b.update(status='open', asked_at=api.clock())
    return api.add('questions', b, req)


def questions_answer(api, req):
    """The owner's answer: `choice`, an optional `note`, and `standing` true when the answer is
    to be a standing rule (a question bound to a record only: the PM logs it, docs/action-hub.md
    "A standing rule"); a re-answer with no `standing` drops the mark. `standing` true with no
    `choice` is the promote of an answer already given (_promote)."""
    allowed = ('choice', 'note', 'standing', 'if_version')
    b = _fields(req, allowed, flags=('standing',))
    standing = _flag(b, 'standing', False)
    doc_id = req.params['id']
    if standing and 'choice' not in b:
        _pin(b)
        if 'note' in b:
            raise _invalid('a note comes with a choice: standing alone keeps the answer given',
                           'note')
        return _promote(api, req, doc_id, b)
    _fields(req, allowed, required=('choice',))
    _pin(b)
    note = (_text(b, 'note') or '').strip()
    d = api.doc('questions', doc_id, b)
    if d.get('status') not in ('open', 'answered', 'filed'):
        raise _invalid('%s is %s: only an open, an answered or a filed question takes an answer'
                       % (doc_id, d.get('status')), 'status')
    if standing and not isinstance(d.get('record'), dict):
        raise _invalid('%s is bound to no record: an answer the PM handles is logged by the PM '
                       'already, so it takes no standing flag' % doc_id, 'standing')
    choice = b['choice']
    options = d.get('options') if isinstance(d.get('options'), list) else []
    if choice == 'other':
        if not note:
            raise _invalid('the choice "other" needs a note saying what', 'note')
        label = 'Other'
    elif isinstance(choice, int) and not isinstance(choice, bool) and 0 <= choice < len(options):
        o = options[choice]
        label = o.get('label') if isinstance(o, dict) and isinstance(o.get('label'), str) else ''
    else:
        raise _invalid('choice is an index into the %d option(s) or "other", not %r'
                       % (len(options), choice), 'choice')
    answer = {'choice': choice, 'label': label, 'note': note, 'at': api.clock(),
              'via': _via(req)}
    changes = {'status': 'answered', 'answer': answer}
    if (d.get('filing') is not None or d.get('filed_at') is not None
            or d.get('filed_as') is not None):
        changes.update(filing=None, filed_at=None, filed_as=None)   # a re-answer is filed afresh
    if standing:
        changes['standing'] = {'at': answer['at'], 'via': answer['via']}
    elif d.get('standing') is not None:
        changes['standing'] = None      # a re-answer without the box is no standing rule
    out = api.change('questions', doc_id, d, changes, req,
                     {'status': 'answered', 'answer': answer})
    if isinstance(d.get('record'), dict):
        got = file_answer(api, doc_id, _via(req), full=req.full)
        if got is not None:
            out['filing'] = got
            if 'version' in got:        # the mark is the last write: its version and revision
                out.update(version=got['version'], revision=got['revision'])
            if got.get('filed'):
                out['status'] = 'filed'
    return out


# what a question's `filing` says, by the code its filing failed with: fixed phrases, since the
# store's own text can name a path and the document goes over the tunnel (GET snapshot)
FILING_SAYS = {
    'not_found': 'the record is completed or gone: the PM handles this answer',
    'invalid': 'the backlog store refused this answer: the PM handles it',
    'store': 'the backlog store could not be written now: the hub tries again',
    'unavailable': 'this hub cannot write a backlog store: the hub tries again',
    'no_store': 'this repository names no backlog store the hub may write (.kit.json '
                '"backlog".store): the PM handles this answer',
}


def _waiting(api, doc_id, record):
    """The other questions bound to `record` that are still open, in id order."""
    return sorted((i for i, q in api.store.docs('questions').items()
                   if i != doc_id and q.get('status') == 'open' and q.get('record') == record),
                  key=_natural)


def _record_state(api, record):
    """The `state:` of the open record `record` ({repo, id}) read now through the `record`
    view ('open' when it has no state line), or None when it cannot be told: no view, a view
    that raised, no fields, or a completed record."""
    try:
        got = api._view('record')(record.get('repo'), record.get('id'))
    except Exception:               # noqa: BLE001 - a view is another module's code
        return None
    if not isinstance(got, dict) or got.get('where') != 'open':
        return None
    fields = got.get('fields')
    if not isinstance(fields, list):
        return None
    for pair in fields:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == 'state':
            return pair[1] if isinstance(pair[1], str) and pair[1] else 'open'
    return 'open'


def _filed_as(before, after):
    """What a filed answer did to its record, from the record's state before and after the
    filing (_record_state): `pulled` (someone is working on it), `freed` (it was parked or
    blocked and now is open: work can start), `held` (it waits on more), or None when a state
    could not be read (the push then treats it as before this field: docs/action-hub.md "Push
    notifications")."""
    if before == 'pulled':
        return 'pulled'
    if before is None or after is None:
        return None
    if before in ('parked', 'blocked') and after == 'open':
        return 'freed'
    return 'held'


def file_answer(api, doc_id, via='api', full=False):
    """File the stored answer of question doc_id into the record it is bound to, through the
    `record_answer` view, then mark the question (docs/action-hub.md "A question bound to a
    record"): status `filed` with `filed_at`, or, when the view refused, status `answered` kept
    with `filing` {at, code, message} (message a fixed phrase of FILING_SAYS), so the PM still
    sees it pending. Both marks are pinned to the version read first: a question that moved
    meanwhile (answered again, logged, done) is left as it is, and the answer that moved it is
    filed by its own call. None for a question that is not answered or is bound to no record;
    else {id, filed, version, revision} or {id, filed: false, code, message} (message the
    view's own text when `full`) and the version and revision of each mark written, or {id,
    filed: false, moved: true}. A mark the hub store refuses otherwise is given as a failure with
    that code, nothing marked. A failure with the code and phrase the question's `filing` has
    already writes nothing: its version and revision are those it has. The record write is
    idempotent (kitlib.hubsite.recordwrite answer), so a crash anywhere in between is mended by
    filing again."""
    d = api.store.get('questions', doc_id)
    if d is None or d.get('status') != 'answered':
        return None
    record, ans = d.get('record'), d.get('answer')
    if not isinstance(record, dict) or not isinstance(ans, dict):
        return None
    version = _version(d)
    before = _record_state(api, record)
    try:
        api._view('record_answer')(
            record.get('repo'), record.get('id'), doc_id, ans.get('at'), d.get('title'),
            ans.get('label'), ans.get('note'), ans.get('via'), ans.get('choice') == 'other',
            _waiting(api, doc_id, record))
    except HubError as e:
        code, said = e.code, e.message
    except Exception as e:          # noqa: BLE001 - a view is another module's code
        code, said = 'internal', _reason(e)
    else:
        code = said = None
    if code is None:
        changes = {'status': 'filed', 'filed_at': api.clock(), 'filing': None,
                   'filed_as': _filed_as(before, _record_state(api, record))}
    else:
        message = FILING_SAYS.get(code, 'the answer could not be filed (%s)' % code)
        was = d.get('filing')
        if isinstance(was, dict) and (was.get('code'), was.get('message')) == (code, message):
            # failed as it failed before: the mark it has stays, its first `at` kept, so a retry
            # moves no version an asker's --if-version holds
            now = api.store.get('questions', doc_id)
            if now is None or _version(now) != version:
                return {'id': doc_id, 'filed': False, 'moved': True}
            return {'id': doc_id, 'filed': False, 'code': code, 'version': version,
                    'revision': api.store.revision, 'message': said if full else message}
        changes = {'filing': {'at': api.clock(), 'code': code, 'message': message}}
    try:
        r = api.store.update('questions', doc_id, dict(changes, via=via), if_version=version)
    except HubError as e:
        if e.code == 'conflict':
            return {'id': doc_id, 'filed': False, 'moved': True}
        return {'id': doc_id, 'filed': False, 'code': e.code,
                'message': e.message if full else AGENT_SAYS.get(e.code, e.message)}
    marked = {'id': doc_id, 'version': r.version, 'revision': r.revision}
    if code is None:
        return dict(marked, filed=True)
    return dict(marked, filed=False, code=code,
                message=said if full else changes['filing']['message'])


def questions_file(api, req):
    """File every answered question bound to a record (or only `id`) that is not filed yet:
    {ok, filed: [ids], failed: [{id, code, message}]}; a question that moved meanwhile is in
    neither. An unknown `id` is `not_found`; one that is not answered or has no record files
    nothing."""
    b = _fields(req, ('id',))
    one = _text(b, 'id')
    if one is not None:
        api.doc('questions', one)
        ids = [one]
    else:
        ids = sorted(api.store.docs('questions'), key=_natural)
    filed, failed = [], []
    for doc_id in ids:
        got = file_answer(api, doc_id, _via(req), full=req.full)
        if got is None or got.get('moved'):
            continue
        if got['filed']:
            filed.append(doc_id)
        else:
            failed.append({'id': doc_id, 'code': got['code'], 'message': got['message']})
    return {'ok': True, 'filed': filed, 'failed': failed}


def _asked(text):
    """The number of a question id (`Q-12`, `Q-012`), or None."""
    m = re.match(r'Q-(\d+)\Z', text or '')
    return int(m.group(1)) if m else None


def _cited(api, repo, item):
    """([question numbers a record's hold cites, in order, once each], why it is not one to read
    or None) for an open item of the backlog view: a parked record's `q:` line, an owner-blocked
    record's `owner_ask`, `q`, `blocked_on` members and `Blocked on` body lines (the record
    view, read now). (None, None) for any other record."""
    from kitlib.backlog import record as brecord
    texts = []
    if item.get('s') == 'parked':
        texts.append(item.get('q') or '')
    elif item.get('s') == 'blocked':
        try:
            got = api._view('record')(repo, item.get('id'))
        except Exception as e:      # noqa: BLE001 - a view is another module's code
            return None, 'the record could not be read (%s)' % _reason(e)
        got = got if isinstance(got, dict) else {}
        if not any(b.get('kind') == 'owner' for b in got.get('blockers') or []
                   if isinstance(b, dict)):
            return None, None
        hold = got.get('hold') if isinstance(got.get('hold'), dict) else {}
        texts += [hold.get('owner_ask') or '', hold.get('q') or '']
        texts += [b.get('value') or '' for b in got.get('blockers') or [] if isinstance(b, dict)]
        texts += [l for l in (got.get('body') or '').split('\n') if l.startswith(brecord.BLOCK_NOTE)]
    else:
        return None, None
    nums = []
    for text in texts:
        for c in brecord.cites(text):
            n = _asked(c)
            if n is not None and n not in nums:
                nums.append(n)
    return nums, None


# the statuses questions bind binds a question from: still open, or answered and not yet handled
BINDS_FROM = ('open', 'answered')


def questions_bind(api, req):
    """POST questions/bind, the PM's alone (`from` pm, as questions_done checks a claim): pair
    each parked record's `q` and each owner-blocked record's cited question with an open hub
    question of the same number, and with `apply` true bind them (set `record`). The backlog is
    read fresh with `apply` (a fetch round) and from the held snapshot without, every configured
    repository. A pair whose record fails its check is in `failed` only. Nothing but an open or
    answered, unbound question is written (BINDS_FROM); a filed, logged or done one is a miss. An
    answered one is filed into its record at once (file_answer), its pair carrying `filing`
    {filed, code, message}. Answers {ok, applied, pairs: [{question, record, bound}],
    misses: [{record, why}], multi: [{record, questions}], skipped: [{question, why}],
    failed: [{question, code, message}]}: `multi` are records citing several questions, left
    alone; `skipped` a question already bound elsewhere or wanted by two records."""
    b = _fields(req, ('from', 'apply'), flags=('apply',))
    _text(b, 'from')
    claimed = (b.get('from') or '').strip() or AGENT_FROM
    if claimed != AGENT_FROM:
        raise HubError('forbidden', 'questions bind is the PM\'s: from is %s (%s was given)'
                       % (AGENT_FROM, ascii(claimed)), 'from')
    apply = _flag(b, 'apply', False)
    docs = api.store.docs('questions')
    numbered = {}
    for qid in docs:
        if _asked(qid) is not None:
            numbered[_asked(qid)] = qid
    found = []                      # (record, the one question number it cites)
    cites = collections.Counter()   # records citing each question number
    misses, multi = [], []
    for entry in api.shown('backlog', repo='', state='', fresh=apply):
        repo = entry.get('repo')
        if entry.get('error'):
            misses.append({'record': {'repo': repo, 'id': None},
                           'why': 'the repository could not be read'})
            continue
        for item in entry.get('items') or []:
            rec = {'repo': repo, 'id': item.get('id')}
            nums, why = _cited(api, repo, item)
            cites.update(nums or ())
            if why:
                misses.append({'record': rec, 'why': why})
            elif nums is None:
                continue
            elif not nums:
                if item.get('s') == 'parked':
                    misses.append({'record': rec, 'why': 'cites no question'})
            elif len(nums) > 1:
                multi.append({'record': rec, 'questions': [numbered.get(n, 'Q-%d' % n)
                                                           for n in nums]})
            else:
                found.append((rec, nums[0]))
    pairs, skipped = [], []
    for rec, n in found:
        qid = numbered.get(n)
        d = docs.get(qid) if qid else None
        if d is None:
            misses.append({'record': rec, 'why': 'Q-%d is not a hub question' % n})
        elif d.get('status') not in BINDS_FROM:
            misses.append({'record': rec, 'why': '%s is %s, not open or answered'
                           % (qid, d.get('status'))})
        elif cites[n] > 1:
            skipped.append({'question': qid, 'why': 'cited by more than one record'})
        elif d.get('record') == rec:
            skipped.append({'question': qid, 'why': 'already bound to this record'})
        elif d.get('record'):
            skipped.append({'question': qid, 'why': 'bound to another record'})
        else:
            pairs.append({'question': qid, 'record': rec, 'bound': False})
    failed = []
    if apply:
        for p in pairs:
            d = api.store.docs('questions').get(p['question'])
            try:
                if d is None or d.get('status') not in BINDS_FROM or d.get('record'):
                    raise HubError('conflict', '%s moved meanwhile' % p['question'], 'id')
                bound = _bound(api, p['record'])
                api.change('questions', p['question'], d, {'record': bound, 'class': None}, req)
                p['bound'] = True
                if d.get('status') == 'answered':
                    # an answer the PM has not handled yet: filed now, as a bound answer is
                    got = file_answer(api, p['question'], _via(req), full=req.full)
                    if got is None:         # no longer answered when the filing read it
                        p['filing'] = {'filed': False, 'unanswered': True}
                    else:
                        p['filing'] = {k: got[k] for k in ('filed', 'code', 'message', 'moved')
                                       if k in got}
            except HubError as e:
                failed.append({'question': p['question'], 'code': e.code,
                               'message': e.message if req.full else AGENT_SAYS.get(
                                   e.code, e.message)})
        lost = set(f['question'] for f in failed)
        pairs = [p for p in pairs if p['question'] not in lost]
    return {'ok': True, 'applied': apply, 'pairs': pairs, 'misses': misses, 'multi': multi,
            'skipped': skipped, 'failed': failed}


# The statuses a question is logged or marked done from: an answer the PM handles, and one filed
# into its record that its asker has not yet read and closed.
HANDLED_FROM = ('answered', 'filed')
# The filing codes no retry mends by itself (FILING_SAYS: the PM handles the answer); any other
# code, or no filing mark yet, may still file on the next filing call (questions_file).
FINAL_FILING = ('not_found', 'invalid', 'no_store')


def _unfiled(d, doc_id, b, verb):
    """The changes `unfiled` adds to log or done (`verb`) of question d, which an answer bound to
    a record and not filed must carry (docs/action-hub.md "Filing the answer"): refused
    `invalid`, field `filing`, while its filing may still succeed (a code not in FINAL_FILING, or
    none yet: file it first); field `unfiled` for a final code with no or a blank `unfiled`, and
    for `unfiled` on any question whose filing did not fail. Else {unfiled_why} or {}."""
    given = b.get('unfiled')
    record = d.get('record')
    if d.get('status') != 'answered' or not isinstance(record, dict):
        if given is not None:
            raise _invalid('%s has no answer left unfiled: only an answer whose filing failed '
                           'takes unfiled' % doc_id, 'unfiled')
        return {}
    named = '%s:%s' % (record.get('repo'), record.get('id'))
    filing = d.get('filing') if isinstance(d.get('filing'), dict) else {}
    code = filing.get('code')
    if code not in FINAL_FILING:
        raise _invalid('%s is bound to %s and its answer is not filed yet (%s): file it first '
                       '(hub.py questions file %s), then %s it'
                       % (doc_id, named, filing.get('message') or 'no filing has run', doc_id,
                          verb), 'filing')
    why = (given or '').strip()
    if not why:
        raise _invalid('%s\'s answer could not be filed into %s (%s): %s; unfiled says why it '
                       'is handled without its record' % (doc_id, named, code,
                                                          filing.get('message')), 'unfiled')
    return {'unfiled_why': why}


def questions_log(api, req):
    """POST questions/{id}/log: mark an answered or filed question logged (status `logged`), the
    PM having logged its answer in the ledger. No ledger id is taken or stored (an owner
    decision): a body with `ruling` is refused as an unknown field. Any other status, `logged`
    included, is refused (`invalid`, field status). An answer bound to a record and not filed is
    refused or needs `unfiled` (_unfiled), which is kept as `unfiled_why`."""
    b = _fields(req, ('if_version', 'unfiled'))
    _pin(b, required=True)
    _text(b, 'unfiled')
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    if d.get('status') not in HANDLED_FROM:
        raise _invalid('%s is %s: only an answered or filed question is logged'
                       % (doc_id, d.get('status')), 'status')
    changes = _unfiled(d, doc_id, b, 'log')
    changes.update(status='logged')
    return api.change('questions', doc_id, d, changes, req, {'status': 'logged'})


def questions_done(api, req):
    """The PM, or the asker of a filed question, marks an answered or filed question handled
    with no ruling (a one-off go the ledger keeps out; an answer the record already holds):
    status `done`, terminal, so it is no longer pending and its push ends. `from` is the
    caller's session key (AGENT_FROM, the PM, when none): refused `forbidden` (field from)
    unless it is the question's own `from` or the PM's, and before anything else about the
    question is said or written. Like _creator it is a claim checked against the item, not
    proof (all agents share one token). Refused (`invalid`) for a question that is neither
    answered nor filed, for a blank why and for a standing rule (the PM logs it). An answer bound
    to a record and not filed is refused or needs `unfiled` (_unfiled), kept as `unfiled_why`."""
    b = _fields(req, ('why', 'from', 'if_version', 'unfiled'), required=('why',))
    _pin(b, required=True)
    _text(b, 'from')
    _text(b, 'unfiled')
    why = _text(b, 'why').strip()
    if not why:
        raise _invalid('why says why the answer needs no ruling: it cannot be blank', 'why')
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    claimed = (b.get('from') or '').strip() or AGENT_FROM
    asker = d.get('from') or AGENT_FROM
    if claimed not in (asker, AGENT_FROM):
        raise HubError('forbidden', '%s is from %s: only its asker or %s can mark it done '
                       '(%s was given)' % (doc_id, asker, AGENT_FROM, ascii(claimed)), 'from')
    if d.get('status') not in HANDLED_FROM:
        raise _invalid('%s is %s: only an answered or filed question is marked done'
                       % (doc_id, d.get('status')), 'status')
    if isinstance(d.get('standing'), dict):
        # the asker's routine done of a filed answer would end the PM's push unlogged
        raise _invalid('%s is a standing rule the owner made: the PM logs it in the ledger '
                       '(hub questions log), it is not marked done' % doc_id, 'standing')
    changes = _unfiled(d, doc_id, b, 'mark done')
    changes.update(status='done', done_why=why, done_at=api.clock())
    return api.change('questions', doc_id, d, changes, req, {'status': 'done'})


def _promote(api, req, doc_id, b):
    """POST questions/{id}/answer with `standing` true and no `choice`: the owner makes the
    answer already given to a question bound to a record a standing rule (the filed card's
    promote button), the answer kept as it is and not filed again: `standing` {at, via}, so the
    question is pending for the PM, who logs it into the ledger, as well as for its asker
    (docs/action-hub.md "A standing rule"). Refused (`invalid`) for a question that is not
    answered or filed, one bound to no record (the PM logs every such answer already) and one
    already standing."""
    d = api.doc('questions', doc_id, b)
    if d.get('status') not in HANDLED_FROM:
        raise _invalid('%s is %s: only an answered or filed question is made a standing rule'
                       % (doc_id, d.get('status')), 'status')
    if not isinstance(d.get('record'), dict):
        raise _invalid('%s is bound to no record: the PM logs its answer already' % doc_id,
                       'record')
    if isinstance(d.get('standing'), dict):
        raise _invalid('%s is a standing rule already' % doc_id, 'standing')
    standing = {'at': api.clock(), 'via': _via(req)}
    return api.change('questions', doc_id, d, {'standing': standing}, req,
                      {'standing': standing})


def todos_list(api, req):
    return {'items': api.items('todos', req)}


def todo_steps(b, judge=True):
    """The fields b of a new to-do with its `steps` (kitlib.hubsite.todosteps) made into text:
    their commands after the given ones, their summary at the end of the detail (or the detail,
    when there is none), and no `steps` field left. Refused (`invalid`, the field named): a
    `commands` that is not a list, a given command holding a lone surrogate, a bad step and, when
    `judge` (an agent's to-do or edit), text the owner's to-do rules forbid (todosteps.check).
    The rules judge the text as given, before any step is rendered: the owner's text is never
    judged. An `os` field ('windows' by default, or 'linux') names the OS the steps' per-OS
    commands are for; it is not kept, and an unknown one is refused (`invalid`, field `os`).
    The caller says it: a machine-OS source is left for a later slice."""
    b = dict(b)
    steps = b.pop('steps', None)
    os_ = b.pop('os', todosteps.DEFAULT_OS)
    commands = _typed(b, 'commands', (list,), 'a list of strings')
    for i, c in enumerate(commands or ()):
        m = _SURROGATE.search(c) if isinstance(c, str) else None
        if m:
            raise _invalid('commands[%d] holds a lone surrogate (U+%04X) at character %d, half '
                           'of a character that cannot be written'
                           % (i, ord(m.group()), m.start()), 'commands')
    try:
        todosteps.check_os(os_)
        if judge:
            todosteps.check(dict(b, steps=steps))
        if steps is not None:
            b['commands'] = list(commands or []) + todosteps.render(steps, os_)
            lines = '\n'.join(todosteps.summary(steps, os_))
            b['detail'] = (b['detail'].rstrip() + '\n\n' + lines) if b.get('detail') else lines
    except (todosteps.StepError, todosteps.Refused) as e:
        raise _invalid(str(e), e.field)
    return b


def todos_add(api, req):
    owner = req.role == OWNER
    names = ('title', 'detail', 'commands', 'steps', 'os', 'repo', 'refs', 'urgent')
    b = _fields(req, names if owner else names + ('from',), required=('title',),
                flags=('urgent',))
    for name in ('title', 'detail', 'repo', 'from'):
        _text(b, name)
    b = todo_steps(b, judge=not owner)
    if owner:
        b['from'] = OWNER_FROM
    else:
        b['from'] = _agent_from(b.get('from') or '')
    b.update(status='open', added_at=api.clock())
    return api.add('todos', b, req)


def todos_done(api, req):
    b = _fields(req, ('done', 'if_version'), flags=('done',))
    _pin(b)
    done = _flag(b, 'done', True)
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    changes = {'status': 'done' if done else 'open', 'done_at': api.clock() if done else None}
    out = api.change('todos', doc_id, d, changes, req, changes)
    # a reopened or re-done to-do is pending afresh: an old hand-over must not outlive it when
    # no read ran in between (_routed forgets only what it sees gone)
    api._handed.pop(('todo', doc_id), None)
    return out


def todos_ack(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    return api.change('todos', doc_id, d, {'pm_ack': True}, req, {'pm_ack': True})


def _own(doc_id, d, what):
    who = d.get('from')
    if who != OWNER_FROM:           # exactly what todos_add writes for the owner
        raise HubError('forbidden', '%s is from %s: the owner can %s only a to-do the owner '
                       'added (from: %s)' % (doc_id, who or 'nobody', what, OWNER_FROM), 'from')


def _creator(b, doc_id, d, what):
    """An agent's edit or withdrawal: refused (`forbidden`, field from) unless the `from` the
    agent gives is the item's own (AGENT_FROM when the item has none) and the item is not the
    owner's. All agents share one token, so this is a claim checked against the item: it stops a
    mistake, not an agent that gives another's name."""
    stored = d.get('from') or AGENT_FROM
    claimed = (b.get('from') or '').strip()
    if stored == OWNER_FROM or claimed != stored:
        raise HubError('forbidden', '%s is from %s: an agent can %s only an item it added, '
                       'giving that item\'s from (%s was given)'
                       % (doc_id, stored, what, ascii(claimed)), 'from')


def _still_open(doc_id, d, what):
    """Refused (`conflict`, field status) once the item is no longer open: an answered, logged
    or done item is the owner's record and is not changed under it."""
    if d.get('status') != 'open':
        raise HubError('conflict', '%s is %s: only an open item can be %s'
                       % (doc_id, d.get('status'), what), 'status')


def _withdrawn(api, coll, doc_id, d):
    r = api.store.delete(coll, doc_id, if_version=_version(d))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


def questions_edit(api, req):
    names = ('title', 'context', 'options', 'recommended', 'refs', 'urgent', 'record', 'class')
    # record null or "" clears the binding
    b = _fields(req, names + ('from', 'if_version'), required=('from',), flags=('urgent',),
                blank=('record',))
    _pin(b, required=True)
    for name in ('from', 'title', 'context'):
        _text(b, name)
    _typed(b, 'options', (list,), 'a list of {label, description}')
    changes = {k: v for k, v in b.items() if k not in ('from', 'if_version')}
    if not changes:
        raise _invalid('give at least one of %s to change' % ', '.join(names), 'body')
    if 'title' in changes and not changes['title'].strip():
        raise _invalid('title cannot be made empty', 'title')
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    _creator(b, doc_id, d, 'edit')
    _still_open(doc_id, d, 'edited')
    if changes.get('record') == '':
        changes['record'] = None
    if 'record' in changes or 'class' in changes:
        # what the question ends with: bound and classless, or unbound with a class (_class);
        # an edit of neither leaves a question asked before classes as it is
        record = changes['record'] if 'record' in changes else d.get('record')
        if record:
            _class(changes, True)
            changes['class'] = None
        else:
            _class({'class': changes['class'] if 'class' in changes else d.get('class')}, False)
    if changes.get('record'):       # only once the asker and the status passed: no git before
        changes['record'] = _bound(api, changes['record'])
    return api.change('questions', doc_id, d, changes, req, {'status': 'open'})


def questions_delete(api, req):
    owner = req.role == OWNER
    b = _fields(req, ('if_version',) if owner else ('from', 'if_version'),
                required=() if owner else ('from',))
    _pin(b, required=not owner)
    _text(b, 'from')
    doc_id = req.params['id']
    d = api.doc('questions', doc_id, b)
    if not owner:
        _creator(b, doc_id, d, 'withdraw')
    _still_open(doc_id, d, 'withdrawn')
    return _withdrawn(api, 'questions', doc_id, d)


def todos_edit(api, req):
    owner = req.role == OWNER
    names = ('title', 'detail', 'commands', 'repo', 'refs', 'urgent')
    b = _fields(req, names + (('if_version',) if owner else ('from', 'if_version')),
                required=() if owner else ('from',), flags=('urgent',),
                blank=('repo', 'detail'))
    _pin(b, required=not owner)
    for name in ('title', 'detail', 'repo', 'from'):
        _text(b, name)
    for name in ('commands', 'refs'):
        _typed(b, name, (list,), 'a list')
    changes = {k: v for k, v in b.items() if k not in ('from', 'if_version')}
    if not changes:
        raise _invalid('give at least one of %s to change' % ', '.join(names), 'body')
    if 'title' in changes and not changes['title'].strip():
        raise _invalid('title cannot be made empty', 'title')
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    if owner:
        _own(doc_id, d, 'edit')
    else:
        _creator(b, doc_id, d, 'edit')
        _still_open(doc_id, d, 'edited')
        # the text an agent gives is judged by the rules its add was (todo_steps)
        changes = todo_steps(changes, judge=True)
    if d.get('pm_ack'):             # the PM read the old text: the changed one is pending again
        changes['pm_ack'] = False
    return api.change('todos', doc_id, d, changes, req, {'status': d.get('status')})


def todos_delete(api, req):
    owner = req.role == OWNER
    b = _fields(req, ('if_version',) if owner else ('from', 'if_version'),
                required=() if owner else ('from',))
    _pin(b, required=not owner)
    _text(b, 'from')
    doc_id = req.params['id']
    d = api.doc('todos', doc_id, b)
    if not owner:
        _creator(b, doc_id, d, 'withdraw')
        _still_open(doc_id, d, 'withdrawn')
    elif d.get('from') != OWNER_FROM:
        # the owner deletes a to-do of their own at any status, an agent's only while open
        _still_open(doc_id, d, 'deleted')
    return _withdrawn(api, 'todos', doc_id, d)


def stories_list(api, req):
    return {'items': api.items('stories', req)}


def stories_add(api, req):
    b = _fields(req, ('repo', 'title', 'story', 'urgent'), required=('repo', 'title', 'story'))
    for name in ('repo', 'title', 'story'):
        _text(b, name)
    b.update({'status': 'submitted', 'from': OWNER_FROM, 'submitted_at': api.clock()})
    return api.add('stories', b, req, {'status': 'submitted'})


def stories_set(api, req):
    b = _fields(req, ('status', 'reviewer', 'review_note', 'backlog_id', 'closed_note',
                      'if_version'), required=('status',))
    _pin(b, required=True)
    for name in ('status', 'reviewer', 'review_note', 'backlog_id', 'closed_note'):
        _text(b, name)
    status = b['status']
    if status not in AGENT_STORY_STATUSES:
        raise _invalid('status is one of %s, not %r (submitted and withdrawn are the owner\'s)'
                       % (', '.join(AGENT_STORY_STATUSES), status), 'status')
    doc_id = req.params['id']
    d = api.doc('stories', doc_id, b)
    if d.get('status') == 'withdrawn':
        raise _invalid('%s was withdrawn by the owner' % doc_id, 'status')
    changes = {k: v for k, v in b.items() if k != 'if_version'}
    if status == 'added':
        changes['added_at'] = api.clock()
    if status == 'declined':
        changes['closed_at'] = api.clock()
    return api.change('stories', doc_id, d, changes, req, {'status': status})


def stories_withdraw(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('stories', doc_id, b)
    if d.get('status') != 'submitted':
        raise _invalid('%s is %s: only a submitted story can be withdrawn'
                       % (doc_id, d.get('status')), 'status')
    return api.change('stories', doc_id, d, {'status': 'withdrawn', 'closed_at': api.clock()},
                      req, {'status': 'withdrawn'})


def to_pm_list(api, req):
    """The notes; an act note's text and refs with no absolute path but for the local owner
    (Request.full), as for its finding (_seen)."""
    items = api.items('to_pm', req)
    if not req.full:
        for d in items:
            _unpath_act(d)
    return {'items': items}


def _unpath_act(d):
    """Note d (a copy) with an act note's text and refs cut as _unpathed; any other note as
    it is: the owner's own words are the PM's to read whole."""
    if isinstance(d, dict) and d.get('kind') == 'act':
        if isinstance(d.get('text'), str):
            d['text'] = _unpathed(d['text'])
        if isinstance(d.get('refs'), list):
            d['refs'] = [_unpathed(r) if isinstance(r, str) else r for r in d['refs']]


def to_pm_add(api, req):
    b = _fields(req, ('kind', 'repo', 'urgent', 'text', 'refs', 'finding'))
    for name in ('kind', 'repo', 'text'):
        _text(b, name)
    b.setdefault('kind', 'question')
    _one_of(b, 'kind', schema.KINDS['to_pm'])
    refs = _typed(b, 'refs', (list,), 'a list of strings')
    if refs is not None and not all(isinstance(r, str) and r.strip() for r in refs):
        raise _invalid('refs is a list of non-empty strings', 'refs')
    if b['kind'] == 'act':
        _act_note(api, b, req)
    elif 'finding' in b:
        raise _invalid('finding is for an act note (kind act), not a %s one' % b['kind'],
                       'finding')
    elif not (b.get('text') or '').strip():
        raise _invalid('text is required', 'text')
    b.update(status='queued', created_at=api.clock(), sent_at=None, reply=None, done_at=None)
    return api.add('to_pm', b, req, {'status': 'queued'})


def _queued(doc_id, d, what):
    if d.get('status') != 'queued':
        raise HubError('conflict', '%s is %s: only a queued note, one not yet sent to the PM, '
                       'can be %s' % (doc_id, d.get('status'), what), 'status')


def to_pm_edit(api, req):
    b = _fields(req, ('kind', 'repo', 'urgent', 'text', 'if_version'), flags=('urgent',),
                blank=('repo',))
    _pin(b)
    for name in ('kind', 'repo', 'text'):
        _text(b, name)
    changes = {k: v for k, v in b.items() if k != 'if_version'}
    if not changes:
        raise _invalid('give at least one of kind, repo, urgent, text to change', 'body')
    if 'text' in changes and not changes['text'].strip():
        raise _invalid('text cannot be made empty', 'text')
    doc_id = req.params['id']
    d = api.doc('to_pm', doc_id, b)
    _queued(doc_id, d, 'edited')
    kind = changes.get('kind', d.get('kind'))
    if kind != d.get('kind') and 'act' in (kind, d.get('kind')):
        raise _invalid('an act note stays an act note and no other note becomes one: %s is %s'
                       % (doc_id, d.get('kind')), 'kind')
    return api.change('to_pm', doc_id, d, changes, req, {'status': 'queued'})


def to_pm_delete(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('to_pm', doc_id, b)
    _queued(doc_id, d, 'deleted')
    r = api.store.delete('to_pm', doc_id, if_version=_version(d))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


def _refusal(docs, coll, doc_id, changes):
    """Why the store would refuse to send a queued note: to merge changes (the status `sent`,
    its time, via) into coll/doc_id of docs (a snapshot's collections), as a HubError, or None.
    It is the schema half of the store's rule (Store._validate), asked before anything is
    written. The other half, a new hubvalidate error or REFUSED finding, cannot arise here: of
    a to_pm note hubvalidate judges the status (`sent` is one), its refs and its repo (warnings
    both), and the changes touch none of the last two."""
    new = dict(docs[coll][doc_id], **changes)
    errors = schema.check(coll, new, doc_id)
    if errors:
        return HubError('invalid', '%s/%s: %s' % (coll, doc_id,
                                                  '; '.join(f.message for f in errors)),
                        errors[0].field)
    return None


def to_pm_send(api, req):
    _fields(req, ())
    at = api.clock()
    changes = {'status': 'sent', 'sent_at': at, 'via': _via(req)}
    _, docs = api.store.snapshot()
    queued = sorted((i for i, d in docs['to_pm'].items() if d.get('status') == 'queued'),
                    key=_natural)
    for doc_id in queued:           # every one is checked before any is sent: a bad one sends none
        why = _refusal(docs, 'to_pm', doc_id, changes)
        if why:
            raise why
    out = {'ok': True, 'sent': queued, 'sent_at': at if queued else None}
    for doc_id in queued:
        r = api.store.update('to_pm', doc_id, changes,
                             if_version=_version(docs['to_pm'][doc_id]))
        out['revision'] = r.revision
    return out


def to_pm_reply(api, req):
    b = _fields(req, ('reply', 'if_version'))
    _pin(b)
    changes = {'status': 'done', 'done_at': api.clock(), 'reply': _text(b, 'reply')}
    doc_id = req.params['id']
    d = api.doc('to_pm', doc_id, b)
    if d.get('status') != 'sent':
        raise _invalid('%s is %s: only a sent item is replied to' % (doc_id, d.get('status')),
                       'status')
    return api.change('to_pm', doc_id, d, changes, req, {'status': 'done'})


def inbox_add(api, req):
    b = _fields(req, ('ref', 'asked', 'text', 'title', 'repo', 'from', 'refs'),
                required=('text',))
    for name in ('ref', 'asked', 'text', 'title', 'repo', 'from'):
        _text(b, name)
    if 'from' in b:                 # the route is an agent's: the same rule as a to-do's from
        b['from'] = _agent_from(b['from'])
    b.update(at=api.clock(), read=False)
    return api.add('inbox', b, req)


def inbox_read(api, req):
    b = _fields(req, ('read', 'if_version'), flags=('read',))
    _pin(b)
    read = _flag(b, 'read', True)
    doc_id = req.params['id']
    d = api.doc('inbox', doc_id, b)
    changes = {'read': read, 'read_at': api.clock() if read else None}
    return api.change('inbox', doc_id, d, changes, req, changes)


# approvals: imported here, not at the top, as kitlib.hubsite.approvals imports this module
def _approval(make, *args):
    from . import approvals as ap
    try:
        return make(*args)
    except ap.ApprovalError as e:
        raise HubError(e.code, e.message, e.field)


def approvals_add(api, req):
    from . import approvals as ap
    kind = req.body.get('kind') if isinstance(req.body, dict) else None
    if kind is not None:
        if kind != ap.KIND_OP:
            raise _invalid('kind is %r or absent, not %r' % (ap.KIND_OP, kind), 'kind')
        b = _fields(req, ('kind', 'op', 'repo', 'line', 'title', 'reason', 'what'))
        doc = _approval(ap.new_op_doc, b.get('op'), b.get('repo'), b.get('line'), b.get('title'),
                        _when(api.clock(), ''), b.get('reason'), b.get('what'))
        return api.add(ap.COLLECTION, doc, req)
    b = _fields(req, ('repo', 'title', 'head', 'files', 'reason'))
    doc = _approval(ap.new_doc, b.get('repo'), b.get('title'), b.get('head'), b.get('files'),
                    _when(api.clock(), ''), b.get('reason'))
    root = api.checkout(doc['repo'])
    if not root:
        raise _invalid('this machine has no checkout of %s' % doc['repo'], 'repo')
    lines = []
    for f in doc['files']:
        base, why = ap.read_base(root, doc['head'], f['path'], f['base_blob'])
        if why:
            raise _invalid('%s: %s' % (f['path'], why), 'files')
        lines.extend(ap.diff(f['path'], base, f['content']))
    doc['diff'] = lines
    return api.add(ap.COLLECTION, doc, req)


def approvals_decide(api, req):
    from . import approvals as ap
    b = _fields(req, ('decision', 'if_version'))
    _pin(b, required=True)
    doc_id = req.params['id']
    d = api.doc(ap.COLLECTION, doc_id, b)
    changes = _approval(ap.decide_fields, api.approval_key(), doc_id, d, b.get('decision'),
                        _when(api.clock(), ''))
    return api.change(ap.COLLECTION, doc_id, d, changes, req, {'status': changes['status']})


def approvals_consume(api, req):
    from . import approvals as ap
    b = _fields(req, ('commit', 'args_sha256', 'if_version'))
    _pin(b, required=True)
    if 'commit' in b and 'args_sha256' in b:
        raise _invalid('give commit (a change) or args_sha256 (an operation), not both',
                       'args_sha256')
    doc_id = req.params['id']
    d = api.doc(ap.COLLECTION, doc_id, b)
    if 'args_sha256' in b:
        changes = _approval(ap.consume_op_fields, api.approval_key(), doc_id, d,
                            b['args_sha256'], _when(api.clock(), ''))
    else:
        changes = _approval(ap.consume_fields, api.approval_key(), doc_id, d, b.get('commit'),
                            _when(api.clock(), ''))
    return api.change(ap.COLLECTION, doc_id, d, changes, req, {'status': changes['status']})


def approvals_delete(api, req):
    """The requester takes back a pending request: the `from` is a claim checked as for a
    question's withdrawal (an approval has none stored, so it is `pm`)."""
    from . import approvals as ap
    b = _fields(req, ('from', 'if_version'), required=('from',))
    _pin(b, required=True)
    _text(b, 'from')
    doc_id = req.params['id']
    d = api.doc(ap.COLLECTION, doc_id, b)
    _creator(b, doc_id, d, 'withdraw')
    _approval(ap.withdraw_check, d, _when(api.clock(), ''))
    return _withdrawn(api, ap.COLLECTION, doc_id, d)


def approvals_verify(api, req):
    from . import approvals as ap
    doc_id = req.params['id']
    d = api.store.get(ap.COLLECTION, doc_id) if schema.check_id(doc_id) is None else None
    if d is None:
        return {'ok': True, 'valid': False, 'why': ap.WHY_UNKNOWN}
    if _q(req, 'kind') == ap.KIND_OP:
        valid, why = ap.check_op(api.approval_key(), doc_id, d, _q(req, 'op'), _q(req, 'repo'),
                                 _q(req, 'args'), _when(api.clock(), ''))
        return {'ok': True, 'valid': valid, 'why': why, 'version': _version(d)}
    try:
        blobs = json.loads(_q(req, 'files'))
    except ValueError:
        blobs = None                # check() answers WHY_FILES
    # a commit given, even empty, is checked: only an absent one leaves it out
    commit = _q(req, 'commit') if 'commit' in (req.query or {}) else None
    valid, why = ap.check(api.approval_key(), doc_id, d, _q(req, 'repo'), _q(req, 'head'), blobs,
                          _when(api.clock(), ''), commit)
    return {'ok': True, 'valid': valid, 'why': why}


def backlog(api, req):
    return {'repos': api.shown('backlog', repo=_q(req, 'repo'), state=_q(req, 'state'),
                               fresh=_q_flag(req, 'fresh'))}


def completed(api, req):
    return {'repos': api.shown('completed', repo=_q(req, 'repo'),
                               days=_q_int(req, 'days', 1, DAYS_MAX))}


# A record route's path segments, checked before the view is asked: what a repository name or a
# record id may be (kitlib.hubsite.backlog checks each again, more narrowly). The refusal is a
# fixed phrase: it never repeats the segment.
_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
SEGMENT_SAYS = {'repo': 'not a repository name', 'id': 'not a record id', 'name': 'not a skill name'}


def _segment(req, name):
    value = req.params.get(name)
    if not isinstance(value, str) or not _SEGMENT.match(value) or '..' in value:
        raise HubError('invalid', SEGMENT_SAYS[name], name)
    return value


def _record_view(api, name, *args):
    """The record view `name` called with args: its HubError (not_found, invalid, unavailable,
    each with a fixed phrase) is the answer; anything else it raises is `unavailable` with the
    short reason. Unlike `shown`, a not_found stays a 404."""
    fn = api._view(name)
    try:
        return fn(*args)
    except HubError:
        raise
    except Exception as e:      # noqa: BLE001 - a view is another module's code
        raise HubError('unavailable', 'the %s view failed: %s' % (name, _reason(e)), 'views')


def skill_get(api, req):
    return _record_view(api, 'skill', _segment(req, 'name'))


def skill_source_get(api, req):
    """The skill's whole SKILL.md and its blob, what the owner edits ("Skill edits")."""
    return _skill_source(api, _segment(req, 'name'))


def record(api, req):
    return _record_view(api, 'record', _segment(req, 'repo'), _segment(req, 'id'))


def record_new(api, req):
    return _record_view(api, 'record_template', _segment(req, 'repo'))


# ---- the owner's record writes (docs/action-hub.md "The record page")
def _strings(b, name):
    """b[name] as a list of strings: absent is [], one string is a list of one."""
    v = b.get(name)
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise _invalid('%s must be a list of strings' % name, name)
    return v


def record_add(api, req):
    repo = _segment(req, 'repo')
    b = _fields(req, ('id', 'summary', 'scope', 'effort', 'ref'), ('id', 'summary', 'scope'))
    out = _record_view(api, 'record_add', repo, _text(b, 'id'), _text(b, 'summary'),
                       _strings(b, 'scope'), _text(b, 'effort'), _text(b, 'ref'))
    return dict(out, ok=True)


def record_edit(api, req):
    repo, rid = _segment(req, 'repo'), _segment(req, 'id')
    b = _fields(req, ('version', 'changes', 'unset', 'body'), ('version',))
    changes = _typed(b, 'changes', (dict,), 'an object of field names and values') or {}
    out = _record_view(api, 'record_edit', repo, rid, _text(b, 'version'),
                       [(k, _strings(changes, k)) for k in changes], _strings(b, 'unset'),
                       _text(b, 'body'))
    return dict(out, ok=True)


def record_delete(api, req):
    repo, rid = _segment(req, 'repo'), _segment(req, 'id')
    b = _fields(req, ('why', 'confirm'), ('why', 'confirm'))
    if _text(b, 'confirm') != rid:
        raise _invalid('confirm must repeat the record id to delete it', 'confirm')
    out = _record_view(api, 'record_delete', repo, rid, _text(b, 'why'))
    return dict(out, ok=True)


def record_unblock(api, req):
    repo, rid = _segment(req, 'repo'), _segment(req, 'id')
    b = _fields(req, ('on', 'reason'))
    out = _record_view(api, 'record_unblock', repo, rid, _text(b, 'on'), _text(b, 'reason'))
    return dict(out, ok=True)
# ---- end of the owner's record writes


def _listing(name):
    def handler(api, req):
        return {'items': api.shown(name)}
    handler.__name__ = name
    return handler


def pending(api, req):
    items = api.pending(_q(req, 'for'))
    api.seen(req, 'pulled')
    api.handed(req, items)
    return {'items': _unpath_pending(api, items, req)}


def _unpath_pending(api, items, req):
    """Pending items (fresh dicts from api.pending) with an act note's title, its text's first
    line, cut as _unpathed but for the local owner (Request.full), as GET to_pm cuts its text.
    The note's kind is the one read with the item (its `note`, dropped here for every caller),
    never read again: a note deleted since is still known for what it was. A question filed
    into its record says so: `filed` true."""
    for d in items:
        note = d.pop('note', '')
        if d.get('kind') == 'question' and note == 'filed':
            d['filed'] = True
        if (not req.full and d.get('kind') == 'to_pm' and note == 'act'
                and isinstance(d.get('title'), str)):
            d['title'] = _unpathed(d['title'])
    return items


def _timeout(req):
    """A wait's `timeout` query: seconds, WAIT_MAX_S when not given; not capped here."""
    raw = _q(req, 'timeout')
    try:
        timeout = float(raw) if raw else float(WAIT_MAX_S)
    except ValueError:
        timeout = float('nan')
    if not math.isfinite(timeout) or timeout < 0:
        raise _invalid('timeout is a number of seconds from 0 to %d, not %r' % (WAIT_MAX_S, raw),
                       'timeout')
    return timeout


def wait(api, req):
    since = _q_int(req, 'since', 0, 10 ** 12)
    if since is None:
        since = api.store.revision
    timeout = _timeout(req)
    want = _q_flag(req, 'pending')
    for_key = _q(req, 'for')
    if for_key and not want:
        raise _invalid('for picks among the pending items: give pending=1 with it', 'for')
    skip = {s.strip() for s in _q(req, 'except').split(',') if s.strip()}
    if skip and not want:
        raise _invalid('except leaves out pending items: give pending=1 with it', 'except')
    filtered = bool(for_key or skip)

    def mine():
        return [p for p in api.pending(for_key) if p['id'] not in skip]
    # pending is read whether asked for or not: it is the read that refuses a closed store,
    # which Store.wait answers at once and with nothing (a client would loop on it)
    found = mine()
    items = found if want else []
    if want:
        api.seen(req, 'pulled')
    if items:
        api.seen(req, 'waited')
        api.listen(req)
        ch = api.store.changes_since(since)
    else:
        key = object()
        api.seen(req, 'waited', opened=key)
        api.listen(req, opened=key)
        try:
            if filtered:
                ch = _wait_for(api, since, min(timeout, WAIT_MAX_S), mine,
                               for_key == AGENT_FROM)
            else:
                ch = api.store.wait(since, min(timeout, WAIT_MAX_S))
        finally:
            api.seen(req, 'waited', closed=key)
            api.listen(req, closed=key)
        found = mine()
        items = found if want else []
    api.handed(req, items)
    # a filtered wait ends on its own items, a reset or the timeout, never on other writes
    quiet = not ch.reset and not items and (filtered or not ch.events)
    return {'revision': ch.revision, 'reset': ch.reset,
            'timed_out': quiet,
            'changes': [{'collection': e.collection, 'id': e.id, 'op': e.op, 'status': e.status}
                        for e in ch.events],
            'pending': _unpath_pending(api, items, req)}


def _wait_for(api, since, timeout, mine, routes=False):
    """Store.wait until mine() (the pending items this wait is for: its `for` key, less its
    `except` ids) is not empty, the store resets, or timeout seconds pass; a write that leaves
    it empty waits on. With routes (a wait for the PM) it also looks again when an item's route
    next turns to the PM, which no write announces. The changes since `since`, all of them."""
    end = time.monotonic() + timeout
    at = since
    while True:
        left = max(0.0, end - time.monotonic())
        if routes:
            flip = api.route_change_in()
            if flip is not None:
                left = min(left, flip + ROUTE_SLACK_S)
        ch = api.store.wait(at, left)
        if ch.reset:
            return ch
        if mine() or time.monotonic() >= end:
            break
        at = ch.revision
    return api.store.changes_since(since)


def pages_list(api, req):
    return {'items': api.items('pages', req)}


def pages_publish(api, req):
    b = _fields(req, ('id', 'title', 'html', 'repo', 'from', 'summary', 'refs', 'if_version'),
                required=('id', 'title', 'html'))
    for name in ('id', 'title', 'html', 'repo', 'from', 'summary'):
        _text(b, name)
    pin = _pin(b)
    doc_id = b['id']
    why = schema.check_id(doc_id)
    if why:
        raise _invalid(why, 'id')
    shaped = _REF_SHAPED.search(doc_id)
    if shaped:
        raise _invalid('a page id cannot hold a hub item\'s or a ruling\'s id (%s holds %s): a ref '
                       'that names it would be read as that item' % (doc_id, shaped.group()), 'id')
    refs = _typed(b, 'refs', (list,), 'a list of strings')
    if refs is not None and not all(isinstance(r, str) for r in refs):
        raise _invalid('refs must be a list of strings', 'refs')
    why = pagecheck.check(b['html'])
    if why:
        raise _invalid(why, 'html')
    with PAGES_LOCK:
        return _pages_write(api, req, b, doc_id, pin)


def _pages_write(api, req, b, doc_id, pin):
    """pages_publish from the reads on, called with PAGES_LOCK held."""
    card, body = api.store.get('pages', doc_id), api.store.get('page_html', doc_id)
    if card is not None and pin is None:
        raise HubError('conflict', 'pages/%s is at version %d: give it as if_version to publish '
                       'over it' % (doc_id, _version(card)), 'if_version')
    if card is not None and pin != _version(card):
        raise HubError('conflict', 'pages/%s is at version %d, not %d'
                       % (doc_id, _version(card), pin), 'if_version')
    if card is None and pin:
        raise HubError('conflict', 'there is no pages/%s at version %d: it is not there'
                       % (doc_id, pin), 'if_version')
    size = len(b['html'].encode('utf-8'))
    fields = {k: b[k] for k in ('title', 'repo', 'summary', 'refs') if k in b}
    fields.update({'from': _agent_from(b.get('from') or ''), 'at': api.clock(), 'bytes': size,
                   'via': _via(req)})
    # the body first, each write pinned to what was read under the lock: a card never names a
    # body that is not there, and a second publisher reads only after both writes ("Pages")
    wrote = api.store.put('page_html', doc_id, {'html': b['html'], 'via': _via(req)},
                          if_version=_version(body) if body is not None else 0)
    r = api.store.put('pages', doc_id, fields, if_version=_version(card) if card is not None else 0)
    return api.written(r, {'bytes': size, 'html_version': wrote.version})


def pages_get(api, req):
    doc_id = req.params['id']
    d = api.doc('page_html', doc_id)
    return {'ok': True, 'id': doc_id, 'html': d.get('html'), 'version': _version(d)}


def pages_delete(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    with PAGES_LOCK:        # no publish runs between the reads and the two deletes
        d = api.doc('pages', doc_id, b)
        r = api.store.delete('pages', doc_id, if_version=_version(d))
        body = api.store.get('page_html', doc_id)   # the card first: it never names no body
        if body is not None:
            # A body delete that fails (the store's own error: under the lock no publish can
            # move its version) leaves an orphan body with no card. The card stays deleted:
            # hubvalidate warns of the orphan, no card offers it, and the next publish of
            # this id writes over it ("Pages").
            r = api.store.delete('page_html', doc_id, if_version=_version(body))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


# ---- the owner's schedules for the kit's automatic tasks ("Schedules")
# The tasks a schedule may name, a fixed table: maint-lint (the maintenance lint, being built),
# worktree-sweep (kitlib.worktree.task) and agent-temp-sweep (kitlib.machinesetup.agenttemp).
# Nothing here runs one.
SCHEDULE_TASKS = ('maint-lint', 'worktree-sweep', 'agent-temp-sweep')
CADENCES = ('off', 'daily', 'weekly')
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_AT = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z')


def _one_of(b, name, allowed):
    v = _text(b, name)
    if v not in allowed:
        raise _invalid('%s is one of %s, not %r' % (name, ', '.join(allowed), v), name)
    return v


def _schedule_repo(api, name):
    """The name of the repository the hub knows as `name` (case ignored): one the store's
    known_repos gives, a repos/ document's name (its id when it has none), or one of its aliases.
    `invalid`, field repo, for any other."""
    names, aliases = {}, {}
    keys = getattr(api.store.known_repos, 'session_keys', None)     # {repo: [hub keys]}
    keyed = {k.lower(): r for r, ks in (keys() if keys else {}).items() for k in ks}
    for n in api.store.known_repos() or ():
        if isinstance(n, str) and n.strip() and n.strip().lower() not in keyed:
            names.setdefault(n.strip().lower(), n.strip())
    for doc_id, d in sorted(api.store.docs('repos').items()):
        own = d.get('name') if isinstance(d.get('name'), str) and d['name'].strip() else doc_id
        own = names.setdefault(own.strip().lower(), own.strip())
        for a in d.get('aliases') if isinstance(d.get('aliases'), list) else ():
            if isinstance(a, str) and a.strip():
                aliases.setdefault(a.strip().lower(), own)
    for k, r in keyed.items():              # a session key stands for its repository
        if r.lower() in names:
            aliases.setdefault(k, names[r.lower()])
    want = name.strip().lower()
    found = names.get(want) or aliases.get(want)
    if found is None:
        raise _invalid('repo %r is not a repository the hub knows (GET repos lists them)' % name,
                       'repo')
    return found


def launcher_checkouts():
    """Api's default schedule_repos: the machine config's launcher.repos as the tick reads them
    (kitlib.autotasks.tick.launcher_repos)."""
    from kitlib.autotasks import tick
    return tick.launcher_repos()


def tick_last_seen():
    """Api's default tick_seen: when the tick last wrote its state file (it does on every tick
    that reads the hub and takes its lock), an ISO time in UTC, or None when there is none."""
    from kitlib.autotasks import tick
    from kitlib.config import ConfigError
    try:
        mtime = os.path.getmtime(os.path.join(tick.state_dir(), tick.STATE_NAME))
    except (OSError, ConfigError):
        return None
    return datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).isoformat(
        timespec='seconds')


TICK_STALE_S = 40 * 60          # no tick seen for this long: it is not running


def tick_every_s():
    """The kit-automatic-tasks task's interval in seconds, the one machine setup registers
    (kitlib.machinesetup.extras.AUTO_EVERY_MINUTES). Imported here, not at the top: the
    machinesetup package costs about three times this module's own import."""
    from kitlib.machinesetup.extras import AUTO_EVERY_MINUTES
    return AUTO_EVERY_MINUTES * 60


def _aware(text):
    """The aware datetime an ISO text names (one with no offset is local), None for no time."""
    try:
        at = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo is not None else at.astimezone()


def _tick_status(api):
    last = api.tick_seen()
    seen, now = _aware(last), _aware(api.clock())
    running = (seen is not None and now is not None
               and (now - seen).total_seconds() <= TICK_STALE_S)
    return {'last': last, 'running': running, 'every_s': tick_every_s()}


def tick_run_state():
    """Api's default tick_state: the tick's records by schedule key (kitlib.autotasks.tick.
    read_state), {} when there is none or the file cannot be read."""
    from kitlib.autotasks import tick
    from kitlib.config import ConfigError
    try:
        return tick.read_state(os.path.join(tick.state_dir(), tick.STATE_NAME))[0]
    except (OSError, ConfigError):
        return {}


def _next_due(fields, now):
    """The first slot strictly after `now` (naive local) of a schedule by kitlib.autotasks.due's
    rule, as naive local ISO text to the minute. None when off or not a schedule due can place."""
    from kitlib.autotasks import due
    at = due.parse_at(fields.get('at'))
    if fields.get('cadence') not in (due.DAILY, due.WEEKLY) or at is None or now is None:
        return None
    slot = due.last_slot(fields['cadence'], at, fields.get('days'), now)
    if slot is None:
        return None
    for _ in range(9):
        slot += datetime.timedelta(days=1)
        if slot > now and due.last_slot(fields['cadence'], at, fields.get('days'), slot) == slot:
            return slot.isoformat(timespec='minutes')
    return None


def schedules_list(api, req):
    from kitlib.autotasks import due, tick
    try:
        state = api.tick_state()
    except (OSError, ValueError):
        state = {}
    now = due.local(api.clock())
    items = []
    for d in api.items('schedules', req):
        rec = state.get(tick.key(d))
        rec = rec if isinstance(rec, dict) else {}
        what = rec.get('what') if isinstance(rec.get('what'), str) else None
        items.append(dict(d, last_outcome=what,
                          last_run=rec.get('at') if what == tick.STARTED
                          and isinstance(rec.get('at'), str) else None,
                          next_due=_next_due(d, now)))
    return {'items': items, 'tick': _tick_status(api)}


def _first_run(fields):
    """The first slot strictly after the schedule's set_at, as naive local HH:MM ISO text: the
    first candidate slot kitlib.autotasks.due.decide calls due with no record. None when off."""
    from kitlib.autotasks import due
    start, at = due.local(fields.get('set_at')), due.parse_at(fields.get('at'))
    if fields.get('cadence') == due.OFF or start is None or at is None:
        return None
    day = start.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    for k in range(9):
        slot = day + datetime.timedelta(days=k)
        if due.decide(fields, None, slot) == (due.DUE, slot):
            return slot.isoformat(timespec='minutes')
    return None


def _one_checkout(api, task, cadence, repo):
    """`invalid`, field repo, unless the tick could run task for repo: exactly one checkout of
    the machine config's launcher.repos matches it (kitlib.autotasks.tick.find_repo). Off, and a
    task that runs for no repository, need none."""
    from kitlib.autotasks import tick
    from kitlib.config import ConfigError
    if cadence == 'off' or not tick.TASKS[task].per_repo:
        return
    try:
        repos = api.schedule_repos()
    except (ConfigError, OSError) as e:      # what the tick takes as an unreadable list
        raise _invalid("the machine config's launcher.repos cannot be read: %s" % e, 'repo')
    if tick.find_repo(repo, repos) is None:
        raise _invalid("repo %r is not exactly one checkout in the machine config's "
                       "launcher.repos, so the automatic task could not run for it: add it "
                       "there once (launcher.repos), then set the schedule again" % repo, 'repo')


def schedules_set(api, req):
    b = _fields(req, ('task', 'repo', 'cadence', 'days', 'at', 'if_version'),
                required=('task', 'repo', 'cadence', 'at'))
    task = _one_of(b, 'task', SCHEDULE_TASKS)
    cadence = _one_of(b, 'cadence', CADENCES)
    at = _text(b, 'at')
    if not _AT.match(at):
        raise _invalid('at is a time of day as HH:MM, 24-hour (00:00 to 23:59), not %r' % at, 'at')
    days = _typed(b, 'days', (list,), 'a list of days') or []
    if not all(isinstance(d, str) and d in DAYS for d in days):
        raise _invalid('days is a list of %s' % ', '.join(DAYS), 'days')
    if cadence == 'weekly' and not days:
        raise _invalid('a weekly schedule needs days: one or more of %s' % ', '.join(DAYS), 'days')
    if cadence != 'weekly' and days:
        raise _invalid('days is for a weekly schedule, not a %s one' % cadence, 'days')
    pin = _pin(b)
    repo = _schedule_repo(api, _text(b, 'repo'))
    doc_id = '%s-%s' % (task, repo)
    why = schema.check_id(doc_id)
    if why:
        raise _invalid(why, 'repo')
    _one_checkout(api, task, cadence, repo)
    fields = {'task': task, 'repo': repo, 'cadence': cadence, 'at': at, 'set_at': api.clock(),
              'via': _via(req)}
    if cadence == 'weekly':
        fields['days'] = [d for d in DAYS if d in days]
    try:
        out = _put_pinned(api, 'schedules', doc_id, fields, pin)
    except HubError as e:
        if e.code == 'conflict':
            e.extra = {'id': doc_id}    # the schedule there, so the page can open it
        raise
    out['first_run'] = _first_run(fields)
    return out


def _put_pinned(api, coll, doc_id, fields, pin):
    """Put fields as coll/doc_id, pinned: writing over a document takes its version as `pin`
    (None, or another, is `conflict`), and a pin for a document not there is `conflict` too."""
    have = api.store.get(coll, doc_id)
    if have is not None and pin is None:
        raise HubError('conflict', '%s/%s is at version %d: give it as if_version to set it '
                       'again' % (coll, doc_id, _version(have)), 'if_version')
    if have is not None and pin != _version(have):
        raise HubError('conflict', '%s/%s is at version %d, not %d'
                       % (coll, doc_id, _version(have), pin), 'if_version')
    if have is None and pin:
        raise HubError('conflict', 'there is no %s/%s at version %d: it is not there'
                       % (coll, doc_id, pin), 'if_version')
    # pinned to the version read (0: not there yet), so a second writer with the same pin loses
    r = api.store.put(coll, doc_id, fields, if_version=_version(have) if have is not None else 0)
    return api.written(r)


# ---- the maintenance lint's findings and the owner's marks on them ("Findings")
_FINGERPRINT = re.compile(r'[0-9a-f]{16}\Z')
FINDING_COPY = ('repo', 'check', 'target', 'text')   # what a mark keeps of its finding
FINDING_PATHS = ('target', 'fix')   # a finding's fields that can name a path on this machine
# an absolute Windows path: a drive's (a drive letter, a colon, then \ or /) or a share's,
# \\host\ or //host/ (a host is needed: a lone doubled backslash is no path, and a URL's :// is
# no share). Quoted, it runs to its quote; bare, on past each space whose next word holds a
# separator and starts no new path, absolute or ./ ../ relative (<drive>\home\A B\x is one
# path), so no folder after a space shows
_PATH_HEAD = r'(?:[A-Za-z]:[\\/]|(?<![:\\/])[\\/]{2,}[^\s\\/"\'`<>|]+[\\/]+)'
_PATH_WORD = r'[^\s"\'`<>|]*'
_ABS_PATH = re.compile(r'"%s[^"]*"' % _PATH_HEAD
                       + r'|(?<![A-Za-z0-9])%s%s' % (_PATH_HEAD, _PATH_WORD)
                       + r'(?:[ \t]+(?![A-Za-z]:[\\/]|[\\/]{2}|\.{1,2}[\\/])[^\s"\'`<>|\\/]*[\\/]%s)*'
                       % _PATH_WORD)


def _unpathed(text):
    """text with each absolute path cut to `.../<its last part>`."""
    def cut(m):
        parts = [p for p in re.split(r'[\\/]+', m.group(0).strip('"')) if p]
        return '.../' + parts[-1] if len(parts) > 1 else '...'
    return _ABS_PATH.sub(cut, text)


def _seen(f, req):
    """Finding f as this caller sees it: whole for the local owner (Request.full), else with no
    absolute path in FINDING_PATHS."""
    if req.full:
        return f
    return dict(f, **{k: _unpathed(f[k]) for k in FINDING_PATHS if isinstance(f.get(k), str)})


def _fp_field(b, name):
    v = b.get(name)
    if not isinstance(v, str) or not _FINGERPRINT.match(v):
        raise _invalid('%s is a finding\'s fingerprint, 16 hex digits (GET findings gives them)'
                       % name, name)
    return v


def _finding(api, fp, field):
    """The finding the lint shows with fingerprint fp, marked or not; not_found on `field`."""
    for f in api.shown('findings'):
        if isinstance(f, dict) and f.get('fingerprint') == fp:
            return f
    raise HubError('not_found', 'the maintenance lint shows no finding %s' % fp, field)


def findings_list(api, req):
    repo = _q(req, 'repo').lower()
    marked = api.store.docs('finding_marks')
    return {'items': [_seen(f, req) for f in api.shown('findings')
                      if isinstance(f, dict) and f.get('fingerprint') not in marked
                      and (not repo or str(f.get('repo') or '').lower() == repo)],
            'runs': [r for r in _lint_runs(api) if not repo or r['repo'].lower() == repo]}


LEDGER_COUNTS = ('stale_cites', 'conflict_pairs', 'overrides')   # kitlib/maintlint/ledger.py


def _utc(text):
    """An ISO time (a trailing Z is UTC) as an aware datetime, or None when it does not parse."""
    if not isinstance(text, str):
        return None
    return _when(text[:-1] + '+00:00' if text.endswith('Z') else text, '')


def _lint_runs(api):
    """Each repo's last lint run from the optional lint_runs view, as {repo, started, ledger,
    skipped}: ledger the LEDGER_COUNTS (whole numbers) or None; skipped the skip marker's {reason,
    at} only when its `at` is later than the run's `started` and both parse (a marker left beside
    a newer result is no skip), else None. No view, a view that raises or a row that is not one:
    nothing, never a failed GET findings."""
    try:
        rows = api.view('lint_runs')
    except Exception:               # noqa: BLE001 - context only; the findings still show
        return []
    out = []
    for r in rows if isinstance(rows, list) else ():
        if not isinstance(r, dict) or not isinstance(r.get('repo'), str):
            continue
        led = r.get('ledger')
        led = ({k: led[k] for k in LEDGER_COUNTS if type(led.get(k)) is int}
               if isinstance(led, dict) else None)
        skip, skipped = r.get('skip'), None
        if isinstance(skip, dict) and isinstance(skip.get('reason'), str):
            at, started = _utc(skip.get('at')), _utc(r.get('started'))
            if at is not None and started is not None and at > started:
                skipped = {'reason': skip['reason'], 'at': skip['at']}
        out.append({'repo': r['repo'], 'started': r['started'] if isinstance(r.get('started'), str)
                    else None, 'ledger': led, 'skipped': skipped})
    return out


def finding_marks_list(api, req):
    return {'items': [_seen(m, req) for m in api.items('finding_marks', req)]}


def finding_marks_set(api, req):
    b = _fields(req, ('fingerprint', 'status', 'note', 'if_version'),
                required=('fingerprint', 'status'))
    fp = _fp_field(b, 'fingerprint')
    status = _one_of(b, 'status', hubvalidate.STATUSES['finding_marks'])
    note = _text(b, 'note')
    pin = _pin(b)
    f = _finding(api, fp, 'fingerprint')
    fields = {k: f[k] for k in FINDING_COPY if isinstance(f.get(k), str)}
    fields.update(status=status, marked_at=api.clock(), via=_via(req))
    if note is not None:
        fields['note'] = note
    return _put_pinned(api, 'finding_marks', fp, fields, pin)


def finding_marks_delete(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('finding_marks', doc_id, b)
    r = api.store.delete('finding_marks', doc_id, if_version=_version(d))
    return {'ok': True, 'id': doc_id, 'deleted': True, 'revision': r.revision}


def _act_note(api, b, req):
    """An `act` note's fields prefilled from its finding as the caller sees it (_seen): repo,
    refs (the fingerprint and the finding's target first, each once; a target's absolute path is
    never a ref) and a text naming the check, target, text and fix."""
    fp = b.get('finding')
    if fp is None:
        raise _invalid('an act note names the finding it acts on (finding)', 'finding')
    f = _seen(_finding(api, _fp_field(b, 'finding'), 'finding'), req)
    target = f.get('target')
    refs = []
    for r in [fp, _unpathed(target) if isinstance(target, str) else None] + list(
            b.get('refs') or ()):
        if isinstance(r, str) and r and r not in refs:
            refs.append(r)
    b['refs'] = refs
    if isinstance(f.get('repo'), str):
        b.setdefault('repo', f['repo'])
    if not (b.get('text') or '').strip():
        b['text'] = 'Act on maintenance finding %s %s: %s (fix: %s)' % tuple(
            f.get(k) if isinstance(f.get(k), str) else '-' for k in ('check', 'target', 'text',
                                                                      'fix'))


# ---- skill edits (module docstring "Skill edits")
SKILL_EDIT_CHANGE = ('draft', 'superseded', 'failed')     # the owner may change the text
SKILL_EDIT_WITHDRAW = ('draft', 'submitted', 'superseded', 'failed')


def _skill_name(b):
    v = _text(b, 'skill')
    if not isinstance(v, str) or not _SEGMENT.match(v) or '..' in v:
        raise HubError('invalid', SEGMENT_SAYS['name'], 'skill')
    return v


def _skill_text(b, name, source):
    """The body's text, CRLF read as LF, `invalid` (field text) where skilledits.why_not says
    it cannot replace the skill source `source` (the skill_source view's answer)."""
    text = _text(b, 'text')
    text = skilledits.normal(text) if isinstance(text, str) else text
    why = skilledits.why_not(text, source.get('text'), name)
    if why is not None:
        raise _invalid(why, 'text')
    return text


def _skill_source(api, name):
    """The skill_source view's answer for the listed skill `name`; its not_found is the answer
    (a skill that is not listed), anything else it raises is `unavailable`."""
    src = _record_view(api, 'skill_source', name)
    if not isinstance(src, dict) or not skilledits.is_blob(src.get('blob')):
        raise HubError('unavailable', 'the skill_source view gave no blob', 'views')
    return src


def _blob_field(b, name):
    v = _text(b, name)
    if v is not None and not skilledits.is_blob(v):
        raise _invalid('%s is a git blob id: 40 lowercase hex digits' % name, name)
    return v


def skill_edits_list(api, req):
    return {'items': api.items('skill_edits', req)}


def _source_at(api, d):
    """{text, blob}: the source text at a superseded or failed edit's source_blob, read with
    kit_text, or None (another status, no source_blob, or kit_text cannot read it)."""
    blob = d.get('source_blob')
    if d.get('status') not in ('superseded', 'failed') or not skilledits.is_blob(blob):
        return None
    try:
        text = api.kit_text(blob)
    except Exception:               # noqa: BLE001 - git could not be asked: not read
        text = None
    return {'text': text, 'blob': blob} if isinstance(text, str) else None


def _main_blob(api, name):
    """The blob of skill `name`'s source on main in the kit checkout (kit_blob), or None."""
    try:
        got = api.kit_blob('main', skilledits.source_path(name))
    except Exception:               # noqa: BLE001 - git could not be asked: not read
        return None
    return got if skilledits.is_blob(got) else None


def skill_edits_get(api, req):
    doc_id = req.params['id']
    d = api.doc('skill_edits', doc_id)
    d['id'] = doc_id
    return {'item': d, 'source': _source_at(api, d)}


def skill_edits_add(api, req):
    b = _fields(req, ('skill', 'text', 'submit'), required=('skill', 'text'), flags=('submit',))
    name = _skill_name(b)
    src = _skill_source(api, name)
    text = _skill_text(b, name, src)
    now = api.clock()
    fields = {'status': 'draft', 'skill': name, 'text': text, 'base_blob': src['blob'],
              'created_at': now}
    if _flag(b, 'submit', False):
        fields.update(status='submitted', submitted_at=now)
    return api.add('skill_edits', fields, req, {'status': fields['status']})


def skill_edits_edit(api, req):
    b = _fields(req, ('text', 'submit', 'if_version'), required=('if_version',),
                flags=('submit',))
    _pin(b, required=True)
    doc_id = req.params['id']
    d = api.doc('skill_edits', doc_id, b)
    status = d.get('status')
    if status not in SKILL_EDIT_CHANGE:
        raise _invalid('%s is %s: only a draft, superseded or failed edit can change'
                       % (doc_id, status), 'status')
    name = d.get('skill')
    src = _skill_source(api, name)
    if b.get('text') is None:
        b['text'] = d.get('text')
    text = _skill_text(b, name, src)
    # a draft keeps the source it was written on; a superseded or failed edit is put on the
    # source the owner was shown to re-apply onto: its source_blob's text when the hub reads it,
    # else the source as served now
    if status == 'draft':
        base = d.get('base_blob')
    else:
        at = _source_at(api, d)
        base = at['blob'] if at else src['blob']
    changes = {'text': text, 'base_blob': base, 'status': 'draft', 'source_blob': None,
               'why': None}
    if _flag(b, 'submit', False):
        main = _main_blob(api, name) if base != src['blob'] else None
        if base != src['blob'] and base != main:
            # the newest source known: main's when the hub reads it, else the served copy
            changes.update(status='superseded', source_blob=main or src['blob'])
        else:
            changes.update(status='submitted', submitted_at=api.clock())
    return api.change('skill_edits', doc_id, d, changes, req, {'status': changes['status']})


def skill_edits_withdraw(api, req):
    b = _fields(req, ('if_version',))
    _pin(b)
    doc_id = req.params['id']
    d = api.doc('skill_edits', doc_id, b)
    if d.get('status') not in SKILL_EDIT_WITHDRAW:
        raise _invalid('%s is %s: a committed or withdrawn edit cannot be withdrawn'
                       % (doc_id, d.get('status')), 'status')
    return api.change('skill_edits', doc_id, d, {'status': 'withdrawn', 'closed_at': api.clock()},
                      req, {'status': 'withdrawn'})


def _submitted(api, b, req):
    """(id, document) of the submitted skill edit the body's if_version pins; `invalid` (field
    status) for any other status."""
    _pin(b, required=True)
    doc_id = req.params['id']
    d = api.doc('skill_edits', doc_id, b)
    if d.get('status') != 'submitted':
        raise _invalid('%s is %s: only a submitted edit is committed or failed'
                       % (doc_id, d.get('status')), 'status')
    return doc_id, d


def skill_edits_done(api, req):
    b = _fields(req, ('branch', 'blob', 'if_version'), required=('branch', 'blob', 'if_version'))
    branch = _text(b, 'branch')
    if not skilledits.BRANCH.match(branch) or '..' in branch or branch.endswith(('/', '.lock')):
        raise _invalid('branch is a branch name, not %r' % branch, 'branch')
    blob = _blob_field(b, 'blob')
    doc_id, d = _submitted(api, b, req)
    text = d.get('text') if isinstance(d.get('text'), str) else ''
    want = skilledits.git_blob(text.encode('utf-8'))
    if blob != want:
        raise _invalid("blob %s is not the owner's text (%s): commit the text `hub.py skill-edit "
                       "show %s --out FILE` writes, byte for byte" % (blob, want, doc_id), 'blob')
    path = skilledits.source_path(d.get('skill'))
    try:
        got = api.kit_blob(branch, path)
    except Exception:               # noqa: BLE001 - git could not be asked: not verified
        got = None
    if got is None:
        raise _invalid('the hub could not read %s on branch %s in the kit checkout'
                       % (path, branch), 'branch')
    if got != want:
        raise _invalid("branch %s holds %s as blob %s, not the owner's text (%s)"
                       % (branch, path, got, want), 'branch')
    return api.change('skill_edits', doc_id, d, {'status': 'committed', 'branch': branch,
                                                 'blob': blob, 'done_at': api.clock()},
                      req, {'status': 'committed'})


def skill_edits_fail(api, req):
    b = _fields(req, ('why', 'current_blob', 'if_version'), required=('why', 'if_version'))
    why = _text(b, 'why')
    if not why.strip():
        raise _invalid('why is required: what stopped the commit', 'why')
    current = _blob_field(b, 'current_blob')
    doc_id, d = _submitted(api, b, req)
    changes = {'status': 'failed', 'why': why, 'closed_at': api.clock()}
    if current is not None and current != d.get('base_blob'):
        changes.update(status='superseded', source_blob=current)
    return api.change('skill_edits', doc_id, d, changes, req, {'status': changes['status']})


def snapshot(api, req):
    revision, docs = api.store.snapshot()
    docs.pop('page_html', None)     # a body is read on its own (GET pages/{id}, "Pages")
    if not req.full:                # the paths as GET to_pm and GET finding_marks cut them
        for d in (docs.get('to_pm') or {}).values():
            _unpath_act(d)
        marks = docs.get('finding_marks') or {}
        for doc_id, m in list(marks.items()):
            if isinstance(m, dict):
                marks[doc_id] = _seen(m, req)
    views, missing = {}, {}
    for name in VIEWS:
        try:
            views[name] = api.view(name)
        except Exception as e:      # noqa: BLE001 - the first paint shows what there is
            views[name] = None
            missing[name] = _reason(e)
    return {'revision': revision, 'collections': docs,
            'pending': _unpath_pending(api, api.pending(), req),
            'views': views, 'unavailable': missing, 'pm_seen': api.pm_seen(),
            'push': api.push_seen(), 'watcher': api.watcher_seen(), 'code': api.code_seen(),
            'strong': req.strong is True}


def health(api, req):
    out, missing = {'git': None, 'backlog': None}, {}
    try:
        extra = api.view('health')
    except Exception as e:          # noqa: BLE001 - health answers whatever a view does
        extra, missing = None, {'health': _reason(e)}
    if isinstance(extra, dict):
        out.update(extra)
    out.pop('store', None)
    out.update(ok=True, version=API_VERSION, revision=api.store.revision, unavailable=missing,
               watcher=api.watcher_seen(), push=api.push_seen(), code=api.code_seen(),
               jobs=api.jobs_seen())
    if req.role == OWNER:           # where the store is on this machine is the owner's to know
        out['store'] = api.store.root
    return out


def _remote(req):
    return req.via == TUNNEL_VIA


def _lock(api, read, *args, **kw):
    """read(door, ...) on the Api's lock door ("Machine lock"), a read or a write. No door, or a door that raises
    anything but a HubError, is `unavailable` (field lock), named by the exception's class
    only. HomeFenced and other BaseExceptions pass."""
    door = api.lock
    if door is None:
        raise HubError('unavailable', 'this server has no machine lock', 'lock')
    try:
        return read(door, *args, **kw)
    except HubError:
        raise
    except Exception as e:          # noqa: BLE001 - any lock failure is the lock being unavailable
        raise HubError('unavailable', 'the machine lock could not be reached (%s)' % _reason(e),
                       'lock')


def _writes(req, view):
    """What this caller may change from the page ("Machine lock"): {lock, strong, why}. lock: the
    LOCK_WRITE routes; strong: the APPROVER ones as well; why: the reason one is false, or ''."""
    if not isinstance(view, dict) or view.get('writer') != 'owner':
        return {'lock': False, 'strong': False, 'why': WRITES_WHY['agent']}
    strong = req.role == OWNER and req.strong is True
    lock = strong or (req.role == OWNER and not _remote(req))
    why = '' if strong else WRITES_WHY['strong'] if lock else WRITES_WHY['tunnel']
    return {'lock': lock, 'strong': strong, 'why': why}


def _viewed(api, req):
    """The lock door's view for this caller, with `writes`."""
    out = dict(_lock(api, lambda door: door.view(remote=_remote(req))))
    out['writes'] = _writes(req, out)
    return out


def lock_view(api, req):
    return _viewed(api, req)


def _wrote(api, req, write):
    """write(door, remote) -> its result; the answer {ok, result} plus the view after it (view
    null, writes all false, when the lock cannot be read after the write)."""
    remote = _remote(req)
    result = _lock(api, write, remote)
    try:
        out = _viewed(api, req)
    except HubError:
        out = {'lock_revision': None, 'view': None, 'writer': None,
               'writes': {'lock': False, 'strong': False, 'why': ''}}
    out.update(ok=True, result=result)
    return out


def _confirmed(req, b):
    if b.get('confirm') != req.params['id']:
        raise _invalid('confirm must repeat the run id %s' % req.params['id'], 'confirm')


def _strs(b, names):
    return [_text(b, n) for n in names]


def lock_hold_add(api, req):
    b = _fields(req, ('kind', 'scope', 'start', 'end', 'note', 'cpu', 'gpu'), ('kind', 'scope'))
    kind = _text(b, 'kind')
    if kind == 'reserve':
        scope, start, end, note = _strs(b, ('scope', 'start', 'end', 'note'))
        if b.get('cpu') or b.get('gpu'):
            raise _invalid('a reserve takes no slots: it is a whole or half hold', 'cpu')
        return _wrote(api, req, lambda door, remote: door.reserve(scope, start, end, note,
                                                                  remote=remote))
    if kind == 'hold':
        scope, start, end, note, cpu, gpu = _strs(b, ('scope', 'start', 'end', 'note', 'cpu',
                                                      'gpu'))
        return _wrote(api, req, lambda door, remote: door.hold(scope, start, end, note, cpu, gpu,
                                                               remote=remote))
    raise _invalid('kind is reserve or hold, not %r' % (kind,), 'kind')


def lock_hold_edit(api, req):
    b = _fields(req, ('start', 'end', 'note'))
    start, end, note = _strs(b, ('start', 'end', 'note'))
    hid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.edit(hid, start, end, note, remote=remote))


def lock_hold_end(api, req):
    _fields(req, ())
    hid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.end_hold(hid, remote=remote))


def lock_division(api, req):
    b = _fields(req, ('fields', 'defaults'), flags=('defaults',))
    defaults = _flag(b, 'defaults', False)
    fields = _typed(b, 'fields', (dict,), 'an object')
    if defaults == (fields is not None):
        raise _invalid('give the division fields, or defaults true, not both', 'fields')
    return _wrote(api, req, lambda door, remote: door.division(None if defaults else fields,
                                                               remote=remote))


def lock_run_end(api, req):
    b = _fields(req, ('confirm',), ('confirm',))
    _confirmed(req, b)
    rid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.end_run(rid, remote=remote))


def lock_run_top(api, req):
    _fields(req, ())
    rid = req.params['id']
    return _wrote(api, req, lambda door, remote: door.move_to_top(rid, remote=remote))


def lock_game_mode(api, req):
    b = _fields(req, ('enabled',), ('enabled',), flags=('enabled',))
    enabled = b['enabled']
    return _wrote(api, req, lambda door, remote: door.game_mode(enabled, remote=remote))


def lock_games(api, req):
    b = _fields(req, ('text',), blank=('text',))
    text = _text(b, 'text') or ''
    return _wrote(api, req, lambda door, remote: door.games(text, remote=remote))


def lock_stats(api, req):
    days = _q_int(req, 'days', 1, LOCK_DAYS_MAX)
    since, until = _q(req, 'since') or None, _q(req, 'until') or None
    return _lock(api, lambda door: door.stats(days=days, since=since, until=until))


def lock_wait(api, req):
    since = _q_int(req, 'since', 0, 10 ** 12)
    timeout = min(_timeout(req), float(WAIT_MAX_S))
    api.store.check_open()
    if since is None:
        since = _lock(api, lambda door: door.revision())
    end = time.monotonic() + timeout
    # the lock store wakes nobody: look at its revision every lock_poll_s, sleeping in the hub
    # store's wait so that closing the hub ends this wait at once
    while _lock(api, lambda door: door.revision()) == since:
        left = end - time.monotonic()
        if left <= 0:
            break
        api.store.wait(api.store.revision, min(api.lock_poll_s, left))
        api.store.check_open()
    out = _viewed(api, req)
    out['changed'] = out.get('lock_revision') != since
    return out


def _r(method, path, role, handler):
    return Route(method, BASE + path, role, handler)


ROUTES = (
    _r('GET', '/questions', ANY, questions_list),
    _r('POST', '/questions', AGENT, questions_add),
    _r('POST', '/questions/{id}/answer', OWNER, questions_answer),
    _r('POST', '/questions/file', AGENT, questions_file),
    _r('POST', '/questions/bind', AGENT, questions_bind),
    _r('POST', '/questions/{id}/log', AGENT, questions_log),
    _r('POST', '/questions/{id}/done', AGENT, questions_done),
    _r('POST', '/questions/{id}/edit', AGENT, questions_edit),
    _r('POST', '/questions/{id}/delete', ANY, questions_delete),
    _r('GET', '/todos', ANY, todos_list),
    _r('POST', '/todos', ANY, todos_add),
    _r('POST', '/todos/{id}/done', OWNER, todos_done),
    _r('POST', '/todos/{id}/ack', AGENT, todos_ack),
    _r('POST', '/todos/{id}/edit', ANY, todos_edit),
    _r('POST', '/todos/{id}/delete', ANY, todos_delete),
    _r('GET', '/stories', ANY, stories_list),
    _r('POST', '/stories', OWNER, stories_add),
    _r('POST', '/stories/{id}', AGENT, stories_set),
    _r('POST', '/stories/{id}/withdraw', OWNER, stories_withdraw),
    _r('GET', '/to_pm', ANY, to_pm_list),
    _r('POST', '/to_pm', OWNER, to_pm_add),
    _r('POST', '/to_pm/send', OWNER, to_pm_send),
    _r('POST', '/to_pm/{id}/reply', AGENT, to_pm_reply),
    _r('POST', '/to_pm/{id}/edit', OWNER, to_pm_edit),
    _r('POST', '/to_pm/{id}/delete', OWNER, to_pm_delete),
    _r('POST', '/inbox', AGENT, inbox_add),
    _r('POST', '/inbox/{id}/read', OWNER, inbox_read),
    _r('GET', '/pages', ANY, pages_list),
    _r('POST', '/pages', AGENT, pages_publish),
    _r('GET', '/pages/{id}', ANY, pages_get),
    _r('POST', '/pages/{id}/delete', OWNER, pages_delete),
    _r('GET', '/schedules', ANY, schedules_list),
    _r('POST', '/schedules', OWNER, schedules_set),
    _r('POST', '/approvals', AGENT, approvals_add),
    _r('POST', '/approvals/{id}/decide', APPROVER, approvals_decide),
    _r('POST', '/approvals/{id}/consume', AGENT, approvals_consume),
    _r('POST', '/approvals/{id}/delete', AGENT, approvals_delete),
    _r('GET', '/approvals/{id}/verify', AGENT, approvals_verify),
    _r('GET', '/backlog', ANY, backlog),
    _r('GET', '/completed', ANY, completed),
    _r('GET', '/records/{repo}/new', ANY, record_new),
    _r('GET', '/records/{repo}/{id}', ANY, record),
    _r('POST', '/records/{repo}', OWNER, record_add),
    _r('POST', '/records/{repo}/{id}/edit', OWNER, record_edit),
    _r('POST', '/records/{repo}/{id}/delete', OWNER, record_delete),
    _r('POST', '/records/{repo}/{id}/unblock', OWNER, record_unblock),
    _r('GET', '/findings', ANY, findings_list),
    _r('GET', '/finding_marks', ANY, finding_marks_list),
    _r('POST', '/finding_marks', OWNER, finding_marks_set),
    _r('POST', '/finding_marks/{id}/delete', OWNER, finding_marks_delete),
    _r('GET', '/notices', ANY, _listing('notices')),
    _r('GET', '/repos', ANY, _listing('repos')),
    _r('GET', '/usage', ANY, _listing('usage')),
    _r('GET', '/agents', ANY, _listing('agents')),
    _r('GET', '/skills', ANY, _listing('skills')),
    _r('GET', '/skills/{name}', ANY, skill_get),
    _r('GET', '/skills/{name}/source', ANY, skill_source_get),
    _r('GET', '/skill_edits', ANY, skill_edits_list),
    _r('POST', '/skill_edits', OWNER, skill_edits_add),
    _r('GET', '/skill_edits/{id}', ANY, skill_edits_get),
    _r('POST', '/skill_edits/{id}/edit', OWNER, skill_edits_edit),
    _r('POST', '/skill_edits/{id}/withdraw', OWNER, skill_edits_withdraw),
    _r('POST', '/skill_edits/{id}/done', AGENT, skill_edits_done),
    _r('POST', '/skill_edits/{id}/fail', AGENT, skill_edits_fail),
    _r('GET', '/pending', ANY, pending),
    _r('GET', '/wait', ANY, wait),
    _r('GET', '/snapshot', ANY, snapshot),
    _r('GET', '/health', ANY, health),
    _r('GET', '/lock/view', OWNER, lock_view),
    _r('GET', '/lock/stats', OWNER, lock_stats),
    _r('GET', '/lock/wait', OWNER, lock_wait),
    _r('POST', '/lock/holds', APPROVER, lock_hold_add),
    _r('POST', '/lock/holds/{id}/edit', APPROVER, lock_hold_edit),
    _r('POST', '/lock/holds/{id}/end', APPROVER, lock_hold_end),
    _r('POST', '/lock/division', APPROVER, lock_division),
    _r('POST', '/lock/runs/{id}/end', LOCK_WRITE, lock_run_end),
    _r('POST', '/lock/runs/{id}/top', LOCK_WRITE, lock_run_top),
    _r('POST', '/lock/game_mode', LOCK_WRITE, lock_game_mode),
    _r('POST', '/lock/games', LOCK_WRITE, lock_games),
)
# the handlers that block: the listener counts each call to one as a wait (kitlib.hubsite.httpd)
WAIT_HANDLERS = (wait, lock_wait)
