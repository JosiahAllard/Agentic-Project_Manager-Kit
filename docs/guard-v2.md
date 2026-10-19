# Guard v2: keep, cut, target size

Design only, with no code changes. The owner rules on it one check at a time: "Checks this design weakens or removes" (below) lists every check the design weakens, one row each, and he answers yes or no per row. A row he refuses stays as it is today, and its lines come back out of the cut. Rows outside that section change no protection, so they need no separate go.

## Sources

- Code: `kitlib/guard` at commit `35741041` (main as merged into this branch), **14,479 lines** in 15 files:

  | File | Lines |
  |---|---|
  | rules.py | 7,280 |
  | commands.py | 2,028 |
  | rolepolicy.py | 1,292 |
  | shell.py | 1,238 |
  | hub.py | 466 |
  | prepush.py | 444 |
  | context.py | 394 |
  | place.py | 351 |
  | ownercommits.py | 303 |
  | fallback.py | 297 |
  | jail.py | 222 |
  | generated.py | 73 |
  | gitread.py | 44 |
  | paths.py | 31 |
  | `__init__.py` | 16 |

  Tests: `tests/guard`, 42 files, 15,480 lines, at the same commit.
- Refusals **[out of repo]**: two logs, read by their tag `[kit guard: rule=<rule> role=<role>]`. Neither is in this repository, so every `L<line>` and `D<line>` below, and the whole Tally, rests on files a reader of this repository cannot check.
  - The PM's cross-repo log (`docs/refusals.md` in the PM repo, kind H rows), cited as `L<line>`.
  - The kit coordinator's own log for one day of work (in the agent temp folder), cited as `D<line>`.

  The guard keeps no log of its own (an open record asks for one). So "absent" below means "absent from the two refusal logs read", not "never fired".
- The rules table: docs/guard.md "Rules". `guard.md:<n>` is a line of docs/guard.md at the same commit.
- Code is cited as `<file>:<line>`, the line a function or table starts on. A range's ends may be off by a comment line or two.

## Method

Each refusal is put in one class:

- **real**: the call would have done what the rule exists to stop (a write outside the jail, a heavy run with no lock, an owner verb).
- **form**: the target was safe, but the call was written so the guard could not see that (a computed path, a computed `-C`, a chained or wrapped call). The refusal is right under the guard's contract; the agent rewrites the call.
- **false positive**: a safe call written plainly was refused (prose that names a word, a read-only git verb, a literal worktree path judged by the shell's directory).
- **absent**: no refusal in either log read.

A rule earns its code by real refusals. Form refusals argue for a short, plain rule with good advice, not a cleverer reader. False positives are cost. An absent rule is kept when it is small and its hazard is one mistake away (one `git reset --hard`). An absent rule that guards the owner's powers or what leaves the machine is not cut on that ground alone: its rows in "Checks this design weakens or removes" ask the owner.

## Tally [out of repo]

| Rule | Real | Form | False positive | Total |
|---|---|---|---|---|
| main-checkout | 6 | 45 | 44 | 95 |
| write-jail | 21 | 26 | 7 | 54 |
| hub-owner-verb, hub-secrets | 9 | 12 | 22 | 43 |
| unreadable | 0 | 22 | 0 | 22 |
| busy-wait | 15 | 0 | 0 | 15 |
| wrapped-gate | 8 | 0 | 1 | 9 |
| role-lock | 1 | 0 | 8 | 9 |
| root-search | 5 | 0 | 4 (old hook) | 9 |
| git-worktree-remove | 8 | 0 | 0 | 8 |
| git main-checkout rules, git-push-main, git-hooks-path | 9 | 0 | 1 | 10 |
| kit-json | 6 | 0 | 0 | 6 |
| heavy | 5 | 0 | 0 | 5 |
| kill-by-name, pm-data, role-shell | 1 | 1 | 1 | 3 |

write-jail's 7 false positives are L177 (a config folder, in its own row below) and the six `python -c` refusals by the shell's directory listed in the python row (L180, L291, L294, L320, L369, L548). The earlier draft said 4 and a total of 51.

Two rules carry most of the cost: main-checkout's `python -c` counting (36 false positives) and the hub rules' text matching (22). Both read the command's text where they should read the program it runs.

## Rules and areas

Line ranges are in `kitlib/guard/rules.py` unless another file is named.
- "keep": kept as is or with a small advice fix.
- "cut": removed, or cut down to the size given.
- "merge": folded into another rule.

### Kept: they catch real hazards

| Area | Where | Lines | Refusals [out of repo] | Verdict | Reason |
|---|---|---|---|---|---|
| decide, judge, shell_command, refuse/tag helpers | 1-277 | 277 | all | keep | The dispatcher. Its order stays: the lock first, edits to edit_target, shell calls per command, then rolepolicy. |
| agent_session | 331-351 | 21 | n/a | keep | Every agent-only rule uses it. |
| write-jail (session mark, roots, refusal, worktree alternative) | 2209-2297; jail.py | 89 + 222 | real: L162, L178, L179, L228, L231, L233, L234, L299, L319, L364, L373, L453, L484, L505, L506, L540, L541, L543, L550, L553, D6; false positive: L177 (a config folder) | keep | The rule with the most real catches. |
| git_jail, git_jail_unknown, git_in_worktree | 5425-5510 | 86 | as write-jail; form (computed `-C` on reads): L187, L189, L190, L201, L216, L218, L250, L305, L311, L324, L336, L341, L342, L356, L359, L379, L380, L430, L441, L472 | keep | A computed `-C` stays refused because the guard cannot tell the checkout. The advice names the literal form. |
| main-checkout for file tools and literal shell writes | 2109-2171; write_targets 3648-3789; shell_writes, write_place 4104-4213 | 63 + 142 + 110 | real: L192, L244, L287, L330; form (non-literal target): L182, L188, L194, L198, L202, L204, L207, L224, L260, L262, L290, L292, L302, L306, L313, L358, L367, L407, L411, L420, L431, L434, L437, L438, L440, L503, L514, L522, L530, L531, L534, L537, L560, L563, L564, L590, L591, D7, D10; false positive: L492, L493 (`commit.py -C <worktree>` judged by the shell's directory) | keep, fix | Non-literal targets are the commonest refusal, and all are agent form errors, so the rule is right. Keep "a target the guard cannot place is refused outside a worktree". Fix: a kit tool's `-C` names where it writes. |
| Unix writer options (sed -i, perl -i, curl -o, wget, patch, rsync), tar, unzip, copiers, find -exec | 2927-3249; 4214-4257 | 323 + 44 | false positive: L315 (`tar -xO`) | keep, fix | Small, table-like code. Treat `tar -xO`/`--to-stdout` as a read. |
| CopyArgs (cp/mv/Copy-Item into a folder) | 3790-3958 | 169 | none of its own | keep | Needed to place copy targets. |
| busy-wait | 6452-6731 | 280 | real: L163, L176, L203, L212, L226, L265, L303, L335, L365, L366, L381, L390, L463, L473, L566 | keep | 15 of 15 real. The bounded-loop advice works. |
| wrapped-gate (P16) | 4376-4449 | 74 | real: L157, L199, L210, L246, L337, L400, L433, L443; false positive: L567 (`--help` piped) | keep, narrow | See "Kit tools may be piped" below; the narrowing is weakening B9. |
| heavy, kit_wrapper | 4582-4671 | 90 | real: L197, L223, L406, L574, L591 | keep | Every catch real. No change to kit_wrapper: it already resolves the script word against the command's own directory (`cmd.cwd`, the directory after any `cd` on the line, else the session's; rules.py:4637), follows links, and treats a script it cannot resolve, or a variable word, as not the kit's, so `heavy` judges that call. The earlier draft's "not `cmd.cwd`" is withdrawn. |
| root-search | 4450-4581 | 132 | real: L81, L95, L96, L132, L407; old-hook false positives: L82, L88, L133, L152 (fixed since) | keep | Real catches. The false positives predate the current rule. |
| git main-checkout rules (checkout, reset --hard, clean, stash, bulk-add, commit-all, restore, main-history), writes_main | 4672-4894 | 223 | real: L168, L254 (main-history), L227, L229 (commit-all), L295, L345 (bulk-add), L235 (restore); false positive: L360 (checkout in a scratch clone) | keep | Small, and each guards against one mistake that loses main's work. reset-hard, clean and stash are absent but cost little. |
| git-push-main, landing_command | 6335-6451 | 117 | real: L159 | keep | Guards main on every repo. |
| git-worktree-remove, git_branch_delete, git_unplaced | 5781-5846 | 66 | real: L171, L173, L211, L225, L482, L538, L542, L565 | keep | Each refusal stopped a remove of an unclean or unmerged tree. |
| lander checks | 5329-5424 | 96 | none of its own | keep | Part of the landing path. |
| kit-json | 2172-2186 | 15 | real: L205, L217, L338, L339, L340, L401 | keep | Small, with real catches. |
| kill-by-name | 7098-7226 | 129 | real: L298 | keep | Killing by name can end another session's process. |
| ledger, lock-state, generated-file, pm-data availability | 1977-2108; 2187-2208; generated.py | 132 + 22 + 73 | L321 (pm-data fails closed when the record does not read) | keep | Small path checks over files that only tools write. lock-state and generated-file are absent; they are kept because they are a few lines each. |
| owner-identity | 7227-7280 | 54 | absent | keep | Stops a commit under the owner's address. It reads assignments through `commands.env_assignments` (commands.py:963, called from rules.py:7238), which the environment cut keeps whole for it. |
| git-hooks-path | 6818-7097 | 280 | real: L481 | keep | It holds every session, the owner's own included (guard.md:125). Most of its lines are the shared config reading (`config_args` 6881, `git_config_call` 6942, `git_prefix_configs` 6965, `clone_configs` 7006, `_config_files` 7026) that owner-identity and git-remote-rewrite also use, so a cut saves little. Its `GIT_CONFIG_PARAMETERS`/`KEY_<n>` forms (`HOOKS_ENV` 7041) cost about six lines over `env_assignments`. The earlier draft cut them; this revision keeps them (withdrawn weakening W1). |
| pre-push hook: leak stage, owner-only commits | prepush.py, ownercommits.py | 444 + 303 | git hooks, so not in the logs | keep | The last check on what leaves the machine. They do not cover what the pm-data cut drops: see "What the pre-push hook does not cover" below. |
| place, context, paths | place.py, context.py, paths.py | 351 + 394 + 31 | n/a | keep | Shared plumbing. context.py should also check `ledger_dir` (an open record). |
| shell and command parser | shell.py; commands.py outside 953-1757 | 1,238 + 1,223 | n/a | keep | Every shell rule reads its output. It shrinks after the cuts by the code only cut rules used; that is not counted in the target. |
| rolepolicy: mode, check, screen, role-shell | rolepolicy.py 1-424, 1191-1292 | 526 | role-shell: L549 (a probe, over-refused) | keep | Read roles and their data roots. role-edit and role-workdir are absent; they are a few lines inside check. |

### Cut or merged

Each "Keep" list is what v2 still refuses. What a cut stops refusing is in "Checks this design weakens or removes", by the row number given.

| Area | Where | Lines | Refusals [out of repo] | Verdict | Size after | What v2 keeps, and what goes |
|---|---|---|---|---|---|---|
| hub text matching: hub-owner-verb (path, family, shape) and hub-secrets on shell text | 601-1976; hub.py | 1,376 + 466 | real (an owner verb, or a reach into the token folder): L427, L478, L479, L517, L523, L544, L586, L588, L597 (also D5); form (chained or computed): L568, L569, L595, D3; agent shell reads, which the owner barred: L486, L501, L508, L520, L575, L587; false positive (prose, log rows, heredoc bodies, grep words, a path in another tool's argument): L409, L410, L413, L416, L426, L436, L449, L464, L465, L466, L470, L518, L521, L545, L571, L581, L585, L592, L593, L594, D12; broad: L494, L498, L507 | cut | ~850 | Judge the program run, not the text. Keep: (1) an owner verb on a parsed `tools/hub.py` or `-m tools.hub` run (`hub.owner_verb`, kitlib/guard/hub.py:279; `OWNER_VERBS` 245, `todo_add_owner` 338); (2) a literal path, in any word, that resolves into the token folder or the hub store; (3) the inline-code check (rules.py `inline_code` 1814, hub.py `INLINE_RE` 69), its pipe exemption re-based on a parsed `tools/hub.py` agent-verb run instead of `hub_shape` (W3); (4) the read-tool rules (rules.py `hub_secret_read` 837; hub.py `glob_reaches` 113, `list_reaches` 129, `expand_braces` 154, `filter_reaches` 178) (W4); (5) hub_mcp and hub-browser (rules.py 1953-1976; hub.py 361-466); (6) the fallback's new hub pattern for lines the parser cannot read (W5). Goes: the fail-closed naming read (weakening A1) and the name-only token words (A2). Fixes 22 false positives and folds five open records. |
| role-lock text machinery | rolepolicy.py 425-1190 | 766 | false positive: L408, L415, L428, L489, L509, L515, L527, L546; real: L552 | cut | ~120 now, 0 later | Keep: a parsed run of the lock CLI with a verb outside the role's list, and the lock MCP tools by verb. Goes: the fail-closed hand-on reading (B5). Once the lock checks its caller itself (an open record), the rest goes. |
| python program proving (py_program, py_reads_only, _python, sleep_wait, py_unread) | 3250-3508; 4035-4103 | 259 + 69 | false positive in substance (`python -c` that only read, run from the main checkout): L191, L206, L208, L209, L220, L258, L274, L300, L318, L329, L331, L332, L346, L352 to L357, L362, L363, L374, L382, L386, L394, L419, L421, L442, L444, L446, L510, L554, L562, L577, D8, D15; sleep-waits: L468, L504, L535, L572, L573, L584; write-jail by the shell's directory: L180, L291, L294, L320, L369, L548 | cut | ~30 | Keep the plain rule: a `python -c`, or python reading code on stdin, counts as a write of the directory it runs in. Drop the prover (`-I`, the module allowlist, the AST walk) that let some through: stricter, so not a weakening. The advice becomes one line: `cd "<worktree>" && python -c ...`, or the Read and Grep tools, and must not tell a worker already in a worktree to add one. The 36 refusals become form refusals whose advice works. The other way, counting `python -c` as a read, is not taken (O1). |
| PowerShell write reading: PSCmdlet, NetWrite, ps_unread, ps_line_plain, ps_pwd_path, dotnet_unplaced, dotnet_process_dir, ps_drive_jail | 2298-2926; 3509-3647; 3959-4034; 4258-4375 | 629 + 139 + 76 + 118 | false positive (literal worktree path judged by the shell's directory): L195, L243, L376, L378, L397; real (computed .NET path): L557, L576 | cut | ~430 | Keep: one table of writer cmdlets and their path parameters; the .NET `File`/`Directory` write methods and the `StreamWriter`/`FileStream` constructors (`::new`, `New-Object`) by their first argument. A literal target is judged where it points, never by the shell's directory. Any other target (computed, piped in, a provider path, a relative .NET path, whose base is the process's directory) is not placed, so it is refused outside a worktree: stricter than `dotnet_process_dir`. Goes: the `StreamWriter` cast form (B7). A `FileStream` opened only to read counts as a write: stricter. Folds three open PowerShell records. |
| environment reading (env-unsafe: PS env provider, bash builtins that assign, double-quoted scanning) | commands.py 953-1757 | 805 | none of its own (they count under the rules above); open false positives: Remove-Item Env:X, double-quoted text, a `GIT_INDEX_FILE` temp index | cut | ~220 | Keep whole: `commands.env_assignments` (963, with `_assigned` 953, `PS_ENV_SET_RE` 923, `NET_SET_ENV_RE` 930, `PS_ENV_SETTERS` 934), which reads the prefix and `env` forms, `export`/`declare`/`typeset`/`local`/`readonly`/`set`/`setx`, `$env:X =`, `SetEnvironmentVariable`, and `Set-Item`/`New-Item`/`Set-Content`/`Add-Content` on `env:`; `git_env_set` 1032, `GIT_SAFE_NAMES` 1075, `line_env_names` 1190, `mark_git_env` 1720, `HOME_ENV_NAMES` 1752. owner-identity, git-hooks-path and home_config read through these. Goes: `read`, `printf -v`, `for`, `mapfile`, `getopts`, `select`, `trap` and arithmetic assignment (`arith_targets` 1245, `bash_var_targets` 1268), cmd `path`/`set` parsing (`cmd_path_set` 1214), the env provider's other item cmdlets (`_ps_env_item_names` 1343), and double-quoted text scanning (B3, B4). |
| pm-data evasion checks: git alias expansion, HOME config, send-pack/remote helpers, remote and config rewrite, data copies | 5847-6334; gitread.py | 488 + 44 | absent | cut | ~295 | Keep: `push --no-verify` and send-pack in a repository with `kit-pm-data.json` (`git_push_pmdata` 5899, `git_send_pack_pmdata` 5968), the remote-helper list (`hookless_sub` 5961, W6), `call_subexpr_pmdata` 5868, HOME-like variables set for `git push` in the plain forms `env_assignments` reads (`home_config` 5945, W8), and `git_remote_rewrite` 6293 whole (W7). Goes: alias expansion through `git config --get` (`git_alias_config` 6106, `split_alias` 6074, gitread.py; B1), copy and shallow-copy detection with its unreadable-record fail-closed (`data_copy` 5999; B2), and the HOME forms the environment cut drops (B3). The earlier draft's reason, that the pre-push leak check takes over, was wrong: see below. |
| git-shared-ref | 5511-5780 | 270 | absent | cut | ~70 | Keep: an agent `update-ref` of anything but `refs/tmp/*` and per-worktree refs is refused. Goes: symbolic-ref chain walking (`_ref_chain` 5646; B6), and the reftable and link checks. The scratch-repo exemption (`_scratch_repo` 5602) goes too: stricter. |
| git read classification (pager, dynamic reads, archive) | 4895-5328 | 434 | false positive: L296 (archive), L511 (patch-id), L512 (apply --check); real: D6 (merge-tree --write-tree) | cut | ~200 | See "Read-only git and `--`" below. Not a weakening: a git line with any `-c`, `--config-env`, `--exec-path`, or a `GIT_*`, `PATH` or `HOME` assignment is not a read, which is stricter than `archive_filter_set` 5097 and `strict_dyn_read` 5061, and `native_unsure` 4990 stays. |
| owner-lock and owner-setup | 360-600 | 241 | absent as such; owner-setup misfired on heredoc text (an open record) | merge | ~100 | One `owner-tool` rule over a parsed invocation: reserve_ui as a program, python's script, a launcher's target or an opened file (guard.md:342, :343, :345); the lock CLI's owner writes (guard.md:350-352); machine setup with `--apply` (guard.md:372). Goes: the code-text and standard-input reading (A3). |
| heredoc-winpath | 6732-6817 | 86 | absent | cut | 0 | B8. A quoting trap, not a hazard. The shell-traps skill already says to write scripts with the Write tool. |
| unreadable (fallback) | fallback.py | 297 | form: L164, L169, L172, L193, L253, L261, L264, L297, L304, L349, L371, L372, L384, L385, L393, L432, L450, L469, L496, L555, L570, L578 | merge | ~260 | Stays the one place for what the parser cannot read. Adds one name pattern (W5). `pwsh -File -` (a script on stdin) counts as unreadable: stricter. Drops the `plainly` rewrite attempts (fallback.py:160-205) beyond the temp-script advice: advice only. |
| lock-mcp | 278-330 | 53 | absent | cut later | 0 | Goes once the lock MCP loses its write tools (an open record); until then it stays. Not a weakening when it goes: the MCP will have no owner tool left to hold. |

### What the pre-push hook does not cover

The earlier draft cut the pm-data checks because "the pre-push leak check judges every push that runs hooks". That is false, in four ways:

- **It needs the record.** The leak stage runs only when the repository's git common dir holds `kit-pm-data.json`; with no record, `prepush.leak_refusal` (prepush.py:227) checks nothing (guard.md:439). A clone or copy of a recorded repository has no record in its own common dir.
- **It needs the hook.** `core.hooksPath` is per-repository config, so a clone may not run the kit's hook at all, and `--no-verify` skips it (guard.md:448).
- **It reads the push's own config.** The hook runs with the push's environment. `_config_env` (prepush.py:88) drops only the `GIT_CONFIG_*` names, so when `HOME`, `XDG_CONFIG_HOME` or `USERPROFILE` points at another folder, `git remote get-url --push` in the hook (prepush.py:105) reads that folder's `.gitconfig`. INFERENCE, not tested: a `url.<public>.pushInsteadOf = <private>` there makes the public URL count as the record's own push URL (`_private`, prepush.py:145), so the leak stage lets the data branch through.
- **owner-only commits is no leak check.** ownercommits.py checks commits that touch owner-only paths by their author. It does not look at the data branch.

So every pm-data check this design cuts has no other cover; each is a row in group B below. The earlier draft also called them a defence "against a deliberate evader, not a mistake". That is INFERENCE: each form takes more than one step (an alias set first, a second clone, a moved HOME), and none is in the two refusal logs read, but nothing shows an agent could not take those steps by mistake while working around another refusal.

### Read-only git and `--`

The read classification becomes one table of read-only subcommands, with the options that make each one write (`archive -o`, `merge-tree --write-tree`, `diff --output`). The table adds patch-id, archive to stdout, and apply --check/--stat.

It reads options only before a `--`, and a read flag counts only as the subcommand's own option.
- Today `config` counts as a read when `-l`/`--list` or any `--get*` appears anywhere in its words, even after `--` (`git_reads` 5245). The read-only git fix that has since landed leaves this unchanged.
- That fix does make `apply_checks` (5283) read `apply`'s options only before the first `--`.

A review note, not yet confirmed: Windows PowerShell 5.1 may drop a bare `--` before a native program sees it.
- If so, `git apply --check -- --apply x` reads as a check, while git receives `--apply`.
- Every `--` scan in the guard assumes the shell passes `--` through.
- So the table step first tests what PowerShell 5.1 passes. On PowerShell, it treats a `--` the way the program will receive it.

## Checks this design weakens or removes

One row per weakening. Each row gives what is caught today (a docs/guard.md line and the code), what stops being caught, what else covers it, and the risk. Every row needs the owner's go; a "no" keeps today's check, and its lines stay. Where a row offers an option, the owner answers the row with the option or without it. The options together add back at most about 150 lines; the target below counts none of them.

### Group A: the owner's own session and the owner's powers

Checks that hold in the owner's own session, or that keep the owner's own verbs (Action Hub answers, the machine lock, machine setup) away from agents. The one check this design touched that holds the owner's own session, the `GIT_CONFIG_PARAMETERS`/`KEY_<n>` forms of `core.hooksPath` (guard.md:125), is now kept (W1), so no row here weakens the owner's own session; all three rows weaken what keeps agents away from the owner's powers.

| # | Check | Caught today | Stops being caught | Other cover | Risk | Owner's go |
|---|---|---|---|---|---|---|
| A1 | hub-owner-verb: the fail-closed naming read becomes a parsed-run check | Any line that names the hub tool outside the one shape the guard reads in full: guard.md:121, :400, :430; rules.py `hub_named` 1593, `hub_text_names` 936, `hub_module_names` 958, `hub_word_module_names` 1002, `hub_crt_words` 1041, `hub_glob_names` 1127, `hub_code_names` 1467, `hub_shape` 1200 | An owner verb reached any way but a parsed `python tools/hub.py <verb>` or `python -m tools.hub <verb>`: `python -c "from tools import hub; hub.main(['questions', 'answer', ...])"`; an import of hub in a heredoc, here-string or piped code; `import_module('hub')`, `run_module`, `runpy`; a wildcard (`tools/h*.py`); a word PowerShell 5.1 splits into `-m hub` at a quote; a python word with a path before it or text glued to it; a copy of the tool under another name | Little. The hub server answers owner verbs only to the owner token, but `tools/hub.py` reads `owner.token` itself and posts owner verbs with it (`_owner_token`, tools/hub.py:793; `_answer` posts as `client.OWNER`, tools/hub.py:997), so code that imports it acts as the owner. Kept: hub-secrets on a literal token path (does not see this case: the code names no token file); the fallback's hub pattern (W5), only for lines the parser cannot read | High: an agent answers the owner's questions, marks his to-dos done or acts as him on any owner verb (`hub.OWNER_VERBS`, kitlib/guard/hub.py:245) | Needed. Option, recommended (~15 lines): also refuse python code (`-c`, a heredoc, a here-string, a pipe) that imports hub or names `tools.hub` or `hub.py`, read only in python's code, not the whole line. With it, what stops being caught is the wildcard, the PowerShell quote split, the pathed or glued python word, a module name built from pieces, and a copy |
| A2 | hub-secrets: token-file names in words the guard cannot place | guard.md:388; rules.py `hub_secret_shell` 1858, `hub_name` 1636; kitlib/guard/hub.py `FOLDER_RE` 67 | A shell word whose last part is a token file (`owner.token`, `agent.token`, `approval.key`) when its folder is not literal (`cat $d/owner.token`), and the token folder named inside a longer word | Kept: a literal path that resolves into the token folder (any word); the inline-code check for python and node code (W3); the Read, Grep and Glob rules (W4) | Medium-high: `owner.token` is every owner power over HTTP | Needed. Option, recommended (~10 lines): keep refusing a word that is not literal whose last part is a token file's name. With it, only the folder inside a longer word stops being caught |
| A3 | owner-lock and owner-setup: code-text and standard-input reading | guard.md:344 (reserve_ui on python's stdin), :346 (`python -c` with `reserve_ui` or `kitlib.lockui`; `-m runpy`; a module named by a variable), :347 (a program named by a variable on a line naming reserve_ui), :373 (a `.ps1` quote), :374 (a setup program named by a variable), :375 (`python -c` or stdin importing `kitlib.machinesetup`); rules.py `RESERVE_UI_RE` 360, `runs_reserve_ui` 404, `owner_lock` 499, `owner_setup` 580 | Each form listed: an agent starts the reserve UI or the lock window's package from python code or stdin, or runs machine setup's apply from python code, stdin or a variable | The lock's own owner check (no agent-host ancestor, kitlib/lock/api.py; guard.md:339), which a detached process can forge (guard.md:339); both setup scripts refuse the `.ps1` quote themselves (guard.md:373); the fallback's setup pattern (guard.md:377) and its new lock pattern (W5), only for lines the parser cannot read | Medium: an agent holds the whole machine, or stores its own address as `owner_email`, which decides whose commits count as the owner's (guard.md:370) | Needed. Option, recommended (~15 lines): keep refusing python code (`-c`, stdin) on a line naming `reserve_ui`, `kitlib.lockui` or `kitlib.machinesetup`. With it, what stops being caught is `-m runpy` and a module or program named by a variable |

### Group B: agent sessions

Checks that hold agent sessions only (a subagent, the marked sessions, the plain session where the rule says so); the owner's own session is not held by any of them today.

| # | Check | Caught today | Stops being caught | Other cover | Risk | Owner's go |
|---|---|---|---|---|---|---|
| B1 | git-push-pmdata (and git-hooks-path, git-remote-rewrite) through a git alias | guard.md:112; rules.py `git_alias_config` 6106, `split_alias` 6074, `GIT_BUILTINS` 6044; gitread.py | In a repository with `kit-pm-data.json`: `git pu --no-verify` where `alias.pu = push`, a `!` shell alias, and `core.hooksPath` or a remote rewritten through an alias. The alias route to hooks-path skips the owner's session (`git_alias_config`), so this is an agent-session loss only | None: `--no-verify` skips the pre-push hook (guard.md:448) | Medium: the data branch reaches a public remote. INFERENCE: it takes an alias written first | Needed. Option, recommended (~10 lines): in a repository with the record, refuse an agent's git whose subcommand is not a git builtin (`GIT_BUILTINS`), with no config read. With it, nothing on this row stops being caught; it refuses every alias there |
| B2 | git-push-pmdata: pushes from a copy of a recorded repository | guard.md:112 (its last paragraph); rules.py `data_copy` 5999, and its fail-closed when the record cannot be read; gitread.py | A push from a clone, a shallow clone or a copied folder of a repository with the record (`git clone <repo> <temp>/c` then a push from there); a record that cannot be read no longer fails closed | None: the copy's common dir has no record, so the leak stage checks nothing (prepush.py:227, guard.md:439), and the copy may not run the hook at all ("What the pre-push hook does not cover") | Medium-high: the data branch reaches a public remote with no check at all | Needed. Option: keep `data_copy` and gitread.py as they are (~85 lines) |
| B3 | git-push-pmdata: HOME-like variables set by unusual forms | guard.md:112; commands.py `arith_targets` 1245, `bash_var_targets` 1268; rules.py `home_config` 5945 | `HOME`, `XDG_CONFIG_HOME` or `USERPROFILE` set for `git push` by `read`, `printf -v`, `for HOME in`, `mapfile`, `getopts`, `select`, arithmetic or a computed name. The plain forms stay caught (W8) | None: the hook reads the same moved config (prepush.py:88, :105) | Medium: INFERENCE, a `pushInsteadOf` in the moved config passes the data branch to a public remote ("What the pre-push hook does not cover") | Needed. A hook-side fix, reading the config with those variables reset to the machine's own, would cover every form; it is a separate record, not part of this design |
| B4 | write-jail and main-checkout for git: unusual environment forms | guard.md:193; commands.py `git_env_unsafe` 1377, `_bash_env_unsafe` 1431, `_ps_env_unsafe` 1581, `_ps_item_unsafe` 1673, `cmd_path_set` 1214 | `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or `GIT_COMMON_DIR` set by the same unusual forms as B3, by cmd's `set` with unusual delimiters, by the env provider's rename, copy and move cmdlets, or inside double-quoted text, so git writes a checkout the guard did not place | Kept: every form `env_assignments` reads (prefix, `env`, the exporters, `$env:X =`, `SetEnvironmentVariable`, `Set-Item` and the like on `env:`); write-jail's `git` word on unreadable lines (fallback.py:272-277) | Low-medium: git writes in a main checkout or another agent's worktree; it takes a deliberate unusual form | Needed |
| B5 | role-lock: the fail-closed hand-on reading | guard.md:242; rolepolicy.py `_ps_caught` 613, `_last_words` 700, `_lock_seq` 834, `_launched` 1015, `_hidden_launch` 1064, `NO_PROGRAM` 1108, `HANDED_ON` 1112 | In a role session (`read`, `test`, `lander`, `write`, `controller`), a lock verb outside the role's list reached any way but a parsed run of the lock CLI: the path handed on by a variable, a copy, `$_`, `$$`, `set --`, `read`, `xargs`, standard input, a launcher the guard does not unwrap, or `runpy` | Kept: owner-lock on the parsed owner writes in every agent session; the lock's own owner check (guard.md:339); the lock's caller check, an open record, would replace this rule | Low-medium: a role session ends another's run or takes a hold its role does not have | Needed |
| B6 | git-shared-ref: symbolic-ref chains | guard.md:108; rules.py `_ref_chain` 5646, `git_shared_ref` 5721 | `git update-ref <name> <sha>` where `<name>` is an allowed ref (`refs/tmp/*`, a per-worktree ref) that is a symbolic ref to a shared one such as `refs/heads/main`: git follows it unless `--no-deref` | The pre-push fast-forward check on the remote's main only (prepush.py `not_fast_forward` 383); nothing for the local main | Low-medium: the local main moves to any commit | Needed. Option (~5 lines): refuse an agent's `update-ref` without `--no-deref`. With it, nothing on this row stops being caught |
| B7 | PowerShell: the `StreamWriter` cast | guard.md:135; rules.py `_net_cast` 2796 | A write through a cast (`[IO.StreamWriter]'<path>'`) to a main checkout or outside the jail. The constructors stay caught (`_net_ctor` 2735's job moves to the .NET table) | None for the cast form | Low: an unusual spelling | Needed |
| B8 | heredoc-winpath | guard.md:124; rules.py `heredoc_winpath` 6787 | python run from a bash heredoc in which a string that is not raw holds a one-backslash drive path (`C:\Users`), which python's escapes change silently | The shell-traps skill's advice to write scripts with the Write tool; the write rules still judge where the script writes | Low: a slip that writes to the wrong path, not past the jail | Needed |
| B9 | wrapped-gate: narrowed to the runs that hold the machine | guard.md:95; rules.py `wrapped_gate` 4426 | Piping or wrapping `gate.py -h`/`--help`, `--plan` and `--measure`, and every lock CLI verb but `run ... -- <command>` | `heavy` still judges whatever such a pipe feeds; these modes run no gate steps and take no lock ("Kit tools may be piped") | Low: the P16 hazard needs a run that holds the machine | Needed |

### Withdrawn: weakenings in the earlier draft that this revision keeps

The earlier draft would have weakened these. This revision keeps each one, so none needs a go.

- **W1. git-hooks-path's `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_KEY_<n>` forms** (guard.md:125; rules.py `HOOKS_ENV` 7041, `git_hooks_path` 7046), which hold the owner's own session too. About six lines over `env_assignments`, which stays.
- **W2. owner-identity's environment forms, `Set-Item env:` among them** (guard.md:126; rules.py `_owner_identity_use` 7236 over `commands.env_assignments` 963). The environment cut keeps `env_assignments` whole.
- **W3. hub-secrets' inline-code check** (guard.md:394; rules.py `inline_code` 1814; kitlib/guard/hub.py `INLINE_RE` 69): python or node code on the line naming a token file. Kept; its pipe exemption reads a parsed `tools/hub.py` agent-verb run instead of `hub_shape`.
- **W4. hub-secrets' read-tool rules** (guard.md:395-399; rules.py `hub_secret_read` 837; kitlib/guard/hub.py 113-226), which the earlier draft's keep list left out.
- **W5. hub and lock calls the parser cannot read.** The earlier draft sent them to `unreadable`, but that answer denies only a subagent: a marked session gets `write-jail` only when the line holds `git` (fallback.py:272-277), and the owner's and the plain session only for a hidden program in a main checkout (fallback.py:290-292). v2 adds one name pattern to the fallback instead, for every session but the owner's own, the plain one included: an unreadable line naming the hub tool (`hub.py`, `tools.hub`, `-m` then `hub`) is refused as `hub-owner-verb`, and one naming the lock CLI (`machine_lock.py`, `tools.machine_lock`) as `owner-tool`. This keeps today's hub cover for unclean lines, read verbs included, and tightens the lock's: today owner-lock has no fallback pattern (guard.md:365). The encoded-command gap is unchanged: `powershell -EncodedCommand` is not decoded, so its text names nothing (guard.md:422).
- **W6. The remote-helper list** (guard.md:112; rules.py `hookless_sub` 5961): `http-push`, `remote-*` and send-pack, four lines.
- **W7. git-remote-rewrite, whole** (guard.md:113; rules.py `git_remote_rewrite` 6293). Its config reading is the shared code git-hooks-path keeps.
- **W8. A HOME-like variable set for `git push` in the plain forms** (rules.py `home_config` 5945).

### Not a weakening

- `python -c` and python on stdin with no prover (the python row), the git read table refusing any config or environment change on the line (the git read row), `pwsh -File -` as unreadable, a relative .NET path as not placed, a read-only `FileStream` as a write, and the scratch-repo exemption's removal: each refuses more than today.
- The `plainly` rewrite attempts: advice only.
- lock-mcp's removal waits until the lock MCP has no owner tool; role-lock's deletion waits until the lock checks its caller. Each is ruled on when it comes.

### Option not taken

- **O1. Count `python -c` as a read.** It cuts the same code and every one of the 36 refusals, but lets a `python -c` write into a main checkout (guard.md:157; rules.py `py_reads_only` 3400 proves some reads today). This design does not take it. It would be a weakening that needs the owner's go if he wants it.

## Kit tools may be piped

The open record that asked for this is closed into this design. Its ask: a kit tool's output may be piped or redirected like any program's (`backlog.py list | head`, `doc_index.py ... > <file in worktree>`, `machine_lock.py run --help | head`).

- **wrapped-gate** fires only on the runs that hold the machine or wait on it for minutes, piped, chained into another command, or wrapped:
  - `gate.py` when it **runs steps**: no mode option (the default steps), `--full`, `--only <steps>`, `land` in any form (it runs the merged gate; `land --submit` starts that run and returns), and `wait-result` (it waits on a landing's gate) (tools/gate.py `main` 273, `gate_parser`);
  - `machine_lock.py run <...> -- <command>`.

  `gate.py` **runs no steps** with `-h`/`--help`, `--plan` (prints the plan) and `--measure` (reads history and writes a proposal file). These, `machine_lock.py run --help` (already passed by `lock_run_help`, rules.py:4440), and every other verb of either tool may be piped (L567 [out of repo]); this is weakening B9. The P16 reason stays for the runs that hold the machine: to the classifier, a piped gate reads as something else.
- **Every other kit tool** has no pipe rule.
  - Today the hub and role-lock text matchers refuse `hub.py` and the lock CLI when chained, piped or wrapped (D3, D5, L568, L569, L595 [out of repo]).
  - After the hub and role-lock cuts, a parsed `hub.py` read verb, or a lock CLI read verb, may be piped or chained. A hub owner verb or a lock owner write is refused in any parsed form, and any hub or lock call on a line the parser cannot read is refused (W5).
- **Redirects** of a kit tool's output are judged by the write rules on the target, as for any program. A literal path in the worktree or temp passes.
- Rule order is unchanged: wrapped-gate stays before heavy.
- Tests: rows in `tests/guard/test_kit_tool_forms.py` for `--help` piped, `--plan` piped, `backlog.py list | head` and `hub.py questions list | head`, and for the refused `gate.py --full | tail` and `gate.py land <branch> | tail`.

## Open guard-area records [out of repo]

The owner asked for every open guard-area backlog record in every repository, each marked keep or close with a reason. Every repository's backlog store was read: the kit's store and each game repository's own store. Only the kit's store holds guard records.

That list names records, and record ids are owner-specific text that the scrub tests bar from tracked files. So it is kept outside the repository and handed to the owner with this doc. Nothing below can be checked from this repository.

In summary:
- **Close: 21.** Each is superseded by a cut above, and its fix is done inside that cut:
  - five false-positive records, by the hub cut (5);
  - three PowerShell records, by the PowerShell cut (3);
  - three by the environment cut (3);
  - three role-lock records (3);
  - the stdin-script gap, by the unreadable merge (1);
  - the workdir follow-ups, the main-checkout test rows and the worktree-add advice, by the prover step (3);
  - the tar read, by the "keep, fix" row (1);
  - the known-limits list, which v2's docs/guard.md takes over (1);
  - one proposed new text rule over backlog calls, whose check belongs inside `backlog.py` (1).

  The earlier draft's heading said 19 over these same 21 items. The list outside the repository is the count of record; whoever hands it to the owner checks that it holds 21.
- **Keep: 11.**
  - the lock's own caller check, and the lock MCP losing its write tools: these let role-lock and lock-mcp go (2);
  - the in-flight `--help` fix, and the landed read-only git fix (2);
  - the automatic refusal log (1);
  - the `ledger_dir` check (1);
  - two flaky-test records for kept code (2);
  - one restore tool, whose guard allowance waits as a new rule (1);
  - one agent-text record (1);
  - one machine-config move (1).

## Target size and order

Each cut's lines now and its size after:

| Cut | Where | Lines now | Size after | Removed |
|---|---|---|---|---|
| hub text matching | rules.py 601-1976 (1,376) + hub.py (466) | 1,842 | ~850 | 992 |
| role-lock text machinery | rolepolicy.py 425-1190 | 766 | ~120 | 646 |
| environment reading | commands.py 953-1757 | 805 | ~220 | 585 |
| PowerShell write reading | 2298-2926 (629) + 3509-3647 (139) + 3959-4034 (76) + 4258-4375 (118) | 962 | ~430 | 532 |
| python program proving | 3250-3508 (259) + 4035-4103 (69) | 328 | ~30 | 298 |
| pm-data evasion checks | 5847-6334 (488) + gitread.py (44) | 532 | ~295 | 237 |
| git read classification | 4895-5328 | 434 | ~200 | 234 |
| git-shared-ref | 5511-5780 | 270 | ~70 | 200 |
| owner-lock + owner-setup merge | 360-600 | 241 | ~100 | 141 |
| heredoc-winpath | 6732-6817 | 86 | 0 | 86 |
| unreadable merge | fallback.py | 297 | ~260 | 37 |
| **Total** | | **6,563** | **~2,575** | **3,988** |

**Target: about 10,500 lines**: 14,479 − 3,988 = 10,491, down 28%. git-hooks-path is kept whole, so it removes nothing.

Later, once the lock checks its own caller and the lock MCP loses its write tools: role-lock's ~120 and lock-mcp's 53 (278-330) go, 10,491 − 173 = **10,318**.

Every option in "Checks this design weakens or removes" that the owner takes, and every row he refuses, adds its lines back: the options at most about 150 in all, a refused row up to its cut's "Removed".

`tests/guard` should shrink with the code it tests. No assertion of a kept rule is dropped; a test of a weakening the owner refuses stays.

Order: one branch each, each gated and reviewed on the strict path. A step that holds a weakening starts only after the owner's answer on its rows.

1. **Hub text matching** (A1, A2) with the fallback's hub and lock pattern (W5): the largest cut, with 22 false positives and five records. It takes in the hub half of the pipe design.
2. **Unreadable merge**, with `pwsh -File -`: this closes a real gap, so it comes early.
3. **wrapped-gate narrowing** (B9): finish the in-flight `--help` fix, then add the pipe tests.
4. **python program proving** and the main-checkout advice, with the main-checkout test rows.
5. **PowerShell write reading** (B7).
6. **Environment reading** (B3, B4), keeping `env_assignments` whole, then the **pm-data evasion checks** (B1, B2), which share the env and alias code.
7. **git read classification**, then **git-shared-ref** (B6).
8. **owner-tool merge** (A3), then **heredoc-winpath** (B8).
9. **role-lock** (B5): cut it to the parsed check now, and delete it once the lock checks its own caller. Then **lock-mcp**, once the lock MCP loses its write tools.

Each step rewrites docs/guard.md, so its rules table always matches the code.
