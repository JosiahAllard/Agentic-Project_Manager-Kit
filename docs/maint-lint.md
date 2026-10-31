# Maintenance lint

`tools/maint_lint.py` lints one repository's upkeep: its backlog store, its worktrees, the
machine's decision ledger, its gate's step times and, with a module map, how often its gates
widened beyond the changed modules. The logic is `kitlib/maintlint/`. It writes
only the gate-time record, in a plain run ("Gate time"); it edits no record, no ledger entry, no
worktree and no git ref. Every finding prints the command that fixes it, and nothing runs that
command.

    python tools/maint_lint.py [-C DIR] [--dry-run] [--result FILE] [--out FILE]

Exit codes: 0 no finding, 1 findings, 2 USAGE (`--dry-run` without `--out`, or no `--result` and
no `pm_data` to put it under), 3 SKIPPED (the machine lock stayed busy or a strong hold reserves
it; see "Schedule"), 4 when the lock refuses the run with its own exit 1 or 2 (mapped, so it cannot
read as findings or USAGE), 5 ERROR (any other error, an `Exception`, raised while booking the
slot or linting: one `ERROR: <type>: <message>` line on stderr instead of a traceback, whose
Python exit 1 would read as findings; `EXIT_ERROR` in the tool), else the lock's own exit code
when it refuses the run for another reason. Ctrl+C and other `BaseException`s are not caught.

## Schedule

The run comes from the auto-tasks tick (`tools/auto_tasks.py`): its task `maint-lint`
(`kitlib/autotasks/tick.py` `TASKS`) runs `tools/maint_lint.py -C <main checkout>` when the
owner's hub schedule for task `maint-lint` is due (cadence `weekly`, its days and time;
docs/action-hub.md "schedules/<task>-<repo>").

A plain run holds the machine lock's `maint-lint` kind (`kitlib/lock/kinds.json`: 1 cpu slot, no
gpu) for the whole run (`kitlib/maintlint/slot.py`, through `kitlib.lock.run.book`). It waits at
most 15 min, fixed in `slot.py` (`WAIT_MIN`), not the kind's `warn_min`, which `gate.py --measure`
rewrites from run history. The run is skipped when the lock stays busy that long (lock exit 6 or
8), or at once when a strong hold, the owner's (quiet hours) or a game's, reserves the machine
(lock exit 11): one `SKIPPED:` line, exit 3, nothing linted, and the previous `result.json` and
page left as they were. `--dry-run` takes no slot.

A skipped run writes the skip marker `skipped.json` beside `result.json`, whole:
`{"at": "<UTC time, %Y-%m-%dT%H:%M:%SZ>", "code": <lock exit>, "reason": "busy|timeout|hold"}`.
`slot.read_skip(<result.json path>)` reads it as that dict, or None. A run that writes
`result.json` removes the marker, so a marker present means the last scheduled run was skipped and
`result.json` is older than it. There is no PM event and no retry before the next scheduled run.

## Evidence, never age

A check flags only on evidence that a thing is spent or wrong. Examples are a duplicate, a path
that is gone, a blocker that names no live record, a pull with no branch, or a tree whose branch
has landed. No check flags a record or a tree for its age alone. A record that is merely old, with
no other evidence, gives no finding. `tests/maintlint/test_maintlint.py` `WholeRun` holds that test.

Left out for that reason:
- records not touched for N days (`kitlib.backlog.stale.sweep` is given no store history, so it
  works out no age);
- a hold flagged only for how long it has been held (`parked_at`, `pulled_at`);
- a worktree flagged only for idle time: an L (live or kept) tree is never a finding.

Each finding carries an age as context only: the record's `pulled_at` or `parked_at`, or a tree's
idle time. No check reads it to decide a finding.

The 7-day audit window is the range of the log that is read, not a trigger. A worktree's class is
`kitlib.worktree.classify.classify`'s own, and its 24 h bar between L and B, C or E is part of that
class.

## Backlog checks

`kitlib/maintlint/holds.py` and `kitlib/maintlint/backlog.py`:
- `keyword-hold`: an offerable record whose summary, requires or expect names the owner's go or a
  live client, but which no hold keeps out of the offer. The body is not read. A live-client hit on a record with a `uses:` line is no finding: the resource keeps it out of the offer while taken, and `blocked_on: live-client` stays for work that needs the owner present (docs/backlog.md "Resources"). The fix is a `block --on owner|live-client`
  whose reason the PM writes, because an owner hold must cite what it waits on; for a live-client hit in a repository that declares resources it also names `set <ID> --uses <resource>`.
- `unpriced`: the records `backlog.py needs-priority` lists.
- `dead-pull`: a pull with no branch by its `pulled_by` (`offer.in_flight_split`). A failed git
  probe gives a note and no finding.
- `dead-blocker`: an `item:X` blocker that names no live record (`store.dangling_items`). Closing
  or retiring a record deletes it.
- `duplicate`: live records whose summaries are the same words in the same order. A near match is
  not flagged. Its target is the sorted record ids.
- `answered-question`: a parked record whose question (`q`) the owner has answered on the Action
  Hub (status answered, logged or done). The hub's questions are read, read-only, with the agent token
  through `kitlib.hubsite.client`, as `tools/hub.py` reads them, and only when a record is parked.
  The fix is `unpark <ID>`, giving the ruling it was logged as (the ledger entry whose Source names
  the question; the hub keeps no id), after the PM logs the answer when it is not logged yet; for a question the PM marked done (`hub.py questions done`), `unpark
  <ID>` alone, with no ruling to cite. No ruling id is shown. A hub that cannot be asked gives a NOT CHECKED note.
- `audit`: `kitlib.backlog.audit_check.run` over 7 days, with no fetch. It gives one count finding
  per kind (UNAUDITED, UNKNOWN, UNMAPPED, UNVERIFIED), for example "12 store change(s) in 7 days with no audit
  line", whose fix is the `backlog_audit.py` command that lists them. The lines themselves are not
  shown: they carry commit subjects. audit_check's NOTE lines become notes.

## Path checks

`kitlib/maintlint/verify.py`. Paths are read in the checkout given, except in a PM data worktree:
there they are read in the code repository's main checkout, against its code main
(`kitlib.maintlint.code_checkout`), because the records name the code's files.
- `body-path-gone` and `file-moved`: `stale.sweep`'s missing body paths, and `files` entries
  changed or moved since `files_at`.
- `scope-path-gone`: a scope path that is missing from the checkout and that the main branch's
  history once held. A path no commit ever held is a file still to be made, so it is not flagged.

## Worktree checks

`kitlib/maintlint/worktrees.py` reads `classify` without apply and reports what the nightly sweep
(`kitlib.worktree.sweep`) leaves, never a tree that sweep removes or abandons on its own:
- an A tree only when a pulled record claims its branch (`claims` is a non-empty list). It also
  gives `landed-still-pulled` per claim, whose fix is a `release`; when "Landing checks" already
  gave that record's `landed-still-pulled`, that one is kept and this one dropped (one row per
  check and target). An A tree no record claims is the sweep's to remove.
- a B or C tree only when no stale mark holds on its branch. A tree whose mark holds is the sweep's
  to abandon; one whose mark is void is reported.
- every E and O tree.

Each finding carries its `worktree_remove.py` form.

## Landing checks

`kitlib/maintlint/verify.py`, in the code checkout (as "Path checks"). Whether the store and the
code agree on what landed, measured against the code base: the pushed
`refs/remotes/<remote>/<main>` when the checkout has it, else `refs/heads/<main>`. A branch has
landed when its tip is in the base and off the base's first-parent chain (the rule of
`kitlib.worktree.remove.merged_worktrees`), so a fresh branch, whose tip is a base tip, never has.
- `landed-still-pulled`: a pulled record whose `pulled_by` branch has landed, for every branch,
  with or without a worktree. The branch is read locally, else on the remote. The fix is a
  `close --reason "landed <merge>"`, or a `release` when that landing was one slice of the record.
- `closed-unlanded`: a record the store no longer holds whose newest close, retire or delete in
  the store's history over the last 7 days (the audit's window) has the subject
  `closes <ID>: landed <sha>`, the form a landing writes, and whose `<sha>` the code checkout has
  and the base does not hold. The fix is an `add <ID> --reopen`, once it is clear the work did not
  land by another commit. A record back in the store was reopened and is not flagged; nor is one
  whose landed close was followed by a close for another reason, a retire or a delete.

Notes, never findings: no base to measure against, a branch listing git cannot give, a store
history that cannot be read, and closes whose `<sha>` this checkout does not have (another
repository's, or not fetched), counted.

## Ledger counts

`kitlib/maintlint/ledger.py` gives these counts:
- `stale_cites`: STANDING digest citations of a retired or absent entry;
- `rules`: the rules of each rulebook file (`rules/<name>.md` beside the log,
  `kitlib/ledger/rulebook.py`; docs/ledger.md "Rulebooks"), by file name;
- `over_budget`: the files with more rules than their `Budget:` (default 40);
- `conflict_pairs`: pairs of rules, each counted once, that `rulebook.conflicts` finds sharing
  content words (a rule is read against its own book and the shared one; rules under the same
  heading are not counted);
- `overrides`: repo rules whose key is also a shared rule's.

The counts are read through `rulebook.load` and `rulebook.conflicts`, not parsed again; a rulebook
that does not parse is a note, and no rulebook folder gives zeros.
Each of `stale_cites`, `over_budget` and `conflict_pairs` above 0 is also a finding for the PM,
whose ledger it is: `ledger-stale-cites` (fix: `ledger.py check`, which names each citation, then
re-point or drop the digest line), `ledger-over-budget` (fix: `ledger.py rules --all`, then
`rule-drop` or `rule-set --budget`) and `ledger-rule-conflicts` (fix: `ledger.py conflicts`, which
shows what a rule is set against). Overrides are information only. The text carries numbers and
file names only.

No entry id, rule key or rule text leaves the module. Dropping the module drops the counts, their
findings and nothing else.

## Gate time

`kitlib/maintlint/gatetime.py`, in the code checkout (as "Path checks"). The timings are the
steps of the repository's gate-OK records (`kitlib/records/gate_ok.py`,
`<dev_drive>\pm-kit\kit-gate-ok\<repo key>\<commit>.json`, or with no `dev_drive` the .git
copies in `<git common dir>/kit-gate-ok/`) of full gates only (`mode_of` "full": `gate.py
--full` and full landings); a selected or docs-only gate times only part of a step. A sample is
a step whose result is pass or warn with seconds above 0. Per step, the newest 10 samples
(`WINDOW`) that ended in the last 14 days (`DAYS`) are taken; with 5 or more (`MIN_SAMPLES`),
a median over `.kit.json` `"maint_lint".gate_time.bound_s` (default 900, `BOUND_S`;
docs/kit-json.md) is a `gate-time` finding whose target is the step name. Fewer samples is a
note; no record folder, or no full record in 14 days, is a NOT CHECKED note.

The one write in maint_lint: after the page is published, a plain run files a backlog record for
each `gate-time` finding, through the store `kitlib.dailyfull.file.open_filing_store` opens (the
repository's, or the PM data branch's when that is the implicit default). Its id is
`<prefix>-GATE-TIME-<STEP>-<YYYYMMDD>`, its ref `maint-lint gate-time <step>`, its scope `none`,
unpriced (the `unpriced` check then lists it). Nothing is filed while a live record of that step
(that ref, or an id that is `<prefix>-GATE-TIME-<STEP>-` and exactly 8 digits) is in the store, in any state. The
date in the id lets the next run over the bound file again once that record is closed: the store
refuses an id it closed before. With no `"maint_lint".gate_time.prefix`, the finding shows and
nothing is filed. Each outcome, and each store or config refusal, is one `gate time:` line, never
an error. `--dry-run` files nothing.

## Module scope

`kitlib.modulemap.scopelog.summary`, in the code checkout (as "Path checks"). In a repository
whose `.kit.json` names a module map, it sums the scope records its gates kept in the last 7 days
(docs/module-map.md "Scope records and the weekly sum"): gate runs, distinct heads, the runs that
widened beyond the changed modules (and their distinct heads), and per route (a changed module
and the reason, such as `via edge build: ...` or `via messaging`) the modules it widened to and
its runs, most runs first. Counts only: never a finding, so the exit code is unchanged. It shows
as the result's `module_scope`, a "Module scope" page section (module names and reasons only,
never a path) and one count line, `module scope: N gate run(s) in 7 days (H head(s)), W widened
beyond the changed modules (P%)`.

With no module map, `module_scope` is null and nothing shows. With a map and no record in the
window, the sum is zeros and a note asks whether a gate step runs `module_map.py --scope
--record`. A record that cannot be read is counted in a note; a folder that cannot be read, or no
dev drive, is a `module scope NOT CHECKED` note.

## Known limits

- Gate-OK records are written for passes only, so a slow gate that failed is not a sample.
- After a fix lands, the window still holds the slower samples until newer full gates replace
  them; a record closed before that may be filed again on the next run.
- Step names that make the same id text (`kit_suite` and `kit-suite`) share one record: a live
  record of either holds back the other.
- An A tree is reported only when it has non-empty claims. An empty A tree whose tip is on the
  base's first-parent chain is skipped by the nightly sweep as fresh
  (`kitlib.worktree.sweep`: a fresh worktree is never swept), so neither tool reports it.
- At 10 rows per check, `page.render` can still go over `pagecheck.MAX_BYTES`.
- `closed-unlanded` reads only a close whose subject is `closes <ID>: landed <sha>`. A close by
  hand with another reason names no commit to check, so it is not checked; a `<sha>` the checkout
  does not have is a note.

## Output

The plain run writes two files and prints their paths, the notes, the counts and the time taken:
- `result.json` (`kitlib/maintlint/result.py`). The default is
  `<pm_data>/maint-lint/<repo>/result.json`, a stable folder beside the backlog's `audit/` folder
  under the machine config's `pm_data.path`, left untracked like it.
- `page.html` beside it, or `--out`, from `kitlib/maintlint/page.py`. It passes
  `kitlib.hubsite.pagecheck.check`: everything is escaped, with no script and no link, and a lone
  surrogate is dropped. When the page would be over pagecheck's size cap, it shows fewer rows per
  check and fewer notes.

`--dry-run` needs `--out`. It writes only the page there, never `result.json`, and prints the same
counts.

The result file holds the last run only, written whole. Beside `counts`, `ledger` and `notes`
it holds `module_scope` ("Module scope"). Each finding row holds its `repo`, `check`,
`target`, `text`, `fix`, `age`, `fingerprint` and `new`. The fingerprint hashes the repo, the check
and the target, never the text, so a reworded finding keeps it. A target names the thing itself:
a record id, a record and a scope member, the sorted ids of duplicates, a worktree path, an audit
kind; never a sha or a count. A finding whose fingerprint was not in the previous result is marked
new.

A check that cannot run gives a NOT CHECKED note, never a finding. Notes cover a store that cannot
be opened, no `pm_data`, no ledger configured, a hub that cannot be asked, or a git failure.

After it writes both files, and outside the lock slot, a plain run publishes the page on the
Action Hub (`kitlib/maintlint/publish.py`) as page `maint-lint-<repo>`, one per repository and
written over by each run, with `from` `maint-lint` and the agent token; a scheduled run has no hub
session key and needs none (docs/action-hub.md "pages/<id> and page_html/<id>"). Over a page
already there it sends that card's version as `if_version`. The page is judged by
`kitlib.hubsite.pagecheck.check` before it is sent. It prints `PUBLISHED maint-lint-<repo> v<n>`;
a page that was not sent (the page check refuses it, the hub is not running or cannot be reached,
the server refuses the write) is one `hub page NOT CHECKED: <why>` note instead, and the exit code
stays what the findings make it. `--dry-run` never publishes.
