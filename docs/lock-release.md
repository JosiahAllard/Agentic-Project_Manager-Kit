# lock release pre-check

Held for the joint review: nothing runs it yet.

`tools/lock_release_check.py` says whether the kit's release checkout may take a commit now. It refuses while the release would change the lock tool and any run in the machine's lock store is live. Logic: `kitlib/lock/release.py`; tests: `tests/lock/test_release.py`. It is the kitlib/lock counterpart of the PM kit's `owner/machine_lock_install.py` pre-check, which reads the queue as `machine_lock.py` `queue_live()` does.

```
python tools/lock_release_check.py [-C KIT] <commit>
```

`-C` is the release checkout (default: the checkout the tool sits in). It reads the store and the repository and writes neither; it never makes the release itself. Run it outside any lock run (see the caller's own run, below).

| Exit | Meaning |
|---|---|
| 0 OK | Release now with the printed git command (the checkout path quoted), or nothing to release. |
| 1 `STOP` | Do not release: a live run, a missing store folder, or git cannot say what changes. |
| 2 usage | No checkout there, or no such commit. |
| 3 `ERROR` | An unexpected failure (`release.ERROR`), such as reading the store or asking Windows about a process. Nothing was checked; do not release. |
| 130 | Ctrl+C. |

## What it decides on

- What the release brings in: HEAD..commit on a fast-forward (`git merge --ff-only`), merge-base..commit when it takes a merge (`git merge --no-ff`). A commit HEAD already holds needs nothing.
- A lock tool change: a changed path under `release.LOCK_PATHS`, the entry point `tools/machine_lock.py` and every kitlib package it imports. The test walks those imports and fails when one falls outside the list.
- A live run: a record in the store's `active/` or an entry in `queue.json` whose process (pid and creation time) is not surely gone. A record that cannot be read, or that names no process, counts as live. So do an unlistable `active/` and an unreadable `queue.json`. Runs that are gone are listed as ignored.
- The caller's own run counts as live. Swapping the lock code under it is the same hazard. When a live run's process is the caller's own process or one of its ancestors, the `STOP` says so: `this check runs inside a lock run (your own process or one of its ancestors)`.
- A missing store folder is a `STOP`, not "no run is live". The check cannot tell whether a run is live.
- Live means the owning process is alive. It does not use `Store.run_dead`'s mutex test, so a run whose wrapper is alive counts as live even when its mutex is gone. This is stricter than `run_dead` and errs on the safe side (review finding 4, left as is).
- Git cannot say what changes: `STOP`.

## What it does not do

- Run the kit tests. They must also be green before the release; the release step runs them.
- Hold anything. A run that starts between the check and the release is not seen, so run the printed command at once.
- See the lock MCP server (`mcp/lock_server.py`). It keeps the code it started with and is not a run; restart it after a release that changes the lock.
