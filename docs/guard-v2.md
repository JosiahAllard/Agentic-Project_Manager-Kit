# Guard v2: keep, cut, target size

Design only (KIT-GUARD-V2-DESIGN, owner R-2107 B; R-2241 adds the open records). No code changes here. This is for approval: the owner approves or changes the keep/cut list and the records list (below) as one set. **New guard rules are paused until then** (R-2241); a record that would add one waits.

## Sources

- Code: `kitlib/guard` at main 9bb23046, **14,444 lines** in 15 files: rules.py 7,245, commands.py 2,028, rolepolicy.py 1,292, shell.py 1,238, hub.py 466, prepush.py 444, context.py 394, place.py 351, ownercommits.py 303, fallback.py 297, jail.py 222, generated.py 73, gitread.py 44, paths.py 31, `__init__.py` 16. Tests: `tests/guard`, 41 files, 15,399 lines.
- Refusals: the PM's cross-repo log (`docs/refusals.md` in the PM repo, kind H rows), cited below as `L<line>`; and the kit coordinator's own log for 2026-10-18 (`refusals-kit-conductor-a3-r6.md` in the agent temp folder), cited as `a3:<line>`. Tags read: `[kit guard: rule=<rule> role=<role>]`. The guard keeps no log of its own (KIT-REFUSAL-LOG-AUTOMATIC), so "never fired" means "never seen in these two logs".
- The rules table: docs/guard.md "Rules".

## Method

Each refusal is put in one class:

- **real**: the call would have done what the rule exists to stop (a write outside the jail, a heavy run with no lock, an owner verb).
- **form**: the target was safe but the call was written so the guard could not see that (a computed path, a computed `-C`, a chained or wrapped call). The refusal is right under the guard's contract; the agent rewrites the call.
- **false positive**: a safe call written plainly was refused (prose that names a word, a read-only git verb, a literal worktree path judged by the shell's directory).
- **never fired**: no refusal in either log.

A rule earns its code by real refusals. Form refusals argue for a short, plain rule with good advice, not a cleverer reader. False positives are cost. Rules that never fired are kept only when the hazard is one mistake away (one `git reset --hard`) and the rule is small.

## Tally

| Rule | Real | Form | False positive | Total |
|---|---|---|---|---|
| main-checkout | 6 | 45 | 44 | 95 |
| write-jail | 21 | 26 | 4 | 51 |
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

Two rules carry most of the cost: main-checkout's `python -c` counting (36 false positives) and the hub rules' text matching (22). Both read text where they should read the program run.

## Rules and areas

Lines are in `kitlib/guard/rules.py` unless another file is named. "Keep" means kept as is or with a small advice fix; "cut" means removed or cut down to the size given; "merge" means folded into another rule.

### Kept: they catch real hazards

| Area | Where | Lines | Refusals | Verdict | Reason |
|---|---|---|---|---|---|
| decide, judge, shell_command, refuse/tag helpers | 1-275 | 275 | all | keep | The dispatcher. The order stays: lock first, edits to edit_target, shell calls per command, then rolepolicy. |
| write-jail (session mark, roots, refusal, worktree alternative) | 2209-2307; jail.py | 99 + 222 | real: L162, L178, L179, L228, L231, L233, L234, L299, L319, L364, L373, L453, L484, L505, L506, L540, L541, L543, L550, L553, a3:6; false positive: L177 (a config folder) | keep | The rule with the most real catches. |
| git_jail, git_jail_unknown, git_in_worktree | 5390-5475 | 86 | as write-jail; form (computed `-C` on reads): L187, L189, L190, L201, L216, L218, L250, L305, L311, L324, L336, L341, L342, L356, L359, L379, L380, L430, L441, L472 | keep | A computed `-C` stays refused: the guard cannot tell the checkout. The advice names the literal form. |
| main-checkout for file tools and literal shell writes | 2109-2171; write_targets 3648-3789; shell_writes, write_place 4104-4213 | 63 + 142 + 110 | real: L192, L244, L287, L330; form (non-literal target): L182, L188, L194, L198, L202, L204, L207, L224, L260, L262, L290, L292, L302, L306, L313, L358, L367, L407, L411, L420, L431, L434, L437, L438, L440, L503, L514, L522, L530, L531, L534, L537, L560, L563, L564, L590, L591, a3:7, a3:10; false positive: L492, L493 (`commit.py -C <worktree>` judged by the shell's directory) | keep, fix | Non-literal targets are the commonest refusal and all are agent form errors; the rule is right. Keep "a target the guard cannot place is refused outside a worktree". Fix: a kit tool's `-C` names where it writes. |
| Unix writer options (sed -i, perl -i, curl -o, wget, patch, rsync), tar, unzip, copiers, find -exec | 2927-3249; 4214-4257 | 323 + 44 | false positive: L315 (`tar -xO`) | keep, fix | Small table-like code. Treat `tar -xO`/`--to-stdout` as a read (KIT-GUARD-SUBAGENT-CWD-TAR (a)). |
| CopyArgs (cp/mv/Copy-Item into a folder) | 3790-3958 | 169 | none apart | keep | Needed to place copy targets. |
| busy-wait | 6432-6703 | 272 | real: L163, L176, L203, L212, L226, L265, L303, L335, L365, L366, L381, L390, L463, L473, L566 | keep | 15 of 15 real. The bounded-loop advice works. |
| wrapped-gate (P16) | 4376-4439 | 64 | real: L157, L199, L210, L246, L337, L400, L433, L443; false positive: L567 (`--help` piped) | keep, narrow | See "Kit tools may be piped" below. |
| heavy, kit_wrapper | 4572-4704 | 133 | real: L197, L223, L406, L574, L591 | keep | Every catch real. Resolve kit_wrapper from the parsed script, not `cmd.cwd` (KIT-GUARD-WORKDIR-FOLLOWUPS item 2). |
| root-search | 4440-4571 | 132 | real: L81, L95, L96, L132, L407; old-hook false positives: L82, L88, L133, L152 (fixed since) | keep | Real catches; the false positives predate the current rule. |
| git main-checkout rules (checkout, reset --hard, clean, stash, bulk-add, commit-all, restore, main-history) | 4705-4867; writes_main 4868-4920 | 216 | real: L168, L254 (main-history), L227, L229 (commit-all), L295, L345 (bulk-add), L235 (restore); false positive: L360 (checkout in a scratch clone) | keep | One mistake from losing main's work; small. reset-hard, clean, stash never fired but cost little. |
| git-push-main, landing_command | 6300-6431 | 132 | real: L159 | keep | Guards main on every repo. |
| git-worktree-remove, git_branch_delete, git_unplaced | 5746-5811 | 66 | real: L171, L173, L211, L225, L482, L538, L542, L565 | keep | Each refusal stopped a remove of an unclean or unmerged tree. |
| lander checks | 5309-5389 | 81 | none apart | keep | Part of the landing path. |
| kit-json | 2172-2186 | 15 | real: L205, L217, L338, L339, L340, L401 | keep | Small; real catches. |
| kill-by-name | 7073-7195 | 123 | real: L298 | keep | A name kill can end another session's process. |
| ledger, lock-state, generated-file, pm-data availability | 1977-2108; 2187-2208; generated.py | 132 + 22 + 73 | L321 (pm-data fails closed when the record does not read) | keep | Small path checks over files only tools write. lock-state and generated-file never fired; kept because they are a few lines each. |
| owner-identity | 7196-7245 | 50 | never fired | keep | 50 lines; stops a commit under the owner's address. |
| pre-push leak check, owner-only commits | prepush.py, ownercommits.py | 444 + 303 | git hooks; not in the logs | keep | The last line for what leaves the machine. They take over what pm-data's alias and HOME checks did (below). |
| place, context, paths | place.py, context.py, paths.py | 351 + 394 + 31 | n/a | keep | Shared plumbing. context.py should check `ledger_dir` (KIT-LEDGER-IDS-GUARD-LEDGER-DIR). |
| shell and command parser | shell.py; commands.py outside the env logic | 1,238 + ~1,220 | n/a | keep | Every shell rule reads its output. Shrinks after the cuts by what only cut rules used (not counted in the target). |
| rolepolicy: mode, check, screen, role-shell | rolepolicy.py 1-424, 1191-1292 | ~525 | role-shell: L549 (a probe, over-refuse) | keep | Read roles and their data roots. role-edit and role-workdir never fired; they are a few lines inside check. |

### Cut or merged

| Area | Where | Lines | Refusals | Verdict | Size after | Reason |
|---|---|---|---|---|---|---|
| hub text matching: hub-owner-verb (path, family, shape) and hub-secrets on shell text | 748-1976; hub.py | 1,229 + 466 | real (owner verb or a reach of the token folder): L427, L478, L479, L517, L523, L544, L586, L588, L597/a3:5; form (chained or computed): L568, L569, L595, a3:3; agent shell reads, blocked by R-1961: L486, L501, L508, L520, L575, L587; false positive (prose, log rows, heredoc bodies, grep words, a path in another tool's argument): L409, L410, L413, L416, L426, L436, L449, L464, L465, L466, L470, L518, L521, L545, L571, L581, L585, L592, L593, L594, a3:12; broad: L494, L498, L507 | cut | ~250 | Judge the program run, never text. Keep: (1) an owner verb on a parsed `hub.py`/`-m tools.hub` invocation; (2) a literal path in any word that resolves into the token folder or the hub store; (3) hub_mcp and hub-browser (MCP names, small). Drop: module-name and package-name matching in code text, the "one shape read in full" family, grep/findstr/Select-String pattern reading, heredoc body reading. A hub call the parser cannot read goes to the one unreadable rule. Fixes 22 false positives and folds five open records. |
| role-lock text machinery | rolepolicy.py 425-1190 | 766 | false positive: L408, L415, L428, L489, L509, L515, L527, L546; real: L552 | cut | ~120 now, 0 after the credential boundary | Match only a parsed run of the lock CLI with a mutating verb, and lock_mcp_tool. Drop matching the lock's name in `--scope` values, commit messages, git grep, `$MyInvocation`. Once KIT-LOCK-CREDENTIAL-BOUNDARY lands the lock checks the caller itself and the rest goes. |
| python program proving (py_program, py_reads_only, _python, sleep_wait, py_unread) | 3250-3528; 4035-4103 | 279 + 69 | false positive in substance (`python -c` that only read, run from the main checkout): L191, L206, L208, L209, L220, L258, L274, L300, L318, L329, L331, L332, L346, L352-L357, L362, L363, L374, L382, L386, L394, L419, L421, L442, L444, L446, L510, L554, L562, L577, a3:8, a3:15; sleep-waits: L468, L504, L535, L572, L573, L584; write-jail cwd: L180, L291, L294, L320, L369, L548 | cut | ~30 | Keep the plain rule: a `python -c` or python on stdin counts as a write of the directory it runs in. Drop the prover (`-I`, module allowlist, AST walk) that tried to let some through. The advice becomes one line: `cd "<worktree>" && python -c ...`, or the Read and Grep tools. The 36 refusals become form refusals with an advice that works, at a fraction of the code. **Owner choice:** the other way (count `python -c` as a read and judge only its redirects) cuts the same code and every one of these refusals but lets a `python -c` write into a main checkout; this design does not take it. |
| PowerShell write reading: PSCmdlet, NetWrite, ps_unread, ps_line_plain, ps_pwd_path, dotnet_unplaced, dotnet_process_dir, ps_drive_jail | 2308-2926; 3529-3647; 3959-4034; 4258-4375 | 619 + 119 + 76 + 118 | false positive (literal worktree path judged by the shell's directory): L195, L243, L376, L378, L397; real (computed .NET path): L557, L576 | cut | ~400 | One table of writer cmdlets and their path parameters, and the .NET `File`/`Directory` write methods by their first argument. A literal target is judged where it points, never by the shell's directory; any other target (computed, piped in, provider path) is not placed and is refused outside a worktree. Drops the .NET constructor and cast reading. Folds KIT-GUARD-MAIN-CHECKOUT-WORKTREE-FALSE-POSITIVE, KIT-GUARD-PS-PIPELINE-PATHS, KIT-GUARD-PS-PROVIDER-SMALL. |
| environment reading (env-unsafe: PS env provider, SetEnvironmentVariable, bash builtins that assign) | commands.py 953-1757 | 805 | none apart (folded into the rules above); open false positives: Remove-Item Env:X, double-quoted text, GIT_INDEX_FILE temp index | cut | ~150 | Keep: `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG*`, a HOME-like variable set on the same line as `git push`, read in the plain forms (prefix, `env`, `export`, `$env:X =`). Drop: arithmetic and `read`/`mapfile`/`getopts` assignment, `Set-Item env:`, double-quoted text scanning. A computed variable name goes to the unreadable rule. |
| pm-data evasion checks: git alias expansion, HOME config, send-pack/remote helpers, remote and config rewrite, data copies | 5812-6299; gitread.py | 488 + 44 | never fired | cut | ~120 | Keep: `push --no-verify` and send-pack in a repository with `kit-pm-data.json`, and remote rewrite as a plain `git remote set-url`/`git config remote.*` check. Drop: alias expansion through `git config --get`, HOME tricks, copy detection, the remote-helper list. These defend against a deliberate evader, not a mistake; the guard is a seatbelt (KIT-LOCK-CREDENTIAL-BOUNDARY), and the pre-push leak check judges every push that runs hooks. |
| git-shared-ref | 5476-5745 | 270 | never fired | cut | ~70 | Keep: an agent `update-ref` of anything but `refs/tmp/*` and per-worktree refs is refused. Drop: symbolic-ref chain walking, scratch-repo detection, the reftable and link checks. |
| git read classification (pager, dynamic reads, archive) | 4921-5308 | 388 | false positive: L296 (archive), L511 (patch-id), L512 (apply --check); real: a3:6 (merge-tree --write-tree) | cut | ~150 | One table of read-only subcommands with the options that make them write (`archive -o`, `merge-tree --write-tree`, `diff --output`). Add patch-id, archive to stdout, apply --check/--stat. The table reads options only before a `--` and the read flag must be the subcommand's own option: today `config` counts as a read when `-l`/`--list` or a `--get*` appears anywhere in the words, after `--` too (`git_reads`, line 5262). A review note, not yet confirmed: Windows PowerShell 5.1 may drop a bare `--` before a native program sees it, so `git apply --check -- --apply x` could look like a read while git gets `--apply`. Every `--` scan in the guard assumes the shell passes `--` through. The table step tests that under PowerShell 5.1 first, and on PowerShell it treats a `--` the way the program will receive it. |
| owner-lock and owner-setup | 377-747 | 371 | never fired as such; owner-setup misfired on heredoc text (KIT-GUARD-HEREDOC-TEXT-FALSE-POSITIVES) | merge | ~100 | One `owner-tool` rule over a parsed invocation: reserve_ui, the lock CLI's owner writes, machine setup acting. No code-text reading. |
| git-hooks-path | 6794-7072 | 279 | real: L481 | cut | ~100 | Keep `git config core.hooksPath` writes, `-c core.hooksPath=`, `clone -c`. Drop the alias and `GIT_CONFIG_PARAMETERS`/`KEY_<n>` forms with the env cut. |
| heredoc-winpath | 6704-6793 | 90 | never fired | cut | 0 | A quoting trap, not a hazard. The shell-traps skill already says to write scripts with the Write tool. |
| unreadable (fallback) | fallback.py | 297 | form: L164, L169, L172, L193, L253, L261, L264, L297, L304, L349, L371, L372, L384, L385, L393, L432, L450, L469, L496, L555, L570, L578 | merge | ~200 | Stays the one place for what the parser cannot read, now also for hub and lock calls it cannot read. `pwsh -File -` (script on stdin) is unreadable (KIT-GUARD-PWSH-FILE-STDIN). Drop the `plainly` rewrite attempts beyond the temp-script advice. |
| lock-mcp | 276-336 | 61 | never fired | cut later | 0 | Goes when KIT-LOCK-MCP-OWNER-WRITES removes the lock MCP's write tools. |
| agent_session | 337-376 | 40 | n/a | keep | 40 | Used by every agent-only rule. |

## Kit tools may be piped (KIT-GUARD-PIPE-KIT-TOOLS)

That record is closed into this design (8088406f, R-2241). Its ask: a kit tool's output may be piped or redirected like any program's (`backlog.py list | head`, `doc_index.py ... > <file in worktree>`, `machine_lock.py run --help | head`).

- **wrapped-gate** fires only on the two runs that hold the machine for minutes: `gate.py` running steps (no subcommand, or `--full`/`--step`) and `machine_lock.py run <...> -- <command>`. `-h`/`--help`, a gate subcommand that runs no steps, and every other verb of either tool may be piped (L567, KIT-GUARD-WRAPPED-GATE-HELP-FALSE-POSITIVE). The P16 reason stays for the two runs: a piped gate reads to the classifier as something else.
- **Every other kit tool** has no pipe rule. Today the hub and role-lock text matchers refuse `hub.py` and the lock CLI when chained, piped or wrapped (a3:3, a3:5, L568, L569, L595). After their cuts above, a parsed `hub.py` read verb or lock CLI read verb may be piped or chained; only an owner verb (hub) or a mutating verb (lock) is refused, in any form.
- **Redirects** of a kit tool's output are judged by the write rules on the target, as for any program: a literal path in the worktree or temp passes.
- Rule order is unchanged: wrapped-gate stays before heavy.
- Tests: rows in `tests/guard/test_kit_tool_forms.py` for `--help` piped, `backlog.py list | head`, `hub.py questions list | head`, and the refused `gate.py --full | tail`.

## Open guard-area records

How they were found: `backlog.py -C Z:/GitHub/project-manager list` (the kit's store, `kit/backlog`, fetched; 109 records). Retribution and GriffBall keep their own ref stores (`.kit.json` `"backlog".store: "ref"`), read with `--no-fetch list` (288 and 124 records): neither holds a kit guard record. Closed records are not listed. A record is guard-area when its scope or text is `kitlib/guard`, `tests/guard`, a guard rule, or a cut above depends on it.

**Close** means: closed as superseded by this design, its fix done inside the named cut. **Keep** means: stays its own record.

| Record | State | Verdict | Reason |
|---|---|---|---|
| KIT-GUARD-V2-DESIGN | pulled | this item | Closed by this branch. |
| KIT-GUARD-BACKLOG-ROLES | parked p1 (Q-945) | close | A new text-matching guard rule over backlog calls, the kind this design cuts. The role check belongs inside `backlog.py` (it knows its verb and caller). KIT-BACKLOG-UNCITE-REST names it as a must-land-first; that line moves to an in-tool role check record if the owner wants one. |
| KIT-GUARD-JAIL-R5-LEFTOVERS | blocked p2 | close | Temp-index `GIT_INDEX_FILE` and `set --`/`declare -p GIT_DIR` false positives go with the environment cut. |
| KIT-GUARD-SUBAGENT-CWD-TAR | open p3 | close | (a) `tar -xO` is the "keep, fix" row above; (b) the archive advice is rewritten with the git read table. |
| KIT-GUARD-INSTEAD-WORKTREE-ADD-IN-WORKTREE | open, unpriced | close | The main-checkout Instead text tells a worker already in a worktree to run `git worktree add`; the right advice is `cd "<worktree>"` or a literal path in it. A sibling of KIT-GUARD-SUBAGENT-CWD-TAR (b). Rewritten with the main-checkout advice in the python-proving step (step 4). |
| KIT-GUARD-READONLY-GIT-WRITEJAIL | pulled p3 | keep | In flight; small; the same fix the git read table makes. Land it now; the table keeps its rows. |
| KIT-GUARD-WRAPPED-GATE-HELP-FALSE-POSITIVE | pulled p3 | keep | In flight; it is step 1 of "Kit tools may be piped". |
| KIT-GUARD-KNOWN-LIMITS | open p4 | close | Alias-to-checkout and dot-sourced Get-Command are evasion forms this design stops chasing; v2's docs/guard.md lists its limits. |
| KIT-GUARD-ENV-AND-HUB-GREP-FALSE-POSITIVES | open | close | Environment cut and hub cut. |
| KIT-GUARD-HEREDOC-TEXT-FALSE-POSITIVES | open | close | Hub cut and the owner-tool merge (no code-text reading). |
| KIT-GUARD-HUB-PATH-IN-ARG | open | close | Hub cut: the program run, not words in another tool's arguments. |
| KIT-GUARD-OWNER-VERB-PROSE | open | close | Hub cut. |
| KIT-GUARD-MAIN-CHECKOUT-WORKTREE-FALSE-POSITIVE | open | close | PowerShell cut: a literal target judged where it points. |
| KIT-GUARD-PS-DQ-FALSE-POSITIVES | open | close | Environment cut drops the double-quoted text scan. |
| KIT-GUARD-PS-PIPELINE-PATHS | open | close | PowerShell cut: a piped-in target is not placed and is refused outside a worktree. |
| KIT-GUARD-PS-PROVIDER-SMALL | open | close | PowerShell cut rewrites that advice. |
| KIT-GUARD-PWSH-FILE-STDIN | open | close | Unreadable merge: `-File -` is unreadable. A real gap today (a write into a main checkout passes), so this is step 2 below, not late. |
| KIT-GUARD-ROLE-LOCK-SCOPE-VALUE | open | close | Role-lock cut. |
| KIT-ROLE-LOCK-ACCEPTED-FALSE-POSITIVES | open | close | Role-lock cut. |
| KIT-ROLE-LOCK-MORE-FALSE-POSITIVES | open | close | Role-lock cut and hub cut. |
| KIT-GUARD-WORKDIR-FOLLOWUPS | blocked p-1 (owner) | close | py_only_sleeps goes with the prover cut; kit_wrapper/heavy cwd is the heavy row above; the git-* check in shell_writes and the Start-Process tests go with the PowerShell cut; the scrub baselines follow any branch that touches them. |
| KIT-TEST-RULES-CWD | open | close | The main-checkout test rows are rewritten with the prover cut; make them cwd-free then. |
| KIT-LOCK-CREDENTIAL-BOUNDARY | open | keep | The lock checks its caller; then role-lock goes to 0. The cut that pays most after the hub. |
| KIT-LOCK-MCP-OWNER-WRITES | open p3 | keep | Removing the lock MCP's write tools lets lock-mcp go. |
| KIT-REFUSAL-LOG-AUTOMATIC | open | keep | Not a rule: one row per refusal, so the next review counts rules from data, not from two hand logs. |
| KIT-LEDGER-IDS-GUARD-LEDGER-DIR | open | keep | Small check in context.py; ledger rule is kept. |
| KIT-TEST-PLACE-DECISIONS-FALLBACK-FLAKY | open | keep | A flaky place test; place.py is kept. |
| KIT-TEST-PREPUSH-OWNER-FLAKY | blocked | keep | prepush.py is kept. |
| KIT-MAIN-STRAY-SAFE-RESTORE | open p2 | keep, paused | A kit tool; its guard allowance would be a new rule, so that part waits for approval. |
| KIT-REFUSAL-RULE-FORM-VS-SUBSTANCE | open | keep | Agent text on retrying form refusals; matches the classes above. |
| KIT-MACHINE-CONFIG-OUT-OF-HOME | parked (Q-961) | keep | Moves guard config keys (`guard_mark_roots`, `ledger_path`); no conflict, plan only. |

Related records outside the guard area, unaffected: KIT-SUITE-CUT (in flight; touches `tests/guard/test_prepush.py` fixtures only), KIT-BACKLOG-UNCITE-REST (see KIT-GUARD-BACKLOG-ROLES), KIT-SUBTRACTION-AUDIT (the kit-wide sibling, R-2107 C).

## Target size and order

**Target: about 9,200 lines, down from 14,444 (-5,250, -36%)**, and about 8,400 once KIT-LOCK-CREDENTIAL-BOUNDARY and KIT-LOCK-MCP-OWNER-WRITES land. `tests/guard` should shrink with the code it tests (roughly a third); no assertion of a kept rule is dropped.

| Cut | Lines removed (approx.) |
|---|---|
| hub text matching (rules.py + hub.py) | 1,445 |
| PowerShell write reading | 530 |
| role-lock text machinery | 645 |
| environment reading (commands.py) | 655 |
| pm-data evasion checks (+ gitread.py) | 410 |
| python program proving | 320 |
| owner-lock + owner-setup merge | 270 |
| git read classification | 240 |
| git-shared-ref | 200 |
| git-hooks-path | 180 |
| unreadable merge | 100 |
| heredoc-winpath | 90 |
| lock-mcp (later) | 60 |

Order, one branch each, each gated and reviewed on the strict path:

1. **Hub text matching** (largest, 22 false positives, five records). Folds the pipe design's hub half.
2. **Unreadable merge** with `pwsh -File -` (closes a real gap first).
3. **wrapped-gate narrowing** (finish KIT-GUARD-WRAPPED-GATE-HELP-FALSE-POSITIVE; then the pipe tests).
4. **python program proving** and the main-checkout advice; KIT-TEST-RULES-CWD with it.
5. **PowerShell write reading**.
6. **Environment reading**, then **git-hooks-path** and **pm-data evasion checks** (they share the env and alias code).
7. **git read classification** (after KIT-GUARD-READONLY-GIT-WRITEJAIL lands), then **git-shared-ref**.
8. **owner-tool merge**, then **heredoc-winpath**.
9. **role-lock**: cut to the parsed check now; delete after KIT-LOCK-CREDENTIAL-BOUNDARY. **lock-mcp** after KIT-LOCK-MCP-OWNER-WRITES.

docs/guard.md is rewritten with each step, so its rules table always matches the code.
