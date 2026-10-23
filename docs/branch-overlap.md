# branch overlap

`tools/branch_overlap.py` lists, for each unmerged branch, the files it changed that are in a set. A reviewer uses it to check a sweep's exclusions against the work in flight without shell loops or scratch files. It is read-only: it never fetches, writes a file or moves a ref. Logic: `kitlib/git/overlap.py`; tests: `tests/git/test_overlap.py`.

It runs from the kit, not through a repository stub: it is in `KIT_ONLY` in `kitlib/stubs/__init__.py`. Name the repository with `-C`:

```
python <kit>/tools/branch_overlap.py -C <checkout> (--files PATH ... | --files-from FILE | --branch BRANCH) [--base REV]
```

Exit 0 `no overlap`; 1 `OVERLAP`; 2 usage or `CANNOT ANSWER`; 130 Ctrl+C. `CANNOT ANSWER` covers these cases:

- git cannot name `-C`'s work tree;
- the base or `--branch` does not resolve, or has no merge base;
- a ref cannot be read: a broken ref, a line `for-each-ref` printed that is not a ref line, or git giving no answer partway through.
  - A dangling `origin/HEAD` (the remote's default branch it points at is gone) draws a git warning (`ignoring dangling symref`), so it gives `CANNOT ANSWER`; `git remote set-head origin -a` repairs it.

`-C` that is not a directory, or is in no git work tree, is a usage error; a drive-letter path with no separators left (`Q:GitHub...`, an unquoted backslash path mangled by Git Bash) gets a quoting hint (`kitlib.git.repo.not_a_directory`).

## The set

- `--files`: repo-relative paths in the backlog scope language.
  - Members are a literal path, or `<path>/**` for everything under it.
  - Backslashes read as slashes and a leading `./` is dropped.
  - Any other wildcard (`*.py`) is a usage error.
  - `none`, in any case, is the root file of that name. It is not the backlog's empty-scope sentinel.
- `--files-from FILE`: the same, one per line. Blank lines and lines starting with `#` are skipped. The file is read as UTF-8 (a BOM is allowed). A file that leaves no member is a usage error.
- `--branch BRANCH`: the set is the files BRANCH changed since its merge base with the base.
  - BRANCH may be a name, `heads/<name>`, a full ref, `HEAD` or a commit.
  - Its name is the branch the ref names, or for `HEAD` the branch HEAD is on.
  - Every ref with that name is left out of the check, so an older pushed copy (`origin/<name>`) is not reported against it. Another branch at the same tip is checked.
  - A bare commit (or a detached `HEAD`) names no branch: the branches at its tip are taken as its name, and a note line names each.

## What it checks

- **Refs:** every ref under `refs/heads/` and `refs/remotes/`, except a symbolic one (`origin/HEAD`).
  - A ref whose tip is an ancestor of the base is merged and skipped.
  - Refs carrying one branch name (`kit/x`, `origin/kit/x`) are one branch: counted once and reported in one block.
- **The base:** `--base`, else the kit default base (docs/gate.md "Default base", `kitlib.git.refs.default_base`). That is `refs/remotes/<remote>/<main_branch>` from `.kit.json` "repo" when it resolves, else the local main branch.
  - When the local main differs from the remote's, the note line says how.
  - The base's own branch is left out: the local `main` when the base is `origin/main`. When it holds commits the base lacks, a note says so, and those commits are not checked.
- **A branch's changes:** `git diff --name-only --no-renames <merge base> <tip>`, so a rename counts as its old and its new path.
- **A match:** a changed file overlaps a member when the backlog pull collision check would call them colliding (`kitlib.backlog.scope.members_collide`). The comparison goes component by component, case-blind, and one side must be a prefix of the other.
  - Some comparisons cannot be decided: a name holding a comma, a control character other than a tab, leading or trailing whitespace, a backslash, or a wildcard character (`*?[]{}`), or one starting with a drive letter (`c:x`). Such a comparison counts as an overlap, with an `undecidable, so counted` note.
  - A tab is an ordinary character, as it is to the collision check (`members_collide` reads members with `tab_ok=True`).
  - The one departure is `none` in any case: it is compared as a path, so a root file `none` matches `none` and `none/**`.

## Output

- The default-base `note` line, if any, comes first, then a `base` line and a `set` line.
- `note` lines for --branch naming no branch, and for the base's own branch left out, follow the `set` line.
- Then one block per branch with hits: `<branch>  [<ref> <sha>, ...]: N overlapping`, then `  <path>  (matches <member>)` per hit.
  - A hit that only some of the branch's refs have ends `[only <ref>]`.
- The last line is the verdict: `OVERLAP: ...`, `no overlap: ...` or `CANNOT ANSWER: ...`.
  - It gives the branches checked, the refs behind them, the merged refs skipped and the refs left out.
- A ref git gives no answer for is a `CANNOT READ` line. The verdict is then `CANNOT ANSWER`, never `no overlap`.
