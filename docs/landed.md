# landed

Held for the joint review: no repository runs it yet.

`tools/landed.py` answers whether a branch has landed. It takes one read-only look and never waits, fetches or writes. Logic: `kitlib/landed/__init__.py`; tests: `tests/landed/test_landed.py`. Ported from the PM kit's `tools/landed.py`.

```
python tools/landed.py [-C DIR] <branch or commit>
```

Exit 0 `LANDED`; 1 `NOT YET`; 2 `FAILED`, `CANNOT ANSWER` (the ref does not resolve, or git gives no answer for it), the kit configuration refused, or usage; 130 Ctrl+C.

## What it decides on

LANDED means the tip is an ancestor of `<remote>/<main_branch>`, read with `git ls-remote`.

- No such remote: the local main branch decides. A local main git gives no answer for is `local ... (UNKNOWN: <why>)` and `contained  UNKNOWN`, never "no such branch here" (`refs.resolve(known=True)`; tests/landed/test_landed.py `test_a_killed_read_of_the_local_main_is_unknown_not_no_such_branch`).
- The remote is unreachable: the last fetched `refs/remotes/<remote>/<main>` decides, and the output says so.
- The remote's sha is not in this clone: `contained  UNKNOWN`, with the fetch to run first.

When the branch has not landed, it prints three more lines:

- `lock`: the kit landing lock (`kitlib.records.landing`), free or held with its holder. A file there that is not a lock record counts as held.
- `gate-ok`: whether the tip has a gate-OK record (`kitlib.records.gate_ok`). This line is information only.
- `verdict`: the newest kit verdict line in the branch's gate logs. FAILED needs this line to be a failure, not older than the tip commit, and the lock free. A failure while a landing holds the lock is `NOT YET (a landing holds the lock)`.

## Where the logs are read

Each log base is `kitlib.gate.runner.log_base`:

- with `"landing".log_dir` set, that folder under the checkout;
- otherwise `<kit temp>/<repo name>-gate`, where the kit temp is `kitlib.config.user.work_temp` of the checkout and the repo name is `"repo".name`, else the checkout folder's name. Then `runner.fallback_log_base` (`<shared temp>/<repo name>-gate`) is read as well: a gate that could not make the default base writes there, and so did every gate before the kit temp moved. A machine config that cannot be read leaves only that base, with a `note` line.

The checkouts read are the one `-C` names, its main checkout, and every worktree that has the branch checked out.

Under each base it reads two folders, recursively: `log_folder(<branch>)` (the branch gate's) and `log_folder('land-' + <branch>)` (the landing's merged gate's). When neither folder exists, it reads the files directly in the base whose name holds `log_folder(<branch>)`. The newest file that holds a verdict line decides, by its last such line.

A verdict line starts with one of these:

- passing: `GATE OK`, `LANDED <branch>`;
- failing: `GATE FAILED`, `NOT LANDED:`, `STUCK:`.

A commit sha names no branch, so no log is read for it.

The kit gate prints these lines to stdout and writes none of them into its step logs. Until the gate saves its verdict in its run folder, a verdict is found only where a caller saved the gate's output under the log folder.

## Keys read

- `.kit.json` "repo": `main_branch`, `remote`, `name`.
- "landing": `log_dir`, `lock_file`, `lanes`.

## Not ported

- The `"landing".verdict_pattern` key, and its tests (a configured pattern, its `failed` group, its compile check). The kit's verdict lines are fixed, so `kitlib.config` now refuses the key as unknown.
- `--root`: `-C` replaces it.
- The source tests dropped with retired mechanisms:
  - an absolute `lock_file` and a relative one under the shared temp: the lock is always `git:<name>` in the common dir;
  - an absolute `log_dir`: `log_dir` is repo-relative (RelPath);
  - the flat-folder slash rule (`feat/x` -> `feat_x`): the kit's `log_folder` gives `feat-x`.
