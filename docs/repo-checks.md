# Repo checks: forbidden, trailers, pieces

Three repository checks; a repository's gate runs them once its `.kit.json` adds the step. Each is a thin entry point in `tools/` over a `kitlib` package; `-C DIR` (default `.`) names the checkout, resolved with `kitlib.git.repo.toplevel`. Exit 2 is also every `USAGE:` error.

## forbidden

`tools/forbidden.py`, logic `kitlib/forbidden`. Refuses a forbidden token anywhere in a path or a file's text, without the token ever being written down: the store holds salted SHA-256 digests (`algo`, `salt`, `canary`, `tokens` of `{length, digest}`, `checksum`), and every window of each token's length in the normalised text (lower-cased, punctuation dropped, whitespace runs one space) is hashed and compared. No output line holds a token; a hit prints `path:line`, or `path   <- in the PATH, not the contents`.

```
python tools/forbidden.py [-C DIR] [--base REV] [--worktree] [--paranoid]   what the branch changed
python tools/forbidden.py [-C DIR] --all | --paths P [P ...]                 every tracked path / these
python tools/forbidden.py [-C DIR] --add                                     one token on stdin
```

- **Store.** `.kit.json` "forbidden" names a repo-relative JSON file; unset, `<tools_dir>/forbidden-tokens.json`. An inline store is refused: the section is opaque to the schema and the store is data, not config. Because `kitlib.config.load` refuses a named file that does not exist, a repository that names one creates it holding `{}` before the first `--add`.
- **Self-test.** Before any scan the store must find its own known-positive (the canary) and its `checksum` must match its token records; either failing is `SELF-TEST FAILED`, exit 2. A store that is missing or holds no tokens is exit 2 too, never a clean pass.
- **What is read.** By default the paths changed against the merge base of `--base` (default `.kit.json` "repo".main_branch when written, else `main` if it exists, else `master`, measured as `<remote>/<that>` when it resolves: gate.md "Default base") and HEAD; `--worktree` adds uncommitted and untracked changes. When the merge base is HEAD (a merged tree) every tracked path is swept, with `--worktree` too. `--paranoid` also checks the space-removed form, which catches a token split across a line break but welds neighbouring words.
- **`--add`** reads one line on stdin (never an argument, so the token is in no history or process list), refuses one under 4 characters, and rewrites the store with the new digest and checksum.
- **Exit:** 0 clean (or added), 1 a hit, 2 refused config, a store missing, empty or failing its self-test, nothing readable, or git failed.

## trailers

`tools/trailers.py`, logic `kitlib/trailerscheck` over the rule in `kitlib/reviewpack/trailers.py` (the one review_pack judges by). Fails a branch holding a commit with no `Co-Authored-By: <name> <noreply@anthropic.com>` trailer.

```
python tools/trailers.py [-C DIR] [--base REV] [--merged]
```

- **Commits read:** every commit `HEAD` has that the base does not (`HEAD --not <base>`, merges included, so a sync merge counts); the base defaults as in gate.md "Default base".
- **FAIL:** a commit made since the rule's cutoff (`TRAILER_FAIL_SINCE`) with no such trailer, or with no readable committer date. **WARNING, exit 0:** an older commit, and a commit carrying `Owner-Approved:` or `Owner-Authored: yes` whose AUTHOR email is `.kit.json` "owner".email, read from the base (an unset email proves nothing). The same claim on anyone else's commit is judged as no claim, with a WARNING.
- **`--merged`:** a landing's merged-tree gate. The first-parent merges in `<base>..HEAD` are the lander's own and are exempt. It holds with HEAD on the `.kit.json` "repo".main_branch, or detached at a landing's merge: the landing tree `gate.py land` merges and gates in (docs/gate.md "Landing"), where HEAD's first-parent chain from the base is merges only and the oldest was made onto the base itself. Refused (exit 2) on any other branch, and at any other detached HEAD, since there the merges are the branch's own sync merges.
- **Config:** owner claims count only with `.kit.json` "owner".email set; without it an owner-authored commit fails.
- **In the gate:** a plain `cmd` step: a FAIL stops the later steps unless the repo adds `"continue": true`, and its WARNING lines show as WARN only if the gate's `warn_lines` matches them.
- **Exit:** 0 no failing commit, 1 a failing commit, 2 usage, refused config, git failed, or `--merged` neither on main nor at a landing's merge.

## pieces

`tools/pieces.py`, logic `kitlib/pieces`. The path-to-piece table: a tool names a piece by id, never by a path literal, so moving a folder is an edit to the table only. The table is `.kit.json` "pieces" (inline, or a repo-relative JSON path); it is repository data, opaque to the schema and checked by `kitlib.pieces.Table`. The field list is in the `kitlib/pieces` docstring; `KINDS` is the vocabulary.

```
python tools/pieces.py [-C DIR] --list | --dir ID | --path ID REL | --piece-of PATH
                                | --gradle ID | --output ID KEY | --was ID
```

A library caller asks `has_table(root)` first: a repository without a table is not an error until something needs one. `table(root)` reads it once per process.

- **Exit:** 0 answered, 2 no or a malformed table, refused config, an unknown id or path, a piece with no Gradle project or no such output.

## Wiring a gate step

A repository runs these as `cmd` steps in its `.kit.json` "gate" (docs/gate.md, Steps); `{here}` is the kit's tools folder:

```json
{"name": "forbidden", "always": true,
 "cmd": ["{python}", "{here}/forbidden.py", "-C", "{root}", "--base", "{base}", "--worktree"]},
{"name": "trailers", "always": true,
 "cmd": ["{python}", "{here}/trailers.py", "-C", "{root}", "--base", "{base}"],
 "merged_args": ["--merged"]}
```

An empty `{base}` falls back to each tool's own default base.
