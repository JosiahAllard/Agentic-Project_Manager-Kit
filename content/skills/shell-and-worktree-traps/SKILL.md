---
name: shell-and-worktree-traps
description: Use before running shell commands on the Windows or Linux machine (Git Bash or PowerShell on Windows, bash on Linux), checking line endings, capturing test output, or removing worktrees and branches. Covers silent shell traps, one section per platform, that produce wrong or stray results, and the worktree and gate rules workers break by accident.
---

# Shell and worktree traps

Two sections, one per platform: read the one your platform line names (Windows or Linux) and skip the other.

## Windows

1. Use absolute paths and your assigned worktree. Never leave a shell inside a worktree about to be removed: `cd` holds a Windows handle on the folder, and a later `git worktree remove` fails "Permission denied".
2. Line endings: check the committed blob, `git show <rev>:<path>`. In Git Bash `grep -c $'\r'` matches every line, an always-true CRLF test; `.gitattributes` normalises on commit.
3. Any script with Windows paths, regexes or escapes: write it to a file with your file-write tool and run it by absolute path. A Bash heredoc mangles backslash paths (a worktree path once ended up mangled and was force-removed), collapses `\\` to `\` in regexes, and can turn `\n` or `\0` into real newline or NUL characters inside a Python file. With no file-write tool, write the file in small quoted-marker heredoc chunks (`<<'EOF'`, a few lines each, appended with `>>`): one long heredoc, worst with backslashes, fails "unexpected EOF looking for a matching quote", and resending the same form fails the same way.
4. PowerShell file APIs: `[IO.File]::ReadAllText` / `WriteAllText` resolve a relative path against the PROCESS cwd (often the main checkout), not `$PWD`: a failed read gives `$null`, then `WriteAllText` creates an empty stray there. Use your file-edit tool, or an absolute path. Run shell probes with your scratch folder as the working directory, never the main checkout: a stray file in main stops every landing.
5. `python - @'...'@` passes the here-string as an argument, not stdin: Python opens a REPL that can loop and fill the disk. Write the script to a file, or pipe the here-string to Python.
6. A PowerShell function or pipeline returning exactly ONE object gives a bare scalar with no `.Count` (`$null`), so `$x.Count -gt 0` passes silently: wrap in `@()` before counting.
7. Subprocesses: pass arguments as an array with the shell disabled.
8. Test output: tee the whole output to a file (or `dotnet test --logger trx`), then read the file; never grep it live (a case-sensitive filter once matched the pass banner but not the failure text). Never pipe a gate or locked run through a truncating reader.
9. One foreground gate per worktree, on its assigned ports; never background one with a stray `&` (two at once fought over the same build).
10. Cleanup goes through the kit's tools (`python <kit>/tools/worktree_remove.py -C <main checkout> <worktree>`), only for a worktree that is yours to remove; otherwise report it to the lander or coordinator.
11. Read a tool's usage with `<tool> --help` alone, or into a file you read with Read; never pipe it to `head`, `tail` or `grep`.
12. Wait on a background run as <kit>/docs/gate.md "Waiting on a gate: who arms what" says (a run of your own: `run_in_background` and its exit notice), never with a foreground `sleep` or a poll loop: the host blocks a foreground `sleep`. To wait on any other condition: `run_in_background` with an until-loop that exits when it holds (`until <check>; do sleep 5; done`), or Monitor, which needs its `description` and `timeout_ms` (a call without them fails).

### Never (Windows)

- `git worktree remove --force`, `git branch -D`, deleting another session's branch, or taking over another session's run.
- `[IO.File]` or `[System.IO]` with a relative path.
- Kill a process by name or command-line match (`Stop-Process -Name`, `taskkill /IM`, `CommandLine -match ... | Stop-Process`): other sessions' gates match too. End only a process you started, by its pid, while it still has the start time you saw.
- Move an assigned worktree to another folder: the configured worktree location holds unless the owner assigns another.

Details: `<kit>/docs/worktree-remove.md`; `<kit>/docs/new-worktree.md`.

## Linux

1. Use absolute paths and your assigned worktree. Never leave a shell inside a worktree about to be removed: a later `git worktree remove` of the folder you stand in fails or strands the shell in a deleted directory.
2. Line endings: check the committed blob, `git show <rev>:<path>`. `grep -c $'\r'` in bash counts real carriage returns, but a file edited on Windows and checked out here may differ from the blob; `.gitattributes` normalises on commit.
3. Any script with regexes or escapes: write it to a file with your file-write tool and run it by absolute path. An unquoted heredoc (`<<EOF`) expands `$var`, backticks and `\`, so a script in it changes before Python or sed reads it; quote the marker (`<<'EOF'`) or write a file. With no file-write tool, write it in small quoted-marker chunks appended with `>>`: one long heredoc fails "unexpected EOF looking for a matching quote", and resending the same form fails the same way.
4. Quote every path and variable: `"$x"`, never bare `$x`. A path with a space or a glob character splits or expands, and `rm -rf $dir/` with `dir` unset expands to `rm -rf /`, the filesystem root; `--preserve-root` catches only that exact form, and `$dir/*` gets no protection. Run shell probes with your scratch folder as the working directory, never the main checkout: a stray file in main stops every landing.
5. A pipeline's exit status is its last command's: `cmd | tee log` is green when `cmd` failed. Run `set -o pipefail`, or write to a file and read `$?` of the command itself. Test output: tee the whole output to a file, then read the file; never grep it live. Never pipe a gate or locked run through a truncating reader (`head`): the writer gets SIGPIPE and dies.
6. A cwd that a sourced file, `cd` or subshell changed does not carry between your tool calls: give each call its own absolute paths or `git -C`.
7. Subprocesses: pass arguments as an array with the shell disabled.
8. One foreground gate per worktree, on its assigned ports; never background one with a stray `&` or `nohup` (two at once fought over the same build), and whether a `&` job survives its shell depends on your host's shell tool, so use the kit's background mode (`run_in_background`), never a bare `&`.
9. Cleanup goes through the kit's tools (`python <kit>/tools/worktree_remove.py -C <main checkout> <worktree>`), only for a worktree that is yours to remove; otherwise report it to the lander or coordinator.
10. Read a tool's usage with `<tool> --help` alone, or into a file you read with Read; never pipe it to `head`, `tail` or `grep`.
11. Wait on a background run as <kit>/docs/gate.md "Waiting on a gate: who arms what" says (a run of your own: `run_in_background` and its exit notice), never with a foreground `sleep` or a poll loop: the host blocks a foreground `sleep`. To wait on any other condition: `run_in_background` with an until-loop that exits when it holds (`until <check>; do sleep 5; done`), or Monitor, which needs its `description` and `timeout_ms` (a call without them fails).
12. File permissions and symlinks: a checkout keeps the executable bit and symlinks as git recorded them; do not `chmod -R` a tree, and do not `cp -r` or `rm -rf` through a symlink that points out of the worktree.

### Never (Linux)

- `git worktree remove --force`, `git branch -D`, deleting another session's branch, or taking over another session's run.
- Kill a process by name or command-line match (`pkill -f`, `killall`, `kill $(pgrep ...)`): other sessions' gates match too. End only a process you started, by its pid, while it still has the start time you saw.
- `sudo`, `chmod -R`, or `rm -rf` on a path built from a variable.
- Move an assigned worktree to another folder: the configured worktree location holds unless the owner assigns another.

Details: `<kit>/docs/worktree-remove.md`; `<kit>/docs/new-worktree.md`.
