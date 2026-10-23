# Maintenance lint

`tools/maint_lint.py` lints one repository's upkeep, read-only: its backlog store, its worktrees
and the machine's decision ledger. The logic is `kitlib/maintlint/`. It edits no record, no ledger
entry, no worktree and no git ref. Every finding prints the command that fixes it, and nothing
runs that command.

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
  live client, but which no hold keeps out of the offer. The patterns are copied from
  `kitlib.backloglint`, and the body is not read. The fix is a `block --on owner|live-client`
  whose reason the PM writes, because an owner hold must cite what it waits on.
- `unpriced`: the records `backlog.py needs-priority` lists.
- `dead-pull`: a pull with no branch by its `pulled_by` (`offer.in_flight_split`). A failed git
  probe gives a note and no finding.
- `dead-blocker`: an `item:X` blocker that names no live record (`store.dangling_items`). Closing
  or retiring a record deletes it.
- `duplicate`: live records whose summaries are the same words in the same order. A near match is
  not flagged. Its target is the sorted record ids.
- `answered-question`: a parked record whose question (`q`) the owner has answered on the Action
  Hub (status answered or logged). The hub's questions are read, read-only, with the agent token
  through `kitlib.hubsite.client`, as `tools/hub.py` reads them, and only when a record is parked.
  The fix is `unpark <ID>`, giving the ruling it was logged as, after the PM logs the answer when
  it is not logged yet. No ruling id is shown. A hub that cannot be asked gives a NOT CHECKED note.
- `audit`: `kitlib.backlog.audit_check.run` over 7 days, with no fetch. It gives one count finding
  per kind (UNAUDITED, UNKNOWN, UNMAPPED), for example "12 store change(s) in 7 days with no audit
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
  gives `landed-still-pulled` per claim, whose fix is a `release`. An A tree no record claims is
  the sweep's to remove.
- a B or C tree only when no stale mark holds on its branch. A tree whose mark holds is the sweep's
  to abandon; one whose mark is void is reported.
- every E and O tree.

Each finding carries its `worktree_remove.py` form.

## Ledger counts

`kitlib/maintlint/ledger.py` gives three numbers:
- `stale_cites`: STANDING digest citations of a retired or absent entry;
- `superseded`: entries that another entry's Supersedes names;
- `superseded_live`: how many of those are not retired.

No entry id and no entry text leave the module. Dropping the module drops the counts and nothing
else.

## Known limits

- An A tree is reported only when it has non-empty claims. An empty A tree whose tip is on the
  base's first-parent chain is skipped by the nightly sweep as fresh
  (`kitlib/worktree/sweep.py` 260-262), so neither tool reports it.
- At 10 rows per check, `page.render` can still go over `pagecheck.MAX_BYTES`.

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

The result file holds the last run only, written whole. Each finding row holds its `repo`, `check`,
`target`, `text`, `fix`, `age`, `fingerprint` and `new`. The fingerprint hashes the repo, the check
and the target, never the text, so a reworded finding keeps it. A target names the thing itself:
a record id, a record and a scope member, the sorted ids of duplicates, a worktree path, an audit
kind; never a sha or a count. A finding whose fingerprint was not in the previous result is marked
new.

A check that cannot run gives a NOT CHECKED note, never a finding. Notes cover a store that cannot
be opened, no `pm_data`, no ledger configured, a hub that cannot be asked, or a git failure.

This slice does not post to the Action Hub.
