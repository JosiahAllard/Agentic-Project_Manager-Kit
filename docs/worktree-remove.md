# Worktree remove

Remove a workstream's worktree only when its work has landed and nothing holds it, abandon unlanded work under a tag, and sweep landed idle worktrees nightly. Ported from the PM kit's `tools/worktree_remove.py` and `tools/worktree_sweep_task.py`. On Windows `git worktree remove` destroys the worktree's registration when it fails partway, so every check runs before git is called.

## Tools

- `tools/worktree_remove.py [-C DIR] <path or slug> [--keep-branch]`: check, remove, then `git branch -d` (never `-D`).
- `... <path or slug> --abandon "<why>" [--keep-branch]`: for UNLANDED work. Tags the tip `archive/<slug>`, then removes the tree and the branch.
- `... <branch> --branch`: a LANDED local branch with no worktree, deleted with `git branch -d` (never `-D`). Refused, with nothing changed: the main branch, a branch any worktree has checked out (use the slug form), a tip not in `<remote>/<main_branch>` as it stands (the refusal names `--branch --abandon`), and a backlog record still pulled by the branch (an open claim, as the sweep reads it). A missing branch is `nothing` (exit 0). The remote branch is not touched.
- `... <branch> --branch --abandon "<why>"`: for an UNLANDED local branch with no worktree. Tags the tip `archive/<name>` (`<name>` is the branch with `branch_template`'s prefix and suffix cut off when it fits, else the whole branch name), then deletes the branch with `git update-ref -d` held to the tagged tip. Refuses the main branch, a missing branch, a branch any worktree has checked out (use the slug form), and a branch already in the pushed base (the refusal names `<branch> --branch`, or the landing cleanup `land.py --sweep-landed`). The remote branch is not touched, as in `--abandon`. `--keep-branch` is a usage error here.
- Both `--branch` forms take the name exactly: one `git check-ref-format --branch` does not echo back (`main~1`, `main^`, `main@{1}`, `@{-1}`) or one differing from a branch only in case (`Main`) is refused, and the main-branch and checked-out checks ignore case (`kitlib.worktree.remove.local_branch`).
- `... --check`: lists stumps and orphans (exit 1 if any) and, for information, landed worktrees.
- `... --sweep-merged [--apply]`: the nightly sweep. Without `--apply` it is a dry run. It refuses anywhere but the main checkout.
- `tools/worktree_sweep_task.py [-C <main checkout>] [--register --owner-confirmed]`: prints the scheduled-task line. With both flags it runs it in PowerShell. A bare `--register` is refused, and agents never type `--owner-confirmed`.

Exit codes: 0 done, 1 refused or failed (or the sweep deferred), 2 usage or a refused `.kit.json`, 130 Ctrl+C.

## Code

| Module | Holds |
|---|---|
| `kitlib/worktree/holders.py` | Windows holder probes (`blockers`, `cwd_holders`, `describe`), link and keeper walks, `path_glob`, `is_build_output` |
| `kitlib/worktree/remove.py` | `remove`, `remove_branch`, `abandon`, `abandon_branch`, `check`, merge check, `dirty`, `last_look`, `git_remove`, `delete_branch`, `pulled_by` |
| `kitlib/worktree/sweep.py` | `sweep`, `sweep_skip`, `last_activity`, `landing_hold`, `machine_lock_entries` |
| `kitlib/worktree/task.py` | the PowerShell registration line, `parse`, `resolve`, `run` |

preflight's `leftovers` line (kitlib/preflight/leftovers.py; docs/preflight.md) is `merged_worktrees` with `skip` set to the worktree it runs in.

A landing's branch cleanup and `land.py --sweep-landed` (kitlib/gate/cleanup.py; docs/gate.md "Branch cleanup") remove a landed branch's worktree with this module's `_remove` and delete a branch with no worktree with its `delete_branch`.

Each verb returns `Outcome(code, lines)` and prints nothing; the tools print the lines. Settings come from `.kit.json`: `repo.main_branch`, `repo.remote`, `repo.worktrees_dir`, `repo.tools_dir`, `repo.name`, `backlog.store`, `landing.lock_file` and `landing.lanes`, and the `worktree_remove` section (docs/kit-json.md).

A `worktree_remove.build_output` entry `{"piece": "<id>", "path": "<folder>/"}` is the literal folder `kitlib.pieces` puts at that piece's dir: an ignored entry that is that folder or lies under it is build output (compared case-sensitively, so a folder git prints in another case is refused), and when the pieces table is missing or malformed, the id is unknown, the piece or path is not a string, the path is empty, a glob or climbs out, or the piece's dir has an empty or `.` part, the entry holds nothing and the removal refuses (`kitlib.worktree.remove.piece_dirs`; tests in `tests/worktree/test_piece_build_output.py`).

## The last status read

`kitlib.worktree.remove.git_remove` calls `last_look` first: one more read of `dirty`, just before `git worktree remove`. git's own refusal names modified and untracked files and never an ignored one, which it deletes with the tree, so this read is what keeps an ignored file written after the caller's own checks. Every removal goes through `git_remove`, so every caller gets it: the remove and abandon verbs (after `clear_disposable`), the sweep (after its second `sweep_status`), the landing cleanup (through `_remove`) and redfirst's cleanup (`kitlib/redfirst/run.py` `remove_tree`).

- It refuses on anything `dirty` lists: a modified or untracked file (one `disposable_untracked` names too: the verbs have cleared those by then), an ignored file outside `build_output`, a keeper inside it. The line is `failed    <path> changed after it was checked - <what>; git was NOT run`. A status that cannot be read gives `failed    <path>: its status could not be read again (<why>); git was NOT run`. The tree stays registered and intact, `--abandon` keeps its tag and the branch, and the sweep counts the worktree under `failed` (exit 1).
- A folder that vanishes between the caller's checks and `git_remove` is refused the same way: no status can be read where it was, so git is not run. Before `last_look`, `git worktree remove` dropped the entry of a missing folder and the branch went with it. Now the registration and the branch stay, and later runs report the entry: `--check` lists it as an `orphan`, the sweep skips it (`its directory is missing`), the landing cleanup (`kitlib/gate/cleanup.py` `clean_branch`) keeps it as `registered, and its folder is gone`, and the remove verb answers `nothing   <path> does not exist`.
- The window is narrowed, not closed. An ignored file written after `last_look` returns and before git deletes its folder is still deleted, without a word from git.
- `last_look` does not repeat the link check (`holders.link_reason`): a link or junction made inside build output after the verb's link check is not seen. One outside build output is refused when the status lists it or what it leads to (`??`, or `!!` when ignored).

Tests: `tests/worktree/test_remove_snapshot.py` (`IgnoredAfterTheLastStatusRead`; after `clear_disposable`, a tracked edit and delete in `FileAppearsInTheWindow`, a `disposable_untracked` file in `DisposableInTheWindow`, a junction in `JunctionAppearsInTheWindow`, a vanished folder in `FolderGoneInTheWindow`) and `tests/worktree/test_worktree_sweep.py` (`Sweep.test_an_ignored_file_written_after_the_sweeps_second_status_read_survives`).

## Behaves differently from the PM copy

- `-C` is a plain path option. There is no `checkout_guard` hand-off to the main checkout's copy, no `KIT_REPO_ROOT` and no `check_same_repo`; the kit has one copy. A relative path argument resolves against the process's own directory.
- Landing lock: read through `kitlib.records.landing.LandingLock` (`<common dir>/kit-landing.lock` unless `landing.lock_file` names another). There is always a place, so "no lock configured, defer" is gone. A file there that is no lock record counts as held.
- Open claim (sweep) and pulled warnings (abandon) read the backlog store backend: a record with `state: pulled` and `pulled_by: <branch>`. The PM copy grepped the base for any `pulled_by:` line. A `ref` or `branch` store is read at its local ref as it stands (not fetched, not per revision); an absent ref holds no records. A store that cannot be read skips the worktree (sweep) or prints a note (abandon).
- Machine lock: read through `~/.claude/tools/machine_lock/machine_lock.py status --json` only. There is no `MACHINE_LOCK_TOOL` or `MACHINE_LOCK_TEST_ROOT`. Both the PM shape (`lock.held/waiting/background`) and the kit snapshot (`runs` plus `queue.entries`) are accepted; anything else defers the sweep.
- Sweep task: `-C` replaces `--repo`. The task runs this kit's `tools/worktree_remove.py` by absolute path with `-C <main checkout>`. The kit-stub check is replaced by a check that the tool sits in a main checkout (not a branch worktree's copy).
- `<branch> --branch` without `--abandon` is new: neither the PM tool nor any repo's own copy removed a landed branch without its worktree. Its checks are the plain verb's merge check and the sweep's open-claim check.
- Output is collected and printed when the verb ends, not streamed. The `--apply` log (`<shared temp>/<repo name>-worktree-sweep.log`) is written from the same lines.

## Not ported

- The PM tool's `--sweep-merged` refusal when run from a worktree's own copy (no copies exist), and the `kit_stubs.MARK` check.

## Tests

- `tests/worktree/test_worktree_remove.py`
- `tests/worktree/test_worktree_sweep.py`
- `tests/worktree/test_worktree_sweep_task.py`
- `tests/worktree/test_remove_snapshot.py` (what appears inside the window before git, after `clear_disposable` and after `last_look`; a failing git)

Every case uses throwaway `tests/gitfixture.py` repositories. The machine lock and PowerShell are seams. The holder and junction cases run on Windows only.
