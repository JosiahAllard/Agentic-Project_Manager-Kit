# Preflight, board and checkout guard

Three read-only tools a session or coordinator runs to see where it stands. Each is a thin entry point; the logic lives in `kitlib/preflight`, `kitlib/board` and `kitlib/git/stray.py`.

One rule runs through all three: a reading that failed is printed as `?`, `UNKNOWN`, `unknown (<why>)` or `NOT CHECKED`. It is never printed as a value such as `0`, `-`, `free` or `(equal)`.

## preflight.py

`python tools/preflight.py [-C DIR] [--fetch] [--base REV]` is the first command of a task. `kitlib.preflight.report.report` prints the following lines, in this order:

- **fetch**: only with `--fetch`, and only when `git fetch <remote>` failed.
- **repo, branch, base**: the base against `<remote>/<base>`, marked `(equal)` only when both refs were read and match.
- **vs base**: off the base branch, the merge-base and behind/ahead counts. Without `--base`, when the local base differs from `<remote>/<base>` they are measured against `<remote>/<base>` and the line ends `(against <remote>/<base>)` (`settings.measure_rev`, gate.md "Default base").
- **dirty**: from `git status`, a new folder counting as one entry; up to 12 are listed.
- **worktrees**: each with its head and branch.
- **leftovers**: registered worktrees whose own commits are in `<remote>/<main_branch>` as it stands (`kitlib/preflight/leftovers.py`, `merged_worktrees` in docs/worktree-remove.md), as `leftovers WARN N under <worktrees_dir>` with the `worktree_remove.py <slug>` that removes each; `leftovers none` when there are none. The worktree preflight runs in, fresh worktrees and exempt folders are not listed. A failed read is `leftovers WARN ... not checked: <why>`. The stump and orphan lines are not listed here: `worktree_remove.py --check` lists those.
- **backlog**: `backlog   WARN the default store refs/backlog/main holds N record(s) here (local ref, not fetched)` when that ref holds records and `.kit.json` does not name `"backlog": {"store": "ref"}` (`kitlib/preflight/defaultstore.py`). Named no store, the line says `backlog.py -C` on the checkout reads that read-only copy and gives `backlog.py -C "<PM data worktree>" list` for the live store when that worktree's `.kit.json` names one (`store.pm_data_live_store`), or says why that worktree was not found or that its `.kit.json` cannot be read, or, when there is none, it is this checkout or it names no store either (it then reads this same copy), that the ref may be named as the store; a PM data worktree naming `ref` makes the ref live, so no line (refs are shared by every worktree); named another store, it gives `backlog.py -C "<checkout>" list`. No line when the ref is named, absent or holds no `.md` record. The local ref is read, never fetched; a failed read is `backlog   WARN default store not checked: <why>`. WARN only: the exit code is unchanged.
- **NOTE**: in the main checkout only. It names `<kit>/tools/new_worktree.py -C <checkout>` and `<worktrees_dir>/<slug>/`.
- **docs**: `<kit>/tools/doc_budget.py -C <checkout>`, judged by `kitlib.verdict.line`.
- **doc index**: `<kit>/tools/doc_index.py -C <checkout>` (`kitlib.docs.callers`), judged on `DOC INDEX OK` / `DOC INDEX FAILED`; no line when `.kit.json` "doc_index".enabled is false. With no index file the line passes and quotes the tool's one-line note that the listing check was skipped (the doc index is opt-in; docs/doc-tools.md "doc_index").
- **stray lines**: from `kitlib.git.stray.check`.
- **report hooks**.
- **engines and ports**: only when configured.
- **config**: which machine config file is read, from `kitlib.preflight.configline` (docs/kit-json.md "Moving the machine config folder"): the PM repository's `.pm-kit` folder with the files in it, `none` (no pointer and no config in the home folder) or `BROKEN` with the reason (a config left in the home folder, a bad pointer, a file the strict reader refuses); keys are named, values never.
- **pointer**: the PM-repo pointer (`HKLM\SOFTWARE\pm-kit` value `pm_repo`), from `configline.pointer_line` over `kitlib.config.pointer` (docs/kit-json.md "The PM repo pointer"): the PM repository's path, `none`, or `BROKEN` with the reason. Under the kit's tests the registry is never read.
- **traps**: `.kit.json` "preflight".traps when set; else `docs/ops/gotchas.md (read once per task; add yours)`, printed only when the checkout has that file (`kitlib/preflight/report.py`, `settings.DEFAULT_TRAPS_FILE`), so no line when it has neither.
- **index**.
- **next**: the kit commands with this session's values, from `kitlib/preflight/nextblock.py`. Each runs the kit's own tool by its absolute path and names the checkout with `-C` (a repository holds no copy of the kit's tools, decision 21 in docs/multi-host-plan.md section 7). In the main checkout this is only `new_worktree.py`. In a worktree it is `commit.py`, `review_pack.py` and `codemap.py`, plus `sync_base.py` when the branch is behind its base.
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

## Agent start block

`kitlib/agentstart` is the cheap part of preflight, read by a host's SubagentStart hook so that a dispatched agent starts knowing where it stands (docs/content.md "Kit header" says which agents get it). Its title line names the role, the time it was read, that nothing was fetched and how long it took, and points to `preflight.py` for the full report. Then, in this order:

- **checkout**: `MAIN checkout <top>` or `worktree <top>`, with the branch and head. At a subagent's start the folder is its parent's, so it is usually the main checkout.
- **next**: in the main checkout only, `<kit>/tools/new_worktree.py -C "<top>" <area>/<slug>`, the command that makes the agent's worktree.
- **base**: the base against `<remote>/<base>`, `(equal)` or `(DIFFERENT)` only when both refs were read, else `(not compared: <ref> not read)`; then how old the remote side is, from the mtime of `FETCH_HEAD` in the common git folder (`<remote> as of the last fetch N min ago`, or `never fetched here`). The base is picked as preflight picks it (`settings.base_names`, `settings.pick_base`).
- **vs base**: in a worktree off the base only, behind and ahead, measured as preflight measures them (`settings.measure_rev`).
- **dirty**: in a worktree only, the count from `git --no-optional-locks status`, so a status cut off at the budget's timeout leaves no `index.lock` behind.
- **stray lines**: from `kitlib.git.stray.check`, at most four.

A folder in no git work tree, or no absolute folder in the hook payload, is one `checkout  NOT CHECKED` line. It never fetches and runs no git command that reaches a remote. It never raises: a failure is a `could not check` line, and each host's call site turns an exception that escapes anyway into one such line after the header (`_agent_start` in hosts/claude/adapter/host.py, `text` in hosts/codex/adapter/start.py). The hook waits at most 1 s (`agentstart.BUDGET`): the reading runs on a daemon thread, each git call it makes itself is given what is left of the budget as its timeout (`repo.classify` and `stray.check` keep their own), and what is not done in time is one `NOT CHECKED - the rest: over the 1.0 s budget` line.

## board.py

`python tools/board.py [-C DIR] [--base REV] [--write-state]` prints the coordinator's whole board. `kitlib.board.report.lines` produces, in order:

1. The base against `<remote>/<base>`. When they differ and no `--base` was given, the rows are measured against `<remote>/<base>`, and the line says so.
2. One row per worktree that is on a branch other than the base: `branch | tip | ahead/behind | has base tip | last commit | dirty | owner-only touched`. The `dirty` column counts tracked files only, and the owner-only paths come from `.kit.json` `owner`.
3. `machine: lock <the lock line above>`.
4. `docs:` with the last line of `<kit>/tools/doc_index.py --stale -C <checkout>`.
5. `machine: engines ... | ports ...`, when configured.

In a row, `?` means git could not answer. `-` means "no owner-only paths", and `has base tip` is `yes` or `NO` only when git answered.

A worktree that git cannot read as its own work tree is a row of `?`, ending `? (not its own work tree: git cannot read it)`. The check is `kitlib.board.report._own_tree`. The case it guards against is a half-deleted worktree under `.claude/worktrees/`: git, asked inside it, walks up and answers for the main checkout.

`--write-state` writes the board into the state file and changes nothing else. kitlib/board/state.py chooses the file, in the main checkout's git common dir, so that no untracked file is left in the work tree (a dirty main checkout refuses `gate.py land`):

1. `<git common dir>/kit/board-state.md` by default.
2. `.kit.json` `board.state_file`, when set and when it lies under the git dir (for example `.git/kit/board-state.md`). Any other value is in the work tree: `board: refused: "board".state_file <path> is inside the work tree...`, exit 2, nothing written.

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

`--allow-tracked` is for a repo's landing gate step (a step's `merged_args`), which runs in the `_landing` linked worktree: with it, "could not tell" is a `[FAIL]` line and exit 1. It exempts no stray, tracked or untracked; the name is kept for the `merged_args` repos already pass.

## .kit.json keys read

All of these keys are in docs/kit-json.md.

- `repo`:
  - `main_branch`: the base. When unset, the base is `main` if it exists, else `master`.
  - `remote`.
  - `tools_dir`: the hooks folder. The NOTE, index and next lines name the kit's tools, never `<tools_dir>`.
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
