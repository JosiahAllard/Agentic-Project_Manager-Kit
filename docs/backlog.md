# backlog.py

The backlog's reads and writes: `tools/backlog.py` (entry) over `kitlib/backlog` (library). docs/backlog-store.md is the design; this page is the behaviour.

A program opens the configured store the way the tool does with kitlib/backlog/opener.py `open_store(root, fetch=True, session=None, branch=None)`, which returns the plan Context (its `store.load()` reads), and maps a write verb to its plan with `planner` (a verb that is no write is `USAGE:`, raised before any plan runs); opener.py `retarget_dry` returns the Change a `retarget` without `--apply` would write.

## Stores

`.kit.json` `"backlog".store` picks the backend (kitlib/backlog/store.py `open_backend`):

| Value | Records live in |
|---|---|
| `ref` | `backlog/<ID>.md` on `refs/backlog/main`, which shares no history with main |
| absent | the same `ref` store, read-only |
| `branch` | the same tree on a `backlog` branch |
| `main` | `docs/backlog/<ID>.md` in the checkout; any other value is that folder |

The `ref` store reached by the default alone (neither `.kit.json` nor `migrate --to` names a store) reads as usual, and every write to it is REFUSED, exit 2, before anything is built (`store.default_store_refusal`), so no repository writes records to a ref nobody reads beside its live store. The refusal names the PM data worktree's store to write to with `-C` when there is one, and always says to set `"backlog": {"store": "ref"}` to write the ref store. A landing never writes the default store. Tests: `tests/backlog/test_store_default_ref.py`.

A ref or branch write (`Store.write`) loops: fetch, plan against that snapshot, validate, build the tree in a temporary index, `commit-tree` on the fetched tip, then `push --force-with-lease=<ref>:<tip>` (with no remote, `update-ref <ref> <new> <tip>`). A lost race reruns the whole attempt, plan included, up to `ATTEMPTS` (5); after that it is ERROR and nothing was written. When a failed push leaves it unknown whether the write landed, the ERROR says to read the store (`check` fetches it) before retrying. A write is refused only for what it adds (a FAIL `check` would report, or an `item:` blocker naming no record), so a store that is already broken can still be repaired. Writes never touch the checkout. A fetching read adds the store's fetch refspec to the remote once, and refuses when the remote has no store but the local ref holds one. The commit body carries `Session:` and, when the write cites one, `Ruling: PM R-nnnn`.

No remote means the repository lists no remote at all; a missing remote beside others is ERROR.

The `main` backend writes files. It has no compare-and-swap on the files: a write checks that each record it rewrites still holds the bytes it read, then writes. So it writes only in a linked worktree and refuses in a main checkout (`MainBackend.publish`). Outside the PM data branch's worktree it commits nothing (landing carries the files).

In the worktree that has the PM data branch checked out, each write commits its own record paths and nothing else (`MainBackend._commit`):

- It first takes git's own lock on that worktree's index (`MainBackend._lock_index`) and holds it until its index update, so a `git commit`, `git add` or `git reset` there meanwhile fails instead of reverting the write. A lock still held after `INDEX_WAIT` (2 s) is ERROR, `errors.Busy`, with nothing written; that lock file is never removed.
- Each file is written whole or not at all; a file that fails to write puts the others back and is ERROR.
- A write stops, ERROR and uncommitted (`errors.Overtaken`), when a commit changed any of its paths on HEAD since it began, and puts each file it wrote back. The message names any file left with `git -C "<worktree>" diff HEAD -- "<path>"...` to compare first.
- Only those paths of the real index are updated, so other staged work is left as it was. A commit that lands with the real index not updated is `errors.IndexStale`: reported as written (`WROTE`, then a `WARNING:` line with the repair `git -C "<worktree>" reset -q -- "<path>"...`), exit 0. A commit that fails after the files are written is ERROR (`errors.Uncommitted`) naming the paths and `catch-up`.
- A backend opened with `clean` (only a landing's close uses it) first requires each path to match HEAD in the worktree and its index (`errors.Dirty`, nothing written). A `backlog.py` write is not `clean`.

Tests: `tests/backlog/test_pmdata_commit.py`.

## Commands

Globals: `-C DIR`, `--session S` (the offer and the commit body), `--as BRANCH` (pull or release as), `--no-fetch` (read the local copy).

| Verb | Does |
|---|---|
| `next [--cap N \| --all] [--with ID]...` | The banner, then up to N offers for `--session` (default 5), then what was passed over and why, then a `blockers, most first:` ranking when any record was passed over on a collision. `--all` offers every startable record, uncapped (USAGE beside `--cap`). A pull whose branch is gone prints `DEAD PULL ID ...`: a pull by the record's own `session:` takes it over, anyone else `release`s it first. `--with ID` puts that record first unless it collides with a live pull. A record whose scope overlaps a path another live pull's branch changed is passed over with `fold it into B, or pull it after B lands` (the fold-in guard, below); when git cannot tell, `next` passes it over rather than failing. Tests: `tests/backlog/test_next_all.py`, `tests/backlog/test_pull_fold_guard.py` `NextFold` |
| `show ID`, `list [--blocked [--on M]] [--needs-priority] [--status S] [--area A]` | Reads; `--needs-priority` is the open unpriced and `priority: -1` records, a record marked for review among them ("Records"). `show` of a missing id is NOT FOUND (exit 1) plus the store commit that deleted its file, or "the id is unknown"; on a `main` store it also says when the file was removed but no commit names it yet |
| `check` | Schema, store and pull checks; no check reads the rulings ledger (below). RED exits 1. `hold citations` WARNs, never red, on a blocked record's `deliberate` or `owner` hold that cites no R-nnnn, Q-nnn, T-nnn or item:<ID> |
| `landable BRANCH...` | 0 LANDABLE, 1 CANNOT TELL, 2 NOT LANDABLE (it holds a pull) |
| `stale [--days N]` | Read-only, default 30 days. Lists records whose last store commit is N+ days old, backticked body paths with no file, and `files` entries changed or gone since `files_at`. A record a test could not judge is NOT CHECKED, never current |
| `add ID --summary S --scope P... --found-on BRANCH\|none --why-not-folded T` | One record, always `priority: -`; `--reopen` to reuse a closed, retired or deleted id. Both fold-in flags are required and checked by the fold-in guard (below) |
| `set ID --<field> V ... [--unset K] [--ruling R] [--body-file F]` | Edits fields; `--body-file F` replaces the whole body (an empty file is refused); `set ID --hold-ruling R` alone re-cites the hold ruling. `--scope P` (repeatable) replaces the whole `scope:` list, so name every path to keep; the output names each entry it removed. On a pulled record, a `--scope` that widens into a live pull, or onto a path another live pull's branch changed, needs `--ruling` (Owner gates, the fold-in guard); narrowing is free |
| `block ID --on M... [--owner-ask Q] [--reason T]`, `unblock ID [--on M] [--reason T] [--ruling R]` | `block` adds to `blocked_on` (a block that adds nothing writes nothing) and takes no `--ruling`. `block --reason T` (12 characters or more) goes in the commit subject and one body line, `Blocked on M (date): T`. `block --on deliberate|owner` needs `--reason` or `--owner-ask` citing the ruling, question, to-do or item it waits on (R-nnnn, Q-nnn, T-nnn or item:<ID>) and its end condition, or it is USAGE: an uncited hold is void. `unblock` clears one blocker: a lone one opens the record; with several, `--on M --reason T` removes M only, and a bare `unblock` is REFUSED with the command for each. Clearing `owner` needs `--ruling` |
| `pull ID [--reason T] [--ruling R]`, `release ID --reason T` | Written at once; `pull` records the branch. Outside the offer it needs `--reason`; a pull whose scope collides with a live pull, or overlaps a path a live pull's branch changed (the fold-in guard), needs `--reason` and `--ruling`. The caller's own branch never collides with itself: `next` run as branch B (`--as B`, or B checked out) offers records whose scope overlaps only B's own pulls, to bundle onto B, and `pull` onto B takes them with no `--reason`. Branch names compare exactly as `pulled_by` is written. Tests: `tests/backlog/test_pull_collision_ruling.py`, `tests/backlog/test_pull_fold_guard.py` |
| `close ID --reason T [--ruling R]`, `retire ID --why T [--ruling R]` | Delete the record and drop `item:ID` from its dependents; an owner-held or owner-priced record needs `--ruling` (Owner gates). `retire --owner-said` is USAGE: the owner's words are never written into a record. `retire` of a pulled record whose branch is live is REFUSED; a dead pull goes with the record |
| `delete ID --why T [--ruling R]` | The record should not exist: deleted as `retire` deletes, with the subject `deletes ID: T`, which history reads as `closes` and `retires` (`add` of the id again needs `--reopen`). Retire's owner gates and live-pull refusal apply |
| `clear-review ID --reason T` | A coordinator drops the review mark ("Records") after reading the record; any state, no ruling, the price untouched. A record with no mark is REFUSED |
| `migrate [--dry-run] [--to ref\|branch] [--dir D]` | Copies `D/*.md` (default `docs/backlog`, or the configured folder) at the remote's main into a new store as its root commit (docs/backlog-store.md "Migration"). With no store named in `.kit.json`, `--to ref` is needed. REFUSED when the store exists |
| `retarget OLD NEW [--apply]` | Rewrites a moved path across every record's `scope:`, `ref:` and `summary:` lines (Retarget, below). A dry run by default: it prints each record and line it would change and writes nothing; `--apply` writes them all as one store commit |
| `catch-up [--apply PATH...]` | A `main` store only. A dry run by default: lists each uncommitted path under the store folder with its kind. `--apply PATH...` commits exactly the named paths, each of which the dry run must list, as one commit, in the PM data worktree only; read the record and compare the file with HEAD first, since it can overwrite another session's commit of that path |
| `set-status ID parked --q Q-nnn`, `unpark ID --ruling R` | Park an open or pulled record on an owner question (a pull is given up; parked records are never offered and take no place in the cap); `unpark` opens it with `resume: R`, which ranks above priority and below a pin |

Exit codes (kitlib/backlog/errors.py): 0 OK; 1 `NOT FOUND:`, `ERROR:`, a RED check; 2 `USAGE:`, `REFUSED:`, or bad arguments. Every write refusal leaves the store untouched. `IndexStale` is reported as written, exit 0 ("Stores").

A record passed over on a scope collision (in `next`, and in a `pull` refused as out of order) names both colliding paths and the holder: `already taken by ID (in flight on BRANCH)` for a live pull, `by ID (session S)` for another session's record, `by ID (offered above)` for one already in the offer A pulled record holds its current `scope:` lines, never its branch's diff, so narrowing them with `set ID --scope P` frees the rest. Taking scope a live pull holds is the PM's call: a `pull` over one, a dead-pull takeover over one, and a `set --scope` that widens a pulled record into one each need `--ruling R-nnnn` (Owner gates).

### The fold-in guard

A finding is fixed on the branch that found it unless it cannot be, so `add` names where it came from and why it was not folded (kitlib/backlog/plan.py `plan_add`; `tests/backlog/test_add_found_on.py`). `--found-on` is the branch the finding was made on (its local name, never a ref spelling or the code main), or `none` when no branch found it or that branch was removed (then `--why-not-folded` names it). A removed branch that landed may still be named: its landing merge on the code main is the proof. `--why-not-folded` is at least 12 characters with no machine path. Both are appended to the summary, `S (found on BRANCH; not folded: T)`, and the commit subject is `backlog: add ID, found on BRANCH`.

- USAGE: either flag missing, a short reason or one holding a machine path, a malformed branch name, `--found-on` naming the code main or spelt as a ref.
- REFUSED: a scope member that overlaps a path the branch changed since its merge base with the code main, or the scope of a record the branch holds pulled. A member that cannot be compared counts as an overlap. Fold it into the branch, or file it after the branch lands; there is no override flag.
- REFUSED: a `--found-on` branch that is not local, not on any remote as last fetched, and has no `Merge B` landing merge on the code main.
- ERROR: a code main that does not resolve, no merge base with it, a git that does not answer, or an unreadable record. The code main is `<code remote>/<code_main>` as last fetched, else the local branch; in the PM data worktree it is the main checkout's (docs/kit-json.md `backlog.code_main`).
- `--found-on none` skips the overlap check.

`pull`, a dead-pull takeover, and `set ID --scope` on a pulled record (for the members it adds only) run the same guard against the other live pulls' branches (kitlib/backlog/plan.py `_pull_fold_hits`; `tests/backlog/test_pull_fold_guard.py`), since a record's declared scope can miss what its branch has actually changed:

- REFUSED: an overlap with a path changed on the branch of any live pull other than the caller's. Overriding it takes `pull ID --reason T --ruling R-nnnn` (a takeover: `--ruling` alone); a write with one prints the fold.
- A code main that does not resolve, or shares no history with a live branch, skips the diffs with a NOTE (set `backlog.code_main`); these never block a pull.
- ERROR: a git that cannot read the code main or a branch, the merge base, or the diff. `next` turns the same failure into a pass-over.

### Retarget

`retarget OLD NEW` follows a moved file or folder through the store (kitlib/backlog/retarget.py `plan_retarget`; `tests/backlog/test_retarget.py`). OLD and NEW are plain repo-relative paths, matched case-sensitively by whole path components; a glob, `none`, a machine path, OLD equal to NEW or one under the other is USAGE.

- `scope:` lines: a member equal to OLD, or under it, is rewritten. A member that covers OLD from above (`docs/**`), or matches only when case is ignored, is listed as `not rewritten`.
- `ref:` and `summary:` lines: OLD is replaced wherever it stands as a whole path.
- Nothing else changes.
- A pulled record whose `scope:` lines it would rewrite is REFUSED while its branch is live, and ERROR when git cannot say; nothing is written.

The dry run plans against a fresh read and runs the same checks a write does. `--apply` writes every changed record in one store commit, `backlog: retarget OLD -> NEW in N record(s)`.

## Owner gates

An owner price (`--priority-by owner|pm`), an owner hold (`blocked_on: owner`, `priority: -1`), and closing, retiring or deleting such a record take `--ruling R-nnnn`; nothing of the owner's is written into the record. A cited id is taken on its word: no write and no `check` reads the rulings ledger. A ruling is refused only for its shape (USAGE), or when the record already spent it (one ruling, one change).

A collision with a live pull (a pulled record whose `pulled_by` branch git still finds; a failed probe holds every pull) on another branch takes the PM's ruling, never `--reason` alone (kitlib/backlog/plan.py `live_hits`; `tests/backlog/test_pull_collision_ruling.py`). This covers a colliding `pull`, a colliding dead-pull takeover, and a `set --scope` that widens a pulled record into one; the refusal names each pair and its holder, and the commit carries the `Ruling:` line. Narrowing, widening into free scope, and a collision on the same branch need nothing. A collision with a record that is not pulled still needs only `--reason`. `unpark`'s ruling is checked as a hold release's and sets `resume`.

## Records

A header key or value is one line: every write refuses a line break or other control character in one with USAGE (kitlib/backlog/record.py `render`); the body is not checked. Keys cut in the rewrite are unknown keys: `keys`, `class`, `small_fix_*`, `migrated`, `artefact`, `anchor`, `hold_spent`, `value*`.

The review mark, `review: <source> <YYYY-MM-DD>`, says a record was added without a coordinator reading it: the Action Hub's `add` writes it through the library; tools/backlog.py's `add` and `set` never write it. While it stands, `pull` is REFUSED (`--reason` and `--ruling` included), as is a takeover of a dead pull of the record, and `next` never offers it: it sits in the banner's `to review` bucket. It can still be priced, blocked, parked or deleted. `clear-review ID --reason T` drops it (Commands). Tests: `tests/backlog/test_review_mark.py`, `tests/backlog/test_cli_review_delete.py`.
