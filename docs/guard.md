# Shell guard and git hooks

`claude/hooks/guard.py` is the kit's one PreToolUse hook (ARCHITECTURE.md "Layers": Guards). `githooks/pre-push` is the kit's guard git hook. Both are thin entries over `kitlib/guard/`. `githooks/` also holds `pre-commit`, the ledger check, inert unless a repository opts in (docs/ledger.md "Pre-commit check").

**Not live:** none is registered in any settings file or `core.hooksPath`.

## Output

- **Allow:** guard.py prints nothing and exits 0.
- **Deny or ask:** it prints `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"|"ask", "permissionDecisionReason": ...}}` and exits 0.
- **Reason format:** `<what and why> Instead: <the exact command to use> [kit guard: rule=<rule> role=<main|subagent>]`. The role is what the event said (`agent_id` present or not). The self-test reads it.
  - After `Instead:` comes one command, ready to run, with no prose after it. Any explanation goes in the sentence before it, which ends with `:` (tests/guard/test_rules.py `test_rows` checks this for every row).
- **Unguarded tools:** a tool no rule can refuse (any tool but Edit, Write, MultiEdit, NotebookEdit, Bash, PowerShell and `mcp__*`) is allowed before kitlib is imported (`claude/hooks/guard.py` `unguarded`; tests/guard/test_entry.py `Unguarded`).
- **Guard error:** if the guard fails on a guarded tool, it denies with rule `guard-error` (fail closed).
  - This includes a kitlib that will not import: `claude/hooks/guard.py` imports kitlib inside its `try` and builds that deny by hand. Its `Instead:` reruns guard.py on the event.
  - If even that cannot be written, guard.py exits 2, which Claude Code also reads as a block.
  - Test: tests/guard/test_entry.py breaks the import.

## Parsing

`kitlib/guard/shell.py` tokenises Bash and PowerShell, and `cmd /c` text. `commands.py` reads the result.

- **Seen as text, not commands:** quotes, here-docs and PowerShell here-strings.
- **Seen as commands:**
  - `;`, `&&`, `||` and `|` split commands;
  - `$(...)`, backticks and `powershell -Command "..."`, `bash -c`, `cmd /c` (`//c`, `/k`, `//k` too, as Git Bash passes them) bodies are parsed again as nested commands;
  - the command after find `-exec`, `-execdir`, `-ok` or `-okdir` (up to `;` or `+`) is a nested command at find's working directory (`-execdir`/`-okdir`: at an unknown one), its words that hold `{}` computed;
  - a `trap` handler is a nested command at the trap's working directory, or at an unknown one when the line holds `cd`, `pushd`, `popd` or `Set-Location` anywhere (`commands.LINE_CD_RE`); a `mapfile`/`readarray -C` callback is a nested command;
  - `env -S <string>` is a nested command;
  - `cd`, `Set-Location`, `pushd` and `env -C <dir>` move the working directory for the commands after them.
- **Wrappers:** `env`, `command`, `exec`, `nohup`, `nice`, `time`, `timeout <n>`, `stdbuf`, `sudo` and `xargs` are skipped with their options (`time -p`, `nice -n 5`, `stdbuf -oL` and so on). The program after them is read. After `xargs -I`, `-i` or `--replace` (`commands.xargs_opts`), every later word that holds the replace string is computed (`commands.fill_words`): the program word and `sh -c` or `env -S` text too. The first `git` word in its tail is the program.
- **Redirects:** recorded per command with their targets.
- **Arguments the shell rewrites:** brace expansion (`src/{a,b}`), ANSI-C strings (`$'a\tb'`) and a bash backslash in a word are read as the words the shell produces.
- **Git spellings:** `-C`, `-c`, `--git-dir`/`--work-tree`, `--config-env` and git's place and config variables are read; long options are matched by unique prefix, and options that take a value skip it.
  - The variables (`commands.GIT_ENV_NAMES`): `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_VALUE_<n>`, `GIT_CONFIG_PARAMETERS`. One counts when set before the command (`=` or bash `+=`), through `env`, by `export` and the like (with or without a value), `$env:`/`${env:}` (`=` alone or in the same word), `Set-Item`, `Set-Content`, `Add-Content` or `New-Item -Name` on `env:`, a `Rename-Item`/`Copy-Item`/`Move-Item` to an `env:` name, or `SetEnvironmentVariable` (`commands.git_env_set`). Set anywhere in the line, nested text included (`eval`, `bash -c`, `env -S`, `Invoke-Expression`, a `source` of `/dev/stdin`), it leaves every git of the line unplaceable (`commands.mark_git_env`), whatever the order. A name is read as the shell passes it: a backslash bash drops (`export GIT_D\IR=`, `export \GIT_DIR=`, `env \GIT_DIR=`) is dropped (`commands.said`, `commands.passed`).
  - Fail closed: anything else on the line that may change the environment also leaves its git unplaceable (`commands.git_env_unsafe`). Only literal assignments are read (`NAME=v`, `export`/`declare`/`local NAME=v`, `env NAME=v`, `$env:NAME = v`, `Set-Item env:NAME`, cmd `set NAME=v`), with a name of `[A-Za-z_][A-Za-z0-9_]*` that is neither `SHELLOPTS`, `BASHOPTS`, `BASH_ENV`, `ENV` nor a `GIT_*` name outside `commands.GIT_SAFE_NAMES` (author and committer name, email and date, `GIT_TERMINAL_PROMPT`, `GIT_MERGE_AUTOEDIT`, `GIT_OPTIONAL_LOCKS`).
  - Everything else counts: a computed name (`export $V=`, `local -x $1=`, `Set-Item "env:$n"`, cmd `set %V%=`), `declare -n`, `-a`/`-o allexport` in `set`, `shopt` or a nested `bash`/`sh`, `source` of a file, `alias`, a `trap` handler other than `''` or `-` on any signal but `EXIT` or `0`, `mapfile`/`readarray -C`; git that `xargs -I`/`-i`/`--replace` runs, or that `xargs` runs with no subcommand (its input may add `--git-dir`); a PowerShell item cmdlet (`Set-Item`, `New-Item`, `Remove-Item`, `Rename-Item`, `Copy-Item`, `Move-Item`, `Set-Content`, `Add-Content`, `Clear-Item`) whose item is computed, piped in or an expression (`Set-Content` and `Add-Content` take piped input as the value, so with a path in the text they are read); a splatted argument (`@h`) on any command; `InvokeScript`, `NewScriptBlock`, `ExpandString`, `GetScriptBlock`, `AddScript`, `[scriptblock]::Create` or `[powershell]::Create` under any `System.Management.Automation.` prefix, or a static call on a computed type (`([type]'scriptblock')::Create`), outside a literal string (`git commit -m "Handle InvokeScript"` passes, `"$(...)"` counts); `Set-Location` to `env:` or a computed place; any `[Environment]::` call but a literal-name `GetEnvironmentVariable`; `Set-Variable` on `env:`; `Set-Alias`, `New-PSDrive`, `Import-Module`; a dot-sourced `.ps1`, cmd `call` or a `.bat`; `Start-Process -Environment`.
  - A `$env:VAR` that starts an item path (`Set-Content "$env:TEMP\x.txt"`, `Copy-Item a.txt $env:TEMP`) is a read, unless the line sets `VAR` (`$env:VAR = v`, also with the `=` starting the value's word: `$env:VAR ="v"`) or an item cmdlet names it beside an `env:` item (`Rename-Item env:FOO VAR`, `Copy-Item env:FOO env:VAR`). A line with no git is not affected (`set -a; . ./.env; set +a` passes).
  - Once one of these, `--git-dir`, `--work-tree`, `core.worktree` or `--config-env` makes git unplaceable, a `-C` before or after it does not place it again (`commands.git_call`).
  - Every git word is matched in the form the shell produces, the subcommand and options included, not only the program word.
  - A subcommand the text does not spell out (a variable, a substitution) makes the parse unclean. So does such a word in an option position that the git rules read.
  - Put `--` before a variable path, so it is not read as an option.
- **Unclean parse:** the parse is unclean when the text holds something the tokeniser does not model:
  - an unclosed quote, substitution or heredoc, or a `case` statement;
  - an encoded command, or a shell, `iex` or `Invoke-Expression` reading its commands from a pipe;
  - `iex`/`Invoke-Expression` of a computed value (`iex ("git " + "stash")`);
  - a shell or `source` reading a process substitution (`bash <(...)`, `source <(...)`);
  - a non-literal `bash -c`, `powershell -Command`, `cmd /c`, `eval` or `env -S` text, `trap` handler or `mapfile -C` callback;
  - a program named by a variable or a substitution;
  - a git subcommand or read option that is not literal;
  - a brace expansion, ANSI-C escape or backslash in a program name;
  - a `cmd` `if` statement or `for ... do` loop.

  The rules still run on what was read, then "Fallback" runs.

Tests: tests/guard/test_shell.py.

## State

Every rule is a read-only check of real state. `kitlib/guard/context.py` `Ctx` gathers what the rules read, so tests can hand them a fake machine.

- **Place:** `kitlib.git.repo.classify`'s answer for the working directory or the target path: MAIN, LINKED, NONE or UNKNOWN.
  - `kitlib/guard/place.py` reads it from `.git`, gitdir and commondir files, so a call starts no git child. Any shape it cannot read the way git would (a bare or `core.worktree` repository, `[extensions]`, a broken gitfile, a path inside a git directory, a junction, symlink or subst drive on the way, a folder another user owns, a discovery variable such as `GIT_OBJECT_DIRECTORY`, any error while reading) goes to `repo.classify`.
  - Tests: tests/guard/test_fsplace.py asks both on real repositories; tests/guard/test_place_decisions.py decides every rule row with each and requires the same decision.
  - The PM data worktree (docs/pm-data-branch.md "Guard place and commit hooks") counts as MAIN, though git calls it LINKED: `Ctx.place` answers MAIN for a linked worktree whose top is the machine config's `pm_data.path`. Only when the machine config has no `pm_data` does the record decide: a linked worktree whose HEAD is `ref: refs/heads/<branch>` of the `kit-pm-data.json` record in its git common dir. When the strict reader refuses the machine config for another key, `pm_data` is read and checked on its own. The main-checkout rules then apply there, and a refusal names no new worktree to make (agents do not work in it). `git push` and `git worktree remove` act on the repository the data worktree shares with the kit, so from it they are judged by the kit's own `.kit.json` (refused when it does not load), and the data branch alone by the data worktree's. A main checkout is never changed. Fail closed (rule `pm-data`, subagent writes): a machine config whose `pm_data` does not read denies every one; a linked worktree whose record or HEAD does not read denies those aimed at or run from a linked worktree of that repository. Tests: tests/guard/test_rules.py `PmDataPlace`.
- **Config:** `.kit.json` of the main checkout a path belongs to, via `kitlib.config.load`. A worktree's own `.kit.json` is never read, so a branch cannot loosen its own rules. The one exception is the PM data worktree, which counts as MAIN: its own `.kit.json` is read (none there: the defaults, under which a subagent may not edit it).
- **Lock:** held only when `MACHINE_LOCK_RUN` names a run that `kitlib.lock.run.nest_check` accepts: an ancestor of this process, alive, and holding. The variable alone is never proof.
- **Gate-OK record:** `kitlib.gate.runner.verify_record(root, sha)`, as the pre-push hook reads it. The record must name a `config_rev` in that commit's history, and its gate config hash must be that of the `.kit.json` "gate" the `config_rev` commit holds (docs/gate.md "Gate-OK record"). A record without `config_rev`, or made under another gate config, does not count.
- **User config:** `~/.claude-pm-kit/config.json`, for `ledger_path`, `guard_mcp_prefix`, `guard_mcp_tools` and `guard_agent_marks`, read tolerantly. A file the strict reader refuses still yields an absolute `ledger_path`, so the ledger stays protected. A file that cannot be read, is not JSON or is not an object denies every subagent write (rule `ledger`, fail closed).

## Rules

`kitlib/guard/rules.py` `decide(event, ctx)`. Every row below is a row in tests/guard/test_rules.py with the exact `Instead:` text. `<main>` is the main checkout, quoted, and `<wt>` is the session's own worktree.

| rule | denied | instead |
|---|---|---|
| `wrapped-gate` | `gate.py` or `machine_lock.py run` piped into another command, as in `cd <wt> && python tools/gate.py 2>&1 \| tail` (refusal pattern P16) | `<the bare command> > "<temp>/<slug>-gate.log" 2>&1`. The sentence before it names the working directory (the `cd` target) and says to read the log. |
| `root-search` | `find /`, `find C:\`, `Get-ChildItem C:\ -Recurse` (or `-Depth`), `dir /s C:\`, `ls -R /`, `tree`, `findstr /s`, `du`, `grep -r` or `rg` on a drive root, `where /r C:\`. The user's home counts as a root, and so do `HOME`, `USERPROFILE`, `HOMEDRIVE`, `SystemDrive` and `HOMEPATH` written as `~`, `$X`, `${X}`, `$env:X` or `%X%`, alone or two together | the same command on the session's working directory, a wildcard leaf kept, else `Glob(pattern="**/*", path="<a project folder>")` |
| `heavy` | a heavy program (`kitlib.config.heavy_programs`: kit defaults godot, godot.\*, godot_\* (not godotenv), blender\* in the background, dotnet `build`/`test`/`publish`/`run`/`pack`/`msbuild`/`vstest`, gradle and gradlew (not `--version`, `--help`, `--stop`, `--status`), plus `.kit.json` `heavy`.programs, where a repo lists its own) without a held lock, unless it is an argument of `gate.py` or `machine_lock.py run` | `python ~/.claude/tools/machine_lock/machine_lock.py run --lock --kind <kind> --repo <repo> --slug <slug> -- <command>` |
| `main-checkout` | Edit, Write, MultiEdit or NotebookEdit into a main checkout or an UNKNOWN place, by a role whose `edit_main_checkout` is false (default: `subagent`). Shell writes count the same way; the list is below the table | the same tool call or command aimed at the same path in `<wt>`, else `git -C <main> worktree add .claude/worktrees/<slug> -b <branch>` |
| `ledger` | any edit or shell write to `ledger_path`, by any role | `python tools/ledger.py add --repo <repo> --prov <prov> --q "<question>" --a "<answer>" --decides "<what it decides>" --source "<source>"`, the kit's `tools/ledger.py` (docs/ledger.md). For a broken user config: `python -m json.tool "<home>/.claude-pm-kit/config.json"` |
| `pm-data` | a subagent's edit, shell write or git write while the guard cannot tell the PM data worktree: the machine config's `pm_data` does not read (every such write), or a linked worktree's `kit-pm-data.json` or HEAD does not (writes aimed at or run from a linked worktree of that repository); see "State" | `python -m json.tool "<home>/.claude-pm-kit/config.json"` |
| `git-checkout` | `checkout`, `switch` in a main checkout | `git -C <main> worktree add .claude/worktrees/<slug> <branch>` (the sentence before it names `git show <rev>:<path>`) |
| `git-reset-hard` | `reset --hard` in a main checkout | `git -C <main> reset --keep <rev>` |
| `git-clean` | `clean` without `-n` in a main checkout | `git -C <main> clean -n` |
| `git-stash` | `stash` (not `list`, `show`) in a main checkout | `git -C <main> worktree add .claude/worktrees/<slug> -b <branch>` |
| `git-bulk-add`, `git-commit-all` | `add -A`/`.`/`-u`, `commit -a` in a main checkout | `git -C <main> add -- <each path you changed>` |
| `git-restore` | `restore` in a main checkout, by any role, unless it only unstages (`--staged`/`-S` without `--worktree`/`-W`) | `git -C <main> restore --staged -- <path>` |
| `git-main-history` | `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `pull`, `apply` (not `--check`/`--stat`/`--numstat`/`--summary`), `rm`, `mv`, `checkout-index`, `update-ref`, `reset` other than `--hard`, `read-tree -u`, `branch -f`/`--force`, in a main checkout, by a role whose `edit_main_checkout` is false (default: `subagent`; a lander gets it from `.kit.json` `roles`) | `git -C <wt> <the same subcommand and arguments>` when the session has a worktree of that checkout, else `git -C <main> worktree add .claude/worktrees/<slug> -b <branch>` |
| `git-branch-force-delete` | `branch -D` or `branch -d --force`, anywhere | `git branch -d <names>` |
| `git-worktree-remove` | `worktree remove`, run from anywhere (a linked worktree too), of a worktree that is not clean, whose HEAD is not merged into main, or that the text cannot name | not named: `git -C <main> worktree remove "<worktree path>"`; not clean: `git -C <target> status`; unmerged: `git -C <target> log --oneline <main branch>..HEAD`. Unmerged work goes to the PM; `worktree_remove.py --abandon` only on the PM's word |
| `git-push-main` | in a `push_block` repo: a push to main whose commit has no gate-OK record, or one with `--no-verify`, force, `+`, delete, a non-literal refspec, or a `.kit.json` that does not load | `python <tools_dir>/land.py <branch> --expect-main <sha> --expect-tip <sha>`. Other cases have their own command: an unknown directory gets `git -C <checkout> push ...`, a bad config gets `python -m json.tool <top>/.kit.json`, and a delete gets `git -C <dir> ls-remote <remote> <main>` |
| `git-alias` | a git alias defined on the command line (`-c alias.x=...`, `--config-env`), anywhere | the command the alias runs, written out |
| `unreadable` | an unclean parse (see "Fallback") | the line written plainly |
| `lock-mcp` | the lock MCP's owner-only tools (`kitlib.lockmcp.server.OWNER_ONLY`) | `python <home>/.claude/tools/machine_lock/machine_lock.py status`; see "Lock MCP" below |
| `owner-lock` | a Bash or PowerShell call that runs `reserve_ui`, or runs a lock CLI owner-only write, in any session but the owner's own; see "Owner lock tools" below | the lock CLI's `status`, as for `lock-mcp` |
| `busy-wait` | a foreground loop that sleeps with nothing bounding it, in any session: bash `while`/`until` (also inside `bash -c`), PowerShell `while`/`do`, sleeping by `sleep`, `Start-Sleep` or `[Threading.Thread]::Sleep`. A bound is a `-lt`/`-le`/`-gt`/`-ge` comparison, `((...))`, `$SECONDS`, a stopwatch's `.Elapsed` or `timeout`. A call with `run_in_background`, a loop sent to the background with `&`, `for`, `foreach` and `while read` (also `while IFS= read`) pass | bash: `timeout 600 bash -c '<the loop>'`, its redirects kept; PowerShell: the loop with `$deadline = (Get-Date).AddMinutes(10)` and a `break` past it. The sentence before it names `run_in_background` and Monitor |
| `heredoc-winpath` | python reading its script from a bash heredoc in which a string that is not raw holds a Windows path with single backslashes (a drive letter, then at least two path characters), in any session. An unquoted `<<EOF` is read as bash hands it over, so its `\\` counts as one backslash. Raw strings, forward slashes, doubled backslashes, comments and escapes such as `"%s:\n"` or `"x:\t"` pass, and so does a heredoc that is not python's script | `python "<temp>/script.py"`; the sentence before it says to write the script with the Write tool |
| `git-hooks-path` | a change of `core.hooksPath` (`git config` set, `--unset`, `--add`, also with `-f`/`--file` naming a config file under a `.git` folder (a worktree's or submodule's included, after `.`/`..` are resolved), a user config file, or a path the text does not name; `-c core.hooksPath=` or `--config-env` on any git command; `git clone -c`/`--config`; `GIT_CONFIG_PARAMETERS` or `GIT_CONFIG_KEY_<n>` set to a value naming it), in every session, the owner's included. Setting it to the kit's own githooks (the install step), `-f` on any other file, and a repository or config file under the temp folder (not `--global`/`--system`), pass | `git -C <dir> config --get core.hooksPath`; for `-c` and `clone -c`, the command without the override |
| `owner-identity` | a session other than the owner's own (see "Session mark") giving a commit the `.kit.json` `owner.email`: `git commit --author`, `user.email`/`author.email`/`committer.email` set by `git -c` or `git config`, or `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_EMAIL` or `EMAIL` set as an environment variable on the line (a bash prefix or `env`, `export`, `setx`, `$env:`, `Set-Item env:`, `[Environment]::SetEnvironmentVariable`). The whole address, any case; a longer address that contains it passes, and so does `email=<address>` that is not one of those variables being set (a form field, a `--email=` option, a commit message) | `git -C <dir> config --get user.email`: commit as yourself |
| `kill-by-name` | an agent session (see "Session mark") ending processes by name: `taskkill /IM` or `/FI "IMAGENAME eq ..."`, `pkill`, `killall`, `Stop-Process -Name`, `Get-Process <name> \| Stop-Process`. The values of options that take one (`pkill -P 1234`, `-u <user>`, `-t <tty>`, `killall -s <signal>`) are not names; a `pkill` or `killall` with no name left passes | `tasklist /FI "IMAGENAME eq <name>"` (PowerShell: `Get-Process -Name <name> \| Select-Object Id, StartTime, Path`); the sentence before it says to stop the one started, by id |
| `lock-state` | an agent session's file tool edit or shell write under `<home>/.claude/machine-lock`, which only the lock tool writes | the lock CLI's `status`, as for `lock-mcp` |
| `kit-json` | an agent session's file tool edit or shell write of a file named `.kit.json` (by its name or the name its links resolve to) in a checkout, a linked worktree's included, or a place git cannot tell. One outside git passes | `python -m json.tool "<the file>"`; the sentence before it says to ask the coordinator to take the change to the owner |
| `write-jail` | an agent session's file tool edit or shell write outside its write roots, and git that changes a checkout outside them; see "Write jail" below. The sentence names every root and says to ask the coordinator | the same tool call or command aimed at the same file name in `<wt>` (else the temp folder); for git, `git -C <wt> <the same subcommand and arguments>`; for an existing file in an add-only folder, the same write to `<folder>/<new file name>` |

**Shell writes** (`kitlib/guard/rules.py` `write_targets`):
- Redirects.
- PowerShell write cmdlets and their aliases, with parameters matched by prefix. These include `Set-ItemProperty` (`sp`), `Start-Transcript` and `[IO.File]`/`[IO.Directory]` calls.
- cp, mv, rm, touch and tee; `sed -i`, `perl -i` and `patch`.
  - cp, mv, ln and install (`rules.CopyArgs`) write the directory `-t` names, also inside a cluster (`-rt <dir>`, `-t<dir>`) or as `--target-directory[=]<dir>` or any unique prefix of it; else the last operand. A long option that is unknown or ambiguous, or an option that is not literal, makes every operand a target. mv also writes its sources; `install -d` writes every operand; `ln <target>` alone writes the working directory.
- `curl -o` (in `--output-dir` when given), `wget -O` (not `--spider`) and `rsync` (not `-n`/`--dry-run`).
- `robocopy` and `xcopy` from any shell, and cmd copy, del and move.
- These write into the working directory, or into the folder they name:
  - `Expand-Archive`;
  - `tar -x` (or `-C <dir>`);
  - `unzip` (or `-d <dir>`; not `-l`/`-t`/`-v`/`-Z`/`-p`);
  - `Start-Process` redirects.
- Code given on the line counts as a write to the working directory: `python -c` or a script on stdin (any `python`, `python3.12`, `py` or `pythonw`), `node -e`/`-p`, `ruby -e` and `perl -e`.
- find `-delete` writes to find's paths.
- A writer fed by `xargs` with no literal target writes the working directory.

A write target that is not literal (a variable, a substitution) is denied when the command runs in a main checkout or a place the guard cannot tell (fail closed). From a linked worktree, or any other place git can tell, it is not denied.

## Write jail

`kitlib/guard/jail.py` (roots and verdicts), `kitlib/guard/rules.py` `write_jail` and `git_jail` (rule `write-jail`). It holds agent sessions only, as `kill-by-name` does ("Session mark"): subagents and the marks in `guard_agent_marks` (default `coordinator` and its old name `conductor`). The owner's unmarked session, the PM's (`pm`) and other marks (`deep-worker`) are not held. Tests: tests/guard/test_jail.py.

- **Write roots** of a session running in `<cwd>` (the hook payload's `cwd`):
  - **its checkout:** the git top of `<cwd>`. In a linked worktree that worktree alone, so a sibling worktree and the main checkout are outside. A session running in a main checkout gets the whole checkout (the `main-checkout` rule still holds a subagent there). Outside git, `<cwd>` itself, unless it is a drive root or the home folder.
  - **temp:** the system temp folder (`Ctx.tempdir`, and `TMP` when it differs), which holds Claude Code's session scratch folders; and the folder the `AGENT_TEMP` environment variable names, when it is set.
  - **data roots:** `.kit.json` `guard.data_roots` (docs/kit-json.md), read from the main checkout only, as every `.kit.json` key is. Each entry is `{path, mode, why}`: `open` (any write), `add-only` (new files only: a write, move or delete of a file that exists is denied; a copy, move or link into the existing folder is judged as `<folder>/<each source's name>`, `rules.copy_into`, when its options are only these: cp `-r -R -a -p -v -n -t`, `--recursive`, `--archive`, `--verbose`, `--no-clobber`, `--target-directory`; mv `-v -n -t` and the same long ones; ln `-s -v -t`, `--symbolic`, `--verbose`, `--target-directory`; install `-v -m -t`, `--verbose`, `--mode`, `--target-directory`. Any other option, or two target directories, is judged as the folder itself, so it is denied. cmd's copy is judged by name only for a source that is an existing file) or `read-only` (no write). A mode holds wherever the folder nests: a read-only folder inside the checkout or temp stays read-only. The kit names no folder.
- **Paths** compare after `os.path.realpath` and `normcase`, so `..`, symlinks and junctions are resolved: a junction in the worktree that points out of it is outside.
- **File tools:** Edit, Write, MultiEdit and NotebookEdit, by their target path.
- **Shell writes:** every literal target "Shell writes" (above) lists. Under bash, `/dev/...` is no file and passes, and `/tmp/...` stands for the folder `AGENT_TEMP` names, else the system temp folder (Git Bash's mount table is not read).
- **git:** a subcommand that writes is judged by the checkout it runs in (`-C`, a `cd` before it, else `<cwd>`), so `git -C <a sibling worktree> commit` is denied. Reads pass anywhere: `status`, `log`, `diff`, `show` and the other names in `rules.GIT_READS`; `stash list`/`show`, `worktree list`, `remote` (`show`, `get-url`), `reflog`, `notes` and `submodule` without a writing verb; `branch` and `tag` listing; `config` reads. `worktree add <path>` and `clone <url> <dir>` are also judged by the folder they make. One exception (`rules.lander_main_git`): the lander role (agent_type `lander`, named in the main checkout's `.kit.json` `roles` with `edit_main_checkout: true`, Go-live step 3) may run exactly these two commands in a main checkout from its worktree, with `<remote>` and `<main>` the main checkout's `.kit.json` `repo.remote` and `repo.main_branch`:
  - `git fetch <remote> <main>`
  - `git merge --ff-only <remote>/<main>`

  git may be spelt `git`, `git.exe` or PowerShell's `& git`, with `-C <main checkout>` or a `cd` before it. Anything else is denied: another option (`--prune`, `--no-ff`, `-s`, `-X`, `--squash`, ...), another ref or refspec (`main:main`, `+main`, `FETCH_HEAD`), a git option before the verb (`-c`, `--config-env`, `--git-dir`, `--work-tree`, `--no-pager`), an environment assignment or wrapper before git (`GIT_DIR=`, `env`), a word that is not literal, any other git write there, a file write there, and every other role. git whose checkout the text cannot place (a variable from "Git spellings" set, `--git-dir`, `--work-tree`, `core.worktree`, `--config-env`, a computed `-C`, whatever the order of `-C`) and that writes is denied for every session the jail holds, the lander included (`rules.git_jail_unknown`): it may be a main checkout, where `symbolic-ref` or `branch -m` would re-point main's HEAD. The refusal names the cause: for a variable or option it says to drop it and run git in the worktree (`rules.git_unplaced`), for a computed `-C` to name the checkout literally. Only a plain read passes (`rules.git_reads` strict): no word that is not literal, no `--output` file (abbreviated too), no `grep -O`/`--open-files-in-pager` (it runs a pager program), and `branch`, `tag` and `config` only with options it names in full, so an abbreviation git expands (`branch --unset-up`) is denied.
- **Not judged:** a target that is not literal, a command other than git whose directory is unknown, and git whose subcommand the text does not name in a checkout the text places. These meet the rules above them (the `main-checkout` rule fails closed in a main checkout) and the fallback, whose parsed rules include the shell writes and so the jail.
- **Order:** `ledger`, `lock-state`, `kit-json` and `main-checkout` are checked first, so a subagent's edit of a main checkout still reads `main-checkout`.

## Fallback

`kitlib/guard/fallback.py` `check` runs only when the parse is unclean. It catches mistakes, not attackers: an unclean line fails closed when it touches a main checkout, a heavy program or a protected path, and a plain line is never denied by it.

- **Patterns:** git_guard's (one repo's `git_guard.ps1`) plus the kit's git rules.
  - When the directory is a main checkout or a place git cannot tell: bulk add, `commit -a`, `checkout`/`switch`, `reset --hard`, `clean`, `stash`, `restore` (unless it only unstages); `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `pull`, `apply`.
  - Anywhere: `branch -D`, `worktree remove`, push to main (only when `push_block` is on), the wrapped gate, root searches.
- **Which directory:** the main-checkout patterns are judged in the directory of the top-level command that holds the match, following a literal `cd` before it. The call's directory is not used alone. The git pattern reads a quoted `-C "<path>"`.
- **A pattern match:** the text from the match to the next `;`, `|`, `&` or newline is read again as a command and the parsed rules run on it.
  - A rule that denies gives the deny, its real `Instead:` included, after "The guard could not read this command; write the program name literally. Why: <reasons>."
  - No rule denies: rule `unreadable`. The sentence ends "Write it plainly:", and `Instead: <the line written plainly>` follows.
- **A heavy program's name:** the same, unless the session holds the lock. A program named by a variable is denied as `heavy`, the `machine_lock.py run` form naming `<the program $X names> <args>`.
- **Anything else:** a subagent gets `unreadable`.
  - A marked session the write jail holds (`guard_agent_marks`) gets `write-jail` when the line holds the word `git` (`fallback.GIT_WORD_RE`; `.git` and `GIT_DIR` are not it), read or write, from any place: the part the guard cannot read may point git at a checkout it cannot place.
  - A session the jail does not hold (the owner's, `pm`, `deep-worker`) gets `unreadable` only when a command runs in a main checkout, or a place git cannot tell, and that command runs one of:
    - a program the text does not name;
    - a shell (`bash`, `sh`, `powershell`, `pwsh`, `cmd` and the like), `eval`, `iex`/`Invoke-Expression` or `source`/`.`; this covers `-enc`/`-EncodedCommand` and a pipe into a shell;
    - git with a subcommand or read option the text does not name.
  - For those sessions the same line from their own worktree is allowed.

Tests: tests/guard/test_rules.py ROWS, tests/guard/shell_rows.py, tests/guard/review_rows.py and tests/guard/extra_rows.py (428 rows at the time of writing); tests/guard/test_session.py for the rules that read the session mark.

### Known limits

The guard reads the command line, not the shell's state:

- **Aliases and functions** are not seen; the name is read as a program. This holds whether they are defined in an earlier call, in a profile, or earlier on the same line (`alias g=git; g push`). It also holds for git aliases in `~/.gitconfig` (only `-c alias.x=` on the command line is caught).
- **`& (Get-Command x)`** and other PowerShell calls of a computed command object are not resolved to the program they run.
- **`coproc`** is not modelled; the command after it is read as a program named `coproc` (`coproc git stash` parses clean, as one command `coproc`).
- **Pre-push config load:** when the clone has no base commit at all, neither the replaced remote main commit nor any default-branch ref, `kitlib/guard/prepush.py` falls back to the defaults: no `push_block`, main named `main`. The gate-OK check is then skipped; the fast-forward and delete refusals still apply to `main` (read from the code; no test covers it).
- **`Invoke-Expression`/`iex`, `eval` of a computed string:** the string is not evaluated; a literal argument is parsed, a computed one leaves the parse unclean.
- **`case` statements** are read as unclean (an unmatched `)`), so a subagent's `case` line is denied as `unreadable` and a jailed mark's as `write-jail` when it holds `git`; a session the jail does not hold is not denied.
- **Heavy fallback on Python 3.14:** `fnmatch.translate` ends with `\z` there, not `\Z`; `heavy_patterns` strips both, else the heavy fallback never matched.
- **Rules the fallback does not run:** `busy-wait`, `heredoc-winpath`, `git-hooks-path`, `owner-identity` and `kill-by-name` are not in `fallback.PARSED_RULES`; an unclean line meets only the fallback's own patterns.
- **busy-wait:** a loop in a script file, or in the Monitor tool, is not read. A bound the rule does not know (an `-eq` count, a check inside a called script) reads as none, so that loop is denied. Any `-lt`/`-le`/`-gt`/`-ge` reads as a bound, so `while [ $(pgrep -c x) -gt 0 ]; do sleep 1; done` passes though nothing ends it. A loop behind `time` or `nohup` (`time while true; do sleep 1; done`) is read as that program's arguments and passes.
- **heredoc-winpath:** only python reading a bash heredoc as its script. A heredoc written to a `.py` file by `cat`, a PowerShell here-string, and other interpreters are not read. A drive letter followed by a single path character (`C:\x`) is not read as a path. A one-letter label, an escape, then text (`"Q:\nWhat"`) is still read as a path and denied.
- **git-hooks-path:** `git clone --template` and `git init --template` with hooks in the template are not seen. Abbreviated long options (`git clone --conf=core.hooksPath=x`) are not seen.
- **owner-identity:** an identity in the environment the session started with, in `GIT_CONFIG_PARAMETERS` or a `GIT_CONFIG_KEY_<n>=user.email` / `GIT_CONFIG_VALUE_<n>` pair, or set inside a script or program the line runs, is not seen. `git commit --author=<name only>` is not caught: git fills in the email from an earlier commit by that author, which can be the owner's; the rule matches the email, not the name.
- **kill-by-name:** `pkill` and `killall` that end processes by user, parent, group or terminal with no name (`pkill -u <user>`) pass.
- **Clearing the mark:** a marked session can start a child agent session with `PM_KIT_ROLE` cleared (`PM_KIT_ROLE= claude ...`, `env -u PM_KIT_ROLE claude ...`, `Remove-Item env:PM_KIT_ROLE`); the child is unmarked and counts as the owner. No rule denies it.
- **Environment changes beside a git write:** the git write of a session the write jail holds (a subagent, a `coordinator` or `conductor` mark), on a line with an environment change the guard does not read, is denied even when the change is harmless (`set -a; . ./.env; set +a; git commit ...`, `Set-Content $f x; git commit ...`); run the git in its own call. The same holds for any git on a line the guard cannot read at all (`eval "$c"; git status`). The owner's unmarked session and the `pm` and `deep-worker` marks are not jailed (`rules.git_jail` returns before `git_jail_unknown`), so their line passes.
- **Startup files:** a nested shell runs files the line does not show before its text: `bash --rcfile <f>` or `--init-file <f>`, and a login or interactive shell under a `HOME=<dir>` the line sets (`HOME=/x bash -l -c '...'`) reads that folder's profile. They are not read; git in the nested text is placed as written.
- **PATH and the exec path:** a `PATH` the line sets, or `git --exec-path=<dir>`, may make `git` or a git subcommand another program. The guard reads the word `git` as git and does not judge `PATH`.
- **Environment the line does not show:** a program the line runs that runs git itself (`python tools/commit.py`) gets the line's environment, but is not judged as git. A PowerShell `function git`, `git -c include.path=`, a .NET `ProcessStartInfo` and a variable persisted by an earlier call (`setx`, User scope) are not seen.
- **`New-Item -Path env:` in a main checkout** is refused by `main-checkout` as a write to a folder named `env:` there.
- **Heavy `when_args`:** an argument counts at any position, so `dotnet new console -n build` is heavy.
- **write-jail reads the line, not the program:** a script or program the agent runs (a `.py` it wrote, `python -c`, a build tool) can write anywhere; the jail judges only the targets the command line names. What such a write puts in a repository reaches main only through a commit and a push, where `tools/commit.py` (docs/commit.md) and the pre-push owner-only paths ("Owner-only commits") still check it; a write outside every repository is not checked at all. A target that is not literal, or git whose subcommand the text does not name, is not judged either.
- **BSD `xargs -S <size>`** is not read as taking a value: the size is read as the program, so the command after it (`sh -c '...'`) is not read.
- **A trap handler or `find -execdir` at an unknown place:** a relative write in it is judged as at an unknown place, so a subagent is denied `cd sub && trap 'rm -f x' EXIT` and `find . -execdir rm {} +`.
- **write-jail temp is shared:** every session's temp root is the same folder, so one agent session may write another's scratch folder.
- **write-jail copy into an add-only folder:** only cp, mv, ln, install, `Copy-Item`, `Move-Item` and cmd copy and move are judged by the source's name. `robocopy`, `xcopy` and `rsync` into the existing folder, a source that is not literal, holds a wildcard or is drive-qualified (`D:name`), `-T`, and any option off the list in "Write jail" are judged as the folder itself, so they are denied.
- **write-jail and options that are not literal:** cp, mv, ln and install read a word that is not literal (`"$F"`) as an operand, though the shell may expand it to an option such as `-t<dir>`.
- **write-jail and a lander in the main checkout:** a lander whose `cwd` is the main checkout gets the whole checkout as its root, so the jail adds nothing there; the `main-checkout` rule and `roles.lander` decide what it may write.
- **write-jail and a session's working directory:** the roots follow the payload's `cwd`. An agent started in another repository's checkout (a subagent of a session that runs there) gets that checkout, not the worktree it was told to work in, and is denied there.

## Lock MCP

- **Which tools:** a tool named `<prefix><owner-only tool>` is caught, so is any `mcp__*__<owner-only tool>`, and so is any full name listed in the user config `guard_mcp_tools`.
- **Prefix:** the default `mcp__plugin_claude-pm-kit_machine-lock__` is INFERRED from Claude Code's plugin MCP naming; no real PreToolUse payload has been captured, as the server is not installed. The user config `guard_mcp_prefix` replaces it and `guard_mcp_tools` adds names, so an observed name goes in config, not code (tests/guard/test_session.py `LockMcpByMark`).
- **Who is asking:** `Ctx.session_role(event)` decides (see "Session mark"); it is replaceable through `role_resolver`.
  - `subagent`, or any mark (pm, coordinator): deny.
  - Unmarked, the owner's own session: allowed. The lock's own owner check still applies.

## Session mark

`kitlib/sessionmark.py`. The launcher that starts an agent session sets `PM_KIT_ROLE` to its kind (`tools/launch.py`, docs/launcher.md "The window": `pm`, `coordinator`, `deep-worker`, `consultant`, `plain`, or another agent's name); a subagent is told apart by the hook payload's `agent_id`. `kitlib/guard/context.py` `default_session_role` gives `subagent`, else the mark, else `owner`.

- **Unmarked is the owner:** a main session nothing marks is the owner's own, exempt from `owner-lock`, `lock-mcp`, `owner-identity`, `kill-by-name`, `lock-state`, `kit-json` and `write-jail`. Until the kit launcher is live, sessions the old launcher starts are unmarked and count as the owner ("Go-live", step 4).
- **Reserved values:** a mark of `owner` or `subagent` reads as `marked-<value>`, never as that standing.
- **Agent sessions:** `kill-by-name`, `lock-state`, `kit-json` and `write-jail` hold subagents and the marks in the user config `guard_agent_marks` (default `coordinator` and `conductor`, the coordinator's old mark; compared without case); other marks are not held to them. `owner-lock`, `lock-mcp` and `owner-identity` hold every marked session.
- **Where it is read:** only where the session cannot change it once started: the guard hook and the lock MCP server, both started with the session's environment (INFERRED; not yet observed live). The lock CLI does not read it, since an agent sets the environment of every command it runs.
- **Lock MCP server:** `mcp/lock_server.py` `main` installs `sessionmark.owner_check` through `kitlib.lock.api.set_owner_check`. In an unmarked session a caller under the session's agent host counts as the owner (the default check sees the agent host as the caller and refuses every owner write); in a marked one only the default check passes.
- **Limits:** a session the launcher did not start is unmarked and counts as the owner. A subagent of the owner's session passes the lock server's owner check; the guard still denies it by `agent_id`. The lock's holds and tree roles do not read the mark.
- **Tests:** tests/guard/test_session.py.

## Owner lock tools

Owner-only lock tools are run by the owner, not agents: `reserve_ui.pyw` and the lock CLI's owner writes. The lock's own owner check (`kitlib/lock/api.py` `who`, `default_owner_check`: no Claude ancestor) can be forged by a detached process, so the guard stops agents from starting them at all. That check stays as a second layer.

- **reserve_ui:** denied when a command runs it (any case, any path, with or without `.pyw`), in `kitlib/guard/rules.py` `runs_reserve_ui`:
  - as the program: a direct path, `&`, `Start-Process`, `cmd /c start`;
  - as the script of `python`, `pythonw`, `py` or `pyw`, also through `start` (`/b` and other options skipped), `Start-Process`, `nohup` or a nested `bash -c`;
  - as python's standard input, when the line names `reserve_ui` (`python < reserve_ui.pyw`, `cat reserve_ui.pyw | python`, `python -`);
  - opened by `Invoke-Item`/`ii` or `explorer`;
  - `python -c` with `reserve_ui` or the window's package (`kitlib.lockui`, `kitlib/lockui`, `from kitlib import lockui`) in its arguments, and `python -m reserve_ui` or `-m runpy`, also in a cluster of short flags (`-um runpy`, `-mrunpy`, `-Sc`) or with the module named by a variable. Other `-m` modules stay open (`python -m py_compile reserve_ui.pyw`, `python -m pytest -k reserve_ui`);
  - a program, script or opened file named by a variable, when the command's text names `reserve_ui` (`$p = "reserve_ui.pyw"; pythonw $p`).

  Reading it and git on it stay open: `cat`, `type`, `Get-Content`, `rg`, `grep`, `git add`, `diff`, `show`, `log`, `commit`, so work on the UI's own code is not blocked.
- **Lock CLI owner writes:** any word whose base name is `machine_lock.py`, run by `python`, `pythonw`, `py`, `start`, `Start-Process`, a direct path, or inside a `run` command, followed by:
  - the installed lock CLI: `reserve`, `release`, `stop --force`, `unbook --force`;
  - the kit CLI (`tools/machine_lock.py`): `hold whole|half|slots`, `division --set|--default`, `game on|off|set`, `top` (move a waiting run to the front), `stop --force`.
- **Still open:** `status`, `run`, `background`, `book`, `unbook` and `stop` without `--force`, `hold agent|list|end|edit`, bare `division` and `game`/`game show`. Ending one's own hold or run is an agent's right (`kitlib.lock.api.authorize`); the text cannot tell whose hold an id names, so the lock decides.
- **Who:** every session but the owner's own (unmarked; see "Session mark"), with no ask. A process the owner's session starts still has an agent host above it, so the lock CLI's own check refuses it there.
- **Limits:** not caught:
  - a lock CLI script named by a variable (`python $tool reserve`);
  - a glob (`pythonw reserve_u*.pyw`);
  - a variable set in an earlier call, or in an outer text that a nested shell's text does not include;
  - `reserve_ui` started by a program the guard does not know as a launcher (for example a `.py` script that runs it);
  - a lock CLI owner write passed as a `Start-Process -ArgumentList @(...)` array (`@("machine_lock.py", "reserve", "whole")`); `reserve_ui` in such an array is caught;
  - `[Diagnostics.Process]::Start(...)` and other .NET process calls;
  - copy-then-run: the UI copied or renamed to another name, then that file run;
  - a heredoc or here-string feeding python (`<<`, `<<<`): only a pipe or a `<` redirect counts as python's standard input.
- **Over-reach:** the standard-input deny fires whenever the line names `reserve_ui`, even when what feeds python is something else (`cat other.py | python; cat reserve_ui.pyw`).
- **Unreadable lines:** `kitlib/guard/fallback.py` runs this rule first when it re-reads a text-pattern match (`PARSED_RULES`). It has no text pattern of its own for these tools, so an unreadable line reaches this rule only when another rule's text pattern (wrapped gate, git, root search, heavy) matches.
- **Tests:** tests/guard/test_rules.py `OwnerLockTools`, including `test_unread_line_rereads_with_owner_lock` for the fallback.

## Pre-push

`githooks/pre-push` calls `kitlib.guard.prepush.main`. Each check is on a ref git pushes whose remote ref is `refs/heads/<main>`, whoever pushes.

- **Fast-forward only, in every repo:** a push that is not a fast-forward of the remote's main is refused, and so is deleting main. A remote main sha this clone lacks cannot be an ancestor of the pushed commit, so that push is refused too ("cannot tell"; fetch first). Git hands the hook such a ref only on a forced push (`--force`, `+`, `--force-with-lease`). A branch other than main may still be rewritten.
- **Gate-OK record:** the pushed sha must have one, read as in "State". Only when `repo.push_block` is set; checked after the fast-forward rule.
- **Owner-only paths:** after the gate-OK check, and also only under `repo.push_block`, a push to main is refused when a commit it adds fails "Owner-only commits" below, record or not. A branch gate records its own tip, so without this a gated agent branch could go straight to main. "Adds" means not in the remote main it replaces when this clone has it, else not in `<remote>/<main>` as last fetched, else the whole history. Other remote-tracking refs are never subtracted: a pushed agent branch would hide its own commits.
- **Order:** delete or fast-forward, then gate-OK, then owner-only paths; the first that refuses is the one reported (tests/guard/test_prepush.py `test_the_rules_refuse_in_order`).
- **Base config:** `push_block` and the main branch's name are read from the remote main commit being replaced when this clone has it, else from the default branch's committed `.kit.json` (the first of `<remote>/HEAD`, `<remote>/main`, `<remote>/master`, local `main`, local `master`). The working tree is never the base, and a pushed commit cannot turn the block off.
- **Broken config:** a `.kit.json` that does not load refuses the push.
- **Tests:** tests/guard/test_prepush.py pushes to a bare scratch remote with `core.hooksPath` set to the kit's githooks.

## Owner-only commits

`kitlib/guard/ownercommits.py` `violations()` judges the net change, then the commits. For each owner-only path (`kitlib/config/owner.py` `is_locked`) that the net change touches, it names every commit that changes the path and whose author email (`%ae`, never mailmapped, replace refs off) is not `"owner".email` (`is_owner`). `gate land` (docs/gate.md "Landing", step 4) and pre-push run it, so a plain `git commit` to an owner-only path does not reach main past either; `commit.py --owner-authored` was the only check before.

- **Net change:** a path counts only when landing the tip on the target main would change it: the target differs for it from the clean merge of the target and the tip (`git merge-tree --write-tree`; the tip itself for a fast-forward), or that merge conflicts on it (`net_change`). The target is `--expect-main` for `gate land`, the remote main being replaced for pre-push. So an agent change a later commit undoes, a rename away and back, and an owner change the branch brought in by merging main do not count. With no target (pre-push with no remote main known), every changed path counts.
- **Rules:** the base's merged with the pushed or landed commit's (`owner.merge`): paths are the union, exceptions the intersection, the owner email the base's only. An unset owner email, or one also in `agent_emails`, refuses every owner-only change.
- **What a commit changes:** a commit with one parent or none, its diff. A two-parent merge, what differs from the clean merge of its parents (`git merge-tree --write-tree`) plus every path that merge conflicts on: a merge that only joins histories changes nothing, and an edit or conflict resolution inside a merge is judged by the merge's author. More parents: the union of the diffs against each. Renames count as a delete and an add.
- **Trailers** play no part here: the author email decides. `tools/commit.py` writes the `Owner-Approved` trailer and refuses an owner-only path without `--owner-authored` (docs/commit.md).
- **Git failure:** refuses (the landing or the push).
- **Tests:** tests/guard/test_ownercommits.py, tests/gate/test_land_owner.py, tests/guard/test_prepush.py `OwnerTest`.

Known limits:

- **The author email is what the committer says** (`git commit --author`, `GIT_AUTHOR_EMAIL`). The guard's `owner-identity` rule denies a session other than the owner's setting the owner's email on the command line; an identity in the environment the session started with is not seen. Signed commits would close it; not built.
- **Only main is checked at push**, and only under `repo.push_block`, like the gate-OK check. A push to another branch, or a merge made on the server (a pull request merged in the web UI), is not seen.
- **One net change per push:** a push to main that carries the owner's unpushed main commits together with a branch is judged as a whole. When an owner commit in it changes a path, an agent commit that touched that path and was undone counts too, and the push is refused (fail closed). Pushing the owner's commits first avoids it.

## Self-test

`claude.guard_selftest.agent_id_check(projects_dir, since)` scans subagent transcripts (`<project>/<session>/subagents/agent-*.jsonl`) for guard tags.

- **What counts:** only tags in tool results marked `is_error` (INFERRED to be where a hook deny lands).
- **Results:**
  - `role=main` in a subagent transcript is an alarm: `agent_id` has disappeared;
  - only subagent roles pass;
  - no tags is inconclusive.
- **Not live:** it is not wired to a session-start hook.

## Refusal patterns pre-empted

The guard denies these with the command to use before the auto-mode classifier sees them (the PM repository's docs/refusals.md):

- **P16:** a gate wrapped as `cd <wt> && python tools/gate.py 2>&1 | tail`. Rule: `wrapped-gate`.
- **A repo's wrapped lander command** (same family as P16). Rule: `wrapped-gate`.
- **git_guard's main-checkout patterns:** the git rules and the fallback cover them.
- **Root searches:** rule `root-search`.

## Go-live (after the joint review)

1. Settings: the one PreToolUse entry that `python <kit>/claude/hooks/guard.py --settings-entry` prints, in exec form:
   - `command` is the `pythonw.exe` beside the python that ran it and `args` is `[<kit>/claude/hooks/guard.py]`. There is no shell and no console: one process per call, against four for a shell-form `python` command (bash, its conhost, a second bash, python; measured 2026-10-10 for KIT-HOOK-SPAWNS).
   - Do not write a bare `pythonw`: on this machine PATH finds the WindowsApps alias, one process more.
   - The matcher is `Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|mcp__.*`: the tools a rule can refuse. Read, Grep, Glob and the rest start no process.
2. Every repo: `git config core.hooksPath <kit>/githooks` (the fast-forward rule is for every repo; the gate-OK check needs `push_block`). This also installs `pre-commit`, inert unless the repo's `.kit.json` sets `ledger.pre_commit_check` (docs/ledger.md "Pre-commit check").
3. Every repo whose landers edit a main checkout: `.kit.json` `roles.lander.edit_main_checkout: true`.
4. The launcher sets `PM_KIT_ROLE` for every session it starts (`tools/launch.py` does: `pm`, `coordinator`, `deep-worker`, `consultant`, `plain`), with or before step 1: until then every main session counts as the owner's own ("Session mark").
5. `~/.claude-pm-kit/config.json` holds `ledger_path`.
6. Write jail ("Write jail"): every repo whose agents write a folder outside git lists it in `.kit.json` `guard.data_roots`, an owner change. Agents that must work in a worktree start there (or enter it) rather than in another checkout, since their roots follow the session's working directory.

Checks after step 1, first one first:

1. A known-denied command is denied: a Bash root search, the call tests/guard/test_entry.py `GoLive` sends, comes back denied with `rule=root-search`.
