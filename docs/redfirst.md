# redfirst

`tools/redfirst.py [-C WORKTREE] --tests FILE[::FILTER] ... [--also FILE ...] [--base REV] [--head REV] [--log-dir DIR]` runs new tests against the pre-change commit and confirms they fail there: red-first evidence with no in-place mutant. Logic: `kitlib/redfirst` (`verdict.py` pure, `run.py` git and cleanup); the module docstrings hold the full rules.

- The test files must be committed at `--head` (default HEAD). They are checked out into a throwaway detached worktree at `--base`, `<main checkout>/<worktrees_dir>/redfirst-<base10>-<pid>`. The default base is the merge-base with `<remote>/<main branch>`. The caller's worktree is only read. A main checkout is refused.
- `.kit.json` "redfirst".runners picks the runner. The first entry whose `glob` matches the path (case-blind) wins. Kinds:
  - `python`: `python -m unittest` with `PYTHONPATH=<pythonpath>`;
  - `gradle`: `<dir>/gradlew <project>:test --offline --tests <filter>`;
  - `dotnet`: `dotnet test <csproj>` with a trx log.
  
  A runner's `args` are appended. The keys are listed in docs/kit-json.md under "`.kit.json` keys".
- One line per selector: RED, GREEN, NONE (no test matched) or ERROR. A python test that does not import against the base is RED, with the final ImportError or ModuleNotFoundError line as its reason. A gradle or dotnet compile error is RED only when it names a copied test file; otherwise it is ERROR.
- Cleanup runs on every exit the process controls, Ctrl-C included:
  - It runs in a child process on its own console.
  - It puts back the base's copy of each `--also` file, then removes the tree with `kitlib.worktree.remove` (the checks and `git_remove` that `worktree_remove.py` uses), never forced.
  - A tree still held after `"redfirst".hold_wait` seconds is left in place, with the `worktree_remove.py` command that removes it.
- Exit codes: 0 every selector RED, 1 any GREEN, 2 usage or refusal (`--help` too), 3 undecided, 4 throwaway left.

Not ported: the pid shield around the test run (a plain subprocess). Tests: `tests/redfirst`.
