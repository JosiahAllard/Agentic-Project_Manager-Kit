# Worktree remove

Remove a workstream's worktree only when its work has landed and nothing holds it, abandon unlanded work under a tag, and sweep landed idle worktrees nightly. On Windows `git worktree remove` destroys the worktree's registration when it fails partway, so every check runs before git is called.

## Tools

- `tools/worktree_remove.py [-C DIR] <path or slug> [--keep-branch]`: check, remove, then `git branch -d` (never `-D`).
- `... <path or slug> --abandon "<why>" [--keep-branch]`: for UNLANDED work. Tags the tip `archive/<slug>`, then removes the tree and the branch.
- Both verbs also take, by its PATH only, a registered DETACHED worktree outside every worktree folder (an agent's red-run tree): the same checks, then removed with no branch to delete; `--abandon` tags its HEAD `archive/<folder name>`. Refused, nothing changed: the main checkout or a path inside it, a link or junction on the way, an exempt, unregistered or locked folder, a tree holding another registered worktree, a tree on a branch, and no HEAD; a missing folder is `nothing` (exit 0) (`kitlib.worktree.remove.outside_target`).
  - Its HEAD reflog and per-worktree refs, deleted with the tree, are checked before any git write: a commit no ref reaches is the owner's call, never this tool's. The plain verb refuses when HEAD or any of those commits is in no branch, remote-tracking branch or tag; `--abandon` refuses too, naming each such commit that the tag on HEAD would not keep. A read that fails refuses. Just before `git worktree remove` both verbs read HEAD, the reflog and the per-worktree refs again and refuse when any moved.
  - A SLUG never names such a tree: it is refused (exit 1) with each path-form command. For an agent's red-run tree under a temp folder use `--scratch`; the plain verb is for a tree whose HEAD has landed, `--abandon` for unlanded work worth a tag.
- `.kit.json` `worktree_remove.branch_template` is one `{slug}` template or a list of them (`phase/{slug}` plus `records/{slug}`): a slug-form removal is refused unless the worktree is on the branch one entry makes from its folder name (the refusal lists every candidate). An empty list, a non-string entry or an entry without exactly one `{slug}` is a configuration error (exit 2), never an allow.
- `... <branch> --branch`: a LANDED local branch with no worktree, deleted with `git branch -d` (never `-D`). Refused, with nothing changed: the main branch, a branch any worktree has checked out (use the slug form), a tip not in `<remote>/<main_branch>` as it stands (the refusal names `--branch --abandon`), and a backlog record still pulled by the branch (an open claim, as the sweep reads it). A missing branch is `nothing` (exit 0). The remote branch is not touched.
- `... <branch> --branch --abandon "<why>"`: for an UNLANDED local branch with no worktree. Tags the tip `archive/<name>`, then deletes the branch with `git update-ref -d` held to the tagged tip. Refuses the main branch, a missing branch, a branch any worktree has checked out (use the slug form), and a branch already in the pushed base (the refusal names `<branch> --branch`, or the landing cleanup `land.py --sweep-landed`). The remote branch is not touched, as in `--abandon`. `--keep-branch` is a usage error here.
- Both `--branch` forms take the name exactly: a revision expression or a name differing from a branch only in case is refused.
- `... <path> --scratch`: a SCRATCH worktree, a detached tree an agent made under its temp folder (red evidence against an older commit). It takes exactly one path and no other option. It removes the tree only when every one of these holds (`kitlib.worktree.scratch`):
  - The path is under a scratch root: `AGENT_TEMP`, `TEMP`, `TMP` or the shared temp folder, read from the environment (a scope hint, not protection: the checks below protect the tree). A path inside the main checkout or a worktree folder is a workstream worktree: use the slug form.
  - No link or junction is on the way. The tree is registered, not locked, detached, and holds no other registered worktree. Its HEAD, every commit in its HEAD reflog and the tip of each of its per-worktree refs are in a branch, remote-tracking branch or tag.
  - Nothing holds it, no link is inside it, and its ignored files are build output only (the rules above).
  - Every uncommitted file, and its staged blob, equals `<ref>:<path>` for some ref under `refs/heads` or `refs/remotes`: the same path only. A rename, copy, conflict, type change, quoted path or nested repository is refused.
  - It then checks each file once more, restores tracked files from HEAD and deletes untracked ones (`restored  N file(s) found in git at their paths`), reads HEAD, the reflog and the per-worktree refs again, and removes the tree through `git_remove` (see "The last status read"), never with `--force`. A read that fails refuses; an unregistered folder is never deleted, and a missing one is `nothing` (exit 0). A reftable repository is refused.
  - To make red evidence without leaving such a tree, use `tools/redfirst.py` (docs/redfirst.md).
- `... --check`: lists stumps and orphans (exit 1 if any) and, for information, landed worktrees.
- `... --sweep-merged [--apply]`: the nightly sweep. Without `--apply` it is a dry run. It refuses anywhere but the main checkout.
- `tools/worktree_sweep_task.py [-C <main checkout>] [--register --owner-confirmed]`: prints the scheduled-task line. With both flags it runs it in PowerShell. A bare `--register` is refused, and agents never type `--owner-confirmed`.

Exit codes: 0 done, 1 refused or failed (or the sweep deferred), 2 usage or a refused `.kit.json`, 130 Ctrl+C.

## Code

| Module | Holds |
|---|---|
| `kitlib/worktree/holders.py` | Windows holder probes (`blockers`, `cwd_holders`, `describe`), link and keeper walks, `path_glob`, `is_build_output` |
| `kitlib/worktree/remove.py` | `remove`, `remove_branch`, `abandon`, `abandon_branch`, `check`, merge check, `dirty`, `last_look`, `git_remove`, `delete_branch`, `pulled_by` |
| `kitlib/worktree/scratch.py` | `remove_scratch` (`--scratch`), `roots`, `target`, `reflog`, `reachable`, `preserved`, `restore`, `unmoved` |
| `kitlib/worktree/sweep.py` | `sweep`, `sweep_skip`, `last_activity`, `landing_hold`, `machine_lock_entries` |
| `kitlib/worktree/task.py` | the PowerShell registration line, `parse`, `resolve`, `run` |

preflight's `leftovers` line (kitlib/preflight/leftovers.py; docs/preflight.md) is `merged_worktrees` with `skip` set to the worktree it runs in.

A landing's branch cleanup and `land.py --sweep-landed` (kitlib/gate/cleanup.py; docs/gate.md "Branch cleanup") remove a landed branch's worktree with this module's `_remove` and delete a branch with no worktree with its `delete_branch`.

Each verb returns `Outcome(code, lines)` and prints nothing; the tools print the lines. Settings come from `.kit.json`: `repo.main_branch`, `repo.remote`, `repo.worktrees_dir`, `repo.name`, `backlog.store`, `landing.lock_file` and `landing.lanes`, and the `worktree_remove` section (docs/kit-json.md).

A `worktree_remove.build_output` entry `{"piece": "<id>", "path": "<folder>/"}` is the literal folder `kitlib.pieces` puts at that piece's dir: an ignored entry that is that folder or lies under it is build output; an entry that cannot be resolved holds nothing and the removal refuses (`kitlib.worktree.remove.piece_dirs`).

## The last status read

`kitlib.worktree.remove.git_remove` calls `last_look` first: one more read of `dirty`, just before `git worktree remove`. git's own refusal names modified and untracked files and never an ignored one, which it deletes with the tree, so this read is what keeps an ignored file written after the caller's own checks. Every removal goes through `git_remove`.

- It refuses on anything `dirty` lists: a modified or untracked file (one `disposable_untracked` names too: the verbs have cleared those by then), an ignored file outside `build_output`, a keeper inside it. The line is `failed    <path> changed after it was checked - <what>; git was NOT run`. A status that cannot be read gives `failed    <path>: its status could not be read again (<why>); git was NOT run`. The tree stays registered and intact, `--abandon` keeps its tag and the branch, and the sweep counts the worktree under `failed` (exit 1).
- A folder that vanishes between the caller's checks and `git_remove` is refused the same way, so the registration and the branch stay; `--check` then lists it as an `orphan`.
- The window is narrowed, not closed. An ignored file written after `last_look` returns and before git deletes its folder is still deleted, without a word from git.
- `last_look` does not repeat the link check (`holders.link_reason`): a link or junction made inside build output after the verb's link check is not seen. One outside build output is refused when the status lists it or what it leads to (`??`, or `!!` when ignored).

## The scheduled task

`kitlib/worktree/task.py` `command` writes the one PowerShell line `tools/worktree_sweep_task.py` prints or, with `--register --owner-confirmed`, runs. The task `<repo name>-worktree-sweep` runs daily at 03:00 as the registering user (`-RunLevel Limited`, `-StartWhenAvailable`), its working directory the main checkout.

- No console window opens: the task's program is the `pythonw.exe` of the base install of the python that runs the tool, with the carrier `tools/no_window.py` (docs/daily-full.md "The schedule") and the arguments `"<kit>\tools\no_window.py" python "<kit>\tools\worktree_remove.py" -C "<main checkout>" --sweep-merged --apply`.
- The tool refuses (exit 1, nothing registered) when that `pythonw.exe` is under a `WindowsApps` folder or is not a file.
- The line does not replace a task: PowerShell refuses to register a name that exists. Remove one with `Unregister-ScheduledTask -TaskName '<repo name>-worktree-sweep' -Confirm:$false`.

## Reads

- Landing lock: `kitlib.records.landing.LandingLock` (`<common dir>/kit-landing.lock` unless `landing.lock_file` names another). A file there that is no lock record counts as held.
- Open claim (sweep) and pulled warnings (abandon): a backlog record with `state: pulled` and `pulled_by: <branch>`, read at the store's local ref. A store that cannot be read skips the worktree (sweep) or prints a note (abandon).
- Machine lock: the kit's own `<kit>/tools/machine_lock.py status --json`; an answer of any other shape defers the sweep.
- The `--apply` log is `<shared temp>/<repo name>-worktree-sweep.log`.

Tests: `tests/worktree`. The holder and junction cases run on Windows only.
