---
name: shell-and-worktree-traps
description: Use before running shell commands on the Windows machine (Git Bash or PowerShell), checking line endings, capturing test output, or removing worktrees and branches. Covers silent PowerShell and Git Bash traps that produce wrong or stray results, and the worktree and gate rules workers break by accident.
---

# Shell and worktree traps

## Git Bash

- `grep -c $'\r'` matches every line here, an always-true CRLF test; `.gitattributes` normalises on commit. Check the committed blob: `git show <rev>:<path>`.
- Backslash Windows paths in a Bash heredoc get mangled (a worktree path once ended up mangled and was force-removed). The heredoc also collapses a double backslash to one, corrupting regexes in a patch script, and can turn `\n` or `\0` escapes into real newline or NUL characters inside a Python file. Write the script with the Write tool and run it by path.
- `cd` in the Bash tool holds a Windows handle on the folder, so a later `git worktree remove` fails "Permission denied". Use absolute paths; never leave a shell inside a worktree about to be removed.

## PowerShell

- `[IO.File]::ReadAllText` and `WriteAllText` resolve a relative path against the PROCESS cwd (often the main checkout), not `Set-Location`'s `$PWD`: a failed read gives `$null`, then `WriteAllText` creates an empty stray there. Never use `[IO.File]` or `[System.IO]` with a relative path; use Edit or Write, or an absolute path.
- `python - @'...'@` passes the here-string as an argument, not stdin: Python opens a REPL that can loop and fill the disk. Write the script with Write and run it by path.
- A function or pipeline returning exactly ONE object gives a bare scalar with no `.Count` (`$null`), so `$x.Count -gt 0` passes silently on non-empty. Wrap in `@()` before counting.

## Test output

- Grepping `dotnet test` output instead of teeing it can lose the result (a case-sensitive filter matched the pass/fail banner but not the failure text). Tee the whole output to a file (or use `--logger trx`), never grep it live.

## Gates and worktrees

- One foreground gate per worktree; never background one with a stray `&` (two at once fought over the same build).
- A worker never force-removes a worktree or deletes a branch (`worktree remove --force`, `branch -D`). Report it; the lander or coordinator handles it.
