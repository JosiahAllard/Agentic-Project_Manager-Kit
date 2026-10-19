# The pending-steps file

The owner's list of hand steps the PM session keeps: one file, a flat numbered list. It is the file the machine config's `pending_path` names (docs/kit-json.md). Entry point `tools/pending.py`; logic `kitlib/pending`; tests `tests/pending`.

## Format

- Items are `N. text`, continuation lines indented. No sections; any other line is kept as it is.
- Numbers stay stable and are never reused. A new item is one past the highest.
- A done item stays in place as `N. DONE MM-DD (note). Was: <text>`. An item whose text begins `DONE` or `CLOSED` is done.
- Lines outside the item touched keep their exact bytes: a BOM, CRLF line ends. Writes are atomic.

## CLI

```
python tools/pending.py list
python tools/pending.py add --text T [COMMIT]
python tools/pending.py done N [--note T] [--when MM-DD] [COMMIT]
python tools/pending.py edit N --text T [COMMIT]

COMMIT: [--commit | --push] [--branch-ok] [--model NAME] [--host NAME]
```

- **`list`** prints the open items, first line each.
- **`add`**, **`done`**, **`edit`** print `added N`, `done N`, `edited N`. `--when` defaults to today.
- **`--commit`** commits the file alone as `pending: <added|done|edited> N`, the way `ledger.py add --commit` commits the ledger (docs/ledger.md "Commit and push"): the same trailer, the same refusals before writing. `--push` also pushes the current branch.
- **Write lock:** `add`, `done` and `edit` hold the file's write lock (`kitlib/pending` `locked`, over `kitlib/fs/writelock.py`) from their read through the write and any `--commit`, so two writers at once never take the same number or drop each other's item; `--push` runs after it is given up. The lock file is `kit-pending-<file name, lower-cased>-<hash>.lock` in the repository's git common dir when the file is in a git checkout, else `.<file name>.lock` beside it, the same rules as the ledger's (docs/ledger.md "Write lock"). A writer waits `LOCK_WAIT` seconds (30) for a busy lock, then is refused naming the holder, with nothing written. When git cannot tell whether the file is in a checkout, every change is refused with nothing written.
- **Replace retry:** the atomic write's final replace is retried for up to 2 seconds while it fails with `PermissionError` (a reader holding the file open), as the ledger's is (`kitlib/fs/replace.py`).
- **Exit codes:** 0 ok; 1 no `pending_path`, an unreadable file, no such item, an item already done, a file that cannot be written, a refused commit, or a commit or push that failed after the write (printed as `written but NOT committed` or `committed ... but NOT pushed`, not as refused); 2 usage.
