# Backlog store (design)

Held for the joint review. Nothing here is live until then.

## Goal

A backlog add, edit, pull or close is one tool call that takes seconds. No work branch, gate, review, lander, batch or 18:00 wait (today about 9 steps and up to hours).

## Where records live

- Each repo keeps its own backlog, in its own repo: no extra repo, nothing shared between projects.
- Records are `backlog/<ID>.md` (same format as today) on the ref `refs/backlog/main`.
- The ref has no shared history with main. It never merges, rebases or fast-forwards with main; the two move independently.
- `.kit.json` `"backlog": {"store": "ref" | "branch" | "main"}`, default `ref`.
  - `ref`: hidden from branch lists and PR menus, not fetched by a default clone (the tool adds the fetch refspec, only once a fetch found the store on the remote: kitlib/backlog/store.py `RefBackend.load`; a refspec for a ref the remote lacks fails every plain `git fetch`).
  - `branch`: the same on a visible `backlog` branch, browsable on GitHub.
  - `main`: today's behaviour, records in main through landings.

## Write path (kitlib/backlog/store.py)

One call does:
1. Fetch the store ref (no checkout; the working tree and main are never touched).
2. Plan the change against the fetched store and validate it (schema, ids, scope, ruling gates).
3. Build the new tree with a temporary index (`GIT_INDEX_FILE`), `commit-tree` on the fetched tip.
4. Push with `--force-with-lease=<ref>:<fetched tip>`, a compare-and-swap. On a lost race: refetch, re-plan, retry (bounded). A repo with no remote uses `update-ref` with the old value.

The commit message names the verb, id, session and any ruling, so `git log refs/backlog/main` is the audit trail; undo is a revert on the ref.

## Reads

`next`, `show`, `list`, `check`, the guards and the hub's backlog view (`kitlib.hubbacklog`, `kitlib.hubsite.backlog`) read the store ref after a fetch. Checks that need code (scope paths exist) read main's tree directly; nothing is copied between the two.

## Lifecycle changes

- `pull` and `release` write at once, so claims are visible to every coordinator immediately. `pull` records the branch: `--as <branch>`, else the branch checked out in `-C`. A pull that would record the configured main branch (made in the main checkout, or `--as <main>`) is refused, and in the PM data worktree, whose configured main is its data branch, the code repository's main too (the main checkout's configured mains, `main` and `master` when that cannot be read; pull_branch.py `code_mains`), after the record's own refusals, with how to pull it instead (from the branch's worktree, or `--as <branch>`): the main branch never lands, so no landing would close the record. Built: kitlib/backlog/pull_branch.py `plan_pull`, which kitlib/backlog/opener.py `planner` gives tools/backlog.py `pull`; tests/backlog/test_pull_main_branch.py.
- Taking over a dead pull: `pull` of a pulled record refuses "already in flight", except when the record's `session:` is the caller's `--session` and the pull is dead by the test `next` uses for its "DEAD PULL" line (kitlib/backlog/offer.py `in_flight_split`: git answered, and no local branch, remote-tracking branch or worktree is named by `pulled_by`). Then it moves the pull to the caller's branch with no `release --reason`: `pulled_by` and `pulled_at` are rewritten, pre-emption lines are kept, and the output says "took over a dead pull from <old branch>", with the "warning: scope collides with ..." lines a pre-empting pull prints for live pulls. It takes no `--reason` or `--blocker`. A takeover whose scope collides with a live pull on another branch is refused without `--ruling R-nnnn`, as a pull is (docs/backlog.md "Owner gates"); a `--ruling` on one that collides with nothing is USAGE. Another session, a record with no `session:`, a live `pulled_by` or a git probe that fails still refuses. A `pulled_by` that already landed (a merge with the subject `Merge <branch>` or `Merge <branch>: ...`, as kitlib/gate/land.py writes it, on the configured main branch, committed on or after the `pulled_at` day so an older landing of a reused branch name does not count; plan.py `landing_merge`) is refused with "close the record"; a history git cannot read fails. Built: kitlib/backlog/plan.py `_plan_takeover`; tests/backlog/test_pull_takeover.py.
- Closing at landing: after `gate.py land` verifies the push, it closes every record still pulled by the landed branch ("landed <merge sha>"), unless a commit of the branch carries a `Part-of: <ID>` line: then the landing is one part of a record, and it closes only the records its `Closes:` lines name and releases every other record the branch holds pulled (below). A failure prints a WARNING with the command; the landing stands. Builders `release` what they did not finish.
  - A branch commit may name records on its own line (`Closes: <ID>[, <ID>...]`) anywhere in its message, not only in git's trailer block, since tools/commit.py writes the attribution as a last paragraph of its own: the line starts with `Closes:` (that case only), each following line that starts with a space or tab continues its value, and the first word of each comma-separated value is the id. Prose (`closes D2`, or `Closes: D2` inside a sentence) names nothing. Built: at_landing `closes_values`. The landing closes each one that is pulled, whoever pulled it, and its `closed` line says it was named by the trailer and who had pulled it. Read from `<tip> --not <the main it merged onto>` for each branch of a batch, so a trailer already on main closes nothing. A record closed this way while a live branch holds it that is neither main (the code repository's, in the PM data store too) nor a branch of the same batch (a local, remote-tracking or worktree branch by its `pulled_by`: offer.py `live_branches`; a probe that fails counts as live) also prints a WARNING to tell that branch's builder. A named record that is not pulled, or one the store refuses to close (owner-held), is a WARNING with the `close` command; the ids no store holds are one NOTE line (a typo, or a case that differs from the file name), printed only when every store taking part was read; trailers git cannot read are a WARNING and the branch's own pulls still close. In the PM data branch's folder store (below), for a named record: after a close that was refused and put back, the WARNING gives the close command while anyone still holds the record pulled, and names that holder (`at_landing._after_put_back` with `named`); a close left uncommitted, or committed with a stale index, still prints the WARNING to tell a live holder's builder (`_close_in`). Built: at_landing `closes_trailers`, with the tip and base kitlib/gate/land.py `close_pulled` passes; tests/gate/test_land_closes_trailer.py.
  - A branch that lands one part of a record a later branch finishes writes `Part-of: <ID>[, <ID>...]` on its own line, read by the `Closes:` line's rules (line start, that case only, folded lines, the first word of each comma-separated value; `trailer_values` with `PART_KEY`), from the same `<tip> --not <base>` for each branch of a batch, so a `Part-of:` line already on main, or in another branch of the batch, changes nothing. Any such line, even one whose value holds no plain id, makes the landing one part: each record the branch holds pulled that no `Closes:` line names is released, not closed: `state: open`, its pull and pre-emption lines cleared (`plan.py` `plan_release`, as the branch itself, so no liveness probe), so `next` offers it again. The reason is `part landed <merge sha> (a Part-of line of <branch>); the rest is not done`, the store commit's subject `releases <ID>: <reason>` and its line `released <ID> in the backlog store ...` (at_landing `release_reason`, `_release_plan`). So a mistyped `Part-of:` id releases rather than closes. A record another branch holds is never released: a `Part-of:` id the branch holds pulled in no store read (a typo, or another branch's record) is one NOTE line, nothing written, printed only when every store taking part was read; an id both a `Closes:` and a `Part-of:` line name is closed, with a NOTE line. A release goes through the same per-store steps as a close (`_close_in`: the clean check, the index lock, put back when overtaken, uncommitted, stale index), and its WARNINGs say released and give `backlog.py --as <branch> release <ID> --reason "<reason>"` in place of the close command (`_Act`, `_after_put_back` with `act`); a record the branch no longer holds pulled is left as it is. No `Part-of:` line: exactly the closes above. When the lines cannot be read, `Part-of:` lines are not read either and the branch's own pulls close, as the WARNING says (`close_landed`). Built: at_landing `part_trailers`, `close_landed`; tests/backlog/test_at_landing_part.py.
  - Built: kitlib/backlog/at_landing.py `close_landed`, after the lock is released (docs/gate.md "Landing", step 9). Records are looked up by file name, in two stores (`_close_in` for each):
    - The configured store, when it is a `ref` or `branch` store: one store commit per record. A configured `main` store, or a remote with no store, gets nothing written and no refspec added.
    - The PM data branch's store (`_pm_data_store`: the store that worktree's own config names, in the worktree that has the PM data branch checked out, unless it is the configured store or the main checkout itself). A `main` (folder) store there: the record file is deleted in that worktree and the deletion is committed on the PM data branch, as every write there is (docs/backlog.md "Stores"; `MainBackend._commit`), and the close line names the commit. The close opens that store with `clean` (`open_backend(..., clean=True)`): before it writes anything, every path the close would write (the record, and each record it unblocks) must match HEAD in that worktree and its index (`MainBackend._require_clean`, a `git status` read with `--no-optional-locks`). One that does not (a hand edit, a staged change, or a record file never committed there) stops that record's close with nothing written (`errors.Dirty`): a WARNING that names the paths, the command to compare them with HEAD first (`git -C "<worktree>" diff HEAD -- "<path>"...`, or `git -C "<worktree>" status --short -- "<path>"...` when one is not in HEAD, which a diff would not show), and the close command to run once that change is committed or put back; the landing's exit code is unchanged. When only that worktree's index differs (its files hold HEAD's content: an `IndexStale` left unrepaired, or HEAD moved by plumbing), the WARNING gives the index repair (`errors.Dirty.repair`, `git -C "<worktree>" reset -q -- "<path>"...`) in place of the compare, then the close command. Built: tests/gate/test_land_store.py `PmDataClose`, tests/backlog/test_pmdata_commit.py. A `backlog.py` write there does not use `clean`, so it still writes over such a change. The close, like every write there, holds git's index lock in that worktree from before it reads HEAD's commit or checks the files until its index update (`MainBackend._lock_index`, docs/backlog.md "Stores"), so a `git commit` there in the middle of it fails instead of committing an index that would revert the close. When another git command holds that lock for `INDEX_WAIT` (2 s), the close stops with nothing written (`errors.Busy`): a WARNING that says it was not closed, with the error, which names the lock file, and the close command; the landing's exit code is unchanged. A close whose commit fails for any other reason leaves the file deleted and uncommitted, and is a WARNING whose command is `catch-up --apply` with every path the close wrote, each in double quotes, the records it unblocked included (`errors.Uncommitted.paths`). A close stopped because another commit changed one of its paths on HEAD (`errors.Overtaken`) puts each file it wrote back (`MainBackend._put_back`: a path that commit changed to HEAD's content, any other to what it held before the close) and is a WARNING that says the close was refused and, from the store read again (`at_landing._after_put_back`), one of five things: the record is still pulled by the branch (for a record a `Closes:` line names, by anyone, naming the holder), with the close command; the store no longer holds it (that commit closed or removed it), with no command; it is in another state and no longer pulled by the branch (for a named record, by anyone), with the `show` command and no close command; its file cannot be read as that commit left it, with the `show` command; or the store could not be read again (a `Refusal` or an `OSError`), with the `show` command. For a file left (`Overtaken.left`) the WARNING says not to commit over that commit and gives `git -C "<worktree>" diff HEAD -- "<path>"...` to compare first. None gives a ready-made `catch-up --apply` command. A close committed with that worktree's index left stale (its update, or the move of the updated copy over it, failed) is a WARNING that says it was closed and gives the `git reset -q -- "<path>"...` repair (`git reset -q` with no paths when it has no index file), never a close command (`errors.IndexStale`). A `branch` store there (`ref` is refused for the data branch, docs/pm-data-branch.md "Layout"): one store commit per record, as above; a remote with no store gets nothing written.
  - A record the branch no longer holds (one a trailer names: that is no longer pulled) is left as it is. An owner-held record needs `--ruling`, so it is a WARNING whose command carries `--ruling R-nnnn`.
- `close` and `retire` stay as verbs for work that never lands.
- The main-checkout refusal for write verbs is dropped: writes never touch a checkout.

## Merge safety

- Plain git refuses unrelated histories; GitHub cannot open a PR between them.
- Records sit under `backlog/`, so even a forced merge overwrites nothing.
- `gate.py land` refuses a branch whose history contains the store's root commit. Built: kitlib/backlog/at_landing.py `ancestry_refusal`, the last locked check. It reads the store's tip from the remote (`ls-remote`, objects fetched with an empty `--refmap`, so no ref moves) and the local copy. A store it cannot read, remote or local, refuses the landing.

## Guards and hooks

- Kit push_block: allow pushes to the store ref whose commits change only `backlog/`; refuse anything else to it.
- A repo's own pre-push hook gets the same allowance at switch-over.

## Readers to repoint

the hub's backlog snapshot (`kitlib.hubbacklog`), kit pmstart/coordinatorstart, `landable`. A repo's own tools that read `docs/backlog` (landing scope, gate scope, doc index, checkout guard) lose those rules when the repo switches (its own tools retire then anyway).

## Migration (per repo, at go-live)

1. Land or port every in-flight records branch first.
2. `backlog.py migrate`: copy `docs/backlog/*.md` at main's tip into the store as its first commit, citing that sha. One-off steps for an older record layout run here: tombstone files are not copied (their ids stay checkable through git history), `migrated` is stripped, anchor and artefact fold into the body, and `reref` is dropped.
   - Built: kitlib/backlog/migrate.py; docs/backlog.md "Commands" has the verb. It only starts a store: an existing one, or one created while it runs, is REFUSED. Files that are not `*.md` are named and not copied. An unreadable record stops it before any write. FAIL findings are printed and copied as they are.
   - Try it on a scratch clone first (no real store written; the repo's `.kit.json` cut to `repo` and `backlog`): `backlog.py -C <scratch clone> migrate --dry-run`, then `migrate` (`--to ref` for a repo whose store is a folder), then `check`; the clone's origin is a bare scratch copy.
3. One normal branch in the repo: delete `docs/backlog`, set `.kit.json` backlog.store, update hooks. Its coordinator builds and lands it.
4. Old record history stays in main's git log.

## Build order

1. kitlib/backlog rewrite with the store layer and three backends. The write path is compare-and-swap concurrency: strong-worker.
2. Landing close and the ancestry refusal in kitlib/gate land. Built on kit/store-landing.
3. The hub's backlog view (`kitlib.hubbacklog`) reads the store.
4. migrate verb, tested on a scratch clone of each repo. Built on kit/store-landing.
5. Joint review, then per-repo switch-over.

## Rules that end at switch-over

The 18:00 records batch, "no separate records commits", and a records-only landing rule in a repo's own lander. They are superseded by entries logged at go-live.

## Open

- Story intake: file the owner's story at once, unpriced, and let the coordinator review it after? Or keep review before filing.
- Hub edits (deferred): the page calls the same verbs through a local tool server, the click logged as the ruling. Its own design once the store exists.
