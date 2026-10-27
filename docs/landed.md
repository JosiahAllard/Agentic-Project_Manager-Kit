# landed

Held for the joint review: no repository runs it yet.

`tools/landed.py` answers whether a branch has landed. It takes one read-only look and never waits, fetches or writes. Logic: `kitlib/landed/__init__.py`; tests: `tests/landed/test_landed.py`.
```
python tools/landed.py [-C DIR] <branch or commit>
```

Exit 0 `LANDED`; 1 `NOT YET`; 2 `FAILED`, `CANNOT ANSWER` (the ref does not resolve, or git gives no answer for it or for whether the remote exists), the kit configuration refused, or usage; 130 Ctrl+C.

## What it decides on

LANDED means the tip is an ancestor of `<remote>/<main_branch>`, read with `git ls-remote`.

- No remote at all (`git remote get-url` exits 2 and `git remote` lists none): the local main branch decides. The remote missing while others exist is a misnamed `"repo".remote`, and that is `CANNOT ANSWER` naming the remotes there are (the rule of kitlib/backlog/store.py `find_remote`; `test_a_misnamed_remote_cannot_answer_and_names_the_remotes_there_are`). Any other failure of that lookup, a git ended from outside included, is `CANNOT ANSWER` (tests/landed/test_landed.py `test_a_killed_remote_lookup_cannot_answer_never_lets_the_local_main_decide`). A local main git gives no answer for is `local ... (UNKNOWN: <why>)` and `contained  UNKNOWN`, never "no such branch here" (`refs.resolve(known=True)`; tests/landed/test_landed.py `test_a_killed_read_of_the_local_main_is_unknown_not_no_such_branch`).
- The remote is unreachable: the last fetched `refs/remotes/<remote>/<main>` decides, and the output says so.
- The remote's sha is not in this clone: `contained  UNKNOWN`, with the fetch to run first. An ancestry read git gives no answer for is `contained  UNKNOWN` with the why, never `no`.

When the branch has not landed, it prints three more lines:

- `lock`: the kit landing lock (`kitlib.records.landing`), free or held with its holder. A file there that is not a lock record counts as held.
- `gate-ok`: whether the tip has a gate-OK record (`kitlib.records.gate_ok`). This line is information only.
- `verdict`: the newest kit verdict line in the branch's gate logs. FAILED needs this line to be a failure, not older than the tip commit, and the lock free. A failure while a landing holds the lock is `NOT YET (a landing holds the lock)`.

## Where the logs are read

Each log base is `kitlib.gate.runner.log_base`:

- with `"landing".log_dir` set, that folder under the checkout;
- otherwise `<kit temp>/<repo name>-gate`, where the kit temp is `kitlib.config.user.work_temp` of the checkout and the repo name is `"repo".name` (docs/kit-json.md; unset, the remote URL's last part, else the main checkout's folder name). With no `name` written, each checkout's own folder name is tried as well (`log_bases`). Then `runner.fallback_log_base` (`<shared temp>/<repo name>-gate`, the shared temp being the machine config's `temp_dir` when its drive exists and the folder can be made, else the system temp folder `%LOCALAPPDATA%/Temp`: `kitlib.config.user.shared_temp`) is read as well: a gate that could not make the default base writes there, and so did every gate before the kit temp moved. A machine config that cannot be read leaves only that base, with a `note` line.

Known limits: only the bases of today's rule are read. A gate that ran while `work_temp` was still a session's TEMP (before it kept out of session temps: docs/test-tools.md "Temp folders") wrote its logs under that older kit temp, which is not read, so such a branch shows no verdict until it is gated again. Likewise fallback logs written under `%LOCALAPPDATA%/Temp` before the shared temp followed `temp_dir` are not read on a machine that sets `temp_dir`.

The checkouts read are the one `-C` names, its main checkout, and every worktree that has the branch checked out.

Under each base it reads two folders, recursively: `log_folder(<branch>)` (the branch gate's) and `log_folder('land-' + <branch>)` (the landing's merged gate's). When neither folder exists, it reads the files directly in the base whose name holds `log_folder(<branch>)`. The newest file that holds a verdict line decides, by its last such line.

A verdict line starts with one of these:

- passing: `GATE OK`, `LANDED <branch>`;
- failing: `GATE FAILED`, `NOT LANDED:`, `CONFLICT <branch> with main <sha>:`, `STUCK:` (`VERDICT` in `kitlib/landed/__init__.py`).

A commit sha names no branch, so no log is read for it.

The kit gate prints these lines to stdout and writes none of them into its step logs. Until the gate saves its verdict in its run folder, a verdict is found only where a caller saved the gate's output under the log folder.

## Keys read

- `.kit.json` "repo": `main_branch`, `remote`, `name`.
- "landing": `log_dir`. The landing lock is always `kit-landing.lock` in the git common dir (kitlib.records.landing `LOCK_NAME`).
