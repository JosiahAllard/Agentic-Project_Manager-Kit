# Test tools

Three tools for a checkout's own test runs: `tools/testlist.py`, `tools/tool_deps.py` and `tools/pytests.py`. All three are ported from the PM kit (kit/pid-hold d151596) and are not live. Each one takes `-C DIR`, the checkout, default here. Each exits 2 on USAGE or on a refused configuration or checkout. Their keys are `.kit.json` "testlist" and "tool_tests"; the defaults are in `kitlib/config/schema.py`, and docs/kit-json.md lists them.

## testlist

`tools/testlist.py` (kitlib/testlist) lists the test names in a tree's result files, so that a move or rename that loses a test shows.

- `--save FILE` before the change, then `--diff FILE` after it. The diff lists removed (`  - `) and added (`  + `) names.
- `--against NEW` diffs two saved lists.
- `--status` adds passed, failed or skipped to each line.
- Result globs: "testlist" `junit` and `trx`.
- Exit codes: 0 same, listed or saved; 1 the names differ; 2 nothing found, or no result newer than the saved list.

## tool_deps

`tools/tool_deps.py` (kitlib/tooldeps) works out which tools test modules a change can break.

- `PATH ...` prints the selected modules, or ALL.
- `--graph` prints every tools module and its direct edges.
- `--check` audits the "tool_tests" tables and exits 1 on a problem.

The tables are read from "tool_tests": `extra`, `pinned`, `patterns`, `whole_tree`, `untested`, `dynamic_imports`, `always_run`, `selector_files`, `ignore`, and the rest of that section. A `{"piece": ...}` or `{"solution": true}` pin needs a pieces table that the entry point does not pass, so it is refused when it is read.

## kit_tests

`tools/kit_tests.py` (kitlib/tooldeps/selftree.py) runs the kit's own suite, whole or only the test packages a branch's change can reach. Kit build agents run their branch with `--select`, and kit landings gate with `--select` too; `--full` runs the whole suite by hand. `gate.py --full` and `daily_full.py` run the whole suite once the kit-suite gate step has a `full_cmd` running `--full` (docs/gate.md "Steps") and `daily_full` is enabled in `.kit.json`; the owner schedules daily_full.py. Wrap either in the machine lock, booking 4 cores (8 logical processors, so 8 modules at once); `<repo>` is `repo.name` in `.kit.json`:

```
python ~/.claude/tools/machine_lock/machine_lock.py run --repo <repo> --slug <slug> --lock --kind engine-test --cores 4 -- python tools/kit_tests.py --select
```

- `--select` diffs against the merge-base with `--base` (default: gate.md "Default base"), the work tree and untracked files included. `--changed PATH ...` takes the list instead.
- It prints each selected `tests/<pkg>` with the reasons it was picked, then runs those packages' test modules.
- `--full` runs every module `python -m unittest discover -s tests -t .` would load. `--plan` prints the selection and runs nothing.
- Tests run on the fake git ("Fake git" below); `--real-git` runs every test on real git instead.
- Nothing selected (a docs-only change) prints `kit_tests: OK, nothing selected (N changed path(s))`, so a gate whose pass pattern is `kit_tests: OK` passes; `--plan` prints the same line.
- Exit codes: 0 passed, nothing selected or `--plan`; 1 a test failed; 2 USAGE, git could not list the change, or `--select` with a malformed table; 130 Ctrl+C. On 2 no test ran.
- `--full` is the escape hatch: with a malformed table it prints a warning, treats `serial` as empty and runs every module. Only `--select` refuses.

Parallel runs (kitlib/tooldeps/selfrun.py):

- One unit is one test module, or one test class of a split module, run as `python -m unittest <unit>` would run it. A unit past its timeout is killed and reported TIMEOUT, and a unit that ran no test is a FAIL.
- Units run in a pool of long-lived interpreters, one per job (kitlib/tooldeps/pool.py, pool_worker.py), so a run starts about one interpreter per job, not one per unit. Each worker has its own kill-on-close job. Each worker imports the units' top-level packages once, before its first unit (selfrun `unit_packages`), so their import code, audit hooks included, does not run again per unit.
- Between units a worker puts back cwd, os.environ (TEMP and TMP included), sys.path, sys.argv, the modules the unit imported and the module globals it changed (pool_worker.py lists the rest).
- A worker is replaced when its unit times out, crashes, is interrupted, or leaves a thread or a process running, and when it ended while idle. Ending a worker returns once every process its job held has ended (pool.py `GONE_WAIT`, 5 s at most). A unit that leaves a non-daemon thread running FAILS, since its own process would never have exited. The log's `kit_tests: interpreters:` line counts the workers, the replacements and why; a `kit_tests: pool worker replaced after` line names the unit behind each replacement.
- A test module with a `KIT_TESTS_OWN_PROCESS = '<why>'` line runs in its own `python -m unittest` process (kitlib.pytests.runner.run_unit), and the log says why.
- `--jobs N` units run at once. The default is the logical processors the lock run holds (`MACHINE_LOCK_CPUMASK`); outside a lock run it is 2, and the tool says which.
- Units start longest first by the seconds each took when last run, recorded in `kit-tests-times.json` in the git common dir (shared by work trees, never a work-tree file). Units with no record start first: the table's `long` modules in order, then the largest source.
- A module expected to take more than half the ideal wall time (expected seconds summed over jobs; at least 60 s) runs one unit per test class, so it cannot floor the wall time; with no record, the `long` modules are split. A module with a `load_tests` hook, or with tests from classes it does not hold by name, is never split. The log names each split module.
- Each unit gets its own TEMP and TMP folder, removed when it ends, so scratch files and scratch lock stores never meet.
- The run folder that holds them is a `kit-tests-*` folder in the kit temp folder (see "Temp folders"), so fixture repositories stay on the checkout's drive. When that folder cannot be made, the run uses the user's temp folder and its log says why.
- When `git` on PATH is Git for Windows' `cmd\git.exe` launcher (as from PowerShell), units get its `mingw64\bin` first on PATH, so each git call is one process, not two; the log says so. Git Bash already does this.
- The table's `serial` modules run after the pool, one at a time.
- Once every pooled unit has started, each unit that ends gives back the cpu slots the units still running do not use, the highest slots first (`kitlib.lock.giveback.drainer`, docs/lock-api.md "Giving back slots"), so a queued run starts on them before this run ends. Only when the lock run offers it (`machine_lock.py run` from this kit, or a gate's last step) and no `serial` module is still to run; the log says what was given back. Units never get the offer. A run under the installed monolith wrapper (the installed `machine_lock.py`) gives back nothing, because only kit-wrapped runs offer slots back; with no offer the log says `kit_tests: give back: no offer ...` once.
- One child process first asks unittest's loader how many tests each module holds. A module with none (a helper `test*.py` matches) is listed and not run. A module whose tests come from an import still counts.
- Ctrl+C starts no new unit, waits for the running ones to end, removes the temp folders, prints the summary and exits 130.
- The log has a line per unit as it ends, the full output of every unit that did not pass, a table per test package, then one summary line.
- `--jobs 1` runs the single `python -m unittest` process instead.
- `--pid-hold` (opt-in, off by default) first starts the pid hold (section "The pid shield"), so no process a test starts can be given a pid that a stale taskkill still names.

The import graph is derived on each run, never written by hand. A changed path selects as follows:

- `kitlib/<m>/...` selects `tests/<m>`, the tests of every kitlib package that imports `m` transitively, the test packages that use any of them (`from kitlib import X` counts), and the test packages naming a tool that imports them.
- `tools/<x>.py` selects the tests of the kitlib packages `x` imports, leaving out the shared core, and the test packages naming `x` or a tool that imports it. A deleted tool still selects the tests of the tools that import it and of the packages that run it.
- A kitlib package whose code holds a string naming `<x>.py` runs that tool, so a change reaching `x` also selects that package and what imports it. A shared-core package that runs it adds only its own tests.

Known limits: a run is seen only as a string literal naming `<x>.py`, so a path built at run time is missed; and a shared-core package that runs a reached tool selects only its own tests, not its importers.
- `tests/<t>/...` selects `t` and the test packages importing `tests.<t>`.

`tests/selection.json` holds the table; a change to it runs the full suite.

- `shared_core`: kitlib packages whose change runs everything. Default git, config, proc, lock.
- `full`: globs that run everything. These are the test helpers (`tests/*.py`), any file directly in `kitlib/`, the selector and the gate.
- `paths`: `[glob, [packages]]`, added on top of any rule. It is for a test that reads another folder, such as a scrub test, or `tests/skills`, which checks that every tool, kitlib and optional path a skill names exists.
- `by_name`: globs selecting the test packages that name the file. If none names it, everything runs.
- `prose`: the same, but a file no test names selects nothing and is listed as `(no test)`.
- `always`: test packages that read every tracked file (`scrub`: no ruling id, no machine path in any file `git ls-files` lists). Any non-empty change that does not run everything adds them, so a doc edit cannot reach main unscanned.
- `serial`: `[module, why]` pairs for test modules that cannot run beside others; each runs alone after the parallel phase.
- `long`: test modules known to run long; with no recorded time they start first and run split by class.

Any other path is unmapped and runs the full suite: unmapped never means skip.

## Fake git

kitlib/git/fake answers git inside the test process (`fake.run(args, cwd, stdin, env)`), reading and writing the real on-disk repository (objects, packs, refs, index, config), so real git can carry on in a repository it touched. It exists to cut the suite's process starts, most of which were git: the full suite at 12 jobs went from 24848 process starts (24021 git) in 278 s to 1972 (1131 git) in 89 s (machine-lock runs r-20261010T120203Z-07ff66 at d4d6921 and r-20261010T123449Z-d7787a on kit/fake-git, each start logged by a sitecustomize audit hook).

- It is on while `KIT_FAKE_GIT=1` (kitlib.git.cmd `fake_active`). kit_tests gives the units 1 unless the environment sets it (`--real-git` gives 0); tests/__init__.py does the same for a direct `python -m unittest` run. Production never sets it.
- Routed to it: `cmd.call` and `cmd.run`, `cmd.spawner()`, `cmd.subprocess_run` (kit code that starts git itself) and kitlib.git.batch's cat-file. A test that swapped `cmd._run` or `subprocess.Popen` keeps its stub. `cmd.genuine()` is false under the fake, so kitlib.git.cache keeps none of its answers.
- Commands (`fake.COMMANDS`): add branch cat-file check-ref-format checkout clone commit commit-tree config diff diff-tree fetch for-each-ref hash-object init log ls-files ls-remote ls-tree merge merge-base merge-tree mv push read-tree remote reset rev-list rev-parse revert rm show status switch symbolic-ref tag update-index update-ref var worktree write-tree.
- Anything else, or an option it does not know, raises `Unsupported` ("unsupported in fake git: <what>"). It never falls back to real git. Unsupported is a BaseException, so `except Exception` around a git call cannot take it for an answer.
- Unreadable files: an open of a ref or a loose object that Windows denies for a moment (another holder excludes readers) is retried for about half a second, then raised; a lock whose write or rename fails is removed (kitlib/git/fake/refs.py `_read_file`, `RefLock.commit`; objects.py `_read_loose`). A tree object that is missing ends the command with git's "fatal: unable to read tree (<oid>)" instead of reading as empty (revs.py `tree_entries`), so `read-tree` cannot write an index that silently lacks the paths under it. Tests: tests/git/test_fake_tier.py `FakeRefReadRace`, `FakeUnreadableObjects`.
- Stat data: like git, it trusts the index's stat data for an entry that is not racily clean, and compares ctime only under `core.trustctime` (tests.gitfixture sets it false). It ignores `core.checkStat`.
- Guard: while `KIT_FAKE_GIT` is 1, tests/__init__.py's audit hook raises when a test process starts git.
- The integration tier: a class marked `@real_git('<what the fake lacks>')` (tests/realgit.py) runs on real git. Today (27 classes) that covers hooks, the reflog, blame, `git grep`, `@{-N}` branch shorthand, shallow clones, combined diffs, directory rename detection, a broken loose ref, the git process itself (its environment, a timed-out call's process tree, counts of git children, a prune from another process) and checks of the fixture and of the guard's filesystem places against git. `kit_tests --real-git` runs every test on real git.
- The gate does not run the real-git tier: `kit_tests --full --real-git` is run by hand until a scheduled run exists.

## Temp folders

The kit temp folder for a checkout is `kitlib.config.user.work_temp`. It holds kit_tests' run folders and the default gate log base (docs/gate.md). In order:

- the machine config's `temp_dir` (docs/kit-json.md), when set;
- on Windows, `AgentTemp` at the root of the checkout's drive, when the checkout is not on the drive of the user's temp folder (a Dev Drive, say);
- otherwise the shared temp folder (`shared_temp`: `%LOCALAPPDATA%/Temp` on Windows), so no drive-root folder is made beside it.

It never reads TEMP or TMP, so every process and shell gets the same folder. Folders that cannot be made, or a machine config that cannot be read, fall back with a note: run folders to the process's temp folder, gate logs to `fallback_log_base`.

Tests that use `tempfile` or tests/gitfixture.py follow their unit's TEMP, so they land in the run folder. A plain `python -m unittest` uses the caller's TEMP.

## pytests

`tools/pytests.py` (kitlib/pytests) is the parallel tools-test runner.

```
pytests.py [-C DIR] [--audit] [--jobs N] [--unit-timeout S] <module|module.Class> ...
pytests.py [-C DIR] --all ...
pytests.py [-C DIR] --select [--base B] [--merged] [--changed PATH ...] [--plan] [--audit]
add --pid-hold to any run: the opt-in pid hold (section "The pid shield")
```

- **Units.** Each unit (a module, a shard of one, or a class with `split_classes`) runs in a fresh child `python <kit>/tools/pytests.py --child`. The child's cwd is the checkout root, and the checkout's tools folder is first on sys.path.
- **Order.** Up to `jobs` units run at once. `serial` units run afterwards, one at a time.
- **The log's last lines** (kitlib/pytests/runner.py `run`):
  - `pysel floor N`: the static `def test_` count of what was named;
  - `Ran N tests in Xs`;
  - `OK` or `FAILED (units: ...)`.
- **Exit codes.** 0 for all passed, nothing selected, or `--plan`; 1 when a unit failed or timed out.
- **Refusals (exit 2).** A checkout without its tools folder. A `--select` whose change list git could not read: no test runs, and the last line is `pytests FAILED | no test ran: git could not read the change list`.
- **`--select`.** Takes `git diff --name-only --no-renames` from the merge-base with `--base` (default: gate.md "Default base"), plus untracked files, or the `--changed` list. These go through kitlib.tooldeps' selection.
- **`--audit`.** Fails a unit (`[FAIL] audit:` lines) for any read under the checkout that the tool_tests tables do not map to its module.

Keys, all in "tool_tests":

| Key | Default | Meaning |
|---|---|---|
| `shards` | `{}` | module -> number of class-shards |
| `serial` | `[]` | modules or `module.Class` run alone, after the pool |
| `split_classes` | false | one unit per TestCase class |
| `jobs_max`, `jobs_divisor` | 6, 4 | default jobs = min(jobs_max, max(2, cpus // jobs_divisor)) |
| `jobs_env` | none | an environment variable whose integer overrides the default job count; kept only as the source used it (no env override of root or config) |
| `unit_timeout` | 3600 | seconds before a unit is killed and reported TIMEOUT |

### Running it from the gate

The gate refuses `"python_tests": "pytests"` and `"python_tests_floor"` (kitlib/gate/steps.py `unsupported`; docs/gate.md, PM kit row). Write the run as an ordinary step instead, and put the floor in its `pass` patterns:

```json
{"name": "tools-tests",
 "globs": ["tools/**"],
 "cmd": ["{python}", "{here}/pytests.py", "-C", "{root}", "--select", "--base", "{base}"],
 "merged_args": ["--merged"],
 "pass": ["^OK$", "^Ran ([5-9]\d|\d{3,}) tests in "]}
```

- `{here}` is the kit's tools folder (kitlib/gate/steps.py `mapping`).
- The second `pass` pattern is a floor of 50 tests. Set it to the repository's own count.
- A run that selects nothing prints no `Ran` line. With a floor, that run fails the step, so give the step `globs` that match only when tests would be selected.

### What was not ported

- The PM kit's `-C` hand-off (chdir_main, KIT_CALLER_CWD, OWN_COPY), the repo guard, and the KIT_REPO_ROOT / KIT_CONFIG overrides. The root is always `-C`.
- The POSIX kill branch. The kit is Windows only.
- The `_kit_module` loader. The child imports kitlib.

## The pid shield

kitlib/proc/shield.py (`shield`, `release`, `process_dacl`) adds one entry to a child process's DACL that denies PROCESS_TERMINATE to Everyone. A stale `taskkill /PID <n> /T /F` aimed at a dead shell then cannot end the child, while a job's TerminateJobObject still can.

- Call it while the child is still suspended (kitlib.proc.job `Job.start`'s `before_resume`).
- A process with a NULL DACL is left as it is (OSError).
- It is not wired into the gate.
- Its users are kitlib.pytests and kit_tests' pool. `runner.shield_unit` shields each unit's own process and each pool worker (kitlib/tooldeps/pool.py), and never the processes a unit starts.
- A unit that cannot be shielded runs unshielded, with a `pytests: could not shield ...` note.
- Never end a unit by its pid. End the runner: KILL_ON_JOB_CLOSE ends its units.

The pid hold covers the processes a unit starts. A test's git can be given the pid of a shell that has just died, and the second stale taskkill then ends it with exit 1 and no output. That is the same shape as a real exit-1 answer, such as `merge-base --is-ancestor`'s NO.

Without the hold, the ancestry question is still read safely. kitlib/git/refs.py `is_ancestor` asks `git rev-list -n 1 <descendant>^{commit}..<ancestor>^{commit}`, whose YES (nothing printed) and NO (one commit) both exit 0, so a killed git gives no answer (None) rather than a NO. Each NO is then confirmed with `git merge-base --independent`, because rev-list's walk can stop early under skewed commit dates and print a commit the descendant does reach. A confirmation that fails is also no answer. The kit's former raw `merge-base --is-ancestor` callers in kitlib/worktree, kitlib/pmdata, kitlib/redfirst and kitlib/board now call it.

- **Opt-in, never in the gate.** `--pid-hold` on `tools/kit_tests.py` or `tools/pytests.py` turns it on; without it no hold is made. It is for agent test runs. The gate refuses `--pid-hold` in `setup`, in a step's `cmd`, `full_cmd` or `merged_args`, and in a landing lane's merged_args (kitlib/gate/steps.py `unsupported`, `build`). There is no environment switch, so nothing can leak it into a gate step.
- kitlib/pytests/runner.py `PidHoldRun.start` starts this process's `PidHold` (kitlib/proc/shield.py `hold()`). It warms the hold for `WARM` (3 s), then publishes it. kit_tests starts it before it copies the units' environment, so the published hold reaches pooled and `--jobs 1` units alike.
- The hold keeps every pid of the user's processes unused until 3 s after its process exits. A stale kill then names an exited process and ends nothing.
- A run that inherits a running hold (`shield.inherited()`) makes no hold of its own.
- **Reporting.** The run prints `pid hold: on (...)` before the first unit and `pid hold: held to the end of the run` before its summary (pytests: before `pysel floor`, so the last line stays `OK` or `FAILED`). A hold that cannot be made, for any reason, prints `pid hold: FAILED, this run is NOT held (<error>)` at the start and `pid hold: FAILED at the start, this run was NOT held (<error>)` at the end; the tests still run. A hold that stops during the run prints `pid hold: FAILED during the run, units after it stopped were NOT held (<error>)` at the end. Unlike the shield's messages these say FAILED: the gate never runs them.
- The gate starts no hold of its own.

Other reads whose "no" was exit 1 with no output now ask in a form that exits 0 either way (KIT-GIT-EXIT1-ANSWERS):

- kitlib/git/refs.py `ref_commit` asks `for-each-ref <ref>`, so a missing ref is exit 0 with no line. A broken loose ref (a file holding garbage) is also exit 0 with no line, but git warns "ignoring broken ref <ref>" on stderr, so no line with anything on stderr is no answer (tests/git/test_git.py `_Parity.test_a_broken_loose_ref_is_no_answer_and_its_siblings_still_answer`). The fake git imitates that warning in for-each-ref only (kitlib/git/fake/refs.py `BrokenRef`). The backlog store (kitlib/backlog/store.py, at_landing.py), kitlib/pmdata/init.py and kitlib/worktree/remove.py `pulled_by` read refs this way.
- `refs.current_branch` asks `branch --show-current`, which prints nothing on a detached HEAD.
- `refs.merge_base` confirms a "no common commit" by comparing the root commits each side reaches (`rev-list --max-parents=0`).
- kitlib/pmdata/init.py reads config with `config --list -z` and filters it in Python.
- `refs.resolve` asks `rev-parse --verify` without `--quiet`, so "no such rev" is exit 128 with `refs.NO_SUCH_REV` alone on stderr, and a silent exit 1 is no answer. By default both still return None, with different whys, so every caller's "missing" path is unchanged; `known=True` returns `('', None)` for "no such rev". kitlib/worktree/remove.py `tag_tip` and `branch_tip` use `known=True`: a killed tag read refuses the abandon ("cannot read tag") instead of taking the name for free.

Reads that still take a quiet exit 1 as an answer include kitlib/guard/prepush.py (`config --get-regexp`, `rev-parse --quiet --verify`), kitlib/ledgerrefs `git grep`, and the other `rev-parse --verify --quiet` reads (kitlib/backlog/landable.py, kitlib/syncbase, kitlib/worktree/new.py, kitlib/redfirst/run.py, kitlib/docs/budget.py, kitlib/reviewpack/trailers.py, kitlib/preflight/settings.py). The default-mode callers of `refs.resolve` still treat a no-answer None as "missing".

### What the hold costs

`PidHold._pass` measured 11.5-14.8% of one core while the hold runs (kitlib/proc/shield.py, comment on `_pass`).

The whole kit suite, measured 2026-10-10 on the development machine (12 physical cores, other agents' runs sharing it). Every run was `python tools/kit_tests.py --full [--pid-hold]` booked through the machine lock with `--kind engine-test --cores 4` (8 logical processors, 8 jobs), 3794 tests in 170 modules, all passing. "Suite" is kit_tests' own `OK in Ns`; "wall" is the whole lock run, booking and the hold's warm-up included.

| Runs | Hold | Suite (s) | Wall (s) |
|---|---|---|---|
| solo | off | 95, 83 | 96, 84 |
| solo | on | 88, 86 | 92, 90 |
| 2 at once | off | 112, 106 | 113 |
| 2 at once | on | 115, 107 | 118 |

- The steady cost does not show above run-to-run noise: two solo runs without the hold differed by 12 s.
- The fixed cost is the 3 s warm-up, paid once per run before the first unit.
- Two holds at once (one per run) added 1-3 s per run and 5 s to the pair's wall time, inside the same noise.
- Whether the gate should ever use the hold is open (docs/go-live.md).
