# Preflight, board and checkout guard

Three read-only tools a session or coordinator runs to see where it stands. They are ported from the PM kit's `tools/preflight.py`, `tools/board.py` and `tools/checkout_guard.py` (the `--main-stray` part). Each is a thin entry point; the logic lives in `kitlib/preflight`, `kitlib/board` and `kitlib/git/stray.py`.

One rule runs through all three: a reading that failed is printed as `?`, `UNKNOWN`, `unknown (<why>)` or `NOT CHECKED`. It is never printed as a value such as `0`, `-`, `free` or `(equal)`.

## preflight.py

`python tools/preflight.py [-C DIR] [--fetch] [--base REV]` is the first command of a task. `kitlib.preflight.report.report` prints the following lines, in this order:

- **fetch**: only with `--fetch`, and only when `git fetch <remote>` failed.
- **repo, branch, base**: the base against `<remote>/<base>`, marked `(equal)` only when both refs were read and match.
- **vs base**: off the base branch, the merge-base and behind/ahead counts. Without `--base`, when the local base differs from `<remote>/<base>` they are measured against `<remote>/<base>` and the line ends `(against <remote>/<base>)` (`settings.measure_rev`, gate.md "Default base").
- **dirty**: from `git status`, a new folder counting as one entry; up to 12 are listed.
- **worktrees**: each with its head and branch.
- **leftovers**: registered worktrees whose own commits are in `<remote>/<main_branch>` as it stands (`kitlib/preflight/leftovers.py`, `merged_worktrees` in docs/worktree-remove.md), as `leftovers WARN N under <worktrees_dir>` with the `worktree_remove.py <slug>` that removes each; `leftovers none` when there are none. The worktree preflight runs in, fresh worktrees and exempt folders are not listed. A failed read is `leftovers WARN ... not checked: <why>`. Ported from the PM preflight's merged-worktree WARN (its stump and orphan lines are not ported: `worktree_remove.py --check` lists those).
- **NOTE**: in the main checkout only. It names `<tools_dir>/new_worktree.py` and `<worktrees_dir>/<slug>/`.
- **docs**: `<kit>/tools/doc_budget.py -C <checkout>`, judged by `kitlib.verdict.line`.
- **doc index**: `<kit>/tools/doc_index.py -C <checkout>` (`kitlib.docs.callers`), judged on `DOC INDEX OK` / `DOC INDEX FAILED`; no line when `.kit.json` "doc_index".enabled is false.
- **stray lines**: from `kitlib.git.stray.check`.
- **report hooks**.
- **engines and ports**: only when configured.
- **config**: which machine config file is read, from `kitlib.preflight.configline` (docs/kit-json.md "Moving the machine config folder"): the file, `FALLBACK`, `WARN both`, `none` or `BROKEN`; keys are named, values never.
- **traps**.
- **index**.
- **next**: the kit commands with this session's values, from `kitlib/preflight/nextblock.py`. In the main checkout this is only `new_worktree.py`. In a worktree it is `commit.py`, `review_pack.py` and `codemap.py`, plus `sync_base.py` when the branch is behind its base.
- **lock**: the last line.

Exit codes:

- **0**: the report. The report is read, not gated on, so a failed reading is a line, never an exit code.
- **2**: a usage error, a `-C` directory in no git work tree, or a `.kit.json` the tool cannot use. A bad `.kit.json` prints `preflight: refused: kit configuration: <why>`.

**The lock line** comes from the kit lock's own status. `kitlib/preflight/lockline.py` runs `<kit>/tools/machine_lock.py status --json` (docs/lock-api.md) and summarises it as:

`cpu N/M: slot<range> repo/slug (kind, run id, X min[, OVERRUN +N min][, background]) | gpu ... | waiting N: #pos repo/slug (kind, needs, starts HH:MM | start not planned[, backfill][, parked]) | hold <scope> until <end>: <note> (<owner>)`

The line reads `unknown (<why>)` in any of these cases:

- the tool is missing;
- it exits non-zero;
- its output is not JSON;
- the snapshot has another shape.

A queue or holds file the lock could not read is shown as `queue unreadable: <error>` or `holds unreadable: <error>`, never as none.

## board.py

`python tools/board.py [-C DIR] [--base REV] [--write-state]` prints the coordinator's whole board. `kitlib.board.report.lines` produces, in order:

1. The base against `<remote>/<base>`. When they differ and no `--base` was given, the rows are measured against `<remote>/<base>`, and the line says so.
2. One row per worktree that is on a branch other than the base: `branch | tip | ahead/behind | has base tip | last commit | dirty | owner-only touched`. The `dirty` column counts tracked files only, and the owner-only paths come from `.kit.json` `owner`.
3. `machine: lock <the lock line above>`.
4. `docs:` with the last line of `<kit>/tools/doc_index.py --stale -C <checkout>`.
5. `machine: engines ... | ports ...`, when configured.

In a row, `?` means git could not answer. `-` means "no owner-only paths", and `has base tip` is `yes` or `NO` only when git answered.

A worktree that git cannot read as its own work tree is a row of `?`, ending `? (not its own work tree: git cannot read it)`. The check is `kitlib.board.report._own_tree`. The case it guards against is a half-deleted worktree under `.claude/worktrees/`: git, asked inside it, walks up and answers for the main checkout.

`--write-state` writes the board into the state file and changes nothing else. kitlib/board/state.py chooses the file, in the main checkout:

1. `.kit.json` `board.state_file`, when set.
2. Otherwise `docs/ops/state.local.md`.
3. Otherwise `ops/state.local.md`, when the main checkout has `ops/` and no `docs/ops/`.

Only the block between `<!-- board:begin -->` and `<!-- board:end -->` is rewritten. A missing file is created from a template.

Exit codes are the same as preflight's: 0 for the board, 2 for a usage error or a bad `.kit.json` (`board: refused: kit configuration: <why>`).

## checkout_guard.py

`python tools/checkout_guard.py [PATH]` prints `main`, `linked`, `none` or `unknown`, followed by the work tree's top level. Exit codes:

- **0**: linked or none.
- **1**: main.
- **2**: unknown, or a usage error.

`python tools/checkout_guard.py --main-stray [--allow-tracked] [PATH]` lists the main checkout's stray untracked or uncommitted files, from `kitlib.git.stray.check`, one `[FAIL]` line each. It does not guess which worktree wrote a stray (the PM kit's attribution was dropped by an owner ruling). Exit codes:

- **1**: any stray.
- **0**: clean. Also 0 when git could not tell; then a WARNING line is printed.

`--allow-tracked` is for the lander's post-merge gate: it ignores tracked edits, and there "could not tell" fails.

## .kit.json keys read

All of these keys are in docs/kit-json.md.

- `repo`:
  - `main_branch`: the base. When unset, the base is `main` if it exists, else `master`.
  - `remote`.
  - `tools_dir`: used for the next block's commands and the hooks folder.
  - `worktrees_dir`: used in the NOTE.
- `preflight`:
  - `traps`.
  - `report_hooks`: `module:function` entries, loaded from `<tools_dir>/<module>.py`. A hook that fails is one `hook NOT CHECKED` line, a `SystemExit` from the hook or a sibling it imports included; a `KeyboardInterrupt` is not caught (`kitlib/preflight/hooks.py`, `lines`). While a hook runs, `<tools_dir>` is first on `sys.path`, so a sibling it imports resolves to the repo's own file even when a kit tool has the same name (`kitlib/preflight/hooks.py`, `_repo_first`). A sibling is a `.py`, `.pyw` or `.pyd` file or a folder; a loaded module of that name, and its submodules, is set aside only when it came from outside `<tools_dir>`, so a builtin stays. Afterwards `sys.path` is put back, the modules the hook loaded from `<tools_dir>` are dropped, the ones set aside are restored, and modules the hook pulled in from elsewhere (the stdlib, `kitlib`) stay loaded - unless a module loaded from `<tools_dir>` has a stdlib name or a set-aside name, when a module pulled in meanwhile may be bound to the repo's copy and every module first imported during the hook is dropped. These changes are process-global: preflight runs hooks on one thread, and no other thread may import while a hook runs.
  - `engines`: process-name prefixes, matched case-blind.
  - `udp_ports`: `[low, high]`.
  
  The board reads `engines` and `udp_ports` too. A bad value refuses both tools.
- `board.state_file`.
- `owner`: the owner-only paths, through `Config.owner_rules`.

## Not ported, and why

- **No change of directory to the main checkout and no hand-off**. The main-checkout refusal is `kitlib.git.checkout.main_refusal`.
- **No environment overrides**.
- **No `KIT_REPO_ROOT` and no `check_same_repo`**: `-C` names the checkout.
- **Old lock formats**: the old tier line, the single lock and the v2 pools. There is one lock now, read through its own CLI.
- **The worktree-leftover warning** belongs to the worktree-remove tool.
- **The main-checkout untracked listing**: `kitlib.git.stray` covers it.
- **The path-based main-checkout guess** is replaced by `kitlib.git.repo.classify`.
- **Decoding of C-quoted status paths**: `kitlib.git.refs.status` reads `-z` output, which is never quoted.
