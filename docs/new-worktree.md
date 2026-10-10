# new_worktree

Held for the joint review: no repository runs it yet.

`tools/new_worktree.py` makes a workstream's worktree in one call. Logic: `kitlib/worktree/new.py`; tests: `tests/worktree/test_new_worktree.py`. Ported from the PM kit's `tools/new_worktree.py`.

```
python tools/new_worktree.py [-C DIR] <area>/<slug>                 a NEW branch off the main branch
python tools/new_worktree.py [-C DIR] --existing <area>/<slug>      an EXISTING branch, resumed
                             [--base BRANCH]                        another base than the main branch
```

Exit 0 made; 1 refused (nothing made, the lines say why); 2 usage, or the kit configuration was refused; 130 Ctrl+C.

- `-C` names the repository by any of its work trees (default: here). The worktree is always the MAIN checkout's: `<main>/<worktrees_dir>/<slug>`.
- It fetches `<remote>`, and refuses unless local `<base>` equals `<remote>/<base>` (exceptions: a landing in progress, and a local base strictly behind, both below). A `--base` given as `<remote>/<name>` names the branch `<name>` (`branch_name`). It never switches a branch in the main checkout and never touches another worktree.
- A new branch starts at the base's hash and tracks nothing.
- The mode is what the caller said: the default refuses a branch that exists, `--existing` refuses one that does not.
- Names: `<area>/<slug>`, ASCII letters, digits, `_ . -`, each part starting with a letter or digit, the area lowercase-first. A new slug is lowercase-first; `--existing` also takes a capital. A slug or branch that differs only in case from one already there is refused.

## The landing exception

The lock is the kit landing lock (`kitlib.records.landing`, the file in the git common dir that `gate.py land` holds). While it is held, the base may carry unpushed merges, so:

- a new branch starts at the pushed `<remote>/<base>`;
- the output names the base's unpushed commits the branch does not contain.

A lock file that is not a lock record counts as held. The one refusal under a held lock is that `<remote>/<base>` does not resolve. With the lock free, the refs are read once more before refusing (`choose_base`), in case a lander pushed and released in between. The lock is only read.

## Local base behind the remote

With the landing lock free and local `<base>` a pure fast-forward behind `<remote>/<base>` (an ancestor of it, not equal: no commit of its own), the new branch starts at `<remote>/<base>` (by hash) and one line says so:

```
base      local main is 2 commit(s) behind origin/main; this branch starts at origin/main <hash> and local main is not updated
```

Local `<base>` is not updated (`behind_count` in `kitlib/worktree/new.py` only reads). A local base with a commit the remote lacks, ahead or diverged, is refused as before. Under a held landing lock the landing wording applies instead.

## Keys read

- `.kit.json` "repo": `main_branch`, `remote`, `worktrees_dir`, `tools_dir`.
- "landing": `lock_file` (`git:<name>`) and `lanes`.

"new_worktree" has no keys.

## Not ported

These source tests were dropped with the mechanisms they tested:

- `TheCallerStandsInTheRepository`, and `-C`'s "another repository is a usage error": `_repo_guard.check_same_repo` is retired.
- `-C`'s "a named worktree's copy never runs" and "a worktree's copy hands off": the `-C` hand-off is retired (one kit copy).
- `StubMode`: the PM kit's stub runtime is retired. Stubs are `kitlib.stubs` now ([install.md](install.md)).
- `NoLockConfigured`: the kit always has a landing lock. `TheDefaultLock` tests the default `kit-landing.lock` instead.
- The special message for `landing_stale_minutes`: it is now the generic unknown-key refusal of `kitlib.config`.
