# Agent profile: the kit

Read by the kit's agents section by section; they find their sections by these headings, so the
headings stay. Owner-only. Nothing here names a machine, a drive, a person or another repository:
machine values come from `.kit.json` and the tools that read it.

## Branches and worktrees
CLAUDE.md: a branch `kit/<slug>` in a worktree made by `tools/new_worktree.py` (docs/new-worktree.md,
docs/worktree-paths.md), never the main checkout. Commit by explicit path with `tools/commit.py`
(docs/commit.md). Remove only your own worktree, with `tools/worktree_remove.py` (docs/worktree-remove.md).

## Build and test
The gate is `python tools/gate.py` (docs/gate.md); `--full` runs the whole suite and takes the
machine lock itself. Single packages: `python tools/kit_tests.py <tests/pkg>` (docs/test-tools.md).
Red-first through `tools/redfirst.py` (docs/redfirst.md), never an in-place mutant.

### Known flakes
None recorded. A failure is real until the same tip passes a rerun; report both runs.

## Test runner
Pass line `kit_tests: OK`; anything else is a failure. Report the summary line and the failing
module lines only.

## Landing
Landing is standing owner-approved work: the lander lands a reviewed branch on the coordinator's or
project manager's go, with the reviewer's verdict in its brief. From the main checkout, on main,
after `git merge --ff-only <remote>/<main>`:
`python tools/gate.py land <branch> --expect-main <sha> --expect-tip <sha>`.
It queues, locks, merges, gates the merge, pushes and verifies (docs/gate.md "Landing"). Success: a
`LANDED ` line and exit 0; any other verdict line (`NOT LANDED:`, `STUCK:`, `REFUSED:`, `BUSY:`) is
reported as is, with the doc's recovery steps, and not retried. Batches: docs/gate.md.

## Shared resources that must be serialised
The machine lock (docs/lock-api.md), through the live install only, never a copy on a branch.
Never pre-empt a holder. Temp folders: the tools' own (`.kit.json`, docs/kit-json.md); do not
hardcode a temp path.

## Risk map
High: `claude/hooks/`, `kitlib/guard/`, `kitlib/gate/`, `kitlib/lock/`, `kitlib/lockmcp/`,
`kitlib/proc/`, `kitlib/git/`, `kitlib/backlog/`. A High change gets a fresh reviewer before landing. Low: docs.

## Moves and renames
Behaviour-preserving only; old paths keep a stub until go-live (docs/go-live.md).

## Records
The configured backlog store (docs/backlog-store.md), written with
`tools/backlog.py`. A builder sets and closes its own record. No ruling ids, owner names or
machine paths in tracked kit files (the scrub test enforces it).

## Art
Not applicable.

## Audio
Not applicable.
