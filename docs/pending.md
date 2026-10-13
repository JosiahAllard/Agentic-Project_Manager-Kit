# The pending-steps file

The commit/push options take `--host NAME`: the agent host for the Co-Authored-By trailer, a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude); `--provider` is its old name, accepted for one release. The trailer comes from `kitlib.attribution` (docs/commit.md); a Codex trailer needs the actual explicit --model id. The host and model are checked before anything is written; the provenance, branch, path and owner checks keep their rules.

The owner's list of hand steps the PM session keeps: one file, a flat numbered list. It is the file the machine config's `pending_path` names (docs/kit-json.md). Entry point `tools/pending.py`; logic `kitlib/pending`; tests `tests/pending`. Ported from the PM repository's `tools/pending.py`.

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

COMMIT: [--commit | --push] [--branch-ok] [--model NAME]
```

- **`list`** prints the open items, first line each.
- **`add`**, **`done`**, **`edit`** print `added N`, `done N`, `edited N`. `--when` defaults to today.
- **`--commit`** commits the file alone as `pending: <added|done|edited> N`, the way `ledger.py add --commit` commits the ledger (docs/ledger.md "Commit and push"): the same trailer, the same refusals before writing. `--push` also pushes the current branch.
- **Exit codes:** 0 ok; 1 no `pending_path`, an unreadable file, no such item, an item already done, a file that cannot be written, a refused commit, or a commit or push that failed after the write (printed as `written but NOT committed` or `committed ... but NOT pushed`, not as refused); 2 usage.

## Not ported

- **The PM tool's co-author environment variable and default model:** replaced by `--model`, else `CLAUDE_AGENT_MODEL`; neither set is a usage error.
- **The PM tool's fixed file path:** replaced by `pending_path`.
- **`add` into a file with no items (deliberate fix):** the item goes before the file's final newline, so the file still ends in one.
