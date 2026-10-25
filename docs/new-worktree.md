# new_worktree

Held for the joint review: no repository runs it yet.

`tools/new_worktree.py` makes a workstream's worktree in one call. Logic: `kitlib/worktree/new.py`; tests: `tests/worktree/test_new_worktree.py`.
## Hosts

`--host NAME` picks the agent host whose worktree folder is used: a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude). A name `hosts.json` lacks is refused (exit 2) before any fetch. The folder (`kitlib/worktree/hostdirs.py`; tests `tests/worktree/test_host.py`):
1. `.kit.json` `repo.worktrees_by_host.<host>` when set;
2. else `repo.worktrees_dir` for the default host, so a repository that sets nothing keeps `.claude/worktrees`;
3. else the host's `hosts.json` `profile.worktrees_directory`.

A folder other than `repo.worktrees_dir` must stay inside the main checkout and be reached through no junction or link, or the call is refused. Creation keeps every branch, base, landing-lock and fetch check. `worktree_remove.py` and the sweep find a worktree under any host's folder and keep their registration, holder, dirty-file, landing and link checks; a bare slug still means `repo.worktrees_dir`, so name another host's worktree by the absolute path printed.

```
python tools/new_worktree.py [-C DIR] <area>/<slug>                 a NEW branch off the main branch
python tools/new_worktree.py [-C DIR] --existing <area>/<slug>      an EXISTING branch, resumed
                             [--base BRANCH]                        another base than the main branch
                             [--base COMMIT | --approval hub:A-<n>] a NEW branch at a fixed commit
                             [--no-wait]                            do not wait for a landing in flight
```

Exit 0 made; 1 refused (nothing made, the lines say why); 2 usage, or the kit configuration was refused; 130 Ctrl+C.

- `-C` names the repository by any of its work trees (default: here). The worktree is always the MAIN checkout's: `<main>/<worktrees_dir>/<slug>`.
- It fetches `<remote>`, and refuses unless local `<base>` equals `<remote>/<base>` (exceptions: a landing in progress, and a local base strictly behind, both below). A `--base` given as `<remote>/<name>` names the branch `<name>` (`branch_name`). It never switches a branch in the main checkout and never touches another worktree.
- A new branch starts at the base's hash and tracks nothing.
- The mode is what the caller said: the default refuses a branch that exists, `--existing` refuses one that does not.
- Names: `<area>/<slug>`, ASCII letters, digits, `_ . -`, each part starting with a letter or digit, the area lowercase-first. A new slug is lowercase-first; `--existing` also takes a capital. A slug or branch that differs only in case from one already there is refused.

## A base commit

`--base <full hash>` (40 hex digits, any case) starts a NEW branch at that commit; `--approval hub:A-<n>` starts one at the owner approval's `head`, its base commit. Made for `commit.py --owner-approval`, which refuses unless HEAD is that base (docs/commit.md "Owner approval"): once the main branch moves, the approved change is committed from a worktree made this way. A shorter hash is a branch name, as before.

- The approval is read as `commit.py` reads it (`approval_base` in `kitlib/worktree/new.py`, through `kitlib.commit.approval`'s `doc_id`, `hub_client`, `fetch` and `state_error`): a hub that cannot be asked, no such approval, one consumed already or not `approved`, or a `head` that is no full hash is refused (exit 1). The hub is asked only after the local branch checks.
- It fetches `<remote>` and refuses unless the repository has the commit. No landing wait, landing lock or remote comparison applies: the start point is fixed. It prints `branch    <branch> from commit <hash>`, after `approval  hub:A-<n> is for base <hash>; this branch starts there`.
- With `--existing`, or `--approval` with `--base`, it is a usage error (exit 2).

Tests: `BaseCommit` and `UsageIsRejectedNotGuessed` in `tests/worktree/test_new_worktree.py`.

## First-use prep

Once a worktree is made, the tool runs the first-use repo prep on the main checkout (`kitlib/git/prep.py`; decisions 23 and 25 of docs/multi-host-plan.md). The PM session start runs the same prep on each `launcher.repos` repository (docs/pm-start.md "Repo prep"). Its lines follow the `worktree` line, as `prep      repo prep: ...`; a repository already prepared prints none. It writes local settings only: nothing tracked changes and nothing is committed.

- **Exclude.** Each worktree folder (every host's `hosts.json` `profile.worktrees_directory`, `repo.worktrees_dir` and `repo.worktrees_by_host`) gets one anchored line, such as `/.claude/worktrees/`, in the repository's `info/exclude`. That file is in the git common dir, so a run from a linked worktree writes the main checkout's. A line already there, with or without its trailing slash, is not written again. The file's other lines and its line ending are kept, and `info/` is made when it is missing. The exclude only hides the folders from the main checkout's untracked list. A worktree has its own index and shares the repository's remotes, so its commits and pushes are untouched. A folder is refused, with a `did not exclude` line, when gitignore would not read it literally (`* ? [ ] ! #`, a line break, a part with a leading or trailing space), when it is not inside the repository, or when git tracks a file under it: `.kit.json` is the repository's own content, and an exclude of `*` or `src` would hide new source files from `git status` and the untracked-file checks.
- **Hooks.** The local `core.hooksPath` is set to this kit's `githooks/` (beside the `kitlib` that runs) only when `git config --get core.hooksPath` finds no value at any level (local, global or system) and the repository's own `hooks/` folder holds no hook other than git's `*.sample` files. A hooks path would silently stop hooks a tool installed there, such as git-lfs's `pre-push` or the pre-commit framework's. An existing value is never overwritten, with one exception. A local value that is an absolute path to a folder named `githooks` that no longer exists is repointed. That is the kit's own setting from a kit folder that moved, and a hooks folder that is gone runs no hooks, so nothing anyone chose is lost. A global or system value is never repointed, even a stale one.
- **Failures.** No git, a folder outside any repository, or a `.git` that cannot be written each give a line. Prep never stops the worktree or the session start.

Tests: `tests/git/test_prep.py` (a fresh clone stays clean, a second run writes nothing, a worktree commit pushes to its remote branch with its `.claude/` files, local and global hooks paths are kept, a linked worktree writes the common exclude, pattern-like and tracked folders are refused, hooks in `.git/hooks` keep running, a prepared repository costs two git calls) and `PrepIsRun` in `tests/worktree/test_new_worktree.py`.

## The landing exception

The lock is the kit landing lock (`kitlib.records.landing`, the file in the git common dir that `gate.py land` holds). While it is held, the base may carry unpushed merges, so:

- a new branch starts at the pushed `<remote>/<base>`;
- the output names the base's unpushed commits the branch does not contain.

A lock file that is not a lock record counts as held. The one refusal under a held lock is that `<remote>/<base>` does not resolve. A non-main `--base` with no pushed branch is refused the same way with the lock free, so waiting does not help; either refusal ends with a `route` line (`route_line` in `kitlib/worktree/new.py`) naming `tools/redfirst.py` for red-first evidence. With the lock free, the refs are read once more before refusing (`choose_base`), in case a lander pushed and released in between. The lock is only read. A base ref git gives no answer for (a git ended from outside included) is refused as `<ref> could not be read (<why>)`, before or after that second read, never taken for one that does not resolve (`refs.resolve(known=True)`).

## Waiting for a landing in flight

Without a wait, a branch made while a lander holds the lock with its merge unpushed starts at the pushed `<remote>/<base>`, without that landing: stale at birth. `.kit.json` "landing".`in_flight` turns on a wait before the fetch, so a landing that pushes meanwhile is in the new branch. Unset (the default), nothing waits and nothing below applies. Logic: `kitlib/records/inflight.py`; `tools/sync_base.py` reads the same key ([sync-base.md](sync-base.md)).

- `{}`: a landing is in flight while the kit landing lock is held (a lock file that is no lock record counts as held).
- `{"command": [argv]}`: for a repository whose landing tool keeps its own lock. The argv runs with the main checkout as its working folder, no input and no shell; `{python}` is the running Python and `{root}` the main checkout. Use the form `["{python}", "tools/x.py"]`: Python finds the script from the main checkout. The program itself is found by the Windows process search (`subprocess.Popen` without a shell), not from the main checkout: a bare name gets only `.exe` added, a `.cmd`, `.bat` or `.ps1` needs its interpreter first in the argv, and a relative program path resolves against the folder the tool was run from, not the main checkout. One look gets 30 seconds (`COMMAND_TIMEOUT`), or what is left of `wait_s` when that is less, never under 2 (`LOOK_MIN_S`). Exit 0: in flight, and the first line printed names the holder. Exit 1: none. Any other exit, a timeout, or a program that cannot start: in flight, since "cannot tell" must not read as "free". So a broken command counts as in flight on every run: each run waits the whole `wait_s`, and a stray unpushed commit then takes the landing exception instead of the "not a landing in progress" refusal.
- `wait_s`: the most seconds to wait, 0 to 3600, counted from just before the first look, so the time the looks take is part of it. Unset: 100 (`WAIT_S`). The 100 is inferred, not measured: under the agent shell's default 120-second command timeout, so a wait that runs out still leaves time to fetch and finish. A look by command still running at the bound ends by its capped timeout (plus a 5-second grace, `KILL_GRACE`, at worst).

The branch checks (`--existing` with no such branch, a branch that exists already) refuse before any look, so a refused run never waits. While a landing is in flight it prints the holder and the bound, looks every 10 seconds, and prints a line each minute:

```
landing   in flight: the landing lock <path> is held: <holder>; waiting up to 100s (--no-wait goes on now)
landing   done after 40s
```

At the bound it goes on, with `landing   still in flight after 100s, going on without it: <holder>`, and the rest runs as without the wait (a held kit lock takes the landing exception above). `--no-wait` does not wait: `landing   in flight, not waited for (--no-wait): <holder>`. The wait only reads; it never takes, breaks or changes a lock.

Behaviour change with a command: after the fetch the command is asked again when the kit lock is free and local `<base>` differs from `<remote>/<base>` (equal refs need no exception, so it is not asked); that look gets what is left of `wait_s`, never under 2 seconds. If it says (or cannot say) a landing is in flight, the landing exception applies (`landing   in progress: landing.in_flight.command: <holder>`, the branch from the pushed `<remote>/<base>`), where before the free-lock refusal fired. Exit 1 keeps the refusal, its reason naming what the command said.

## Local base behind the remote

With the landing lock free and local `<base>` a pure fast-forward behind `<remote>/<base>` (an ancestor of it, not equal: no commit of its own), the new branch starts at `<remote>/<base>` (by hash) and one line says so:

```
base      local main is 2 commit(s) behind origin/main; this branch starts at origin/main <hash> and local main is not updated
```

Local `<base>` is not updated (`behind_count` in `kitlib/worktree/new.py` only reads). A local base with a commit the remote lacks, ahead or diverged, is refused as before. Under a held landing lock the landing wording applies instead.

## Keys read

- `.kit.json` "repo": `main_branch`, `remote`, `worktrees_dir`. The commands its lines name (the `next` preflight, `worktree_remove.py --check`, `redfirst.py`) are the kit's own tools by absolute path with `-C`, never `<tools_dir>` copies (`kitlib.kitroot.command`).
- "landing": `in_flight` (see "Waiting for a landing in flight").

"new_worktree" has no keys.

## Not ported

These source tests were dropped with the mechanisms they tested:

- `TheCallerStandsInTheRepository`, and `-C`'s "another repository is a usage error": `_repo_guard.check_same_repo` is retired.
- `-C`'s "a named worktree's copy never runs" and "a worktree's copy hands off": the `-C` hand-off is retired (one kit copy).
- `StubMode`: the PM kit's stub runtime is retired. Stubs are `kitlib.stubs` now ([install.md](install.md)).
- `NoLockConfigured`: the kit always has a landing lock. `TheDefaultLock` tests the default `kit-landing.lock` instead.
- The special message for `landing_stale_minutes`: it is now the generic unknown-key refusal of `kitlib.config`.
