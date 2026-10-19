# landed

`tools/landed.py` answers whether a branch has landed. It takes one read-only look and never waits, fetches or writes. Logic: `kitlib/landed/__init__.py`; tests: `tests/landed/test_landed.py`.

```
python tools/landed.py [-C DIR] <branch or commit>
```

Exit 0 `LANDED`; 1 `NOT YET`; 2 `FAILED`, `CANNOT ANSWER` (the ref does not resolve, or git gives no answer for it or for whether the remote exists), the kit configuration refused, or usage; 130 Ctrl+C.

## What it decides on

LANDED means the tip is an ancestor of `<remote>/<main_branch>`, read with `git ls-remote`.

- No remote at all (`git remote get-url` exits 2 and `git remote` lists none): the local main branch decides. The remote missing while others exist (a misnamed `"repo".remote`), or any other failure of that lookup, is `CANNOT ANSWER`. A local main git gives no answer for is `contained  UNKNOWN`.
- The remote is unreachable: the last fetched `refs/remotes/<remote>/<main>` decides, and the output says so.
- The remote's sha is not in this clone: `contained  UNKNOWN`, with the fetch to run first. An ancestry read git gives no answer for is `contained  UNKNOWN` with the why, never `no`.

When the branch has not landed, it prints three more lines:

- `lock`: the kit landing lock (`kitlib.records.landing`), free or held with its holder. A file there that is not a lock record counts as held.
- `gate-ok`: whether the tip has a gate-OK record (`kitlib.records.gate_ok`). This line is information only.
- `verdict`: the newest kit verdict line in the branch's gate logs. FAILED needs this line to be a failure, not older than the tip commit, and the lock free. A failure while a landing holds the lock is `NOT YET (a landing holds the lock)`.

## Where the logs are read

Each log base is `kitlib.gate.runner.log_base`:

- with `"landing".log_dir` set, that folder under the checkout;
- otherwise `<kit temp>/<repo name>-gate`, where the kit temp is `kitlib.config.user.work_temp` of the checkout and the repo name is `"repo".name`, else the checkout folder's name. Then `runner.fallback_log_base` (`<shared temp>/<repo name>-gate`) is read as well: a gate that could not make the default base writes there. A machine config that cannot be read leaves only that base, with a `note` line.

The checkouts read are the one `-C` names, its main checkout, and every worktree that has the branch checked out.

Under each base it reads two folders, recursively: `log_folder(<branch>)` (the branch gate's) and `log_folder('land-' + <branch>)` (the landing's merged gate's). When neither folder exists, it reads the files directly in the base whose name holds `log_folder(<branch>)`. The newest file that holds a verdict line decides, by its last such line.

A verdict line starts with one of these:

- passing: `GATE OK`, `LANDED <branch>`;
- failing: `GATE FAILED`, `NOT LANDED:`, `CONFLICT <branch> with main <sha>:`, `STUCK:` (`VERDICT` in `kitlib/landed/__init__.py`).

A commit sha names no branch, so no log is read for it.

The kit gate prints these lines to stdout and writes none of them into its step logs. Until the gate saves its verdict in its run folder, a verdict is found only where a caller saved the gate's output under the log folder.

## Keys read

- `.kit.json` "repo": `main_branch`, `remote`, `name`.
- "landing": `log_dir`, `lock_file`, `lanes`.

## Not ported

- The `"landing".verdict_pattern` key: the kit's verdict lines are fixed, so `kitlib.config` refuses the key as unknown.
- `--root`: `-C` replaces it.
