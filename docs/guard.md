# Shell guard and git hooks

`boot/hook.py <host> PreToolUse` (`boot/hook.py claude PreToolUse` on Claude) is the kit's one PreToolUse hook (ARCHITECTURE.md "Layers": Guards). `githooks/pre-push` is the kit's guard git hook. Both are thin entries over `kitlib/guard/` ("Hosts" below). `githooks/` also holds `pre-commit`, the ledger check, inert unless a repository opts in (docs/ledger.md "Pre-commit check").

**Live and enforcing:** the guard is registered as the PreToolUse hook in the user settings ("Go-live", step 1). `githooks/pre-push` is live only in a repository whose `core.hooksPath` names `githooks/` ("Go-live", step 2).

## Output

- **Allow:** the hook prints nothing and exits 0.
- **Deny or ask:** it prints `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"|"ask", "permissionDecisionReason": ...}}` and exits 0.
- **Reason format:** `<what and why> Instead: <the exact command to use> [kit guard: rule=<rule> role=<main|subagent>]`. After `Instead:` comes one command, ready to run, with no prose after it; any explanation goes in the sentence before it, which ends with `:` (tests/guard/test_rules.py `test_rows` checks every row).
- **Unguarded tools:** a tool no rule can refuse (any tool outside `hosts.json` `claude.hook.guarded_tools`) is allowed by `boot/hook.py` before kitlib is imported. Anything it cannot read goes to the full guard, never to an allow (tests/guard/test_entry.py `Unguarded`).
- **Guard error:** if the guard fails on a guarded tool, it denies with rule `guard-error` (fail closed; `kitlib.host.hookmain.run`), including a guard that will not import. If the adapter or hookmain themselves will not import, the hook exits 2, which Claude Code also reads as a block (tests/guard/test_entry.py).

## Hosts

The guard judges a neutral `kitlib.host.Action` and answers a `kitlib.host.Decision` (ARCHITECTURE.md "Host contract"); no module under `kitlib/guard/` reads a host's payload or writes its output.

- **The hook:** `boot/hook.py claude PreToolUse` hands Claude's adapter to `kitlib.host.hookmain`, which parses the event (`hosts/claude/adapter/events.py`), judges it with `kitlib.guard.decide(action)`, lowers an ask when the host cannot show one (`kitlib.host.degrade`) and renders the answer. The fail-closed deny and the settings entry are hookmain's, the same for every host.
- **What Claude's adapter reads:** `tool_name` sorts the call into `edit`, `shell`, `read` (Read, Grep, Glob), `mcp` or `other`; `agent_id` is `Action.agent`, and a subagent's `agent_type` is `Action.agent_role` with the kit plugin prefix dropped ("Agent names"). `PM_KIT_ROLE` is read from the hook's environment.
- **Paths:** every path the guard compares goes through `kitlib/guard/paths.py` `norm`.
- **Tests:** the guard tests build Claude payloads and decide them through the adapter (`tests/guard/claudehost.py`); tests/guard/test_entry.py runs the hook end to end.

## Parsing

`kitlib/guard/shell.py` tokenises Bash, PowerShell and `cmd /c` text; `commands.py` reads the result.

- **Seen as text, not commands:** quotes, here-docs and PowerShell here-strings.
- **Seen as commands:** `;`, `&&`, `||` and `|` split commands. Substitutions, backticks, `powershell -Command`, `bash -c`, `cmd /c`, `env -S`, `trap` handlers, find `-exec` and the shell a `Start-Process` starts are parsed again as nested commands. `cd`, `Set-Location`, `pushd` and `env -C` move the working directory for the commands after them; a `Start-Process -WorkingDirectory` places the program it starts.
- **Wrappers** (`env`, `command`, `exec`, `nohup`, `nice`, `time`, `timeout`, `stdbuf`, `sudo`, `xargs`) are skipped with their options, and the program after them is read.
- **Git spellings:** `-C`, `-c`, `--git-dir`/`--work-tree`, `--config-env` and git's place and config variables are read. A git place variable (`commands.GIT_ENV_NAMES`) set anywhere on the line, or an environment change the guard cannot read, leaves every git on the line unplaceable (fail closed). Put `--` before a variable path, so it is not read as an option.
- **Unclean parse:** text the tokeniser does not model makes the parse unclean: an unclosed quote, substitution or heredoc, an encoded command, a shell or `iex` reading from a pipe, a computed `bash -c`/`-Command`/`eval` text, a program, git subcommand or read option named by a variable, a `case` statement, a `cmd` `if` or `for` loop, more than `commands.MAX_NESTS` nested texts. The rules still run on what was read, then "Fallback" runs.

Tests: tests/guard/test_shell.py.

## State

Every rule is a read-only check of real state. `kitlib/guard/context.py` `Ctx` gathers what the rules read, so tests can hand them a fake machine.

- **Place:** MAIN, LINKED, NONE or UNKNOWN, as `kitlib.git.repo.classify` answers for the working directory or the target path. `kitlib/guard/place.py` reads it from the git files without starting git, and hands any shape it cannot read the way git would to `repo.classify` (tests/guard/test_fsplace.py, tests/guard/test_place_decisions.py).
  - The PM data worktree (docs/pm-data-branch.md "Guard place and commit hooks") counts as MAIN, though git calls it LINKED. While the guard cannot tell it (a machine config whose `pm_data` does not read, a record or HEAD that does not), subagent writes fail closed (rule `pm-data`). Tests: tests/guard/test_rules.py `PmDataPlace`.
- **Config:** `.kit.json` of the main checkout a path belongs to. A worktree's own `.kit.json` is never read, so a branch cannot loosen its own rules; the PM data worktree, which counts as MAIN, is the one exception.
- **Lock:** held only when `MACHINE_LOCK_RUN` names a run that `kitlib.lock.run.nest_check` accepts. The variable alone is never proof.
- **Gate-OK record:** `kitlib.gate.runner.verify_record(root, sha)`, as the pre-push hook reads it (docs/gate.md "Gate-OK record").
- **User config:** the machine config (docs/kit-json.md "Machine config keys"), read tolerantly; a file that cannot be read at all denies every subagent write (rule `ledger`, fail closed).

## Rules

`kitlib/guard/rules.py` `decide(action, ctx)`. Every row below is a row in tests/guard/test_rules.py with the exact `Instead:` text. `<main>` is the main checkout, quoted, and `<wt>` is the session's own worktree.

| rule | denied | instead |
|---|---|---|
| `wrapped-gate` | `gate.py` or `machine_lock.py run` piped into another command, as in `cd <wt> && python tools/gate.py 2>&1 \| tail` (refusal pattern P16), the tool known by its file name (tests/guard/test_kit_tool_forms.py) | the bare command, nothing piped or redirected (`rules.bare_wrapper`): the tool by its absolute path under `<kit>` ("Kit paths in advice"), with `-C "<the cd target or the call's directory>"` added to a gate that gave none and the `cd` chain dropped, so `cd <wt> && python tools/gate.py --full \| tail` gets `python "<kit>/tools/gate.py" -C "<wt>" --full`. The sentence before it says to run it as a call of its own in the background and read the log gate.py names on its completion notice |
| `root-search` | `find /`, `find C:\`, `Get-ChildItem C:\ -Recurse` (or `-Depth`), `dir /s C:\`, `ls -R /`, `tree`, `findstr /s`, `du`, `grep -r` or `rg` on a drive root, `where /r C:\`. The user's home counts as a root, and so do `HOME`, `USERPROFILE`, `HOMEDRIVE`, `SystemDrive` and `HOMEPATH` written as `~`, `$X`, `${X}`, `$env:X` or `%X%`, alone or two together | the same command on the session's working directory, a wildcard leaf kept, else `Glob(pattern="**/*", path="<a project folder>")` |
| `heavy` | a heavy program (`kitlib.config.heavy_programs`: godot, blender in the background, dotnet `build`/`test`/`publish`/`run`/`pack`/`msbuild`/`vstest`, gradle, plus `.kit.json` `heavy`.programs) without a held lock, unless it is an argument of `gate.py` or `machine_lock.py run`. An interpreter a repo lists as heavy may run only the kit's own (or a kit export's) `tools/gate.py` or `tools/machine_lock.py run` (`rules.kit_wrapper`) | `python "<kit>/tools/machine_lock.py" run --kind <kind> --repo <repo> --slug <slug> -- <command>`; `<kit>` is the kit folder advice names ("Kit paths in advice"; tests/guard/test_lock_advice.py) |
| `main-checkout` | Edit, Write, MultiEdit or NotebookEdit into a main checkout or an UNKNOWN place, by a role whose `edit_main_checkout` is false (default: `subagent`); a write the guard cannot place passes only when both the kit's default roles and the repository's `roles` let the role edit one. Shell writes count the same way ("Shell writes" below) | the same tool call or command aimed at the same path in `<wt>`, else `git -C <main> worktree add .claude/worktrees/<slug> -b <branch>`. A `python -c` that only sleeps is named a sleep-wait, with advice to run the work in the background or use the Monitor tool (`rules.sleep_refusal`); any other python counted as a write says so (`rules.py_unread`) |
| `ledger` | any edit or shell write to `ledger_path`, by any role | `python "<kit>/tools/ledger.py" add --repo <repo> --prov <prov> --q "<question>" --a "<answer>" --decides "<what it decides>" --source "<source>"`, the kit's `tools/ledger.py` (docs/ledger.md). For a broken user config: `python -m json.tool "<config-dir>/config.json"` |
| `pm-data` | a subagent's edit, shell write or git write while the guard cannot tell the PM data worktree: the machine config's `pm_data` does not read (every such write), or a linked worktree's `kit-pm-data.json` or HEAD does not (writes aimed at or run from a linked worktree of that repository); see "State" | `python -m json.tool "<config-dir>/config.json"` |
| `git-checkout` | `checkout`, `switch` in a main checkout | `git -C <main> worktree add .claude/worktrees/<slug> <branch>` (the sentence before it names `git show <rev>:<path>`) |
| `git-reset-hard` | `reset --hard` in a main checkout | `git -C <main> reset --keep <rev>` |
| `git-clean` | `clean` without `-n` in a main checkout | `git -C <main> clean -n` |
| `git-stash` | `stash` (not `list`, `show`) in a main checkout | `git -C <main> worktree add .claude/worktrees/<slug> -b <branch>` |
| `git-bulk-add`, `git-commit-all` | `add -A`/`.`/`-u`, `commit -a` in a main checkout | `git -C <main> add -- <each path you changed>` |
| `git-restore` | `restore` in a main checkout, by any role, unless it only unstages (`--staged`/`-S` without `--worktree`/`-W`) | `git -C <main> restore --staged -- <path>` |
| `git-main-history` | `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `pull`, `apply` (not `--check`/`--stat`/`--numstat`/`--summary`), `rm`, `mv`, `checkout-index`, `update-ref`, `reset` other than `--hard`, `read-tree -u`, `branch -f`/`--force`, in a main checkout, by a role whose `edit_main_checkout` is false (default: `subagent`; a lander gets it from `.kit.json` `roles`) | `git -C <wt> <the same subcommand and arguments>` when the session has a worktree of that checkout, else `git -C <main> worktree add .claude/worktrees/<slug> -b <branch>`. An `update-ref` that `git-shared-ref` refuses is refused by that rule first, from any checkout; one of a ref it allows (`refs/tmp/<name>`) gets the worktree form here. A session `git-shared-ref` does not hold gets, for `update-ref -d refs/heads/<b>`, `git -C <wt or main> branch -d <b>`, and for another ref `git -C <wt or main> show-ref <ref>` for the PM to delete (`rules.ref_delete_hint`) |
| `git-shared-ref` | `update-ref` run from any checkout, a linked worktree included, by an agent session other than the PM's (`rules.shared_ref_held`): every worktree shares its refs, so `git -C <wt> update-ref -d refs/heads/main` deletes main. It may write `refs/tmp/<name>` and the per-worktree refs (`rules.scratch_ref`), and in a linked worktree its `HEAD` and the branch checked out there unless that is a guarded main. A symbolic ref is judged by every ref it points to; `--stdin`, an unknown option or a word that is not literal is refused as unread. A scratch repository whose git directories lie in a temp root is not judged, unless it holds a link | a delete: `git -C <wt or main> branch -d <b>` for a branch, `git -C <wt or main> show-ref <ref>` for a guarded main or another ref; an update: `git -C <wt or main> show-ref <ref>`; unread: `git -C <wt or main> update-ref refs/tmp/<name> <new-oid>` |
| `git-branch-force-delete` | `branch -D` or `branch -d --force`, anywhere | `git branch -d <names>` |
| `git-worktree-remove` | `worktree remove`, run from anywhere (a linked worktree too), of a worktree that is not clean, whose HEAD is not merged into main, or that the text cannot name | not named: `git -C <main> worktree remove "<worktree path>"`; not clean: `git -C <target> status`; unmerged: `git -C <target> log --oneline <main branch>..HEAD`. Unmerged work goes to the PM; `worktree_remove.py --abandon` only on the PM's word |
| `git-push-main` | in every repository, with or without a `.kit.json` (docs/kit-json.md "With no file"): a push that deletes main or forces it (`--force`, `--force-with-lease`, `--force-if-includes`, `--mirror`, a `+` refspec), a non-literal refspec, a computed remote with a force, delete or `--no-verify` flag, and any push where the `.kit.json` does not load; in a `push_block` repo also a push to main whose commit has no gate-OK record, one with `--no-verify`, and any non-literal word. Main is `repo.main_branch`, unset: read from the git files, with `main` guarded as well (`Config.guarded_mains`) | `python "<kit>/tools/land.py" -C <main checkout> <branch> --expect-tip <sha>` (`rules.landing_command`); with no `push_block`, a forced push gets `git -C <dir> push <remote> <refspecs without +>`; a delete gets `git -C <dir> ls-remote <remote> <main>`; a bad config gets `python -m json.tool <top>/.kit.json` |
| `git-push-pmdata` | in a repository whose git common dir holds `kit-pm-data.json` (docs/pm-data-branch.md), or from a place git cannot tell, in every session but the owner's own: a push that skips the PM data leak check ("Pre-push"): `git push --no-verify` in any abbreviation, `send-pack`, `http-push` and remote helpers run by hand; `git push` or git with a word computed at run time, or run by `xargs`; a git alias (read with `git config --get` and expanded, chains too) that runs any of these; a line the parser cannot read that runs a program the text does not name, and PowerShell's `& (<subexpression>)`; `git push` on a line that sets `HOME` or another variable git finds its user config through. A push from a copy of the data branch with no record of its own (a clone holding the recorded root, or a shallow one) is denied when the machine config names `pm_data`; a machine record the guard cannot read denies every such push (`rules.data_copy`; tests/guard/test_pmdata_gaps.py, tests/guard/test_pmdata_fixes.py, tests/guard/test_pmdata_review.py) | `git -C <dir> push <the same arguments without --no-verify>`; for send-pack and the helpers, `git -C <dir> push <remote> <branch>`; for an alias, `git -C <dir> <subcommand> <arguments>`; for `& (...)`, `& <program> <arguments>`; for a copy, `git -C <dir> remote -v`; for a machine record that does not read, `python "<kit>/tools/pm_data.py" init --adopt --path <pm_data.path>` |
| `git-remote-rewrite` | in a repository with `kit-pm-data.json` (or a place git cannot tell), in every session but the owner's own: `git remote set-url`, `add`, `rename`, `remove` or a computed verb; a `git config` write of a key or section under `remote.`, `url.`, `include.` or `includeIf.`, a `git config` with a computed word, and `git config --edit`; a `--global` or `--system` write counts wherever the machine config names `pm_data`. The leak stage tells the private remote by its configured URLs, so a remote pointed elsewhere would make a public one look private (`rules.git_remote_rewrite`) | `git -C <dir> remote -v`; the sentence before it says to ask the owner through the PM |
| `git-alias` | a git alias defined on the command line (`-c alias.x=...`, `--config-env`), anywhere | the command the alias runs, written out |
| `unreadable` | an unclean parse (see "Fallback"), and a subagent's PowerShell `& (<subexpression>)` outside a repository with `kit-pm-data.json` | the line written plainly; when the guard cannot rewrite it, or its rewrite does not read cleanly either, a run of a script file in the temp folder (`bash "<temp>/script.sh"`, PowerShell `powershell -NoProfile -ExecutionPolicy Bypass -File "<temp>/script.ps1"`; `fallback.plainly`), never the refused line itself; for `& (...)`, `& <program> <arguments>` |
| `lock-mcp` | the lock MCP's owner-only tools (`kitlib.lockmcp.server.OWNER_ONLY`) | `python "<kit>/tools/machine_lock.py" status` (`<kit>` as for `heavy`); see "Lock MCP" below |
| `owner-lock` | a Bash or PowerShell call that runs `reserve_ui`, or runs a lock CLI owner-only write, in any session but the owner's own; see "Owner lock tools" below | the lock CLI's `status`, as for `lock-mcp` |
| `owner-setup` | a Bash or PowerShell call that runs machine setup to act, in any session but the owner's own; see "Owner machine setup" below | `python "<kit>/tools/machine_setup.py"` (its dry run); the sentence before it says to ask the owner |
| `hub-mcp` | the hub MCP server's owner tools (`kitlib/guard/hub.py` `OWNER_MCP_TOOLS`: `hub_answer`, `hub_todo_done`, `hub_story_submit`, `hub_to_pm_send`, under any server name) and a hub tool whose name holds `decide` or `approve`, in any session but the owner's own; see "Hub secrets" below | `python "<kit>/tools/hub.py" questions add --title "<what the owner should decide>"` (`rules.hub_ask_alternative`) |
| `hub-browser` | a browser or computer-use MCP call (a server name holding chrome, browser, playwright, puppeteer or computer-use) whose input names the hub's own origin, a loopback host on a hub port, in any session but the owner's own | as for `hub-mcp` |
| `hub-owner-verb` | a Bash or PowerShell call whose text names the hub tool as a module or a script (`tools/hub.py`, `tools.hub`, an import of hub) anywhere outside the one shape the guard reads in full (python, plain options, `tools/hub.py` or `-m tools.hub`, an agent verb, every word literal), or runs an owner verb (`answer`, `open`, `pair`, `unpair`, `stop`, `restart`, `todos done`, `stories add`/`withdraw`, `to-pm add`/`send`, `inbox read`, `approvals approve`/`deny`, `todos add --owner`), in any session but the owner's own (fail-closed) | as for `hub-mcp` |
| `hub-secrets` | a file tool edit in the hub's token folder or its store, a Read of the token folder or a file in it, a Grep or Glob in it or from a parent whose terms can reach it, and a Bash or PowerShell call that names the token folder or a token file, reaches either folder, or runs python or node code that names them or the hub site, in any session but the owner's own | `python "<kit>/tools/hub.py" questions` (`rules.hub_read_alternative`) |
| `busy-wait` | a foreground loop that sleeps with nothing bounding it, in any session: bash `while`/`until`, PowerShell `while`/`do`, sleeping by `sleep`, `Start-Sleep` or `[Threading.Thread]::Sleep`. A bound is a `-lt`/`-le`/`-gt`/`-ge` comparison, `((...))`, `$SECONDS`, a stopwatch's `.Elapsed` or `timeout`. A call with `run_in_background`, a loop sent to the background with `&`, `for`, `foreach` and `while read` pass | bash: `SECONDS=0; <the loop with [ $SECONDS -ge 600 ] && break; after its do>` (or `timeout 600 bash -c '<the loop>'`); PowerShell: the loop with `$deadline = (Get-Date).AddMinutes(10)` and a `break` past it. The sentence before it names `run_in_background` and Monitor |
| `heredoc-winpath` | python reading its script from a bash heredoc in which a string that is not raw holds a Windows path with single backslashes (a drive letter, then at least two path characters), in any session. An unquoted `<<EOF` is read as bash hands it over, so its `\\` counts as one backslash. Raw strings, forward slashes, doubled backslashes, comments and escapes such as `"%s:\n"` or `"x:\t"` pass, and so does a heredoc that is not python's script | `python "<temp>/script.py"`; the sentence before it says to write the script with the Write tool |
| `git-hooks-path` | a change of `core.hooksPath` (`git config` set, `--unset` or `--add` on a repository's or user config, `-c core.hooksPath=` or `--config-env` on any git command, `git clone -c`, or `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_KEY_<n>` naming it), in every session, the owner's included. Setting it to the kit's own githooks (the install step), and a repository or config file under the temp folder, pass | `git -C <dir> config --get core.hooksPath`; for `-c` and `clone -c`, the command without the override |
| `owner-identity` | a session other than the owner's own (see "Session mark") giving a commit the `.kit.json` `owner.email`: `git commit --author`, `user.email`/`author.email`/`committer.email` set by `git -c` or `git config`, or `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_EMAIL` or `EMAIL` set as an environment variable on the line (a bash prefix or `env`, `export`, `setx`, `$env:`, `Set-Item env:`, `[Environment]::SetEnvironmentVariable`). The whole address, any case; a longer address that contains it passes, and so does `email=<address>` that is not one of those variables being set (a form field, a `--email=` option, a commit message) | `git -C <dir> config --get user.email`: commit as yourself |
| `kill-by-name` | an agent session (see "Session mark") ending processes by name: `taskkill /IM` or `/FI "IMAGENAME eq ..."`, `pkill`, `killall`, `Stop-Process -Name`, `Get-Process <name> \| Stop-Process`. The values of options that take one (`pkill -P 1234`, `-u <user>`, `-t <tty>`, `killall -s <signal>`) are not names; a `pkill` or `killall` with no name left passes | `tasklist /FI "IMAGENAME eq <name>"` (PowerShell: `Get-Process -Name <name> \| Select-Object Id, StartTime, Path`); the sentence before it says to stop the one started, by id |
| `lock-state` | an agent session's file tool edit or shell write under `<home>/.claude/machine-lock`, which only the lock tool writes, judged by the resolved path (links, junctions and short names followed, `jail.real`) | the lock CLI's `status`, as for `lock-mcp` |
| `kit-json` | an agent session's file tool edit or shell write of a file named `.kit.json` (by its name or the name its links resolve to) in a checkout, a linked worktree's included, or a place git cannot tell. One outside git passes | `python -m json.tool "<the file>"`; the sentence before it says to ask the coordinator to take the change to the owner |
| `generated-file` | in any session, the owner's own too: a file tool edit or shell write, in a kit checkout, of a file `tools/render_content.py` writes (see "Generated files" below) | `python "<checkout>/tools/render_content.py" --write`; the sentence before it names the file's source (the role file, the skill under `content/skills/`, `boot/`, or for a manifest `kit.json`, `hosts.json` and the host's renderer) to edit first |
| `write-jail` | an agent session's file tool edit or shell write outside its write roots, and git that changes a checkout outside them; see "Write jail" below. The sentence names every root and says to ask the coordinator. Also a marked agent session's PowerShell `& (<subexpression>)` outside a repository with `kit-pm-data.json` ("Fallback"), with `Instead: & <program> <arguments>` | the same tool call or command aimed at the same file name in `<wt>` (else the temp folder); for git, `git -C <wt> <the same subcommand and arguments>`; for an existing file in an add-only folder, the same write to `<folder>/<new file name>` |

**Shell writes** (`kitlib/guard/rules.py` `write_targets`): redirects; PowerShell write cmdlets and .NET file writers; cp, mv, rm, ln, install, touch, tee, `sed -i`, `perl -i` and `patch`; `curl -o`, `wget -O`, `rsync`; `robocopy`, `xcopy` and cmd copy, del and move; archive extractors (`Expand-Archive`, `tar -x`, `unzip`) into their folder; find `-delete`, and a writer find `-exec` or `xargs` runs with no literal target.
- Code given on the line (`python -c` or a script on stdin, `node -e`, `ruby -e`, `perl -e`) counts as a write to the working directory. The one exception is a `python -I -c` the guard proves reads only (`rules.py_reads_only`: literal code, a short allowlist of modules, names and syntax, `open()` only for reading); anything else stays a write (tests/guard/test_pyreads.py). Under cmd.exe, Start-Process, or a PowerShell word 5.1 may pass differently, python is never proved.
- A write target that is not literal is denied in a main checkout or a place the guard cannot tell (fail closed); under PowerShell it is denied from every location, since one `$p` can name the main checkout. Name temp paths literally.


### Kit paths in advice

An alternative that runs a kit tool names it under one folder (`rules.advice_kit`): the installed plugin export the session's kit tools run from, when the guard can tell it, else the kit the guard runs from (`Ctx.kit`). The hook is not told which folder the session's header named, so a session started on an older export is pointed at the current one; the `heavy` rule lets an interpreter run any export's `tools/gate.py` and `tools/machine_lock.py run` as the kit's own.

## Write jail

`kitlib/guard/jail.py` (roots and verdicts), `kitlib/guard/rules.py` `write_jail` and `git_jail` (rule `write-jail`). It holds agent sessions only ("Session mark"): subagents and the marks in `guard_agent_marks` (default `coordinator`, its old name `conductor`, `deep-worker`, `consultant` and `pm`). The owner's unmarked session and other marks (`plain`) are not held. Tests: tests/guard/test_jail.py.

- **Write roots** of a session running in `<cwd>` (the hook payload's `cwd`):
  - **its checkout:** the git top of `<cwd>` (in a linked worktree, that worktree alone); outside git, `<cwd>` itself unless it is a drive root or the home folder;
  - **its repository's worktrees:** every linked worktree under the same main checkout, but not the main checkout or the PM data worktree;
  - **temp:** the system temp folder, and `AGENT_TEMP` when set;
  - **data roots:** `.kit.json` `guard.data_roots` (docs/kit-json.md), each `{path, mode, why}` with mode `open`, `add-only` (new files only) or read-only;
  - **mark roots:** the machine config's `guard_mark_roots`, `{<mark>: [{path, mode, why}]}`, for the main sessions of one launcher mark; a subagent gets none.
  - **No root on a machine config folder:** a root that is, holds or lies inside either machine config folder is dropped unless read-only.
- **Paths** compare after `os.path.realpath` and `normcase`, so a junction in the worktree that points out of it is outside.
- **What is judged:** file tool edits by their target, every literal shell write target ("Shell writes"), and git that writes, by the checkout it runs in (`git -C <another repository> commit` is denied, `git -C <a worktree of the same repository> commit` passes). Git reads pass anywhere; `worktree add`, `clone` and `archive -o` are also judged by the folder or file they make.
- **The lander in a main checkout:** where that checkout's `.kit.json` sets `roles.lander.edit_main_checkout`, a lander may run exactly `git fetch <remote>`, `git fetch <remote> <main>` and `git merge --ff-only <remote>/<main>` there, each alone on its line (`rules.lander_alone`); anything else is denied, and the refusal names the first test that failed.
- **Not judged:** a target that is not literal under bash or cmd, a non-git command whose directory is unknown, and git whose subcommand the text does not name. These meet the rules above them and the fallback.
- **Order:** `ledger`, `lock-state`, `kit-json`, `generated-file` and `main-checkout` are checked first.

## Generated files

`kitlib/guard/generated.py` `source(top, rel)`; rule `generated-file`, checked with the other edit rules for a file tool's target and each shell write target (docs/content.md "Generated files"). It judges only a checkout with the kit's layout (`content/roles/` and `tools/render_content.py`), in any session:

| rendered | its source |
|---|---|
| `hosts/<host>/shell/agents/**` | `content/roles/<role>.md` |
| `hosts/<host>/shell/skills/**` | the same file under `content/skills/` |
| `hosts/<host>/shell/boot/**`, `hosts/<host>/shell/hosts.json` | `boot/`, `hosts.json` |
| `hosts/<host>/shell/plugin.json`, `mcp.json`, `hooks/hooks.json`; `.<host>-plugin/plugin.json` and `marketplace.json` at the root | `kit.json`, `hosts.json` and `hosts/<host>/adapter/content.py` |

The folders themselves count too, so `rm -r` of one is refused. git that restores, merges or removes these files passes, and so does the renderer itself (a script's own writes are not shell write targets). tests/guard/test_generated.py.

## Role policy

`kitlib/guard/rolepolicy.py`, run by `decide` on a call the shared rules allow, the same on every host (ARCHITECTURE.md "Roles").

- **One switch per repository:** the policy runs only when the main checkout's `.kit.json` sets `guard.role_policy: true` (docs/kit-json.md; default false). With it off no role is held. The kit's own repository has it on.
- **Where a mode comes from:** `roles.<role>.mode` in that `.kit.json` (an old role name counts as its new one), else the `mode:` line of `content/roles/<role>.md` under the kit the guard runs from (docs/content.md "Role front matter").
- **The kit's modes:** `controller` pm, coordinator; `write` weak-worker, worker, strong-worker, senior-worker, deep-worker, asset-smith, sound-smith, tech-artist, spec-writer, records; `read` reviewer, researcher, consultant; `test` test-runner; `lander` lander.
- **The role:** a subagent's role (`Action.agent_role` when a role names it, else `subagent`); a main session's launcher mark; else `main`. The owner's own session ("Session mark") is never held.
- **`writes`:** a role file may name repo-relative paths or globs that its `read`, `test` or `lander` role may still edit in its checkout with a file tool (the consultant's notes file).
- **Rules:**
  - `role-edit`: a file-tool edit inside the session's checkout by a `read`, `test` or `lander` role, unless `writes` names the file. Its `Instead:` is prose (return the change in the report).
  - `write-jail`, with more read-only roots for `read`, `test` and `lander`: every host's owner paths (on Claude `CLAUDE.md` and `.claude/`, its worktrees folder excepted), and for `read` and `test` the checkout itself.
  - `role-workdir`: a shell call whose own working directory is outside the session's checkout.
  - `role-shell` (`shell_gap`): an interpreter that would wait for input on the terminal.
  - `role-lock`: `machine_lock.py` run with a verb outside the mode's list (`rolepolicy.LOCK_VERBS`) or with `--force`; any command that names the lock CLI is read as a lock call (fail closed).
- **Tests:** tests/guard/test_rolepolicy.py; tests/content/test_render.py pins the kit's own table.

## Fallback

`kitlib/guard/fallback.py` `check` runs only when the parse is unclean. It catches mistakes, not attackers: an unclean line fails closed when it touches a main checkout, a heavy program or a protected path, and a plain line is never denied by it.

- **Patterns:** the kit's git rules (bulk add, `commit -a`, `checkout`, `reset --hard`, `clean`, `stash`, `restore` and the history writes in a main checkout or a place git cannot tell; `branch -D`, `worktree remove`, `update-ref`, pushes to main and pushes that skip the hook anywhere), wrapped gates, root searches and heavy programs.
- **A pattern match:** the text from the match to the next `;`, `|`, `&` or newline is read again as a command and the parsed rules run on it. A rule that denies gives its deny after "The guard could not read this command; write the program name literally."; else rule `unreadable`, with `Instead:` the line written plainly (or a script file in the temp folder when it cannot be rewritten).
- **A heavy program's name:** the same, unless the session holds the lock; one named by a variable is denied as `heavy`.
- **Anything else:** a subagent gets `unreadable`; a marked session the write jail holds gets `write-jail` when the line holds the word `git`; a session the jail does not hold gets `unreadable` only when a command in a main checkout runs a program, shell or git subcommand the text does not name. In a repository with `kit-pm-data.json`, any session but the owner's gets `git-push-pmdata` for such a line.
- **`& (<subexpression>)`:** leaves the line clean, so `rules.call_subexpr_pmdata` gives the same answers on it.

Tests: tests/guard/test_rules.py ROWS, tests/guard/shell_rows.py, tests/guard/review_rows.py and tests/guard/extra_rows.py; tests/guard/test_session.py for the rules that read the session mark.

### Known limits

The guard reads the command line, not the shell's state. It is a speed bump, not a jail:

- **Aliases and functions** are not seen; the name is read as a program (git aliases in `~/.gitconfig` too, except where `git-push-pmdata` expands them).
- **Computed commands:** `& (Get-Command x)`, `Invoke-Expression`/`eval` of a computed string, a `PATH` or `--exec-path` that makes `git` another program, and environment the line does not show (a PowerShell `function git`, a variable persisted by `setx`) are not resolved.
- **Programs the line runs:** a script or program the agent runs (a `.py` it wrote, a build tool) can write anywhere; the write jail judges only the targets the command line names. What it puts in a repository still meets `tools/commit.py` and the pre-push owner-only check.
- **Direct edits of a repository's config** (`<git common dir>/config` by a file tool or shell write) are judged only by the path rules, so they can repoint a remote unseen.
- **Rules the fallback does not run:** `busy-wait`, `heredoc-winpath`, `git-hooks-path`, `owner-identity` and `kill-by-name`; an unclean line meets only the fallback's own patterns.
- **busy-wait:** a loop in a script file or the Monitor tool is not read; any `-lt`/`-le`/`-gt`/`-ge` reads as a bound, and an unknown bound reads as none.
- **heredoc-winpath:** only python reading a bash heredoc as its script is read.
- **git-hooks-path:** `git clone --template` and `git init --template` are not seen.
- **owner-identity:** an identity in the session's starting environment, in `GIT_CONFIG_*` pairs or set inside a program the line runs is not seen; `--author=<name only>` is not caught.
- **kill-by-name:** `pkill` and `killall` by user, parent or terminal with no name pass.
- **Clearing the mark:** a marked session can start a child session with `PM_KIT_ROLE` cleared; the child counts as the owner.
- **Environment changes beside a git write:** a held session's git write on a line with an environment change the guard does not read is denied even when the change is harmless; run the git in its own call.
- **write-jail:** roots follow the payload's `cwd`, so an agent started in another checkout gets that checkout; temp is shared by every session; a lander whose `cwd` is the main checkout gets the whole checkout as its root.
- **Pre-push config load:** a clone with no base commit at all falls back to the defaults (no `push_block`), so the gate-OK check is skipped there.

## Lock MCP

- **Which tools:** a tool named `<prefix><owner-only tool>` (`kitlib.lockmcp.server.OWNER_ONLY`), any `mcp__*__<owner-only tool>`, and any full name in the user config `guard_mcp_tools`. The prefix is the asking host's `hosts.json` `mcp_tool_format` with the kit plugin and lock server filled in.
- **Who is asking** (`Ctx.session_role`, "Session mark"): a subagent or any mark is denied; the owner's own unmarked session is allowed, and the lock's own owner check still applies.

## Session mark

`kitlib/sessionmark.py`. The launcher that starts an agent session sets `PM_KIT_ROLE` to its kind (`tools/launch.py`, docs/launcher.md "The window": `pm`, `coordinator`, `deep-worker`, `consultant`, `plain`, or another agent's name); a subagent is told apart by the hook payload's `agent_id` (`Action.agent`). `kitlib/guard/context.py` `default_session_role` gives `subagent`, else the mark, else `owner`. The mark is the hook's own environment's, unless the host's adapter hands it over as `Action.role` (Claude's does not: the hook inherits the session's environment).

- **Unmarked is the owner:** a main session nothing marks is the owner's own, exempt from `owner-lock`, `owner-setup`, `lock-mcp`, the four hub rules (`hub-mcp`, `hub-browser`, `hub-owner-verb`, `hub-secrets`), `owner-identity`, `git-push-pmdata`, `kill-by-name`, `lock-state`, `kit-json` and `write-jail`. Until the kit launcher is live, sessions the old launcher starts are unmarked and count as the owner ("Go-live", step 4).
- **Reserved values:** a mark of `owner` or `subagent` reads as `marked-<value>`, never as that standing.
- **The plugin prefix:** a mark is read without the kit plugin's agent prefix, before the reserved check ("Agent names"): `<plugin>:coordinator` is held as `coordinator`, `<plugin>:owner` reads as `marked-owner`. The launcher writes bare marks (docs/launcher.md "The window").
- **Agent sessions:** `kill-by-name`, `lock-state`, `kit-json` and `write-jail` hold subagents and the marks in the user config `guard_agent_marks` (default `kitlib/guard/rules.py` `AGENT_MARKS`: `coordinator`, `conductor` (the coordinator's old mark, removed after the switch), `deep-worker`, `consultant` and `pm`; compared without case); other marks (`plain`, another agent's name) are not held to them. Setting `guard_agent_marks` replaces the default list rather than adding to it, so a value must also list every default mark it keeps holding (`ctx.user().get('guard_agent_marks') or AGENT_MARKS`). `owner-lock`, `owner-setup`, `lock-mcp`, the four hub rules and `owner-identity` hold every marked session.
- **The PM's mark:** only the session the kit launcher resumes carries `pm` (docs/launcher.md "The window"); a PM session opened by hand or in the desktop app is unmarked and counts as the owner's own. The jailed PM keeps the folders it writes outside its checkout through `guard_mark_roots` ("Write jail"), which the owner sets.
- **Where it is read:** only where the session cannot change it once started: the guard hook and the lock MCP server, both started with the session's environment (INFERRED; not yet observed live). The lock CLI does not read it, since an agent sets the environment of every command it runs.
- **Lock MCP server:** `mcp/lock_server.py` `main` installs `sessionmark.owner_check` through `kitlib.lock.api.set_owner_check`. In an unmarked session a caller under the session's agent host counts as the owner (the default check sees the agent host as the caller and refuses every owner write); in a marked one only the default check passes.
- **Limits:** a session the launcher did not start is unmarked and counts as the owner. A subagent of the owner's session passes the lock server's owner check; the guard still denies it by `agent_id`. The lock's holds and tree roles do not read the mark.
- **Tests:** tests/guard/test_session.py.

## Agent names

`kitlib/agentname.py`. Claude Code names an agent a plugin bundles `<plugin>:<name>`; every place the guard compares an agent name accepts the bare name and the kit plugin's prefixed form (`agentname.PREFIX`).

- **Agent types** (`agentname.bare`): the adapter drops exactly one leading prefix into `Action.agent_role`; any other form names no role and is held as a plain subagent.
- **Session marks** (`agentname.bare_mark`): the prefix is dropped in any case and as often as it repeats (the restrictive reading).
- **Tests:** tests/agentname, tests/config/test_policy.py, tests/guard/test_session.py, tests/guard/test_prefix_parity.py.

## Owner lock tools

Owner-only lock tools are run by the owner, not agents: `reserve_ui.pyw` and the lock CLI's owner writes. The lock's own owner check can be forged by a detached process, so the guard stops agents from starting them at all (rule `owner-lock`, every session but the owner's own); that check stays as a second layer.

- **reserve_ui:** denied when a command runs it in any form (as the program, python's script or standard input, `Start-Process`, `start`, `Invoke-Item`, `python -m`/`-c` naming it or `kitlib.lockui`, or a variable when the text names it). Reading it and git on it stay open.
- **Lock CLI owner writes:** a word whose base name is `machine_lock.py`, followed by `reserve`, `release`, `stop --force`, `unbook --force` (the installed CLI), or `hold whole|half|slots`, `division --set|--default`, `game on|off|set`, `top`, `stop --force` (the kit CLI).
- **Still open:** `status`, `run`, `background`, `book`, `unbook` and `stop` without `--force`, `hold agent|list|end|edit`, bare `division` and `game show`; the lock decides whose hold an id names.
- **Limits:** a script named by a variable, a glob, a copy under another name, `.NET` process calls and `Start-Process -ArgumentList @(...)` arrays are not caught.
- **Tests:** tests/guard/test_rules.py `OwnerLockTools`.

## Owner machine setup

Machine setup's `--apply` writes the machine config, and its `owner_email` decides which commits count as the owner's (docs/machine-setup.md "Owner email"), so rule `owner-setup` (`kitlib/guard/rules.py` `owner_setup`) denies it to every session but the owner's own: `machine_setup.py`, `machine-setup.ps1` or `new-repo.ps1` (or `python -m ...machine_setup`) with `--apply` in any prefix argparse or PowerShell takes, a `.ps1` entry point with a double quote inside an argument, an entry point followed by a word the text does not show, and python code that imports `kitlib.machinesetup`. The dry run, `--help`, reads and git on its files stay open; `--repo` is held the same way.

- **Limits:** an agent's own script that writes the machine config, or a renamed copy of the tool, is not caught.
- **Tests:** tests/guard/test_rules.py `OwnerSetup`.

## Hub secrets

The Action Hub (`kitlib/hubsite`) keeps its tokens in a folder named `hub` beside the machine config file: `owner.token` answers as the owner, `agent.token` is every agent's, and `approval.key` signs the owner's approvals. Its store is the machine config's `hub.store`, unset: `<pm_data.path>/hub`. Agents reach the hub only through `tools/hub.py`'s agent verbs and the hub MCP server, so every session but the owner's own is held by four rules:

- **hub-secrets:** a file tool edit of the token folder or a store; a Read of the token folder, or a Grep or Glob that can reach it (the store stays readable); a shell command naming the token folder or a token file, a literal path into either folder (outside `tools/hub.py` itself), or python or node code on the line that names them or the hub site (`hub.INLINE_RE`). Search patterns and git messages are not counted as paths.
- **hub-owner-verb:** fail closed. A line naming the hub tool is denied unless it is the one shape the guard reads in full (`rules.hub_shape`: python with plain flags running `tools/hub.py` or `-m tools.hub` with an agent verb and every word literal), or one simple command of the path family (`rules.hub_path_family`: a reader such as `cat` or `grep`, git's reads, staging and commits, or `tools/commit.py`, `backlog.py` or `ledger.py` naming it as a path or message). Owner verbs (`answer`, `open`, `pair`, `unpair`, `stop`, `restart`, `todos done`, `stories add`, `to-pm send` and the others) are always denied.
- **hub-mcp:** an MCP tool in `hub.OWNER_MCP_TOOLS` (the hub MCP server's `OWNER_ONLY`) under any server name, or a hub tool whose name holds `decide` or `approve`.
- **hub-browser:** a browser or computer-use MCP call whose input names a loopback host on a hub port (8790, 8791 and the configured ports).

- **Limits:** names built at run time (concatenation, encodings, variables), the home folder computed rather than spelled, a script the agent wrote, a recursive read of a parent folder, an encoded PowerShell command run from a worktree by a marked session, and a click on a hub tab already open are not caught.
- **Over-reach:** python code merely naming `hubsite` or `hub.py`, a word that is itself a path into either folder, a bare `hub` in the PM data folder or `~/.pm-kit`, and any naming of the hub tool outside the allowed shapes are denied though nothing secret is read.
- **Tests:** tests/guard/test_rules_hub.py, tests/guard/test_rules_hub_read.py, tests/guard/test_entry.py `test_a_subagent_read_of_the_hub_owner_token_is_denied`.

## Pre-push

`githooks/pre-push` calls `kitlib.guard.prepush.main` with git's two arguments. The PM data stage judges every ref line first; the checks after it are on a ref whose remote ref is `refs/heads/<main>`.

**PM data branch** (`prepush.leak_refusal`; docs/pm-data-branch.md "Pre-push guard"), only when the git common dir holds `kit-pm-data.json`:
- **Fail closed:** a record that cannot be read, the root commit missing from the clone, or remotes that cannot be read refuse every push.
- **Which remote is private:** the URL git hands the hook decides, not the remote's name; it must be one of the recorded remote's push URLs and no other remote's.
- **Public remote:** refused when the ref is the data branch, when the pushed sha holds the recorded root (or git cannot tell), or when the objects the push may send share any object with the data history. Deleting a leaked data branch from a public remote is allowed.
- **Private remote:** it receives only `refs/heads/<data branch>`, with the root in its history.
- **Not caught:** `--no-verify` skips the hook; the guard rule `git-push-pmdata` denies it outside the owner's own session ("Rules").

**Main branch rules**, in order after that stage:
- **Fast-forward only, in every repo:** a push that is not a fast-forward of the remote's main, or deletes it, is refused; a remote main sha this clone lacks is refused too ("cannot tell"; fetch first).
- **Gate-OK record:** under `repo.push_block`, the pushed sha must have one ("State").
- **Owner-only paths:** under `repo.push_block`, a push to main is refused when a commit it adds fails "Owner-only commits" below.
- **Base config:** `push_block` and the main branch's name are read from the remote main commit being replaced, else the default branch's committed `.kit.json`; never the working tree. A `.kit.json` that does not load refuses the push.
- **Tests:** tests/guard/test_prepush.py.

## Owner-only commits

`kitlib/guard/ownercommits.py` `violations()` judges the net change, then the commits. For each owner-only path (`kitlib/config/owner.py` `is_locked`) that the net change touches, it names every commit that changes the path and whose author email (`%ae`, never mailmapped, replace refs off) is not `"owner".email` (`is_owner`). `gate land` (docs/gate.md "Landing", step 4) and pre-push run it, so a plain `git commit` to an owner-only path does not reach main past either.

- **Net change:** a path counts only when landing the tip on the target main would change it: the target differs for it from the clean merge of the target and the tip (`git merge-tree --write-tree`; the tip itself for a fast-forward), or that merge conflicts on it (`net_change`). The target is the landing's base for `gate land` and `land.py` (`<remote>/<main>` as fetched at its turn; docs/gate.md "Landing", step 4), the remote main being replaced for pre-push. So an agent change a later commit undoes, a rename away and back, and an owner change the branch brought in by merging main do not count. With no target (pre-push with no remote main known), every changed path counts.
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

`hosts.claude.adapter.guard_selftest.agent_id_check(projects_dir, since)` scans subagent transcripts (`<project>/<session>/subagents/agent-*.jsonl`) for guard tags.

- **What counts:** only tags in tool results marked `is_error` (INFERRED to be where a hook deny lands).
- **Results:**
  - `role=main` in a subagent transcript is an alarm: `agent_id` has disappeared;
  - only subagent roles pass;
  - no tags is inconclusive.
- **Not live:** it is not wired to a session-start hook.

## Refusal patterns pre-empted

The guard denies these with the command to use before the auto-mode classifier sees them (the PM repository's docs/refusals.md):

- **P16:** a gate wrapped as `cd <wt> && python tools/gate.py 2>&1 | tail`. Rule: `wrapped-gate`, whose alternative is `python "<kit>/tools/gate.py" -C "<wt>"` run bare in the background.
- **A repo's wrapped lander command** (same family as P16). Rule: `wrapped-gate`.
- **git_guard's main-checkout patterns:** the git rules and the fallback cover them.
- **Root searches:** rule `root-search`.

## Go-live (after the joint review)

1. Settings: the one PreToolUse entry that `python <kit>/boot/hook.py claude --settings-entry` prints, in exec form: `command` is the `pythonw.exe` beside the python that ran it (never a bare `pythonw`, which may find the Store alias) and `args` is `[<kit>/boot/hook.py, "claude", "PreToolUse"]`. The matcher is `Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|Read|Grep|Glob|mcp__.*` (`hosts/claude/adapter/host.py` `MATCHER`). The guard stays in each account's settings and never goes into the plugin.
2. Every repo: `git config core.hooksPath <kit>/githooks` (the fast-forward rule is for every repo; the gate-OK check needs `push_block`).
3. Every repo whose landers edit a main checkout, or fetch and fast-forward one from a worktree ("Write jail"): `.kit.json` `roles.lander.edit_main_checkout: true`.
4. The launcher sets `PM_KIT_ROLE` for every session it starts; until then every main session counts as the owner's own ("Session mark").
5. The machine config holds `ledger_path`.
6. Every repo whose agents write a folder outside git lists it in `.kit.json` `guard.data_roots`, an owner change.
7. The owner lists in the machine config's `guard_mark_roots`, under `pm`, each folder the jailed PM writes outside its checkout and temp, before the launcher's `pm` mark goes live.

Check after step 1: a known-denied command is denied: a Bash root search, the call tests/guard/test_entry.py `GoLive` sends, comes back denied with `rule=root-search`.
