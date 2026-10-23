# Maintenance lint

`tools/maint_lint.py` lints one repository's upkeep, read-only: its backlog store, its worktrees
and the machine's decision ledger. The logic is `kitlib/maintlint/`. It edits no record, no ledger
entry, no worktree and no git ref. Every finding prints the command that fixes it, and nothing
runs that command.

    python tools/maint_lint.py [-C DIR] [--dry-run] [--result FILE] [--out FILE]

Exit codes: 0 no finding, 1 findings, 2 USAGE.

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
  not flagged.
- `audit`: `kitlib.backlog.audit_check.run` over 7 days, with no fetch. Its UNAUDITED, UNKNOWN and
  UNMAPPED lines become findings.

## Path checks

`kitlib/maintlint/verify.py`. Paths are read in the checkout given, except in a PM data worktree:
there they are read in the code repository's main checkout, against its code main
(`kitlib.maintlint.code_checkout`), because the records name the code's files.
- `body-path-gone` and `file-moved`: `stale.sweep`'s missing body paths, and `files` entries
  changed or moved since `files_at`.
- `scope-path-gone`: a scope path that is missing from the checkout and that the main branch's
  history once held. A path no commit ever held is a file still to be made, so it is not flagged.

## Worktree checks

`kitlib/maintlint/worktrees.py` reports one finding per A, B, C, E and O tree from `classify`, read
without apply. Each finding carries its `worktree_remove.py` form. An A tree whose branch still
holds a pulled record also gives `landed-still-pulled`, whose fix is a `release`.

## Ledger counts

`kitlib/maintlint/ledger.py` gives three numbers:
- `stale_cites`: STANDING digest citations of a retired or absent entry;
- `superseded`: entries that another entry's Supersedes names;
- `superseded_live`: how many of those are not retired.

No entry id and no entry text leave the module. Dropping the module drops the counts and nothing
else.

## Output

`--dry-run` writes nothing. It prints each finding with its fix, the notes, the counts and the
time taken.

Without `--dry-run`, the tool writes two files:
- `result.json` (`kitlib/maintlint/result.py`). The default is
  `<kit temp>/<repo>-maint-lint/result.json`, where the kit temp is `kitlib.config.user.work_temp`
  of the checkout, outside every repository.
- `page.html` beside it (`--out`), from `kitlib/maintlint/page.py`. It passes
  `kitlib.hubsite.pagecheck.check`: everything is escaped, with no script and no link. When the
  page would be over pagecheck's size cap, it shows fewer rows per check.

The result file holds the last run only, written whole. A finding whose fingerprint (its check, key
and text) was not in the previous result is marked new.

A check that cannot run gives a NOT CHECKED note, never a finding. Notes cover a store that cannot
be opened, no `pm_data`, no ledger configured, or a git failure.

This slice does not post to the Action Hub and has no schedule.
