# branch overlap

`tools/branch_overlap.py` lists, for each unmerged branch, the files it changed that are in a set. A reviewer uses it to check a sweep's exclusions against the work in flight without shell loops or scratch files. It is read-only: it never fetches, writes a file or moves a ref. Logic: `kitlib/git/overlap.py`; tests: `tests/git/test_overlap.py`.

```
python tools/branch_overlap.py [-C DIR] (--files PATH ... | --files-from FILE | --branch BRANCH) [--base REV]
```

Exit 0 `no overlap`; 1 `OVERLAP`; 2 usage, or `CANNOT ANSWER` (the base or `--branch` does not resolve, has no merge base, or git gives no answer for a branch); 130 Ctrl+C.

## The set

- `--files`: repo-relative paths in the backlog scope language: a literal path, or `<path>/**` for everything under it. Backslashes read as slashes and a leading `./` is dropped. Any other wildcard (`*.py`) is a usage error, as is `none`.
- `--files-from FILE`: the same, one per line; blank lines and lines starting with `#` are skipped. The file is read as UTF-8 (a BOM is allowed).
- `--branch BRANCH`: the files BRANCH changed since its merge base with the base. BRANCH itself, and every ref with its tip or its name (`kit/x` and `origin/kit/x`), are left out of the check.

## What it checks

- Branches: every ref under `refs/heads/` and `refs/remotes/`, except a symbolic one (`origin/HEAD`). A branch whose tip is an ancestor of the base is merged and skipped.
- The base: `--base`, else the kit default base (docs/gate.md "Default base", `kitlib.git.refs.default_base`): `refs/remotes/<remote>/<main_branch>` from `.kit.json` "repo" when it resolves, else the local main branch. A local main that differs from the remote's is named in a note line.
- A branch's changes: `git diff --name-only --no-renames <merge base> <tip>`, so a rename counts as its old and its new path.
- A match: a changed file overlaps a member exactly when the backlog pull collision check would call them colliding (`kitlib.backlog.scope.members_collide`): component by component, case-blind, one a prefix of the other. A comparison it cannot decide (a changed file whose name holds a wildcard character) counts as an overlap, with a note.

## Output

A `base` line and a `set` line, then one block per branch with hits: `<branch> (<sha>): N overlapping`, then `  <path>  (matches <member>)` per hit. The last line is the verdict: `OVERLAP: ...`, `no overlap: ...` or `CANNOT ANSWER: ...`, with the counts of branches checked and merged ones skipped. A branch git gives no answer for is a `CANNOT READ` line, and the verdict is then `CANNOT ANSWER`, never `no overlap`.
