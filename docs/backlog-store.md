# Backlog store (design)

## Goal

A backlog add, edit, pull or close is one tool call that takes seconds: no work branch, gate, review or lander.

## Where records live

- Each repo keeps its own backlog, in its own repo: no extra repo, nothing shared between projects.
- Records are `backlog/<ID>.md` on the ref `refs/backlog/main`.
- The ref has no shared history with main. It never merges, rebases or fast-forwards with main; the two move independently.
- `.kit.json` `"backlog": {"store": "ref" | "branch" | "main"}`, default `ref`, which a write must name: the default alone is read-only (docs/backlog.md "Stores").
  - `ref`: hidden from branch lists and PR menus, not fetched by a default clone (the tool adds the fetch refspec, only once a fetch found the store on the remote: kitlib/backlog/store.py `RefBackend.load`; a refspec for a ref the remote lacks fails every plain `git fetch`).
  - `branch`: the same on a visible `backlog` branch, browsable on GitHub.
  - `main`: records in main through landings.

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

- `pull` and `release` write at once, so claims are visible to every coordinator immediately. `pull` records the branch: `--as <branch>`, else the branch checked out in `-C`. A pull that would record a main branch (the configured one, or in the PM data worktree the code repository's) is refused with how to pull it instead: the main branch never lands, so no landing would close the record (kitlib/backlog/pull_branch.py).
- Taking over a dead pull: `pull` of a pulled record refuses "already in flight", except when the record's `session:` is the caller's `--session` and the pull is dead by the test `next` uses for its "DEAD PULL" line. Then it moves the pull to the caller's branch and says "took over a dead pull from <old branch>". A takeover whose scope collides with a live pull needs `--ruling R-nnnn` (docs/backlog.md "Owner gates"). A `pulled_by` that already landed is refused with "close the record" (kitlib/backlog/plan.py `_plan_takeover`).
- Closing at landing: after `gate.py land` verifies the push, it closes every record still pulled by the landed branch ("landed <merge sha>"), after the lock is released (docs/gate.md "Landing", step 9; kitlib/backlog/at_landing.py `close_landed`). A failure prints a WARNING with the command; the landing stands. Builders `release` what they did not finish.
  - `Closes: <ID>[, <ID>...]` on its own line in a branch commit closes each named record that is pulled, whoever pulled it. Prose (`closes D2`) names nothing. Only commits the main it merged onto lacks are read. A named record that is not pulled, or that the store refuses to close (owner-held: the WARNING's command carries `--ruling R-nnnn`), is a WARNING; an id no store holds is a NOTE.
  - `Part-of: <ID>[, <ID>...]` makes the landing one part of a record: the records its `Closes:` lines name close, and every other record the branch holds pulled is released (`state: open`, so `next` offers it again) with the reason `part landed <merge sha> ...`. The branch that finishes it pulls it again and lands with `Closes: <ID>`.
  - The implicit default store (`refs/backlog/main` while `.kit.json` names no store) is only read at a landing, never written: a record stranded there is a WARNING per record, and `backlog.py check` adds a `default store` WARN line.
  - Records are closed in the configured `ref` or `branch` store and in the PM data branch's store (`pm_data_store`). In the PM data worktree's folder store the close is committed on the data branch; it first requires every path it writes to match HEAD (`errors.Dirty`), holds git's index lock (`errors.Busy` after `INDEX_WAIT`), and puts its files back when another commit changed them (`errors.Overtaken`). Each such stop is a WARNING with the command to compare or repair and the close command; the landing's exit code is unchanged.
- `close` and `retire` stay as verbs for work that never lands.
- Writes never touch a checkout.

## Merge safety

- Plain git refuses unrelated histories; GitHub cannot open a PR between them.
- Records sit under `backlog/`, so even a forced merge overwrites nothing.
- `gate.py land` refuses a branch whose history contains the store's root commit. (kitlib/backlog/at_landing.py `ancestry_refusal`, the last locked check). A store it cannot read, remote or local, refuses the landing.

## Guards and hooks

- Kit push_block: allow pushes to the store ref whose commits change only `backlog/`; refuse anything else to it.
- A repo's own pre-push hook gets the same allowance at switch-over.

## Migration (per repo)

1. Land or port every in-flight records branch first.
2. `backlog.py migrate`: copy `docs/backlog/*.md` at main's tip into the store as its first commit, citing that sha.
   - docs/backlog.md "Commands" has the verb. It only starts a store: an existing one, or one created while it runs, is REFUSED. Files that are not `*.md` are named and not copied. An unreadable record stops it before any write. FAIL findings are printed and copied as they are.
   - Try it on a scratch clone first (no real store written; the repo's `.kit.json` cut to `repo` and `backlog`): `backlog.py -C <scratch clone> migrate --dry-run`, then `migrate` (`--to ref` for a repo whose store is a folder or whose `.kit.json` names no store: the default `ref` is read-only, and both runs are refused without it), then `check`; the clone's origin is a bare scratch copy.
3. One normal branch in the repo: delete `docs/backlog`, set `.kit.json` backlog.store, update hooks. Its coordinator builds and lands it.
4. Old record history stays in main's git log.

## Open

- Story intake: file the owner's story at once, unpriced, and let the coordinator review it after? Or keep review before filing.
- Hub edits (deferred): the page calls the same verbs through a local tool server, the click logged as the ruling. Its own design once the store exists.
