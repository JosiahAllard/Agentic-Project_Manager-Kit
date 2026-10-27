# Guard v2: what was cut, and the checks it weakened

Status: built, one step per branch. docs/guard.md describes the guard as it is now; this doc keeps the reasons behind the cuts and the list of checks they weakened. "Built" below means the symbols the row names are gone from `kitlib/guard`; the line numbers the design once carried are dropped, since the code has moved on.

The owner ruled on it one check at a time. "Checks this design weakens or removes" lists every check that stopped refusing something, one row each. A refused row stays as it was, and its lines come back out of the cut. Rows outside that section changed no protection.

## Method

Each refusal in the two logs read (the PM's cross-repo refusal log and one coordinator's day log, neither in this repository) was put in one class:

- **real**: the call would have done what the rule exists to stop (a write outside the jail, a heavy run with no lock, an owner verb).
- **form**: the target was safe, but the call was written so the guard could not see that (a computed path, a computed `-C`, a chained or wrapped call). The refusal is right under the guard's contract; the agent rewrites the call.
- **false positive**: a safe call written plainly was refused (prose that names a word, a read-only git verb, a literal worktree path judged by the shell's directory).
- **absent**: no refusal in either log.

A rule earns its code by real refusals. Form refusals argue for a short, plain rule with good advice, not a cleverer reader. False positives are cost. An absent rule is kept when it is small and its hazard is one mistake away (one `git reset --hard`). An absent rule that guards the owner's powers or what leaves the machine is not cut on that ground alone: it has a row below. "Absent" means absent from those two logs, not "never fired"; the logs are outside this repository, so no count here can be checked from it.

Two rules carried most of the cost: main-checkout's `python -c` counting, and the hub rules' text matching. Both read the command's text where they should read the program it runs.

## What was kept, cut and merged

Kept as they are (each catches a real hazard): the dispatcher and its order (the lock first, edits to the edit rules, shell calls per command, then the role policy); write-jail and its worktree advice; the git jail; main-checkout for file tools and literal shell writes; the Unix writer options, `tar`, `unzip` and copiers; busy-wait; wrapped-gate (narrowed, B9); heavy and the kit wrapper; root-search; the git main-checkout rules (checkout, reset --hard, clean, stash, bulk add, commit-all, restore, main history); git-push-main; git-worktree-remove; the lander checks; kit-json; kill-by-name; the ledger, lock-state, generated-file and pm-data checks; owner-identity; git-hooks-path (whole, W1); the pre-push leak stage and owner-only commits; the shell and command parser; the role policy's mode, screen and role-shell.

Cut or merged, with what stays:

| Area | What stays | What went |
|---|---|---|
| Hub text matching (hub-owner-verb, hub-secrets on shell text) | An owner verb on a parsed `tools/hub.py` or `-m tools.hub` run (`hub.owner_verb`); a literal path in any word that resolves into the token folder; the inline-code check (W3); the read-tool rules (W4); hub_mcp and hub-browser; the fallback's hub pattern (W5); hub-secrets on an edit. The shell half of the path rule refuses only the token folder, not the store, and reads a wildcard path as the literal folder before its first wildcard (`rules.glob_folder`). | The fail-closed naming read (A1) and the name-only token words (A2). |
| Role-lock text machinery | A parsed run of the lock CLI with a verb outside the role's list, and the lock MCP tools by verb. | The fail-closed hand-on reading (B5). Once the lock checks its caller itself (open), the rest goes. |
| Python program proving | A `python -c`, or python reading code on stdin, counts as a write of the directory it runs in. The advice is one line: `cd "<worktree>" && python -c ...`, or the Read and Grep tools, and must not tell a worker already in a worktree to add one. | The prover (`-I`, module allowlist, AST walk): stricter, so no weakening. Counting `python -c` as a read was not taken (O1). |
| PowerShell write reading | One table of writer cmdlets and their path parameters; the .NET `File`/`Directory` write methods by the arguments each writes (`rules._net_written`: `Copy` its destination, `Move` both, `Replace` all three, any other its first); the `StreamWriter`/`FileStream` constructors by their first argument. A literal target is judged where it points, never by the shell's directory. A computed or unread PowerShell cmdlet target is refused from every location; a provider, drive or .NET target the guard cannot place is refused outside a worktree, as a bash one is. A `FileStream` opened only to read counts as a write: stricter. | The cast form (B7); from a worktree, computed, provider and relative .NET targets (B16, B17). |
| Environment reading | `commands.env_assignments` whole (prefix and `env` forms, the exporters, `set`/`setx`, `$env:X =`, `SetEnvironmentVariable`, item cmdlets on `env:`), `git_env_set`, `line_env_names`, `mark_git_env`, `HOME_ENV_NAMES`. owner-identity, git-hooks-path and home_config read through these. | The fail-closed layer that marked a git blind on a line with an environment change it could not read (A4, B4, B10 to B14), and the `read`, `printf -v`, `for`, `mapfile`, `getopts`, `select`, `trap` and arithmetic assignment forms, cmd's `path`, and double-quoted scanning (B3, B4, B13). |
| pm-data evasion checks | `push --no-verify` and send-pack in a repository with `kit-pm-data.json`; the remote-helper list (W6); `call_subexpr_pmdata`; HOME-like variables set for `git push` in the plain forms (W8); `git_remote_rewrite` whole (W7). | Alias expansion through `git config --get` (B1), and the HOME forms the environment cut dropped (B3). |
| git-shared-ref | Today's rule: from any checkout an `update-ref` may write or delete `refs/tmp/*` and the per-worktree refs other than HEAD; only in a linked worktree that is not the PM data worktree may it also write HEAD and the branch checked out there, never a guarded main; any other ref, and any delete of one, is refused. A blind git, a computed word, `--stdin`, an option the guard does not read and a place that is not a main or linked checkout are refused as unread. A coordinator's main session may also delete a stray ref in its own repository (`rules._stray_delete`); there a symbolic ref or a reftable repository is still refused unless --no-deref is given (`rules._deref_unsure`). The scratch-repo exemption (`_scratch_repo`) stays: an `update-ref` in a throwaway repository under the temp folder is no mistake. | Symbolic-ref chain walking (B6), the check that a ref's file is not reached through a link in the git directory (B18), and the reftable refusal (B19). |
| Git read classification | One table of read-only subcommands with the options that make each one write or run a program; branch, tag and config keep the strict list, so an option not named in full is not a read (`strict_read_opts`). grep's `-O` and archive's `--remote`/`--exec` stay reads. `config` counts a read flag only as its own option (`config_reads`). `apply` with `--stat`, `--numstat` or `--summary` and no `--apply` is a read. A `-c`, `--config-env` or environment change does not make every git on the line a non-read. | Pager and dynamic-read special cases. |
| owner-lock and owner-setup | Merged into `owner-tool` over a parsed invocation (`rules.owner_tool_run`): reserve_ui as a program, python's script, a launcher's target or an opened file; the lock CLI's owner writes; machine setup with `--apply`. | The code-text and standard-input reading (A3). |
| heredoc-winpath | Nothing. A quoting trap, not a hazard. The shell-traps skill says to write scripts with the Write tool. | The rule (B8). |
| Unreadable (fallback) | The one place for what the parser cannot read. Adds one name pattern (W5). `pwsh -File -` is read from its pipe as `-Command -` is, and only a pipe the guard cannot read is unreadable (docs/guard.md "Parsing"). `fallback.plainly` gives only the script-file run as advice; the guard never rewrites a refused line. | The rewrite attempts. |
| lock-mcp | Stays until the lock MCP loses its write tools (open). | Then the rule goes; not a weakening, since no owner tool is left to hold. |

Not built, open: the lock checking its own caller (which lets role-lock go), the lock MCP losing its write tools (which lets lock-mcp go), and a `ledger_dir` check in context.py.

## Checks this design weakens or removes

One row per weakening: what stops being caught, what else covers it, the risk. Where a row offers an option, the owner answered with the option or without it; the doc does not record which for each row, so read the code. Group B rows hold agent sessions only unless the row says otherwise. B4 and B10 to B14 lose refusals that git-push-main, the main-checkout rules on an unknown place and git-worktree-remove gave in the owner's own session too; that loss is A4.

### Group A: the owner's own session and the owner's powers

| # | Check | Stops being caught | Other cover | Risk |
|---|---|---|---|---|
| A4 | git-push-main, the git main-checkout rules and git-worktree-remove, on a git the guard cannot place, in every session, the owner's own included | Every form B4 and B10 to B14 list stops making git unknown, so git is judged in the directory the guard believes, not the one `GIT_DIR` or `GIT_WORK_TREE` names: a push judged against the believed checkout's record, a `reset --hard` placed in a worktree so no main-checkout rule fires | The literal forms `git_env_set` reads; the pre-push hook's fast-forward check on the remote's main, only when the hook runs; nothing for `reset --hard`, `clean`, `stash`, `checkout`, `restore`, a bulk add or `commit -a`. The option (a coarse mark: git unknown when its line also holds `set -a`, a sourced script, cmd `call`, an `env:` item cmdlet, or a computed name) is offered, not verified here. | Medium-high: in the owner's own session, main moves without a gate-OK record, or a main checkout loses work, from one unusual form typed by mistake |
| A1 | hub-owner-verb: the fail-closed naming read became a parsed-run check | An owner verb reached any way but a parsed `python tools/hub.py <verb>` or `-m tools.hub <verb>`: `python -c` importing hub, an import in a heredoc or pipe, `import_module`, `runpy`, a wildcard path, a quote-split `-m hub`, a copy of the tool | Little by itself: `tools/hub.py` reads `owner.token` itself and posts owner verbs with it, so code that imports it acts as the owner. Kept: the inline-code check (W3), hub-secrets on a literal token path, and the fallback's hub pattern (W5) for lines the parser cannot read. The option (also refuse python code importing hub or naming `tools.hub`/`hub.py`) is offered, not verified here. | High: an agent answers the owner's questions or acts as the owner on any owner verb (`hub.OWNER_VERBS`) |
| A2 | hub-secrets: token-file names in words the guard cannot place | A shell word whose last part is a token file (`owner.token`, `agent.token`, `approval.key`) when its folder is not literal (`cat $d/owner.token`), and the token folder inside a longer word. Built with the further cut: `hub_name` is gone, and only a literal path into the token folder is refused (docs/guard.md "Hub secrets"). | A literal path into the token folder (any word); the inline-code check (W3); the Read, Grep and Glob rules (W4) | Medium-high: `owner.token` is every owner power over HTTP |
| A3 | owner-lock and owner-setup: code-text and standard-input reading | An agent starting the reserve UI or the lock window's package from python code or stdin, or running machine setup's apply from python code, stdin or a variable | The lock's own owner check (no agent-host ancestor, `kitlib/lock/api.py`; docs/guard.md "Owner lock tools"), which a detached process can forge; both setup scripts refuse the `.ps1` quote themselves; the fallback's setup pattern and its lock pattern (W5) | Medium: an agent holds the whole machine, or stores its own address as `owner_email`, which decides whose commits count as the owner's |

### Group B: agent sessions

| # | Check | Stops being caught | Other cover | Risk |
|---|---|---|---|---|
| B1 | git-push-pmdata (and git-hooks-path, git-remote-rewrite) through a git alias | In a repository with `kit-pm-data.json`: `git pu --no-verify` where `alias.pu = push`, a `!` alias, and `core.hooksPath` or a remote rewritten through an alias | None: `--no-verify` skips the pre-push hook. `rules.inline_alias` expands an alias the line itself defines, and `GIT_BUILTINS` marks the subcommands git will not let an alias hide. | Medium: the data branch reaches a public remote; it takes an alias written first (inference) |
| B2 | git-push-pmdata: pushes from a copy of a recorded repository | CUT: the guard no longer looks for a push from a clone, a shallow clone or a copied folder of a recorded repository; `rules.data_copy` keeps only its fail-closed on a machine record it cannot read. | None; see "What the pre-push hook does not cover" | Medium-high: the data branch reaches a public remote with no check |
| B3 | git-push-pmdata: HOME-like variables set by unusual forms | `HOME`, `XDG_CONFIG_HOME` or `USERPROFILE` set for `git push` by `read`, `printf -v`, `for HOME in`, `mapfile`, `getopts`, `select`, arithmetic or a computed name. The plain forms stay caught (W8). | None: the hook reads the same moved config | Medium (inference): a `pushInsteadOf` in the moved config passes the data branch to a public remote. A hook-side fix, reading the config with those variables reset, would cover every form; it is a separate record. |
| B4 | write-jail and main-checkout for git: `GIT_DIR` and the like set in a form `git_env_set` does not read | A `GIT_ENV_NAMES` name set under a computed name (`export $n=x`, `env "$n"=x git`), by cmd's `set` with a computed name or trailing characters, by `setx` with a computed name, or by an env-provider item cmdlet whose item is computed. git then writes a checkout the guard did not place. | Every literal form `git_env_set` reads; write-jail's `git` word on unreadable lines (`fallback.py`) | Low-medium: it takes a deliberate unusual form |
| B5 | role-lock: the fail-closed hand-on reading | In a role session, a lock verb outside the role's list reached any way but a parsed run of the lock CLI: a path handed on by a variable, a copy, `$_`, `xargs`, standard input, an unrecognised launcher, `runpy` | owner-lock on the parsed owner writes in every agent session; the lock's own owner check; the lock's caller check (open) would replace this rule | Low-medium: a role session ends another's run or takes a hold its role does not have |
| B6 | git-shared-ref: symbolic-ref chains | `git update-ref <name> <sha>` where `<name>` is an allowed ref (`refs/tmp/*`, a per-worktree ref) that is a symbolic ref to a shared one such as `refs/heads/main`: git follows it unless `--no-deref`. The option (refuse an agent's `update-ref` without `--no-deref`) is offered, not verified here. | The pre-push fast-forward check on the remote's main only | Low-medium: the local main moves to any commit |
| B7 | PowerShell: the `StreamWriter` cast | A write through `[IO.StreamWriter]'<path>'` to a main checkout or outside the jail. The constructors stay caught. | None for the cast form | Low: an unusual spelling |
| B8 | heredoc-winpath, in every session, the owner's own included | python run from a bash heredoc where a non-raw string holds a drive path with single backslashes, which python's escapes change silently | The shell-traps skill's advice; the write rules still judge where the script writes | Low: a slip that writes to the wrong path, not past the jail |
| B9 | wrapped-gate narrowed to the runs that hold the machine, in every session, the owner's own included | Piping or wrapping `gate.py -h`/`--help`, `--plan` and `--measure`, and every lock CLI verb but `run ... -- <command>` | `heavy` still judges whatever such a pipe feeds ("Kit tools may be piped") | Low |
| B10 | git on a line that sets a git or shell variable outside the safe list | In the plain forms: a `GIT_*` name outside `GIT_ENV_NAMES` (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_EXEC_PATH`, `GIT_OBJECT_DIRECTORY`, `GIT_EDITOR`), or `BASH_ENV`, `ENV`, `SHELLOPS`, `BASHOPTS`, set for a git on the line. `git archive` also stops counting as a read for it. The option (keep `env_name_unsafe` and its lists) is offered, not verified here. | The pre-push hook keeps `GIT_CONFIG_GLOBAL` out of its own run | Medium: a moved global config sets `core.hooksPath` or a `pushInsteadOf`; `GIT_EXEC_PATH` runs another program as a git subcommand |
| B11 | git beside a bash command that may change the shell's state | `set -a`/`-o allexport`, `shopt -o allexport`, `bash -a -c`, a sourced script, an `alias`, a `trap` on any signal but EXIT, `mapfile -C`, a nameref | write-jail's `git` word on unreadable lines only | Low-medium: as B4 |
| B12 | git beside a PowerShell command that reshapes the session | `Set-Alias`/`New-Alias`, `New-PSDrive`/`mount`, `Import-Module`, a dot-sourced script, `Invoke-Command` with a script, `Set-Variable` or `-OutVariable` aimed at `env:` or a computed name, `Set-Location env:` | As B11 | Low-medium: as B4 |
| B13 | git beside a PowerShell environment call the reader cannot read | `InvokeScript`, `InvokeProvider`, a splat on `Set-Item`, `[Environment]::` calls with a computed name or any but `GetEnvironmentVariable`, a static call through a variable, `Start-Process -Environment` | As B11 | Low-medium: as B4 |
| B14 | cmd and xargs forms, and `PATH` set in forms the reader does not read | git on a cmd line that runs `call` or a batch file; `xargs -I {} env {} git`; cmd's `path <dir>`, and `read PATH`, `for PATH in` and the B3 forms before a `git archive`, which then counts as a read although git may find a filter's program on the new `PATH` | As B11 | Low |
| B15 | PowerShell: a computed or unread write target, from a worktree | NOT CUT: a computed or unread PowerShell cmdlet target is still refused as unplaced from every location, a worktree included (docs/guard.md "Rules"; `rules._write_from`, `ps_unread`) | nothing lost | none |
| B16 | PowerShell: a provider or PS-drive write target, from a worktree | A write to a non-FileSystem provider path, a drive name longer than one letter (`Main:\x` after `New-PSDrive`), or from a location on such a drive | None from a worktree | Medium: `New-PSDrive Main -Root <main checkout>` then a write to `Main:\x` writes the main checkout |
| B17 | PowerShell: a .NET write with a relative, driveless or computed path | `[IO.File]::WriteAllText('x', ...)` or a computed path, from a worktree, judged only where the PowerShell location puts it. .NET resolves a relative path against the process's directory, which `Set-Location` does not move, so a session started in the main checkout that runs `cd <worktree>; [IO.File]::WriteAllText('x', ...)` writes the main checkout (docs/guard.md "Known limits"). | None from a worktree | Medium |
| B18 | git-shared-ref: a ref reached through a link in the git directory | An `update-ref` of an allowed ref whose file a junction or symbolic link sends to a shared ref (a junction from `refs/tmp` to `refs/heads`, then `update-ref refs/tmp/main <sha>`). `--no-deref` does not help. The option (a link check on the ref being written) is offered, not verified here. | The pre-push fast-forward check on the remote's main only | Low-medium: it takes a link made first |
| B19 | git-shared-ref: a repository whose refs are in a reftable | A reftable repository is judged by the ref's name alone (except for that coordinator delete), so a symbolic ref there (B6) is not seen. The option (keep the refusal) is offered, not verified here. | B6's option, if taken | Low: git makes a reftable repository only when asked |

A relative write after a `cd` to a location the guard cannot tell, by a session the write jail holds whose role may edit a main checkout (a coordinator or pm mark), is refused as `unplaced` in every shell (`rules._write_from`, `ps_drive_jail`), as a computed PowerShell target is; it was a silent allow. The same holds for a shell that Start-Process starts in a computed `-WorkingDirectory` (`commands._sp_shell_text`).

### What the pre-push hook does not cover

The pm-data checks above are not made redundant by the pre-push leak check, in four ways:

- It needs the record. The leak stage runs only when the repository's git common dir holds `kit-pm-data.json` (`prepush.leak_refusal`); a clone or copy has none in its own common dir (docs/guard.md "Pre-push").
- It needs the hook. `core.hooksPath` is per-repository config, so a clone may not run the kit's hook, and `--no-verify` skips it.
- It reads the push's own config. The hook runs with the push's environment: `HOME`, `XDG_CONFIG_HOME` or `USERPROFILE` pointing at another folder (or `GIT_CONFIG_GLOBAL` at another file, B10) moves the `.gitconfig` that `git remote get-url --push` reads. INFERENCE, not tested: a `url.<public>.pushInsteadOf = <private>` there makes the public URL count as the record's own push URL, so the leak stage lets the data branch through.
- owner-only commits is no leak check: it checks commits that touch owner-only paths, by author, and does not look at the data branch.

That these checks defend only against a deliberate evader is INFERENCE; nothing shows an agent could not take the steps by mistake while working around another refusal.

### Withdrawn weakenings: kept, so no go is needed

- **W1.** git-hooks-path's `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_KEY_<n>` forms (`HOOKS_ENV`), which hold the owner's own session too.
- **W2.** owner-identity's environment forms, `Set-Item env:` among them, read through the whole `env_assignments`.
- **W3.** hub-secrets' inline-code check (`rules.inline_code`, `hub.INLINE_RE`): python or node code on the line naming a token file or the token folder (`hub.py` and `hubsite` are not secrets). A parsed `tools/hub.py` agent-verb run piped into code given on the command line is that code's data, so a token file's name in its words is not read (`rules.hub_run_verb`, `inline_code`'s `skip_pipe`); piped into code read from standard input it is still read.
- **W4.** hub-secrets' read-tool rules (`rules.hub_secret_read`; `hub.glob_reaches` and its neighbours).
- **W5.** Hub and lock calls the parser cannot read. A marked session gets write-jail on an unreadable line only when it holds `git`, so v2 adds name patterns to the fallback for every session but the owner's own, the plain one included: an unreadable line naming the hub tool (`hub.py`, `tools.hub`, `-m hub`, `kit hub`) is refused as `hub-owner-verb`, and one naming the lock CLI (`machine_lock.py`, `tools.machine_lock`, `kit machine_lock`) as `owner-tool`. The encoded-command gap is unchanged: `powershell -EncodedCommand` is not decoded, so its text names nothing.
- **W6.** The remote-helper list (`rules.hookless_sub`): `http-push`, `remote-*` and send-pack.
- **W7.** git-remote-rewrite, whole (`rules.git_remote_rewrite`).
- **W8.** A HOME-like variable set for `git push` in the plain forms (`home_config`).

### Option not taken

- **O1.** Count `python -c` as a read. It would cut the same code and every refusal of its class, but lets a `python -c` write into a main checkout. It would need the owner's go.

### Known limits

- **Backlog roles are a speed bump, not a credential.** The guard's backlog roles (docs/guard.md "Backlog roles") and the tool's own role check read a role the session states, so they hold an honest agent to its role; a write boundary an agent cannot step around (a credential the writer must hold) stays with its own record.

## Kit tools may be piped

A kit tool's output may be piped or redirected like any program's (`backlog.py list | head`, `doc_index.py ... > <file in worktree>`, `machine_lock.py run --help | head`).

- **wrapped-gate** fires only on the runs that hold the machine or wait on it for minutes, piped, chained into another command, or wrapped:
  - `gate.py` when it runs steps: no mode option (the default steps), `--full`, `--only <steps>`, `land` in any form, and `wait-result`;
  - `machine_lock.py run <...> -- <command>`.

  `gate.py` runs no steps with `-h`/`--help`, `--plan` and `--measure` (`rules.GATE_NOTHING`, `gate_runs_nothing`). These, `machine_lock.py run --help` (`lock_run_help`), and every other verb of either tool may be piped; this is weakening B9. The reason for the rule stays for the runs that hold the machine: to the classifier, a piped gate reads as something else (refusal pattern P16).
- **Every other kit tool** has no pipe rule. A parsed `hub.py` read verb, or a lock CLI read verb, may be piped or chained. A hub owner verb or a lock owner write is refused in any parsed form, and any hub or lock call on a line the parser cannot read is refused (W5).
- **Redirects** of a kit tool's output are judged by the write rules on the target, as for any program. A literal path in the worktree or temp passes.
- Rule order: wrapped-gate stays before heavy.
- Tests: `tests/guard/test_kit_tool_forms.py`.

## Refusal log

Every deny the guard's hook answers adds one JSON line to `<pm_data>/audit/refusals.jsonl` (`kitlib/guard/refusal_log.py`), so the owner can count which rules refuse which roles without asking an agent to keep a log. docs/guard.md "Refusal log" is the reference for what the code does: who writes it (`kitlib.host.hookmain.run` for every host; on Codex also the broker in `hosts/codex/adapter/runtime.py` `evaluate_bound`), the fields, what a line never holds (the reason, command, path, working directory or Instead text), the lock, the best-effort rule and the limits.

Design points that are not code facts:

- The hook writes the line after its answer text and before it exits. The host reads the answer only when the hook exits, so the log's time (up to `refusal_log.BOUND`, about 1.55 s) is added to the hook's.
- Appends hold an OS lock (`kitlib.fs.treelock`) on `.refusals.lock` beside the log. Nothing on the log's way starts a process, so a slow or hung git cannot hold a deny up; `kitlib.fs.writelock` would ask git where the checkout is first.
- The `repo` field logs `-` for any layout but `<checkout>/.git`, for a checkout that is the user-profile folder and for one whose name equals `USERNAME` (ignoring case). Nothing else is screened, so a folder name that merely contains the account name logs as it is. The log stays on this machine and is never committed.
- The folder is the backlog audit log's (docs/backlog.md "Audit log"), made on the first write; the guard refuses agents' own edits there (`pm-data-audit`), and the hook's own append is not a tool call.
- Only the kit guard's denies are logged: not the host's own permission system or classifier, an ask the owner then declines, or a kit tool's own REFUSED exit. No kit tool reads or tallies the log yet. A block before kitlib loads (`boot/hook.py`, `boot/kitboot.py`) writes no line.
- Tests: `tests/guard/test_refusal_log.py`, `tests/host/test_hookmain.py` `RefusalLog`, `tests/codex/test_runtime.py` `BoundGuard`.
