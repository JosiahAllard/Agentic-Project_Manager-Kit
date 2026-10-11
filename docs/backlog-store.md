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

`next`, `show`, `list`, `check`, the guards and hub_backlog read the store ref after a fetch. Checks that need code (scope paths exist) read main's tree directly; nothing is copied between the two.

## Lifecycle changes

- `pull` and `release` write at once, so claims are visible to every coordinator immediately. `pull` records the branch.
- Taking over a dead pull: `pull` of a pulled record refuses "already in flight", except when the record's `session:` is the caller's `--session` and the pull is dead by the test `next` uses for its "DEAD PULL" line (kitlib/backlog/offer.py `in_flight_split`: git answered, and no local branch, remote-tracking branch or worktree is named by `pulled_by`). Then it moves the pull to the caller's branch with no `release --reason`: `pulled_by` and `pulled_at` are rewritten, pre-emption lines are kept, and the output says "took over a dead pull from <old branch>", with the "warning: scope collides with ..." lines a pre-empting pull prints for live pulls. It takes no `--reason`, `--ruling` or `--blocker`. Another session, a record with no `session:`, a live `pulled_by` or a git probe that fails still refuses. A `pulled_by` that already landed (a merge with the subject `Merge <branch>` or `Merge <branch>: ...`, as kitlib/gate/land.py writes it, on the configured main branch, committed on or after the `pulled_at` day so an older landing of a reused branch name does not count; plan.py `landing_merge`) is refused with "close the record"; a history git cannot read fails. Built: kitlib/backlog/plan.py `_plan_takeover`; tests/backlog/test_pull_takeover.py.
- Closing at landing: after `gate.py land` verifies the push, it closes every record still pulled by the landed branch ("landed <merge sha>"). A failure prints a WARNING with the command; the landing stands. Builders `release` what they did not finish.
  - Built: kitlib/backlog/at_landing.py `close_landed`, after the lock is released (docs/gate.md "Landing", step 9). One store commit per record, looked up by file name. A record the branch no longer holds is left as it is. An owner-held record needs `--ruling`, so it is a WARNING whose command carries `--ruling R-nnnn`. A `main` store, or a remote with no store, gets nothing written and no refspec added.
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

hub_backlog snapshot, kit pmstart/coordinatorstart, `landable`. A repo's own tools that read `docs/backlog` (landing scope, gate scope, doc index, checkout guard) lose those rules when the repo switches (its own tools retire then anyway).

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
3. hub_backlog reads the store.
4. migrate verb, tested on a scratch clone of each repo. Built on kit/store-landing.
5. Joint review, then per-repo switch-over.

## Rules that end at switch-over

The 18:00 records batch, "no separate records commits", and a records-only landing rule in a repo's own lander. They are superseded by entries logged at go-live.

## Open

- Story intake: file the owner's story at once, unpriced, and let the coordinator review it after? Or keep review before filing.
- Hub edits (deferred): the page calls the same verbs through a local tool server, the click logged as the ruling. Its own design once the store exists.
