# backlog.py

The backlog's reads and writes: `tools/backlog.py` (entry) over `kitlib/backlog` (library). docs/backlog-store.md is the design; this page is the behaviour. Not live until reviewed.

## Stores

`.kit.json` `"backlog".store` picks the backend (kitlib/backlog/store.py `open_backend`):

| Value | Records live in |
|---|---|
| absent or `ref` | `backlog/<ID>.md` on `refs/backlog/main`, which shares no history with main |
| `branch` | the same tree on a `backlog` branch |
| `main` | `docs/backlog/<ID>.md` in the checkout; any other value is that folder |

A ref or branch write (`Store.write`) loops: fetch, plan against that snapshot, validate, build the tree in a temporary index, `commit-tree` on the fetched tip, then `push --force-with-lease=<ref>:<tip>`. With no remote it runs `update-ref <ref> <new> <tip>`. A lost race (including `cannot lock ref`, read from both of the push's output streams) reruns the whole attempt, plan included, up to `ATTEMPTS` (5); after that it is ERROR and nothing was written. A push that exits non-zero yet left the remote at or past the new commit (kitlib/git/refs.py `is_ancestor`) succeeded and is not re-planned; one rejected with the remote unmoved is ERROR. When the remote or the fetch that would tell cannot be read after a failed push, the ERROR says the write may have landed: read the store (`check` fetches it) before retrying (`RefBackend._maybe_landed`). Without a remote, an `update-ref` that fails or times out after the ref already moved to the new commit succeeded. After a push the local copy of the ref moves only as a fast-forward (`RefBackend._advance_cache`). A write is refused only for what it adds: a FAIL `check` would report, ruling checks aside (`check.fail_findings`), or an `item:` blocker naming no record (`check_change`), so a store that is already broken can still be repaired. Writes never touch the checkout. A fetching read adds the store's fetch refspec (`+refs/backlog/main:refs/backlog/main` for `ref`) to the remote once (`RefBackend.ensure_refspec`), and refuses when the remote has no store but the local ref holds one. The commit body carries `Session:` and, when the write cites one, `Ruling: PM R-nnnn`.

No remote means `git remote get-url <remote>` exits 2 and the repository lists no remote at all (`find_remote`); a missing remote beside others, or a lookup that fails any other way, is ERROR.

The `main` backend writes files and commits nothing (landing carries them). It has no compare-and-swap: a write checks that each record it rewrites still holds the bytes it read, then writes. So it writes only in a linked worktree and refuses in a main checkout (`MainBackend.publish`).

## Commands

Globals: `-C DIR`, `--session S` (the offer and the commit body), `--as BRANCH` (pull or release as), `--no-fetch` (read the local copy).

| Verb | Does |
|---|---|
| `next [--cap N] [--with ID]...` | The banner, then up to N offers for `--session`, then what was passed over and why |
| `show ID`, `list [--blocked [--on M]] [--needs-priority] [--status S] [--area A]` | Reads; `--needs-priority` is the open unpriced records, sorted by id; a parked row adds its `q` and days parked |
| `check` | Schema, store and pull checks; ruling checks only with a ledger (below). RED exits 1 |
| `landable BRANCH...` | 0 LANDABLE, 1 CANNOT TELL, 2 NOT LANDABLE (it holds a pull) |
| `stale [--days N]` | Read-only, default 30 days. Lists records whose last store commit is N+ days old, backticked body paths with no file, and `files` entries changed or gone since `files_at`. A record a test could not judge is NOT CHECKED, never current; the SWEPT line counts skips. Age is the last commit that wrote the record: after `migrate` it is the migrate date until the next write, and an uncommitted `main`-store edit ages by its last commit |
| `add ID --summary S --scope P...` | One record, always `priority: -`; `--reopen` to reuse a closed id |
| `set ID --<field> V ... [--unset K] [--ruling R] [--body-file F]` | Edits fields; `--body-file F` replaces the whole body (an empty file is refused; the kit has no body-line keys, so no `edit-body`); `set ID --hold-ruling R` alone re-cites the hold ruling |
| `block ID --on M... [--owner-ask Q]`, `unblock ID` | `block` replaces `blocked_on` |
| `pull ID [--reason T] [--ruling R]`, `release ID --reason T` | Written at once; `pull` records the branch; outside the offer it needs `--reason` |
| `close ID --reason T [--ruling R]`, `retire ID --why T [--ruling R]` | Delete the record and drop `item:ID` from its dependents; an owner-held or owner-priced record needs `--ruling` (Owner gates). `retire --owner-said` is refused with USAGE: the owner's words are never written into a record |
| `migrate [--dry-run] [--to ref\|branch] [--dir D]` | Copies `D/*.md` (default `docs/backlog`, or the configured folder) at the remote's main into a new store as its root commit, with the one-off migration steps (docs/backlog-store.md "Migration"). `--to` defaults to the configured `ref` or `branch` store. REFUSED when the store exists |
| `set-status ID parked --q Q-nnn`, `unpark ID --ruling R` | Park an open or pulled record on an owner question (a pull is given up; parked records are never offered and take no place in the cap); `unpark` opens it with `resume: R`, which ranks above priority and below a pin |

Exit codes (kitlib/backlog/errors.py): 0 OK; 1 `NOT FOUND:`, `ERROR:`, a RED check; 2 `USAGE:`, `REFUSED:`, or bad arguments. Every write refusal leaves the store untouched. By kind: `Usage` 2, `Refusal` 2, `NotFound` 1, `Failure` 1; a bad id shape and an unblock of a record that is not blocked are `USAGE:`, an id already taken is `REFUSED:`. `tests/backlog/test_refusal_kinds.py` pins the codes and audits the guard sites by AST.

## Owner gates

An owner price (`--priority-by owner|pm`), an owner hold (`blocked_on: owner`, `priority: -1`), and closing or retiring such a record take `--ruling R-nnnn`; for a retirement the ruling alone satisfies the owner check (an owner kind, never a delegated one) and nothing of the owner's is written into the record. The ledger entry is read only when `ledger_path` in the machine config names an existing file. Without one, only the ruling's shape is checked and `check` skips the ruling checks. A release, a close and a retirement also need the entry's `Items:` line to name the record. `unpark`'s ruling is checked as a hold release's (an owner or delegated entry) without the `Items:` line (`plan_unpark`).

## Records

The existing record format, unchanged (every record of the first repository ported parses). Keys cut in the rewrite are unknown keys: `keys`, `class`, `small_fix_*`, `migrated`, `artefact`, `anchor`, `hold_spent`, `value*`.
