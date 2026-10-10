# gate.py

The universal gate: `tools/gate.py` (entry) over `kitlib/gate` (library). ARCHITECTURE.md "Contracts" (Booking, Kinds, Gate-OK record, Landing) is the design; this page is the behaviour. Keys: docs/kit-json.md "gate" and "landing".

## Commands

| Command | Does |
|---|---|
| `gate.py [-C DIR] [--base REV] [--full \| --only STEP ...] [--plan] [--slug S] [--log-dir D]` | Gates the checkout under `book(<gate.kind>)` (default `engine-test`), booked as a gate run: the build daemons it leaves that serve nothing outside its job are stopped at its end (Build daemons). The base is the default base (Default base); the merge base with HEAD is what is compared. |
| `land.py BRANCH [BRANCH ...] --expect-main SHA --expect-tip SHA [--expect-tip SHA ...] [-C DIR] [--lane L] [--summary T] [--full] [--fresh] [--wait S] [--log-dir D] [--keep-branch]` | The landing tool (tools/land.py): lands BRANCH, or a batch, from the main checkout (see Landing). One `--expect-tip` per branch, in the same order. `--wait` defaults to `"landing".wait_s`. Landed, each branch is cleaned up unless `--keep-branch` (see Branch cleanup). |
| `land.py --sweep-landed [--apply] [-C DIR]` | The branch cleanup for every branch main already contains (`kitlib.gate.cleanup.sweep`; see Branch cleanup). Without `--apply` a dry run: it changes nothing and names what would go. |
| `land.py --queue [-C DIR]` | Read-only: the landing lock's holder, then the landing queue in turn order (`kitlib.gate.land.queue`). |
| `gate.py land BRANCH --expect-main SHA --expect-tip SHA [-C DIR] [--lane L] [--summary T] [--full] [--fresh] [--wait S] [--log-dir D] [--keep-branch]` | The same landing for one branch, with `--wait` defaulting to 0 (see Landing). |
| `gate.py wait-result BRANCH --tip SHA [--kind gate\|land] [--timeout S] [-C DIR]` | Read-only. Waits until BRANCH's final result record names a tip starting with SHA (7 to 40 hex digits), prints it and exits 0 when its verdict is `pass`, 1 for any other verdict, 7 when no such record appears within S seconds (default: no limit). See Result record. |
| `gate.py --measure --out PATH [--days N]` | Writes a proposed kinds table from the lock's run history (kitlib/gate/measure.py). It never writes the live kitlib/lock/kinds.json. Every kind's cpu in the proposal is a whole number 1..the layout's agent_cap: the p90 peak busy cpu slots rounded up, or agent_cap when the runs fill what they booked (pinned runs cannot show more). A kind with too few runs is written as the size it books on this layout now. It prints one line per kind. |

## Default base

`kitlib.git.refs.default_base` is the one rule every kit tool uses when no `--base` is given: `refs/remotes/<remote>/<main>` (as last fetched) when it resolves, else the local `<main>`. A local main lags whenever nobody pulls into the main checkout, and measuring against it counts the remote's newer commits as the branch's work. When the local main is at another commit the tool prints `base: local main <hash> is N behind <remote>/<main> <hash>; measuring against <ref>` (or ahead, or both). An explicit `--base` is always used as given. Users: gate.py, doc_budget.py, review_pack.py, ruling_cites.py, forbidden.py, kit_tests.py and pytests.py `--select`; preflight.py and board.py measure their rows against `<remote>/<base>` when the two differ.

Every kit `git fetch` and `git ls-remote` runs with `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never` and `GIT_HTTP_LOW_SPEED_LIMIT=1000`/`GIT_HTTP_LOW_SPEED_TIME=60` (`kitlib.git.cmd.NETWORK_ENV`): a remote that wants credentials, or a transfer stalled below 1000 bytes/s for 60 s, fails instead of hanging. An ssh passphrase prompt is not covered (forcing `ssh -o BatchMode=yes` would override the user's `core.sshCommand`, `GIT_SSH` and ssh config; see the `kitlib.git.cmd` docstring). A push keeps the user's credential prompts.

## Exit codes

From kitlib/gate/__init__.py. The machine lock's refusal codes pass through unchanged (kitlib/lock/store.py: 6 busy, 8 timeout, 10 require-lock, 11 reserved, 12 hold). Ctrl+C is 130.

| Code | Name | First line printed |
|---|---|---|
| 0 | OK | `GATE OK[ (warned: ..; skipped: ..)]`, `LANDED <branch> on <main> at <merge> (pushed and verified on <remote>)` (followed by `WARNING: the landing lock could not be released: ...` and the command to break it when only the release failed), or `proposal written: <path>` |
| 1 | FAILED | `GATE FAILED: <steps> (<first>: <reason>)`, or `NOT LANDED: ...` (red merge undone, conflict aborted, a step left untracked files in the main checkout, an exception or SystemExit after the merge that was undone) |
| 2 | USAGE | `USAGE: ...`: bad arguments, an `--only` name no step has, a `--base` that does not resolve, an unknown `--lane`, a machine-lock usage error such as an unknown `"gate".kind`. Never worded as a refusal (refusal-log P10). |
| 3 | STUCK | `STUCK: landing <branch> stopped in phase <phase>: ...`, then what is left and the steps to clear it (see Landing); the landing lock stays held at phase `stuck`. A step that committed or changed a tracked file is STUCK, and so is a Ctrl+C or SystemExit whose undo cannot complete |
| 4 | REFUSED | `REFUSED: ...; nothing was changed` or `... nothing was merged` (a config error before the merge, `"landing".lock_file` included, is one); `REFUSED: no gate step selected: ...` when the steps cover none of the changed paths; `REFUSED: no gate step ran: ...` when every selected step was skipped for a missing `requires` (it names each one and what it requires). In both, no record is written and a landing undoes its merge. |
| 5 | BUSY | `BUSY: landing lock: HELD ...; nothing was changed` (the turn or the lock not had within `--wait`; with landings ahead in the queue, `landing queue: N ahead: ...` follows the holder), or `BUSY: ... not broken, because ...` at once for a gone holder that left something unpushed |

`gate.py wait-result` has its own codes: 0 (the record's verdict is `pass`), 1 (any other verdict), 2 (USAGE), 7 (TIMEOUT: no final record at that tip in time).

Any other machine lock refusal prints `NOT RUN: the machine lock refused the gate: ...`. Ctrl+C is 130 only when nothing is left: before the merge, or after it once the merge was undone and the lock released.

A landing's verdict is its exit code, and in text its first line starting `LANDED `, `NOT LANDED:`, `STUCK:`, `REFUSED:`, `BUSY:`, `USAGE:` or `NOT RUN:`. Key on that line, not on `^GATE (OK|FAILED)`, which is the merged gate's own verdict (`kitlib.gate.runner.verdict`). A `GATE OK` line appears only in a landing that LANDED; a green merged gate that is then not landed prints it as `merged gate passed ...`, so no `GATE OK` line stands beside a `NOT LANDED`.

## Steps

kitlib/gate/steps.py. A step has a `cmd` (argv with `{python} {root} {base} {here} {tools}` substituted) or a `check` (a function in the repo hook module's `GATE_CHECKS`).

- **Full command.** A `cmd` step may also have `full_cmd`, an argv substituted the same way. A full run (`gate.py --full`, `land.py --full`, `daily_full.py`) runs `full_cmd` instead of `cmd` (`steps.build(full=True)`); every other run, a landing without `--full` included, runs `cmd`. `merged_args` are appended to whichever runs. Use it for a step whose `cmd` selects tests by the change: on an unchanged tree such a `cmd` selects nothing and passes without running a test.

- **Selection.** In diff mode a step runs when it is `always`, or when a changed path (tracked diff from the merge base, plus untracked files git does not ignore) matches its `globs` and none of its `exclude`, or when its `content` regex is in a changed file. `--full` runs every step that is not `manual`. `--only` runs exactly the named steps. `skip_if_selected` drops a step. Order: `setup`, then stages `first`, `main`, `last`, then `defer`.
- **Judging.** A step passes only when its exit code is in `ok_exits`, every `pass` pattern matches, no `fail` pattern matches, a cmd step's log is not empty, and it did not run zero tests (`Ran 0 tests` or a `zero_tests` pattern). The `errors` policy must also find no unexplained line. A `warn` step that fails, or a `warn_lines` hit, is WARN. A FAIL stops the rest unless the step has `continue`. A `requires` entry is a command name (looked up on PATH) or an argv probe that must exit 0 within 60 s (`PROBE_TIMEOUT`; on timeout its whole process tree is ended). One that is missing is SKIP, or FAIL with `requires_strict`. A gate whose selected steps were all skipped did not run anything, so it is REFUSED, not OK.
- **Reuse.** A cmd step with `reuse` (`true`, or a group name: a group is reused whole or not at all) may be counted as passed in a landing without running (see Gate reuse). A check step never is.
- **Check contract.** `GATE_CHECKS[name](ctx)`; ctx has `root base merged changed options name log run`. `run(argv, cwd=None)` runs in the slot's job and returns the exit code. A check returns 0/True to pass. Raising fails the check, and the traceback goes in the log. Calling `sys.exit` (any code, 0 included) also fails it, and the code goes in the log. Ctrl+C is not caught: it stops the gate.

## Build daemons

At the release of a gate run that ended "done", `kitlib/lock/run.py` `Slot.stop_daemons` stops the build daemons the gate's commands left in its job (policy: `machine_lock.daemons`, default `gates`, in `kitlib/lock/site.py`). A daemon is shared: any build on the machine may reuse one this gate started, so `kitlib/proc/daemons.py` `stop` ends one only when it is shown to serve the gate's job alone:

- MSBuild nodes and the C#/VB compiler server (VBCSCompiler) are always left running. Their clients reach them over named pipes, and which process holds the other end of a pipe cannot be read from outside.
- A Gradle or Kotlin daemon is stopped, with its children, only when the other end of every established TCP connection on this machine (`kitlib/proc/tcp.py` `peers`) of every process in its tree is in the gate's job (a job nested in it counts as in), in that same tree, or a process that has gone. `kitlib/proc/tcp.py` reads the connection table and `kitlib/proc/job.py` `Job.contains` asks Windows (`IsProcessInJob`).
- Anything else leaves it running, never killed: a client outside the job, a client whose job Windows will not tell, a connection on this machine (to a loopback address, or to an address that is the local end of any row in the table) whose other end the table does not show, a connection table or process tree that cannot be read. A daemon in the tree of a daemon left running, or serving a process in that tree, is left too.

Each daemon is said, as `run <id>: stopped the <label> (pid N) it left: <result>` or `run <id>: left the <label> (pid N) running: <why>`, and listed in the run's ended record under `daemons` (`pid`, `what`, `result`; a left one's result is `left: <why>`). A gate nested in another run says them and records none. The tree and the table are read once, before any daemon is ended, so a client that connects between that read and the kill is not seen. Exactly the (pid, creation time) pairs judged are ended, children first, each by `terminate_verified`; the tree is not read again, so a judged child whose pid a newer process now holds is not hit, and a process a daemon starts after the read is not ended. Tests: tests/proc/test_daemons.py, tests/proc/test_tcp.py, tests/lock/test_release_daemons.py.

## Gate-OK record

The branch gate writes `<git common dir>/kit-gate-ok/<sha>.json` (kitlib/records/gate_ok.py) only when all of these hold:

- at least one step was selected, every selected step passed, warned or was skipped, and at least one passed or warned;
- no `--only` was given;
- the base is the default base (`--base` naming another commit makes the gate partial);
- the work tree was clean (no tracked change, no untracked file) at the start and at the end;
- HEAD did not move.

With no step selected, or every selected step skipped, it is REFUSED (exit 4). Otherwise a missed condition prints `no gate-OK record: <why>` and still exits 0 when the gate passed. `gate_config` is `.kit.json` "gate" over its defaults (`kitlib.gate.runner.gate_config`); guard.py and pre-push verify with the same value. A landing writes the record for its merge commit after the merged gate passes, after the checkout is checked unchanged, and before the push.

`config_rev` (an added key, `gate_ok.ADDED_KEYS`) is the commit the gate config was read from: HEAD for a branch gate, `--expect-main` for a landing. `gate_ok.verify(root, commit, config_at=...)` requires it to be in the commit's history and compares the record's `gate_config` with the config that commit holds; `kitlib.gate.runner.verify_record(root, commit)` does that with `.kit.json` read by `kitlib.config.load_at`. `verify(root, commit[, gate_config])` without `config_at` is unchanged.

## Result record

kitlib/gate/result.py. `<git common dir>/kit-gate-result/<stem>.<kind>.json`, beside `kit-gate-ok/`: one folder per repository, shared by every worktree and the main checkout, never tracked. The kind is `gate` (`kitlib.gate.runner.branch_gate`) or `land` (`kitlib.gate.land.Lander.run`).

**Writes.** A run writes the file twice, each time atomically (temp file, then `os.replace`): a `running` record when it starts, then its final record with the same `run_id` and `started_at`. The `running` record replaces any earlier record of that branch and kind, at that tip or another. A branch gate starts the record before anything else; a landing starts it once its locked checks pass, just before the merge. Each write is said as a progress line, `result record: <path>`, before the outcome's own lines, so the verdict line stays last; a write that fails says `WARNING: the result record could not be written: ...` instead, and the outcome, exit code and verdict line stand. The outcome's `result_record` fact is the final record's path, or None. An exception or Ctrl+C that escapes the run is written as verdict `error` and raised again unchanged.

**No record** (the file keeps whatever it held): `--plan`; a detached HEAD; `gate.py` failing to open the machine lock store before the gate starts; a landing that ends before its locked checks pass (USAGE, REFUSED before anything was changed, BUSY on the landing lock: a duplicate or retry overwrites nothing). A process killed outright leaves its `running` record with no final one.

**File name.** The stem is the branch with every character outside `[a-z0-9._-]` written `%XX` (its UTF-8 bytes, capital hex), capital letters included, so `kit/A` is `kit%2F%41`: two branches never share a file, even where file names ignore case. A stem whose first part is a Windows device name (`con`, `nul`, `com1`, ...) has its first letter encoded too. A stem over 120 characters (`result.MAX_STEM`) is cut and ends `~` and a hash of the branch.

| Key | Value |
|---|---|
| `schema` | 1 |
| `kind` | `gate` or `land` |
| `branch` | the branch name |
| `tip` | full hash: HEAD for a branch gate, `--expect-tip` for a landing |
| `base` | the merge base gated against, or `--expect-main`; null when the gate stopped before it knew one |
| `run_id` | one run's id; its `running` and final records share it |
| `started_at`, `finished_at` | UTC, `YYYY-MM-DDTHH:MM:SSZ`; `finished_at` is null while running |
| `verdict` | `running`; or final: `pass`, `fail`, `refused`, `stuck`, `busy`, `usage`, `not-run` (a machine lock refusal), `error` (an exception or Ctrl+C ended the run) |
| `code` | the exit code gate.py gives that outcome; null for `running` and `error` |
| `log` | the run's log folder, or null when no step ran |
| `summary` | the verdict line (`GATE OK ...`, `GATE FAILED: ...`, `LANDED ...`, `NOT LANDED: ...`, `REFUSED: ...`, ...), or the exception for `error` |
| `gate_ok_record` | the branch gate's gate-OK record path, or null (always null for a landing) |
| `cleanup` | a landing that landed: this branch's cleanup (see Branch cleanup); null for a branch gate, a landing that did not land, and one whose cleanup did not run |

A pass is not a gate-OK record: a gate that passes a dirty tree is `pass` with `gate_ok_record` null. A waiting session that treats `pass` as ready to land checks `gate_ok_record` too.

**Waking a waiting session.** The gating session runs the gate as usual (`gate.py`, or `gate.py land`) and tells the waiting session the branch and the tip it is gating (`git rev-parse HEAD`) once the gate has said its first `result record:` line. The waiting session arms after that: before it, an earlier run's record at the same tip would answer at once. `wait-result` is read-only. It blocks until the file holds the final record of that branch and kind at that tip (`branch` and `kind` equal, `tip` starting with `--tip`, any case; a `running` record does not count), then prints two lines, `RESULT <verdict> <kind> <branch> at <tip>: <summary> (<log>)` and the record as one line of JSON, and exits. Example for a branch gate of `kit/x` at tip `<sha>` in the repository at `<repo>`:

```
Monitor({description: "gate result kit/x", timeout_ms: 1800000,
         command: 'python <kit>/tools/gate.py wait-result kit/x --tip <sha> --timeout 1740 -C <repo>'})
```

`<kit>` is the kit checkout (`<tools_dir>` in docs/guard.md), `<repo>` any checkout of the gated repository. Wait for a landing with `--kind land`. The Monitor caps `timeout_ms` at 30 minutes, so `--timeout 1740` ends first. Exit 7 with a `TIMEOUT:` line means no final record at that tip yet: the run is still going, has not started, or ended without one (see No record); arm it again, and look at the gating session when it keeps timing out. The exit code is the verdict (0 pass, 1 anything else, `error` included); the printed record says which. A `--tip` that is not 7 to 40 hex digits, or a `--timeout` that is negative, `nan` or infinite, is USAGE (2). Do not pipe it into another command: guard.py's `wrapped-gate` rule (docs/guard.md) refuses `gate.py` piped.

## Landing

kitlib/gate/land.py runs in the main checkout, on main, in this order:

1. **Usage.** Every hash is a full hash, one `--expect-tip` per branch, each branch and tip named once. The lane is `full` or one that `.kit.json` "landing".lanes declares, read from `--expect-main`'s config.
2. **Refusals.** It refuses when the checkout is not main, HEAD is not on main, a branch is main or missing, the lane's `branch_prefix` does not match, or a batch has more branches than `"landing".batch_max`.
3. **Fetch, queue and lock.** It joins the repository's landing queue, then takes the landing lock (kitlib/records/landing.py) at its turn (see The landing queue). `--wait` seconds covers both, counted from the first look that finds either taken, when it says `waiting up to <s> s for the landing lock` once (`Lander.take`); not had in time is BUSY. Branch gates and other repos never take this lock or queue.
4. **Locked checks.** It fetches again, then checks: main equals `<remote>/<main>` equals `--expect-main`; each tip equals its `--expect-tip`; the tree is clean; no merge is in progress; no branch is already merged; in a batch, no tip contains another; in a lane, every changed path is in its `paths`; for every owner-only path that landing the branch would change, every branch commit that changes it is authored by the owner ("Owner-only commits" in docs/guard.md); and, for a `ref` or `branch` backlog store, no branch's history holds a root commit of the store (kitlib/backlog/at_landing.py `ancestry_refusal`; a store that cannot be read refuses too). Any miss is REFUSED, with nothing merged. A landing that waited (its first look found the queue turn or the lock taken, and `--wait` let it wait) follows main forward: when main and `<remote>/<main>` agree on a commit that descends from `--expect-main`, it merges onto that commit and gates and lands as usual, saying `main moved on from --expect-main <old> to <new>, which descends from it`; the lane, branch and batch checks are rerun against the new main's config. Main moved any other way (rewritten or reset), main and the remote disagreeing, or a new config that names another main, remote or lock is REFUSED. A landing that took its turn and the lock at once, or ran with `--wait 0`, keeps the exact check (`Lander.follow`).
5. **Merge.** `git merge --no-ff <tip>`, for each branch in order. A conflict is aborted, the batch's earlier merges are undone, and it gives FAILED.
6. **Merged gate.** It gates the merged tree once under `book()`, adding each step's `merged_args`, then the lane's `merged_args`. Check steps run the hook module as `--expect-main` has it, read from that commit like the config (kitlib/gate/hookrev.py), so a branch is gated by main's checks, not its own. Step `cmd`s run the merged tree's files. A step may be reused instead of run (see Gate reuse). On red, `git reset --keep <expect-main>` runs (with the merge's 300 s timeout), but only while HEAD is on main, main's first-parent line beyond `--expect-main` is exactly this landing's merges and no tracked file changed. The result is FAILED: a red merged gate never leaves its merge on main. Untracked files a step left are kept: they are named, with a line saying the main checkout needs cleaning.
7. **Unchanged.** On green, HEAD must still be on main at the merge, with no tracked change and no untracked file. Otherwise nothing is recorded or pushed and the merge is undone as on red (FAILED, `NOT LANDED`). A step that committed, changed a tracked file or switched HEAD to another branch makes that undo unsafe, so it is STUCK. A `"landing".log_dir` inside the checkout must be git-ignored: the gate's logs are untracked files there, so the landing is NOT LANDED.
8. **Push.** It writes the record, then runs `git push <remote> <merge>:refs/heads/<main>` (never forced). `git ls-remote` must show the merge. From then on the landing stands (`Lander.phase` `pushed`): a release of the lock that fails, or an exception or Ctrl+C before the release ends, is still LANDED (exit 0) with a WARNING: the tracking ref is set to the verified merge, and the lines name the command to break the lock when it is still held, and the command to close the branch's backlog records when the store has any (`Lander.landed_anyway`).
9. **Close.** After the release, every record a `ref` or `branch` store holds pulled by the branch is closed, `landed <merge>`, one store commit each (`at_landing.close_landed`). A close that fails, and an exception or Ctrl+C here, is a WARNING with the command to finish it; the landing stays LANDED and is never undone.
10. **Cleanup.** Then each branch's remote branch, worktree and local branch go, unless `--keep-branch` (see Branch cleanup). Its lines come before the `LANDED` line.

An exception, SystemExit or Ctrl+C under the lock: before the merge, the lock is released (a config error is REFUSED). After the merge, a merge stopped part way is aborted, and a finished one is undone, and the lock released (FAILED); Ctrl+C is then raised again, so the exit is 130. When that undo cannot complete, or the push had started and `ls-remote` had not yet shown the merge, the result is STUCK (exit 3), Ctrl+C included.

Anything after the merge that cannot be undone safely gives STUCK: an undo that does not verify, an interrupted or rejected or unverified push, or a git failure. The lock stays held so that no landing stacks on top. STUCK prints a `left:` line (the branch HEAD is on, main, the remote's main as `git ls-remote` reads it, the tracking ref, tracked changes, untracked files), then numbered steps to clear it, in order. Each command is on its own line starting `    $ ` (`land.STEP_CMD`). The steps fit what is left (`Lander.clear_steps`):

- **Push unknown** (`ls-remote` failed): the push may have landed. First `git fetch` and `git rev-parse <remote>/<main>`. If that shows the merge, the landing is done and only the lock is broken; if it shows `--expect-main`, go on.
- **Pushed** (the remote has the merge): no reset. When the tracking ref is stale, `git fetch` comes first, since `break_lock` compares main with it.
- **HEAD on another branch**: `git switch <main>` comes before any reset, so a reset never moves another branch.
- **Tracked changes**: keep or drop them by hand; `reset --keep` refuses to overwrite them.
- **Commits on main beyond the merge** (and beyond the branch it merged): they are named, and `git branch kit-stuck-<sha> <main>` keeps them before the reset; re-apply them after the lock is broken.
- **Unpushed**: `git -C <root> reset --keep <expect-main>`.
- Last, a `python -c` line that runs `LandingLock.break_lock` (it refuses while the holder is alive or main differs from the remote's tracking ref).

A gate-OK record is written for the merge before the push. When the merge is then undone (a push that did not land, cleared by hand from STUCK), the record for that merge commit stays in `<git common dir>/kit-gate-ok/`. It is stale but harmless: records are keyed by commit (`gate_ok.record_path`), that commit is no longer on main, and a reader verifying any other commit never reads it. Only a later push of that same merge commit would find it. Delete it by hand when clearing such a STUCK.

**First switch.** A repo's first switch from the old PM kit is landed by the owner, by hand. Its base `.kit.json` fails to load with the kit loader (old key spellings such as `gate.hook`, `doc_budget.owner_email`/`agent_emails`/`owner_only`/`owner_only_except`, and dropped keys such as `dependency_pin_check` and `python_tests_audit*`). Five sites then refuse: the pre-push hook (kitlib/guard/prepush.py), the guard's git-push-main rule (kitlib/guard/rules.py `git_push`), the land.py preconditions (kitlib/gate/land.py), the trailers check (kitlib/trailerscheck) and review_pack's base read.

1. Rewrite the base `.kit.json` to the kit spelling: rename the old keys, drop the removed ones (docs/kit-json.md).
2. Check that it loads with the kit loader.
3. The owner commits and pushes that switch commit himself; `gate.py land`, pre-push and the trailers check refuse until main loads.
4. After that, normal landings work (docs/go-live.md "Landing tool").

Reopen the first-switch item if a repo ever has to switch from the old PM kit.

### The landing queue

Landings into one repository take turns through the machine lock's store: the queue is the exclusive resource `landing:<git common dir>` in `resources.json` (kitlib/lock/resource.py; docs/lock-api.md "Exclusive resources"), so every checkout of a repository shares one queue. A landing joins at the back. Only the first entry has the turn, and no later landing goes ahead of an earlier one, however often it looks (`Lander.poll_s`, 5 s). No landing is ever pre-empted. At its turn the landing takes the lock file:

- **Free:** taken.
- **Held at phase `stuck`:** BUSY at once; a stuck landing is cleared by hand from its STUCK report.
- **Held by a process that is gone** (pid and start time, `LandingLock.holder_state`): when nothing is unpushed, the lock is broken (`LandingLock.break_lock`), said as `broke the landing lock of a holder that is gone ...`, and taken. When main differs from the tracking ref `<remote>/<main>`, or a merge is in progress (`LandingLock.unpushed`), it is never broken: BUSY at once, naming what is unpushed and the break command. When main is only behind the tracking ref, with nothing unpushed, BUSY says so and gives the fast-forward, `git -C <root> merge --ff-only <remote>/<main>`; the next landing then breaks the lock itself.
- **Unreadable or of an old format** (`LandingLock.read` fails): waited for like a live holder, never broken; BUSY with lane `?` when `--wait` runs out.
- **Held by a live process, or by a token holder** (a tool outside the queue): waited for.

A queue entry lives while its process holds the entry's run mutex, as a lock run's queue entry does, so a landing that dies leaves an entry the next look drops. The turn is given up with the lock and whenever the landing ends; STUCK included, since the stuck lock file keeps the next landing out. `land.py --queue` prints the lock and the queue without joining it.

An unreadable `resources.json` (kitlib/lock/resource.py `ResourceUnreadable`) is never read as empty, so every landing in every repository is REFUSED (`the landing lock: ...`) until it is repaired: with no landing running, rename it aside (never delete it) in the machine lock store, the `.claude/machine-lock` folder of the user profile (kitlib/lock/store.py); the next landing to join writes a new one.

### Batch

`land.py A B --expect-tip TA --expect-tip TB` lands A and B as one landing: each passes the locked checks, they merge in the order given (`Merge A: <summary>`, then `Merge B: <summary>` on top), the merged tree is gated once against `--expect-main` (log slug `A+B`), and the last merge is pushed. A conflict on any branch aborts it and undoes the earlier merges; a red gate undoes them all. A branch whose tip is already inside another tip of the batch is refused: land the containing branch alone. Each branch gets its own result record (`facts['result_record']` is then a list). The verdict line names the branches joined by `, `. At most `"landing".batch_max` branches (default 3).

### Gate reuse

kitlib/gate/reuse.py, ported from one repo's gate reuse. In a landing of one branch, a step with `reuse` is not run when the branch gate passed it on exactly the tree the landing merged. It is fail-closed: any doubt or error runs every step, and `gate: reuse: none: <why>` says why. Nothing is reused with `--fresh`, `--full` or a batch, or unless all of these hold (`reuse.landing_why`):

- the result records name this tip with no `land` record (an earlier landing that did not land) and no `gate` verdict but `pass`; the branch's own `gate` record is `pass` and names the gate-OK record;
- that record verifies (`runner.verify_record`) with `config_rev` the tip, `gate_config` that of `--expect-main`'s config, `base` `--expect-main`, `tree` the merge's tree, and `kit` (`runner.kit_identity`: commit, python, clean) the running kit's, a clean commit;
- it ended at most `"landing".reuse_max_age_h` hours ago (0: never reuse);
- the merge changes no `.kit.json`, section file, hook module or `"landing".reuse_never` path, and the main checkout is clean.

Then a step is reused when the record has it as `pass` and its merged-gate argv and cwd equal its branch-gate ones (its `merged_args`, or the lane's, make it run). A reused step's result is `reused` (`REUSED` with its reason in the output, `reused: <steps>` in the verdict); the lines go to `reuse.log` in the log folder. When every selected step is reused, nothing is booked and `setup` does not run.

A reused step does not run, so nothing it would leave behind (a build output, a file a later step reads) exists in the merged gate: give a step whose output a later step needs the same group as that step. In the kit's own repository reuse never fires: the branch gate runs the worktree's kit (`kit.commit` is the tip, when the worktree's own gate.py runs, as The caller's checkout documents; `runner.kit_identity` reads the checkout of the gate.py that ran) and the landing runs the main checkout's (the merge), so the kit identities differ.

### Branch cleanup

kitlib/gate/cleanup.py runs in the landing (`Lander.clean_up`) once the push is verified, the lock released and the backlog records closed: never on a landing that did not land. For each branch, three parts, each judged on its own:

- **Remote branch.** Deleted when main contains its tip: `git push --force-with-lease=refs/heads/<branch>:<tip> <remote> :refs/heads/<branch>`. The lease is a compare-and-swap, not a force: the remote deletes the branch only while it still points at the tip read just before, so a branch pushed to since is refused and kept. `git ls-remote` must then show it gone.
- **Worktree.** Removed by kitlib/worktree/remove.py, the code `worktree_remove.py` runs (docs/worktree-remove.md): under the worktrees folder, landed in `<remote>/<main>`, nothing holding it, nothing uncommitted, untracked or ignored outside build output, no link inside; then `git worktree remove`, never forced. That code then deletes the local branch with `git branch -d`.
- **Local branch** with no worktree: `git branch -d`, never `-D`.

Nothing is forced. A tip main does not contain, a worktree with work in it, a worktree in use, and anything git refuses are kept. Each kept part is named on a `  kept` line under the branch's `cleanup   <branch>: ...` line, and the landing still counts as landed. A branch never pushed has its remote branch `absent`. The result record's `cleanup`, and the landing's `facts['cleanup'][branch]`, hold `remote_branch` (`deleted`, `absent`, `kept`, `skipped`), `worktree` (`removed`, `absent`, `kept`, `skipped`), `worktree_path`, `local_branch` (`deleted`, `absent`, `kept`, `skipped`) and `kept` (one reason per kept part). A local branch git gives no answer for after its delete is `kept`, with "whether it is gone could not be read", never `deleted` (kitlib/gate/cleanup.py `_left`). `--keep-branch` skips all three (`skipped`). An exception in one branch's cleanup is a WARNING and the next branch is cleaned; Ctrl+C stops it with a WARNING. Both name the sweep command, and so does a landing stopped after its verified push (`Lander.landed_anyway`).

`land.py --sweep-landed [--apply]` applies the same rule to every branch, local or on the remote, that main already contains: it fetches `<main>`, and takes a tip in `<remote>/<main>` and off its first-parent line (a branch made from main with no commit of its own is never one). Branches main does not contain are not named. It does nothing while a landing holds the landing lock (BUSY). A dry run, the default, names what would go and ends with the `--apply` command. The landing cleanup and the sweep delete a remote branch only by the lease push above and a local branch only with `git branch -d`; guard.py gains no allowance for an agent's `git branch -D` or `git push --delete`.

## The caller's checkout

A stub run from outside its checkout runs the kit tool in the stub's checkout (docs/install.md "Stubs"), so a shell in one checkout that runs another checkout's gate stub would gate that other checkout. The stub then passes `[stub checkout, caller directory]` on in `KIT_STUB_CALLER`; `tools/gate.py` removes it, so no step inherits it, and for a branch gate (`--plan` included) applies kitlib/gate/caller.py `refusal`:

- The caller's directory is in a different checkout from the gated one (their git dirs differ): `REFUSED: the caller is in the checkout ...`, exit 4, nothing run, no record.
- The caller's directory is in no repository, or is in the gated checkout: it runs.
- git cannot place the caller's directory or the gated tree, or the variable is malformed (a NUL in either path counts): REFUSED (fail closed).
- An absolute `-C` names the tree, so it runs. A relative `-C` resolves against the stub's checkout, not the caller's directory, so the rule applies to it. Without a stub the gate runs in the caller's directory and this rule never applies (the no-stub rule below does).
- A value whose stub checkout is not the gate's own directory was inherited from a stub further up, not set for this process: it is dropped and the gate runs. The two are compared as one directory (os.path.samefile), so a junction, symlink or 8.3 short name for the checkout still counts as its own.

With no stub there is a second hazard, in the kit's own repository: every checkout has a real `tools/gate.py`, so `python <worktree>/tools/gate.py` run from the main checkout with no `-C`, or with a relative one such as `-C .`, would gate the main checkout with the worktree's code. For a branch gate run with no stub and no `-C` or a relative one, `tools/gate.py` applies kitlib/gate/caller.py `own_copy_refusal` to the tree it would gate (the directory it was run from, or where the relative `-C` resolves) and its own checkout (`KIT` in tools/gate.py, the same directory as kitlib/gate/steps.py `KIT_ROOT`):

- The two are different checkouts of one repository (the same git common dir, different git dirs): `REFUSED: <gate.py> is in the checkout ...`, exit 4, nothing run. Each following line gives the documented command, `python <tree>/tools/gate.py -C <tree>`, for one tree, with a path that holds a space in double quotes. A relative `-C` names the tree meant, so only its line is given, headed `  to gate <tree>:`. With no `-C` the tree meant is ambiguous (the gate.py's path names one checkout, the directory another), so both are given, headed by intent: `  if you meant the directory you ran from:` (the checkout the directory is in) and `  if you meant the checkout this gate.py is in:`.
- An absolute `-C` names the tree on purpose and is not checked: main's gate.py with an absolute `-C <worktree>` runs main's code on the worktree.
- They are the same checkout, the gate.py is in another repository (a repository's stubs run the kit checkout written into them, kitlib/stubs `KIT_ROOT`), or either is in no repository: it runs.
- git cannot place the gate.py's checkout (dubious ownership, a git timeout): REFUSED only when its path nests (one inside the other) with the gated checkout or with the folder that checkout's git common dir is in, so the two could be one repository's checkouts; the command lines follow as above. A sibling that only shares a name prefix (`main-co-x` beside `main-co`) does not nest. Otherwise it runs, as a repository's gate did before this check. A directory git cannot place is left to the gate itself.

A relative `--log-dir` resolves against the directory the gate was run from, not the gated tree, so when that directory is in another checkout the logs would be stray files there: kitlib/gate/caller.py `log_dir_refusal` makes it `USAGE: --log-dir <D> is relative ...`, exit 2, nothing run. An absolute `--log-dir`, none, or a directory in no repository passes.

The one documented way to gate a worktree from another directory is that worktree's own gate.py with an absolute `-C` naming it:

```
python <worktree>/tools/gate.py -C <worktree> --full
```

Running the worktree's own gate.py matters in the kit's repository: the gate records the identity of the kit that ran it (`kit.commit`, Gate reuse), and the code under test is the code that gates.

Landings, `wait-result` and `--measure` apply neither rule. Tests: tests/gate/test_caller_checkout.py (`Refusal` and `ThroughAStub` for the stub rule; `OwnCopy`, `LogDir` and `RunByPath` for the no-stub rule and `--log-dir`).

## Known limits

- No per-step timeout. A step that hangs is ended by stopping the gate (Ctrl+C, or the machine lock's stop, which ends the slot's job).
- The hook module's module-level code runs when it is loaded, before the gate is booked. It runs outside the slot, and a `sys.exit` there is REFUSED.
- Files git ignores, and files marked assume-unchanged or skip-worktree, are outside the record. The clean-tree checks do not see them.

## Not ported

These are general behaviour the kit gate leaves out, or repo-specific behaviour, so each repo keeps it in its steps or hook module.

| From | What | Why |
|---|---|---|
| PM kit gate.py | `-C` hand-off | retired |
| PM kit gate.py | `--hook-arg` / `gate_hook` | replaced by the hook module's `GATE_CHECKS` |
| PM kit gate.py | `python_tests: pytests`, `python_tests_floor`, py-drops | refused if set; write the test run as a step with a `pass` floor (docs/test-tools.md) |
| PM kit gate.py | trailer check (`trailers`) | ported as the `cmd` step over `{here}/trailers.py` (docs/repo-checks.md "trailers"); the `trailers` gate key is refused if set |
| PM kit gate.py | `kill_retry`, pid shield | refused if set; the slot's job ends the tree. `--pid-hold` in `setup`, a step's `cmd`, `full_cmd` or `merged_args`, or a lane's merged_args is refused too: the pid hold is opt-in for agent test runs (docs/test-tools.md "The pid shield") |
| PM kit gate.py | pin-check / offline hatch, doc-budget, forbidden paths | repo checks: the doc budget is a `cmd` step over `{here}/doc_budget.py -C <root>` (docs/doc-tools.md); forbidden paths and ruling citations are `cmd` steps over `{here}/forbidden.py` and `{here}/ruling_cites.py` (docs/repo-checks.md); machine paths over `tools/machine_paths.py -C <root>` (0 OK, 1 FAILED, 2 NOT CHECKED; docs/commit.md) |
| PM kit gate.py | dotnet zero-test regexes | repo data: the gate's `zero_tests` |
| PM kit gate.py | machine-state print on failure | the lock's own status shows it |
| PM kit gate.py | `--merged` merge train | a batch landing (Landing, Batch) is merged and gated whole, never as a train of partial landings |
| PM kit gate.py | `--setup` print, `cmd_windows` | `setup` runs as a step; one argv per step |
| PM kit gate.py | worktree removal after landing | ported as the branch cleanup (Landing, Branch cleanup), which runs the `worktree_remove.py` code |
| a repo's land.py | review-pack precondition, records-only rule | repo policy; a lane or a check step |
| a repo's land.py | land_scope classify | now data: `.kit.json` "landing".lanes |
| a repo's gate reuse | log hashes, forecast, stale-base notes, `--force` names | the gate-OK record is the evidence; `--fresh`, `--full` and `merged_args` force a run |

## Go-live notes

- A repo's Python tests must be steps (`python_tests: discover` is the default and runs nothing by itself).
- Turn on `push_block` only after the repo lands through `gate.py land`.
- Once switched, delete the retired tools (ARCHITECTURE.md "Details").
- In a landing, only the hook module's own file comes from `--expect-main`; a hook module that imports sibling files from its tools folder is not supported there (kitlib/gate/hookrev.py).
- A `landing.log_dir` inside the repo must be gitignored. Otherwise the gate's logs are untracked files: a branch gate writes no record, and a landing is NOT LANDED (Landing, step 7).
- Without `landing.log_dir`, gate logs go to `<kit temp>/<repo>-gate/<branch>/` (kitlib.gate.runner.log_base over kitlib.config.user.work_temp; docs/test-tools.md "Temp folders"), never an inherited TEMP, so agents and the owner's shells read the same folder. When that base cannot be made, or the machine config cannot be read, the run's logs go under `runner.fallback_log_base` (`%LOCALAPPDATA%/Temp/<repo>-gate`) with a `gate logs:` note on stderr (`runner.make_log_dir`, which redfirst and daily-full use too); landed.py reads it as well. A Windows process without LOCALAPPDATA and with no usable drive root stops with a RuntimeError instead of falling back to TEMP.
- The gate prints its verdict to stdout only, so landed.py finds one only where a caller saved that output under the gate log folder (docs/landed.md).
- A lander's post-merge stray check: `python <kit>/tools/checkout_guard.py --main-stray --allow-tracked <main checkout>` (exit 1 on any stray, or when git could not tell; docs/preflight.md).
- Doc budget check: `[sys.executable, <kit>/tools/doc_budget.py, -C, <repo>]`, judged with kitlib.verdict (`DOC BUDGET OK` / `DOC BUDGET FAILED`). Exit 2 means refused or a usage error; see docs/doc-tools.md.
- There is no keep-red option. A red merge is always undone by default.
