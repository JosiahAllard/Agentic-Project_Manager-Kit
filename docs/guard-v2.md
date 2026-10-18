# Guard v2: keep, cut, target size

Design only, with no code changes. This is for approval: the owner approves or changes the keep/cut list here and the open-records list (below) as one set. **New guard rules are paused until then**; a record that would add one waits.

## Sources

- Code: `kitlib/guard` on main when this was written, **14,444 lines** in 15 files:

  | File | Lines |
  |---|---|
  | rules.py | 7,245 |
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

  Tests: `tests/guard`, 41 files, 15,399 lines.
- Refusals: two logs, read by their tag `[kit guard: rule=<rule> role=<role>]`.
  - The PM's cross-repo log (`docs/refusals.md` in the PM repo, kind H rows), cited below as `L<line>`.
  - The kit coordinator's own log for one day of work (in the agent temp folder), cited as `D<line>`.

  The guard keeps no log of its own (an open record asks for one). So "never fired" means "never seen in these two logs".
- The rules table: docs/guard.md "Rules".

## Method

Each refusal is put in one class:

- **real**: the call would have done what the rule exists to stop (a write outside the jail, a heavy run with no lock, an owner verb).
- **form**: the target was safe, but the call was written so the guard could not see that (a computed path, a computed `-C`, a chained or wrapped call). The refusal is right under the guard's contract; the agent rewrites the call.
- **false positive**: a safe call written plainly was refused (prose that names a word, a read-only git verb, a literal worktree path judged by the shell's directory).
- **never fired**: no refusal in either log.

A rule earns its code by real refusals. Form refusals argue for a short, plain rule with good advice, not a cleverer reader. False positives are cost. A rule that never fired is kept only when it is small and its hazard is one mistake away (one `git reset --hard`).

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

Two rules carry most of the cost: main-checkout's `python -c` counting (36 false positives) and the hub rules' text matching (22). Both read the command's text where they should read the program it runs.

## Rules and areas

Line ranges are in `kitlib/guard/rules.py` unless another file is named.
- "keep": kept as is or with a small advice fix.
- "cut": removed, or cut down to the size given.
- "merge": folded into another rule.

### Kept: they catch real hazards

| Area | Where | Lines | Refusals | Verdict | Reason |
|---|---|---|---|---|---|
| decide, judge, shell_command, refuse/tag helpers | 1-275 | 275 | all | keep | The dispatcher. Its order stays: the lock first, edits to edit_target, shell calls per command, then rolepolicy. |
| write-jail (session mark, roots, refusal, worktree alternative) | 2209-2307; jail.py | 99 + 222 | real: L162, L178, L179, L228, L231, L233, L234, L299, L319, L364, L373, L453, L484, L505, L506, L540, L541, L543, L550, L553, D6; false positive: L177 (a config folder) | keep | The rule with the most real catches. |
| git_jail, git_jail_unknown, git_in_worktree | 5390-5475 | 86 | as write-jail; form (computed `-C` on reads): L187, L189, L190, L201, L216, L218, L250, L305, L311, L324, L336, L341, L342, L356, L359, L379, L380, L430, L441, L472 | keep | A computed `-C` stays refused because the guard cannot tell the checkout. The advice names the literal form. |
| main-checkout for file tools and literal shell writes | 2109-2171; write_targets 3648-3789; shell_writes, write_place 4104-4213 | 63 + 142 + 110 | real: L192, L244, L287, L330; form (non-literal target): L182, L188, L194, L198, L202, L204, L207, L224, L260, L262, L290, L292, L302, L306, L313, L358, L367, L407, L411, L420, L431, L434, L437, L438, L440, L503, L514, L522, L530, L531, L534, L537, L560, L563, L564, L590, L591, D7, D10; false positive: L492, L493 (`commit.py -C <worktree>` judged by the shell's directory) | keep, fix | Non-literal targets are the commonest refusal, and all are agent form errors, so the rule is right. Keep "a target the guard cannot place is refused outside a worktree". Fix: a kit tool's `-C` names where it writes. |
| Unix writer options (sed -i, perl -i, curl -o, wget, patch, rsync), tar, unzip, copiers, find -exec | 2927-3249; 4214-4257 | 323 + 44 | false positive: L315 (`tar -xO`) | keep, fix | Small, table-like code. Treat `tar -xO`/`--to-stdout` as a read. |
| CopyArgs (cp/mv/Copy-Item into a folder) | 3790-3958 | 169 | none of its own | keep | Needed to place copy targets. |
| busy-wait | 6432-6703 | 272 | real: L163, L176, L203, L212, L226, L265, L303, L335, L365, L366, L381, L390, L463, L473, L566 | keep | 15 of 15 real. The bounded-loop advice works. |
| wrapped-gate (P16) | 4376-4439 | 64 | real: L157, L199, L210, L246, L337, L400, L433, L443; false positive: L567 (`--help` piped) | keep, narrow | See "Kit tools may be piped" below. |
| heavy, kit_wrapper | 4572-4704 | 133 | real: L197, L223, L406, L574, L591 | keep | Every catch real. Resolve kit_wrapper from the parsed script, not `cmd.cwd`. |
| root-search | 4440-4571 | 132 | real: L81, L95, L96, L132, L407; old-hook false positives: L82, L88, L133, L152 (fixed since) | keep | Real catches. The false positives predate the current rule. |
| git main-checkout rules (checkout, reset --hard, clean, stash, bulk-add, commit-all, restore, main-history) | 4705-4867; writes_main 4868-4920 | 216 | real: L168, L254 (main-history), L227, L229 (commit-all), L295, L345 (bulk-add), L235 (restore); false positive: L360 (checkout in a scratch clone) | keep | Small, and each guards against one mistake that loses main's work. reset-hard, clean and stash never fired but cost little. |
| git-push-main, landing_command | 6300-6431 | 132 | real: L159 | keep | Guards main on every repo. |
| git-worktree-remove, git_branch_delete, git_unplaced | 5746-5811 | 66 | real: L171, L173, L211, L225, L482, L538, L542, L565 | keep | Each refusal stopped a remove of an unclean or unmerged tree. |
| lander checks | 5309-5389 | 81 | none of its own | keep | Part of the landing path. |
| kit-json | 2172-2186 | 15 | real: L205, L217, L338, L339, L340, L401 | keep | Small, with real catches. |
| kill-by-name | 7073-7195 | 123 | real: L298 | keep | Killing by name can end another session's process. |
| ledger, lock-state, generated-file, pm-data availability | 1977-2108; 2187-2208; generated.py | 132 + 22 + 73 | L321 (pm-data fails closed when the record does not read) | keep | Small path checks over files that only tools write. lock-state and generated-file never fired; they are kept because they are a few lines each. |
| owner-identity | 7196-7245 | 50 | never fired | keep | 50 lines that stop a commit under the owner's address. |
| pre-push leak check, owner-only commits | prepush.py, ownercommits.py | 444 + 303 | git hooks, so not in the logs | keep | The last check on what leaves the machine. They take over what pm-data's alias and HOME checks did (below). |
| place, context, paths | place.py, context.py, paths.py | 351 + 394 + 31 | n/a | keep | Shared plumbing. context.py should also check `ledger_dir` (an open record). |
| shell and command parser | shell.py; commands.py outside the env logic | 1,238 + ~1,220 | n/a | keep | Every shell rule reads its output. It shrinks after the cuts by the code only cut rules used; that is not counted in the target. |
| rolepolicy: mode, check, screen, role-shell | rolepolicy.py 1-424, 1191-1292 | ~525 | role-shell: L549 (a probe, over-refused) | keep | Read roles and their data roots. role-edit and role-workdir never fired; they are a few lines inside check. |

### Cut or merged

| Area | Where | Lines | Refusals | Verdict | Size after | Reason |
|---|---|---|---|---|---|---|
| hub text matching: hub-owner-verb (path, family, shape) and hub-secrets on shell text | 748-1976; hub.py | 1,229 + 466 | real (an owner verb, or a reach into the token folder): L427, L478, L479, L517, L523, L544, L586, L588, L597 (also D5); form (chained or computed): L568, L569, L595, D3; agent shell reads, which the owner barred: L486, L501, L508, L520, L575, L587; false positive (prose, log rows, heredoc bodies, grep words, a path in another tool's argument): L409, L410, L413, L416, L426, L436, L449, L464, L465, L466, L470, L518, L521, L545, L571, L581, L585, L592, L593, L594, D12; broad: L494, L498, L507 | cut | ~250 | Judge the program run, never the text. Keep: (1) an owner verb on a parsed `hub.py`/`-m tools.hub` invocation; (2) a literal path, in any word, that resolves into the token folder or the hub store; (3) hub_mcp and hub-browser (MCP names; small). Drop: module-name and package-name matching in code text, the "one shape read in full" family, grep/findstr/Select-String pattern reading, and heredoc body reading. A hub call the parser cannot read goes to the one unreadable rule. Fixes 22 false positives and folds five open records. |
| role-lock text machinery | rolepolicy.py 425-1190 | 766 | false positive: L408, L415, L428, L489, L509, L515, L527, L546; real: L552 | cut | ~120 now, 0 later | Match only a parsed run of the lock CLI with a mutating verb, and lock_mcp_tool. Drop matching the lock's name in `--scope` values, commit messages, git grep and `$MyInvocation`. Once the lock checks its caller itself (the credential-boundary record), the rest goes. |
| python program proving (py_program, py_reads_only, _python, sleep_wait, py_unread) | 3250-3528; 4035-4103 | 279 + 69 | false positive in substance (`python -c` that only read, run from the main checkout): L191, L206, L208, L209, L220, L258, L274, L300, L318, L329, L331, L332, L346, L352 to L357, L362, L363, L374, L382, L386, L394, L419, L421, L442, L444, L446, L510, L554, L562, L577, D8, D15; sleep-waits: L468, L504, L535, L572, L573, L584; write-jail cwd: L180, L291, L294, L320, L369, L548 | cut | ~30 | Keep the plain rule: a `python -c`, or python reading code on stdin, counts as a write of the directory it runs in. Drop the prover (`-I`, the module allowlist, the AST walk) that tried to let some through. The advice becomes one line: `cd "<worktree>" && python -c ...`, or the Read and Grep tools. The advice must not tell a worker already in a worktree to add one. The 36 refusals become form refusals whose advice works, at a fraction of the code. **Owner choice:** the other way (count `python -c` as a read and judge only its redirects) cuts the same code and every one of these refusals, but lets a `python -c` write into a main checkout. This design does not take it. |
| PowerShell write reading: PSCmdlet, NetWrite, ps_unread, ps_line_plain, ps_pwd_path, dotnet_unplaced, dotnet_process_dir, ps_drive_jail | 2308-2926; 3529-3647; 3959-4034; 4258-4375 | 619 + 119 + 76 + 118 | false positive (literal worktree path judged by the shell's directory): L195, L243, L376, L378, L397; real (computed .NET path): L557, L576 | cut | ~400 | One table of writer cmdlets and their path parameters, plus the .NET `File`/`Directory` write methods by their first argument. A literal target is judged where it points, never by the shell's directory. Any other target (computed, piped in, a provider path) is not placed, so it is refused outside a worktree. Drops the .NET constructor and cast reading. Folds three open PowerShell records. |
| environment reading (env-unsafe: PS env provider, SetEnvironmentVariable, bash builtins that assign) | commands.py 953-1757 | 805 | none of its own (they count under the rules above); open false positives: Remove-Item Env:X, double-quoted text, a `GIT_INDEX_FILE` temp index | cut | ~150 | Keep: `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG*`, and a HOME-like variable set on the same line as `git push`, read in the plain forms (prefix, `env`, `export`, `$env:X =`). Drop: arithmetic and `read`/`mapfile`/`getopts` assignment, `Set-Item env:`, and double-quoted text scanning. A computed variable name goes to the unreadable rule. |
| pm-data evasion checks: git alias expansion, HOME config, send-pack/remote helpers, remote and config rewrite, data copies | 5812-6299; gitread.py | 488 + 44 | never fired | cut | ~120 | Keep: `push --no-verify` and send-pack in a repository with `kit-pm-data.json`, and remote rewrite as a plain `git remote set-url`/`git config remote.*` check. Drop: alias expansion through `git config --get`, HOME tricks, copy detection and the remote-helper list. These defend against a deliberate evader, not a mistake. The guard is a seatbelt, not a fence, and the pre-push leak check judges every push that runs hooks. |
| git-shared-ref | 5476-5745 | 270 | never fired | cut | ~70 | Keep: an agent `update-ref` of anything but `refs/tmp/*` and per-worktree refs is refused. Drop: symbolic-ref chain walking, scratch-repo detection, and the reftable and link checks. |
| git read classification (pager, dynamic reads, archive) | 4921-5308 | 388 | false positive: L296 (archive), L511 (patch-id), L512 (apply --check); real: D6 (merge-tree --write-tree) | cut | ~150 | See "Read-only git and `--`" below. |
| owner-lock and owner-setup | 377-747 | 371 | never fired as such; owner-setup misfired on heredoc text (an open record) | merge | ~100 | One `owner-tool` rule over a parsed invocation: reserve_ui, the lock CLI's owner writes, and machine setup acting. No code-text reading. |
| git-hooks-path | 6794-7072 | 279 | real: L481 | cut | ~100 | Keep `git config core.hooksPath` writes, `-c core.hooksPath=` and `clone -c`. Drop the alias and `GIT_CONFIG_PARAMETERS`/`KEY_<n>` forms, along with the environment cut. |
| heredoc-winpath | 6704-6793 | 90 | never fired | cut | 0 | A quoting trap, not a hazard. The shell-traps skill already says to write scripts with the Write tool. |
| unreadable (fallback) | fallback.py | 297 | form: L164, L169, L172, L193, L253, L261, L264, L297, L304, L349, L371, L372, L384, L385, L393, L432, L450, L469, L496, L555, L570, L578 | merge | ~200 | Stays the one place for what the parser cannot read, now also for hub and lock calls it cannot read. `pwsh -File -` (a script on stdin) counts as unreadable. Drop the `plainly` rewrite attempts beyond the temp-script advice. |
| lock-mcp | 276-336 | 61 | never fired | cut later | 0 | Goes once the lock MCP loses its write tools (an open record). |
| agent_session | 337-376 | 40 | n/a | keep | 40 | Every agent-only rule uses it. |

### Read-only git and `--`

The read classification becomes one table of read-only subcommands, with the options that make each one write (`archive -o`, `merge-tree --write-tree`, `diff --output`). The table adds patch-id, archive to stdout, and apply --check/--stat.

It reads options only before a `--`, and a read flag counts only as the subcommand's own option.
- Today `config` counts as a read when `-l`/`--list` or any `--get*` appears anywhere in its words, even after `--` (`git_reads`). The read-only git fix that has since landed leaves this unchanged.
- That fix does make `apply_checks` read `apply`'s options only before the first `--`.

A review note, not yet confirmed: Windows PowerShell 5.1 may drop a bare `--` before a native program sees it.
- If so, `git apply --check -- --apply x` reads as a check, while git receives `--apply`.
- Every `--` scan in the guard assumes the shell passes `--` through.
- So the table step first tests what PowerShell 5.1 passes. On PowerShell, it treats a `--` the way the program will receive it.

## Kit tools may be piped

The open record that asked for this is closed into this design. Its ask: a kit tool's output may be piped or redirected like any program's (`backlog.py list | head`, `doc_index.py ... > <file in worktree>`, `machine_lock.py run --help | head`).

- **wrapped-gate** fires only on the two runs that hold the machine for minutes:
  - `gate.py` running steps (no subcommand, or `--full`/`--step`);
  - `machine_lock.py run <...> -- <command>`.

  `-h`/`--help`, a gate subcommand that runs no steps, and every other verb of either tool may be piped (L567). An in-flight fix covers `--help`. The P16 reason stays for the two runs: to the classifier, a piped gate reads as something else.
- **Every other kit tool** has no pipe rule.
  - Today the hub and role-lock text matchers refuse `hub.py` and the lock CLI when chained, piped or wrapped (D3, D5, L568, L569, L595).
  - After the hub and role-lock cuts above, a parsed `hub.py` read verb, or a lock CLI read verb, may be piped or chained. Only a hub owner verb or a lock mutating verb is refused, in any form.
- **Redirects** of a kit tool's output are judged by the write rules on the target, as for any program. A literal path in the worktree or temp passes.
- Rule order is unchanged: wrapped-gate stays before heavy.
- Tests: rows in `tests/guard/test_kit_tool_forms.py` for `--help` piped, `backlog.py list | head` and `hub.py questions list | head`, and for the refused `gate.py --full | tail`.

## Open guard-area records

The owner asked for every open guard-area backlog record in every repository, each marked keep or close with a reason. Every repository's backlog store was read: the kit's store and each game repository's own store. Only the kit's store holds guard records.

That list names records, and record ids are owner-specific text that the scrub tests bar from tracked files. So it is kept outside the repository and handed to the owner with this doc for approval.

In summary:
- **Close: 19.** Each is superseded by a cut above, and its fix is done inside that cut:
  - five false-positive records, by the hub cut;
  - three PowerShell records, by the PowerShell cut;
  - three by the environment cut;
  - three role-lock records;
  - the stdin-script gap, by the unreadable merge;
  - the workdir follow-ups, the main-checkout test rows and the worktree-add advice, by the prover step;
  - the tar read, by the "keep, fix" row;
  - the known-limits list, which v2's docs/guard.md takes over;
  - one proposed new text rule over backlog calls, whose check belongs inside `backlog.py`.
- **Keep: 11.**
  - the lock's own caller check, and the lock MCP losing its write tools: these let role-lock and lock-mcp go;
  - the in-flight `--help` fix, and the landed read-only git fix;
  - the automatic refusal log;
  - the `ledger_dir` check;
  - two flaky-test records for kept code;
  - one restore tool, whose guard allowance waits as a new rule;
  - one agent-text record;
  - one machine-config move.

## Target size and order

**Target: about 9,200 lines, down from 14,444 (-5,250, -36%).** About 8,400 once the lock checks its own caller and the lock MCP loses its write tools. `tests/guard` should shrink with the code it tests (roughly a third). No assertion of a kept rule is dropped.

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

Order: one branch each, each gated and reviewed on the strict path.

1. **Hub text matching**: the largest cut, with 22 false positives and five records. It takes in the hub half of the pipe design.
2. **Unreadable merge**, with `pwsh -File -`: this closes a real gap, so it comes early.
3. **wrapped-gate narrowing**: finish the in-flight `--help` fix, then add the pipe tests.
4. **python program proving** and the main-checkout advice, with the main-checkout test rows.
5. **PowerShell write reading**.
6. **Environment reading**, then **git-hooks-path** and the **pm-data evasion checks**, which share the env and alias code.
7. **git read classification**, then **git-shared-ref**.
8. **owner-tool merge**, then **heredoc-winpath**.
9. **role-lock**: cut it to the parsed check now, and delete it once the lock checks its own caller. Then **lock-mcp**, once the lock MCP loses its write tools.

Each step rewrites docs/guard.md, so its rules table always matches the code.
