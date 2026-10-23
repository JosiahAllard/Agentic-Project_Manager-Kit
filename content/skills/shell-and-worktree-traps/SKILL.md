---
name: shell-and-worktree-traps
description: Use before running shell commands on the Windows machine (Git Bash or PowerShell), checking line endings, capturing test output, or removing worktrees and branches. Covers silent PowerShell and Git Bash traps that produce wrong or stray results, and the worktree and gate rules workers break by accident.
---

# Shell and worktree traps

1. Use absolute paths and your assigned worktree. Never leave a shell inside a worktree about to be removed: `cd` holds a Windows handle on the folder, and a later `git worktree remove` fails "Permission denied".
2. Line endings: check the committed blob, `git show <rev>:<path>`. In Git Bash `grep -c $'\r'` matches every line, an always-true CRLF test; `.gitattributes` normalises on commit.
3. Any script with Windows paths, regexes or escapes: write it to a file with your file-write tool and run it by absolute path. A Bash heredoc mangles backslash paths (a worktree path once ended up mangled and was force-removed), collapses `\\` to `\` in regexes, and can turn `\n` or `\0` into real newline or NUL characters inside a Python file.
4. PowerShell file APIs: `[IO.File]::ReadAllText` / `WriteAllText` resolve a relative path against the PROCESS cwd (often the main checkout), not `$PWD`: a failed read gives `$null`, then `WriteAllText` creates an empty stray there. Use your file-edit tool, or an absolute path. Run shell probes with your scratch folder as the working directory, never the main checkout: a stray file in main stops every landing.
5. `python - @'...'@` passes the here-string as an argument, not stdin: Python opens a REPL that can loop and fill the disk. Write the script to a file, or pipe the here-string to Python.
6. A PowerShell function or pipeline returning exactly ONE object gives a bare scalar with no `.Count` (`$null`), so `$x.Count -gt 0` passes silently: wrap in `@()` before counting.
7. Subprocesses: pass arguments as an array with the shell disabled.
8. Test output: tee the whole output to a file (or `dotnet test --logger trx`), then read the file; never grep it live (a case-sensitive filter once matched the pass banner but not the failure text). Never pipe a gate or locked run through a truncating reader.
9. One foreground gate per worktree, on its assigned ports; never background one with a stray `&` (two at once fought over the same build).
10. Cleanup goes through the kit's tools (`python <kit>/tools/worktree_remove.py -C <main checkout> <worktree>`), only for a worktree that is yours to remove; otherwise report it to the lander or coordinator.

## Never

- `git worktree remove --force`, `git branch -D`, deleting another session's branch, or taking over another session's run.
- `[IO.File]` or `[System.IO]` with a relative path.
- Kill a process by name or command-line match (`Stop-Process -Name`, `taskkill /IM`, `CommandLine -match ... | Stop-Process`): other sessions' gates match too. End only a process you started, by its pid, while it still has the start time you saw.
- Move an assigned worktree to another folder: the configured worktree location holds unless the owner assigns another.

Details: `<kit>/docs/worktree-remove.md`; `<kit>/docs/new-worktree.md`.
