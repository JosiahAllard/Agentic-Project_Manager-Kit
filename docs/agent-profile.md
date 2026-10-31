# Agent profile: the kit

Read by the kit's agents section by section; they find their sections by these headings, so the
headings stay. Owner-only. Nothing here names a machine, a drive, a person or another repository:
machine values come from the machine config (docs/kit-json.md "Machine config keys") and the tools that read it.

No profile: a repository's profile is optional (docs/multi-host-plan.md decision 26). A role whose
repository has none reads the kit's `content/default-profile.md`, which has every heading below,
plus the repository's README and instructions file where they exist. The same holds for the
coordinator profile: with none, the coordinator works from the repository's own files.

## Branches and worktrees
AGENTS.md: a branch in a worktree under `.claude/worktrees/<slug>` made by `tools/new_worktree.py` (docs/new-worktree.md,
docs/worktree-paths.md), never the main checkout. Commit by explicit path with `tools/commit.py`
(docs/commit.md). Remove only your own worktree, with `tools/worktree_remove.py` (docs/worktree-remove.md).
A session started with `launch.py --home-worktree` runs in a detached, locked `home-<session name>`
worktree (docs/launcher.md "The home worktree"): never edit or check out in it; it is not a task worktree.

## Build and test
The gate is `python tools/gate.py` (docs/gate.md); `--full` runs the whole suite and takes the
machine lock itself. Selected tests: `python tools/kit_tests.py --select` (what the change reaches;
`--changed <path> ...` names the paths, `--plan` prints the pick; docs/test-tools.md).
Red-first through `tools/redfirst.py` (docs/redfirst.md), never an in-place mutant.

### Known flakes
None recorded. A red `gate.py --full` gets one targeted rerun of its failing tests, `python tools/gate.py --rerun` (docs/gate.md "Targeted rerun"): FLAKE lands the tip, and you report the flake record line it prints; REAL, or a second failure of the same test, is real and never lands. Report both runs.

## Test runner
Pass line `kit_tests: OK`; anything else is a failure. Report the summary line and the failing
module lines only.

## Landing
Landing is standing owner-approved work and needs no further go: the coordinator queues a verdict for
every landing, then submits it. Before a landing, per branch, from the main checkout:
`python tools/verdict.py add -C <repo> --branch <branch> --tip <sha> --verdict LAND ...`
(docs/gate.md "Strict paths"), then `python tools/land.py <branch> --expect-tip <sha> --reviewed-by <reviewer id> --submit` (the verdict's reviewer_id), then
the `gate.py wait-result ... --kind land` command it prints, kept running in the background. A lander
role may do the submit, wait and report for the coordinator. No agent merges or pushes, and no
expected-main hash is given: a detached process books its turn in the machine lock (the landing
queue), so submitted landings merge in ticket order, each onto the main it finds; it gates the merge
in its own landing tree and pushes only when green (docs/gate.md "Landing"). Success: a `RESULT pass
land` line with `LANDED `. `CONFLICT <branch> with main <sha>: the author merges main` goes back to
the branch's author; any other verdict line (`NOT LANDED:`, `STUCK:`, `REFUSED:`, `BUSY:`, `NOT
RUN:`, `USAGE:`, `GATE FAILED:`, `RERUN VOID:`) is reported as is, with the doc's recovery steps, and not retried. Batches allowed: up to
`"landing".batch_max` branches (3), each with its own reviewer, range and LAND verdict; never a
branch changing a strict path (docs/gate.md "Batch").

## Review
A coordinator reviews a branch itself when it is small and low risk: read every hunk; skim
docs and additive content. "Small" is the `.kit.json` key `landing.self_review_max_lines`
(docs/kit-json.md): `tools/verdict.py add` refuses a coordinator's own review of a larger diff, so a
larger diff needs an analyst review. Larger, Medium or High: a fresh reviewer.
Continue an agent 2-3 rounds per artefact, then reseed from a short written summary. Reseed a
strong- or deep-tier coder whose context is above about 150k tokens.

## Shared resources that must be serialised
The machine lock (docs/lock-api.md), through the live install only, never a copy on a branch.
Never pre-empt a holder. Temp folders: the tools' own (machine config `temp_dir`, docs/kit-json.md "Machine config keys"); do not
hardcode a temp path.

## Risk map
High: `boot/`, `kitlib/host/hookmain.py`, `kitlib/guard/`, `kitlib/gate/`, `kitlib/lock/`, `kitlib/lockmcp/`,
`kitlib/hubmcp/`, `kitlib/mcpwire/`, `kitlib/commit/`, `kitlib/records/`, `kitlib/proc/`, `kitlib/git/`,
`kitlib/backlog/`, `tools/land.py`, `tools/gate.py`, `hosts/*/adapter/`, `hosts/*/shell/hooks/`,
`hosts/*/shell/agents/`, `content/roles/`, `content/skills/`. A High change gets a fresh reviewer before landing. Low: docs.

## Moves and renames
Behaviour-preserving only; old paths keep a stub until go-live (docs/go-live.md).

Equivalence check, in full, for every move or rename:
1. Whitelist: the brief lists every old -> new name. Every changed line in
   `git diff <base>..<tip>` is one of those substitutions, a moved block unchanged
   (`git diff -M --color-moved`), or a stub at an old path. Report any other changed line; each
   one is a finding, not part of the move.
2. Full gate on both: run `python tools/gate.py -C <worktree> --full` on the merge base and on
   the tip, and keep both suite logs. The `Ran N tests` count and the skip count must match, and no
   test name may be gone (a test that moved with its module keeps its name under the new path; list
   each pair in the report).
3. Guard parity: when the move touches kitlib/guard, boot/hook.py, or any path or name the guard
   matches, run the guard's `decide()` over every row in tests/guard on the base and on the tip.
   Every decision must be identical (rows whose expected text names the old path change only by the
   whitelisted substitution).
4. Grep the old name across the tree: zero hits, or each remaining hit explained (the stub itself,
   tests/oldpaths, history in docs/go-live.md). The stub left at the old path still runs and names
   the new one (tests/oldpaths).

Any difference in steps 2 or 3 is a hard stop, not a re-baseline: report both sides and stop. No
known tie in this repo allows a rerun.

## Records
The configured backlog store (docs/backlog-store.md), written with
`tools/backlog.py`. A builder sets its own record and names it in a `Closes:` trailer in its
last commit; it never runs `backlog.py close` (land.py closes it at landing). No ruling ids,
owner names or machine paths in tracked kit files (the scrub test enforces it).

## Art
Not applicable.

## Audio
Not applicable.
